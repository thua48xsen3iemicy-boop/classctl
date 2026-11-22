"""Separate HTTP and mTLS listeners, plus optional real Chromium and screens."""

import asyncio
import json
import os
import pwd
import secrets
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from aiohttp import (
    ClientConnectorError,
    ClientError,
    ClientSession,
    ClientTimeout,
    WSServerHandshakeError,
)
from access_acceptance import identities

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def check_listeners(tmp, origin, agent_origin, contexts):
    async with ClientSession(timeout=ClientTimeout(total=5)) as client:
        for path in ("/", "/state"):
            async with client.get(origin + path) as response:
                assert response.status == 200
                if path == "/":
                    assert (
                        response.headers["Content-Security-Policy"]
                        == "frame-ancestors 'none'"
                    )
        async with client.get(
            origin + "/state", headers={"Host": "other.example"}
        ) as response:
            assert response.status == 403
        async with client.get(origin + "/source") as response:
            assert response.status == 404
        try:
            async with client.get(origin.replace("127.0.0.1", "127.0.0.2")):
                raise AssertionError("UI accepted a connection on another address")
        except ClientConnectorError:
            pass

        for headers in ({}, {"Origin": "https://other.example"}):
            try:
                async with client.ws_connect(origin + "/stream", headers=headers):
                    raise AssertionError("UI accepted a WebSocket with an invalid Origin")
            except WSServerHandshakeError as error:
                assert error.status == 403
        async with client.ws_connect(origin + "/stream", origin=origin):
            pass

        pc = contexts["pc"]
        for path in ("/", "/state", "/stream"):
            async with client.get(agent_origin + path, ssl=pc) as response:
                assert response.status == 404
        try:
            async with client.ws_connect(agent_origin + "/source", ssl=contexts["guest"]):
                raise AssertionError("An unassigned certificate was accepted as an agent")
        except WSServerHandshakeError as error:
            assert error.status == 403
        no_cert = ssl.create_default_context(cafile=tmp / "ca.pem")
        try:
            async with client.ws_connect(agent_origin + "/source", ssl=no_cert):
                raise AssertionError("An agent connected without a client certificate")
        except WSServerHandshakeError:
            raise AssertionError("A connection without a certificate reached HTTP")
        except ClientError:
            pass
        async with client.ws_connect(agent_origin + "/source", ssl=pc):
            pass
    print("T11: loopback UI, route separation, browser headers and agent mTLS OK")


def check_dense_grid(context, origin, status):
    """Fixture only: measure layout and target selection independently of throughput."""
    page = context.new_page()
    nodes = {}
    seats = []
    for i in range(25):
        node = f"pc-{i + 1:02}"
        nodes[node] = {**status, "nodeId": node}
        seats.append(
            dict(seatId=f"desk-{i + 1}", nodeId=node, row=i // 5 + 1, column=i % 5 + 1)
        )
    state = dict(
        room=dict(roomId="lab", seats=seats),
        nodes=nodes,
        iceServers=[],
        controlNodes=list(nodes),
        powerNodes=list(nodes),
    )
    page.route("**/state", lambda route: route.fulfill(json=state))
    requests = []

    def stream(ws):
        ws.on_message(lambda message: requests.append(json.loads(message)))

    page.route_web_socket("**/stream", stream)
    # Simulate a managed browser that rejects the fullscreen request.
    page.add_init_script(
        "window.nativeFullscreen=Element.prototype.requestFullscreen;Element.prototype.requestFullscreen=()=>Promise.reject(new DOMException('Denied','NotAllowedError'))"
    )
    try:
        page.goto(origin)
        page.wait_for_function("document.querySelectorAll('.monitor').length===50")
        for w, h in [(1366, 768), (1920, 1080)]:
            page.set_viewport_size(dict(width=w, height=h))
            assert page.evaluate("""()=>document.documentElement.scrollWidth===innerWidth &&
                document.documentElement.scrollHeight===innerHeight &&
                [...document.querySelectorAll('.monitor')].every(m=>{
                  const r=m.getBoundingClientRect();return r.width>0&&r.height>0&&r.right<=innerWidth&&r.bottom<=innerHeight})""")
        caption = page.locator(".caption").first
        assert caption.inner_text() == "pc-01"

        def dimensions():
            return caption.evaluate(
                """el => [el.closest('.seat').querySelector('.monitor').offsetWidth,
                          el.offsetHeight, getComputedStyle(el).fontSize]"""
            )

        before = dimensions()
        page.keyboard.down("Control")
        page.mouse.move(200, 200)
        page.mouse.wheel(0, -120)
        page.keyboard.up("Control")
        page.wait_for_function(
            "document.querySelector('#grid').offsetWidth > document.querySelector('#viewport').clientWidth"
        )
        after = dimensions()
        assert after[0] > before[0] and after[1:] == before[1:]
        page.keyboard.press("Control+0")
        page.wait_for_function(
            "document.querySelector('#grid').offsetWidth === document.querySelector('#viewport').clientWidth"
        )
        monitor = status["session"]["monitors"][1]["monitorId"]
        preview = page.get_by_role("button", name="pc-25 · Экран 2", exact=True)
        preview.click(modifiers=["Control"])
        assert page.locator('[data-node-id="pc-25"][data-selected]').count() == 1
        assert not page.locator("#focus").is_visible()
        preview.click()
        for _ in range(100):
            if any(m.get("request", {}).get("kind") == "access" for m in requests):
                break
            page.wait_for_timeout(50)
        access = next(
            m["request"] for m in requests if m["request"]["kind"] == "access"
        )
        assert access["nodeId"] == "pc-25" and access["mode"] == "control"
        assert access["target"] == {"scope": "monitor", "monitorId": monitor}
        assert page.evaluate("""()=>{const r=document.querySelector('video').getBoundingClientRect();
            return r.width===innerWidth && r.height>innerHeight-55 && r.bottom===innerHeight}""")
        assert page.evaluate(
            "!document.fullscreenElement && document.querySelector('#drawer').inert"
        )
        handle = page.locator("#handle")
        assert handle.evaluate("""handle => {
            const r = handle.getBoundingClientRect();
            return r.left === 0 && r.top + r.height / 2 === innerHeight / 2;
        }""")
        handle.hover()
        page.wait_for_function("!document.querySelector('#drawer').inert")
        page.get_by_role("button", name="Другой монитор").click()
        for _ in range(100):
            access_requests = [m["request"] for m in requests if m["request"]["kind"] == "access"]
            if len(access_requests) == 2:
                break
            page.wait_for_timeout(50)
        assert access_requests[1] == {
            **access,
            "target": {"scope": "monitor", "monitorId": status["session"]["monitors"][0]["monitorId"]},
        }
        page.mouse.move(20, 20)
        page.wait_for_function("document.querySelector('#drawer').inert")
        page.wait_for_timeout(200)
        # Send a finger tap through Chromium's native touch input pipeline.
        box = handle.bounding_box()
        cdp = context.new_cdp_session(page)
        cdp.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart",
                "touchPoints": [
                    {
                        "x": box["x"] + box["width"] / 2,
                        "y": box["y"] + box["height"] / 2,
                    }
                ],
            },
        )
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        cdp.detach()
        page.wait_for_function("!document.querySelector('#drawer').inert")
        page.get_by_role("button", name="← В аудиторию").click()
        page.wait_for_function("document.querySelectorAll('.monitor').length===50")
        state["room"]["seats"] = seats[:1]
        nodes["pc-01"]["session"] = {
            **status["session"],
            "monitors": status["session"]["monitors"][:1],
        }
        page.wait_for_function("document.querySelectorAll('.monitor').length===1")
        page.evaluate(
            "Element.prototype.requestFullscreen=window.nativeFullscreen;void 0"
        )
        page.locator(".monitor").click()
        page.wait_for_function(
            "document.fullscreenElement===document.querySelector('#focus')"
        )
        assert page.locator("#swap").evaluate("button => button.hidden")
        page.evaluate("document.exitFullscreen()")
        page.wait_for_function(
            "document.querySelector('.monitor') && document.querySelector('#focus').hidden"
        )
        print(
            "Layout fixture: 25 seats, 50 monitor cards, no scrolling; correct target and full-page video"
        )
    finally:
        page.close()


def check_overview_grid(context, origin, status):
    page = context.new_page()
    page.set_viewport_size({"width": 1366, "height": 768})
    seats = []
    nodes = {}
    for i, count in enumerate([2, 1, 2, 1, 2, 0], 1):
        node_id = f"pc-{i:02}"
        seats.append(dict(seatId=f"desk-{i}", nodeId=node_id, row=1, column=i))
        session = {**status["session"], "monitors": status["session"]["monitors"][:count]}
        nodes[node_id] = {**status, "nodeId": node_id, "session": session if count else None}
    state = dict(room=dict(roomId="lab", seats=seats[::-1]), nodes=nodes, iceServers=[])
    long_name = ("Александра Константинопольская-Миропольская " * 4).strip()
    state["labels"] = {
        "pc-01": dict(hostname="actual-hostname", displayName="Анна Иванова"),
        "pc-02": dict(hostname="legacy-hostname", user="legacy-login"),
        "pc-03": dict(hostname="actual-hostname", displayName=None),
        "pc-04": dict(hostname="actual-hostname", displayName=long_name),
    }
    page.route("**/state", lambda route: route.fulfill(json=state))
    page.route_web_socket("**/stream", lambda ws: ws.on_message(lambda _: None))
    page.add_init_script("""
        window.previewSockets = [];
        window.WebSocket = class extends WebSocket {
            send(message) {
                const {request} = JSON.parse(message);
                if (request?.kind === 'preview.subscribe') {
                    previewSockets.push({nodeId: request.nodeId, ws: this});
                }
                super.send(message);
            }
        };
    """)
    try:
        page.goto(origin)
        page.get_by_text("Нет графической сессии").wait_for()
        cards = page.locator(".seat")
        captions = cards.locator(".caption")
        assert captions.all_text_contents() == [
            "pc-01 · Анна Иванова", "pc-02", "pc-03", f"pc-04 · {long_name}", "pc-05", "pc-06",
        ]
        long_caption = captions.nth(3)
        assert long_caption.get_attribute("title") == f"pc-04 · {long_name}"
        assert long_caption.evaluate("""caption => {
            const [computer, student] = caption.children;
            return computer.scrollWidth === computer.clientWidth &&
                student.clientWidth > 0 && student.scrollWidth > student.clientWidth &&
                getComputedStyle(student).textOverflow === 'ellipsis' &&
                caption.scrollWidth === caption.clientWidth;
        }""")
        assert cards.evaluate_all("cards=>cards.map(c=>c.querySelectorAll('.monitor').length)") == [2, 1, 2, 1, 2, 0]
        boxes = [card.bounding_box() for card in cards.all()]
        assert boxes[0]["y"] == boxes[1]["y"] == boxes[2]["y"]
        assert boxes[3]["y"] == boxes[4]["y"] == boxes[5]["y"] > boxes[0]["y"]
        assert boxes[0]["x"] == boxes[3]["x"] < boxes[1]["x"] < boxes[2]["x"]
        assert all(b["width"] > b["height"] for b in boxes)
        assert boxes[1]["x"] - boxes[0]["x"] - boxes[0]["width"] == 6
        assert boxes[3]["y"] - boxes[0]["y"] - boxes[0]["height"] == 6
        assert cards.first.evaluate("card => getComputedStyle(card).borderWidth") == "0px"
        dual = cards.first.locator(".monitor").all()
        assert [m.get_attribute("aria-label") for m in dual] == [
            "pc-01 · Экран 1",
            "pc-01 · Экран 2",
        ]
        assert not cards.locator(".connector").count()
        left, right = [m.bounding_box() for m in dual]
        assert left["y"] == right["y"] and left["x"] + left["width"] < right["x"]
        assert cards.nth(1).locator(".monitor").bounding_box()["width"] > left["width"]

        page.set_viewport_size(dict(width=390, height=844))
        narrow = [card.bounding_box() for card in cards.all()]
        assert all(b["x"] == narrow[0]["x"] and b["x"] + b["width"] <= 390 for b in narrow)
        assert all(a["y"] + a["height"] < b["y"] for a, b in zip(narrow, narrow[1:]))
        page.set_viewport_size(dict(width=1366, height=768))

        handle = page.get_by_role("button", name="Меню аудитории")
        assert handle.evaluate("""handle => {
            const r = handle.getBoundingClientRect();
            return r.left === 0 && r.top + r.height / 2 === innerHeight / 2;
        }""")
        pin = page.get_by_role("button", name="Закрепить панель")
        viewport = page.locator("#viewport")
        grid_width = viewport.bounding_box()["width"]
        handle.hover()
        page.get_by_role("heading", name="Аудитория lab").wait_for()
        assert viewport.bounding_box()["width"] == grid_width
        page.mouse.move(1000, 700)
        page.wait_for_function("document.getElementById('sidebar-panel').inert")
        handle.focus()
        page.keyboard.press("Enter")
        pin.click()
        assert pin.get_attribute("aria-pressed") == "true"
        assert viewport.bounding_box()["width"] < grid_width

        page.get_by_role("button", name="Компьютеры").click()
        only_sessions = page.get_by_role("checkbox", name="Только с графической сессией")
        assert page.locator("#choices-panel input").first.get_attribute("id") == "active-sessions-only"
        first = page.get_by_role("checkbox", name="pc-01", exact=True)
        page.wait_for_function("previewSockets.length === 8")
        only_sessions.check()
        assert cards.count() == 5 and page.locator("#count").inner_text() == "Показано 5 из 6"
        first.uncheck()
        assert cards.count() == 4
        assert captions.first.inner_text() == "pc-02"
        assert page.evaluate("""previewSockets.every(({nodeId, ws}) =>
            nodeId === 'pc-01' ? ws.readyState >= WebSocket.CLOSING : ws.readyState === WebSocket.OPEN)""")
        page.reload()
        cards.nth(3).wait_for()
        assert cards.count() == 4
        assert pin.get_attribute("aria-pressed") == "true"
        page.get_by_role("button", name="Компьютеры").click()
        assert only_sessions.is_checked() and not first.is_checked()
        first.check()
        assert captions.first.text_content() == "pc-01 · Анна Иванова"
        assert cards.count() == 5
        sixth = cards.filter(has=page.get_by_role("heading", name="pc-06", exact=True))
        nodes["pc-06"]["session"] = nodes["pc-02"]["session"]
        sixth.locator(".monitor").wait_for()
        assert cards.count() == 6
        nodes["pc-06"]["session"] = None
        sixth.wait_for(state="detached")
        only_sessions.uncheck()
        assert cards.count() == 6
        page.keyboard.press("Escape")
        assert page.locator("#choices-panel").is_hidden()
        pin.click()
        assert viewport.bounding_box()["width"] == grid_width
        page.mouse.move(1000, 700)
        page.wait_for_function("document.getElementById('sidebar-panel').inert")
        print("Layout fixture: compact grid, sidebar, persisted filters and automatic card updates OK")
    finally:
        page.close()


def run(tmp, stack, x11, listeners_only):
    def start(args):
        p = subprocess.Popen(args, cwd=tmp, stdout=subprocess.DEVNULL)

        def stop():
            p.terminate()
            try:
                p.wait(4)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

        stack.callback(stop)
        return p

    contexts, _ = identities(tmp)
    policy_path = tmp / "policy"
    policy = json.loads(policy_path.read_text())
    policy["teachers"] = {"local": {"rooms": {"lab-a": ["view"]}}}
    policy_path.write_text(json.dumps(policy))
    agent_port = port()
    ui_port = port()
    while ui_port == agent_port:
        ui_port = port()
    agent_origin = f"https://localhost:{agent_port}"
    origin = f"http://127.0.0.1:{ui_port}"
    ice = []
    if not listeners_only and shutil.which("turnserver"):
        turn = port()
        password = secrets.token_urlsafe(12)
        start(
            [
                "turnserver",
                "-n",
                "--no-cli",
                "--no-tls",
                "--no-dtls",
                "--no-udp",
                "--no-multicast-peers",
                "--allow-loopback-peers",
                "--lt-cred-mech",
                "--realm=localhost",
                "--listening-ip=127.0.0.1",
                "--relay-ip=127.0.0.1",
                "--relay-threads=1",
                f"--listening-port={turn}",
                f"--pidfile={tmp}/turn.pid",
                "--log-file=stdout",
                "--user=test:" + password,
            ]
        )
        ice = [
            dict(
                urls=f"turn:127.0.0.1:{turn}?transport=tcp",
                username="test",
                credential=password,
            )
        ]
    (tmp / "ice").write_text(json.dumps(ice))
    coordinator = start(
        [
            sys.executable,
            ROOT / "server/live.py",
            "--db",
            "db",
            "--policy",
            "policy",
            "--ice",
            "ice",
            "--cert",
            "localhost.pem",
            "--key",
            "localhost.key",
            "--ca",
            "ca.pem",
            "--host",
            "0.0.0.0",
            "--port",
            str(agent_port),
            "--ui-port",
            str(ui_port),
        ]
    )
    for _ in range(100):
        if coordinator.poll() is not None:
            raise RuntimeError("Coordinator exited during startup")
        try:
            with socket.create_connection(("127.0.0.1", ui_port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("UI listener did not start")
    asyncio.run(check_listeners(tmp, origin, agent_origin, contexts))
    if listeners_only:
        return

    from playwright.sync_api import sync_playwright

    start(
        [
            sys.executable,
            ROOT / "node/live.py",
            "--node",
            "pc-01",
            "--test-session",
            "--socket",
            tmp / "socket",
            "--server",
            agent_origin,
            "--uid",
            str(os.getuid()),
            "--cert",
            "pc.pem",
            "--key",
            "pc.key",
            "--ca",
            "ca.pem",
        ]
    )
    for _ in range(100):
        if (tmp / "socket").exists():
            break
        time.sleep(0.05)

    def login(name):
        script = ROOT / "session/live.py"
        args = ["--socket", tmp / "socket", "--node", "pc-01"]
        if x11:
            return start([sys.executable, script, *args])
        mock_nss = """
import os
import pwd
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

script, name, *args = sys.argv[1:]
user = list(pwd.getpwuid(os.getuid()))
user[0] = "ipa-login"
user[4] = f"  {name}  ,101,555-0123"
sys.argv = [script, *args]
sys.path.insert(0, str(Path(script).parent))
with patch("pwd.getpwuid", return_value=pwd.struct_passwd(user)):
    runpy.run_path(script, run_name="__main__")
"""
        return start([sys.executable, "-c", mock_nss, script, name, *args, "--synthetic"])

    source = login("Анна Иванова")
    pw = stack.enter_context(sync_playwright())
    browser = pw.chromium.launch(
        executable_path=shutil.which("chromium"), args=["--no-sandbox"]
    )
    stack.callback(browser.close)
    ctx = browser.new_context()
    page = ctx.new_page()
    page.add_init_script("""window.sockets=[];window.WebSocket=class extends WebSocket{constructor(...a){super(...a);sockets.push(this)}};window.pcs=[];window.RTCPeerConnection=class extends RTCPeerConnection{
      constructor(...a){super(...a);pcs.push(this)}}""")
    page.goto(origin)
    grid = "document.querySelectorAll('canvas').length===2 && [...document.querySelectorAll('canvas')].every(c=>c.width>1)"
    page.wait_for_function(grid)
    monitors = page.evaluate(
        "async()=> (await (await fetch('/state')).json()).nodes['pc-01'].session.monitors"
    )
    def check_caption(name):
        if x11:
            name = pwd.getpwuid(os.getuid()).pw_gecos.split(",", 1)[0].strip()
        name = name if 0 < len(name) <= 255 else None
        page.wait_for_function(
            "async name => (await (await fetch('/state')).json()).labels?.['pc-01']?.displayName === name",
            arg=name,
        )
        page.get_by_role("heading", name=f"pc-01 · {name}" if name else "pc-01", exact=True).wait_for()

    check_caption("Анна Иванова")
    status = page.evaluate("async()=>(await (await fetch('/state')).json()).nodes['pc-01']")
    check_overview_grid(ctx, origin, status)
    check_dense_grid(ctx, origin, status)
    first, second = monitors

    def wait_for_video(monitor):
        page.wait_for_function(
            "([w,h])=>{const v=document.querySelector('video');return v&&v.readyState>=2&&v.videoWidth===w&&v.videoHeight===h}",
            arg=[monitor["width"], monitor["height"]],
        )

    for index, monitor in enumerate((first, second), 1):
        label = "pc-01 · " + monitor["monitorId"]
        page.get_by_role("button", name=f"pc-01 · Экран {index}").click()
        wait_for_video(monitor)
        print(
            label,
            page.evaluate("""async()=>{
          const s=await pcs.at(-1).getStats(), transport=[...s.values()].find(x=>x.type==='transport');
          const pair=s.get(transport.selectedCandidatePairId), c=s.get(pair.localCandidateId);
          return [c.candidateType,c.protocol,c.relayProtocol]}"""),
            flush=True,
        )
        page.wait_for_function(
            "document.fullscreenElement===document.querySelector('#focus')"
        )
        if monitor is first:
            page.locator("#handle").hover()
            swap = page.get_by_role("button", name="Другой монитор")
            for target in (second, first):
                swap.click()
                wait_for_video(target)
                assert page.locator("#target").inner_text() == "pc-01 · " + target["monitorId"]
                assert page.evaluate("""() =>
                    document.fullscreenElement === document.querySelector('#focus') &&
                    pcs.at(-2).connectionState === 'closed' &&
                    !document.querySelector('#grid canvas')""")
            page.keyboard.press("Escape")
        else:
            page.locator("#handle").hover()
            page.get_by_text("← В аудиторию").click()
        assert page.evaluate("pcs.at(-1).connectionState==='closed'")
    if "--dummy-layout" in sys.argv:
        if [first["monitorId"], second["monitorId"]] != ["DUMMY0", "DUMMY1"]:
            raise ValueError("Use only the disposable Xorg dummy display")

        from input_acceptance import exercise

        exercise(page, tmp, second)

        def randr(*args):
            subprocess.run(["xrandr", *args], check=True)

        try:
            page.get_by_role("button", name="pc-01 · Экран 2").click()
            page.wait_for_function("document.querySelector('video')?.readyState>=2")
            page.wait_for_function(
                "document.querySelector('#mode').textContent==='Управление включено'"
            )
            randr("--addmode", "DUMMY0", "lab640")
            randr(
                "--output",
                "DUMMY0",
                "--mode",
                "lab640",
                "--output",
                "DUMMY1",
                "--same-as",
                "DUMMY0",
            )
            page.wait_for_function("""async()=>{const s=(await (await fetch('/state')).json()).nodes['pc-01'].session;
                return s?.monitors.length===2 && s.monitors.every(m=>m.mirrorGroup) && pcs.at(-1).connectionState==='closed'}""")
            randr("--output", "DUMMY1", "--off")
            page.wait_for_function("document.querySelectorAll('canvas').length===1")
            print("X11: mirror, layout revocation and output removal OK")
        finally:
            randr(
                "--output",
                "DUMMY0",
                "--mode",
                "800x600",
                "--output",
                "DUMMY1",
                "--mode",
                "lab640",
                "--right-of",
                "DUMMY0",
            )
        page.wait_for_function(grid)
    page.get_by_role("button", name="Меню аудитории").hover()
    page.get_by_role("button", name="Компьютеры", exact=True).click()
    only_sessions = page.get_by_role("checkbox", name="Только с графической сессией")
    only_sessions.check()
    source.terminate()
    source.wait()
    page.get_by_text("Нет компьютеров с графической сессией.", exact=True).wait_for()
    assert page.locator("#grid .seat").count() == 0
    only_sessions.uncheck()
    page.get_by_text("Нет графической сессии").wait_for()
    page.get_by_role("heading", name="pc-01", exact=True).wait_for()
    assert page.evaluate(
        "async()=>(await (await fetch('/state')).json()).labels['pc-01'].displayName"
    ) is None
    only_sessions.check()
    login("Иван Петров")
    page.wait_for_function(grid)
    assert page.locator("#grid .seat").count() == 1
    assert page.locator("#notice").is_hidden()
    check_caption("Иван Петров")
    page.get_by_role("button", name="pc-01 · Экран 1").click()
    page.wait_for_function("document.querySelector('video')?.readyState>=2")
    policy = json.loads((tmp / "policy").read_text())
    policy["teachers"].clear()
    (tmp / "policy").write_text(json.dumps(policy))
    page.wait_for_function(
        "pcs.at(-1).connectionState==='closed' && !document.querySelector('video') && !document.querySelector('canvas')"
    )
    with sqlite3.connect(tmp / "db") as db:
        subjects = db.execute("SELECT DISTINCT subject FROM audit").fetchall()
    assert subjects == [("local",)]
    print("T05: grid, individual monitors, track closure, logout/login with session filter, revocation OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        run(Path(directory), stack, "--x11" in sys.argv, "--listeners-only" in sys.argv)
