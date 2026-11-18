"""Real Chromium and disposable PKI, synthetic screens, optional local TURN."""

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright
from access_acceptance import identities

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(tmp, stack, x11):
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

    identities(tmp)
    api = port()
    origin = f"https://localhost:{api}"
    ice = []
    if shutil.which("turnserver"):
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
    start(
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
            "--port",
            str(api),
        ]
    )
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
            origin,
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
            try:
                with socket.create_connection(("127.0.0.1", api), timeout=0.1):
                    break
            except OSError:
                pass
        time.sleep(0.05)

    def login():
        return start(
            [
                sys.executable,
                ROOT / "session/live.py",
                "--socket",
                tmp / "socket",
                "--node",
                "pc-01",
            ]
            + ([] if x11 else ["--synthetic"])
        )

    source = login()
    pw = stack.enter_context(sync_playwright())
    browser = pw.chromium.launch(
        executable_path=shutil.which("chromium"), args=["--no-sandbox"]
    )
    stack.callback(browser.close)
    ctx = browser.new_context(
        ignore_https_errors=True,
        client_certificates=[
            dict(
                origin=origin, certPath=tmp / "teacher.pem", keyPath=tmp / "teacher.key"
            )
        ],
    )
    page = ctx.new_page()
    page.add_init_script("""window.pcs=[];window.RTCPeerConnection=class extends RTCPeerConnection{
      constructor(...a){super(...a);pcs.push(this)}}""")
    page.goto(origin)
    grid = "document.querySelectorAll('canvas').length===2 && [...document.querySelectorAll('canvas')].every(c=>c.width>1)"
    page.wait_for_function(grid)
    monitors = page.evaluate(
        "async()=> (await (await fetch('/state')).json()).nodes['pc-01'].session.monitors"
    )
    first, second = monitors
    for label, w, h in [
        (f"Открыть {second['monitorId']}", second["width"], second["height"]),
        (
            "Весь рабочий стол",
            max(m["x"] + m["width"] for m in monitors) - min(m["x"] for m in monitors),
            max(m["y"] + m["height"] for m in monitors) - min(m["y"] for m in monitors),
        ),
    ]:
        page.get_by_role("button", name=label).click()
        page.wait_for_function(
            "([w,h])=>{const v=document.querySelector('video');return v&&v.readyState>=2&&v.videoWidth===w&&v.videoHeight===h}",
            arg=[w, h],
        )
        print(
            label,
            page.evaluate("""async()=>{
          const s=await pcs.at(-1).getStats(), transport=[...s.values()].find(x=>x.type==='transport');
          const pair=s.get(transport.selectedCandidatePairId), c=s.get(pair.localCandidateId);
          return [c.candidateType,c.protocol,c.relayProtocol]}"""),
            flush=True,
        )
        page.get_by_text("Вернуться в грид").click()
        assert page.evaluate("pcs.at(-1).connectionState==='closed'")
    if "--dummy-layout" in sys.argv:
        if [first["monitorId"], second["monitorId"]] != ["DUMMY0", "DUMMY1"]:
            raise ValueError("Use only the disposable Xorg dummy display")

        def randr(*args):
            subprocess.run(["xrandr", *args], check=True)

        try:
            page.get_by_text("Открыть DUMMY1").click()
            page.wait_for_function("document.querySelector('video')?.readyState>=2")
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
    source.terminate()
    source.wait()
    page.wait_for_function("document.querySelectorAll('canvas').length===0")
    page.get_by_text("Нет графической сессии").wait_for()
    login()
    page.wait_for_function(grid)
    page.get_by_text("Открыть " + first["monitorId"]).click()
    page.wait_for_function("document.querySelector('video')?.readyState>=2")
    policy = json.loads((tmp / "policy").read_text())
    policy["teachers"].clear()
    (tmp / "policy").write_text(json.dumps(policy))
    page.wait_for_function(
        "pcs.at(-1).connectionState==='closed' && !document.querySelector('video') && !document.querySelector('canvas')"
    )
    print("T05: grid, focus, desktop, track closure, logout/login, revocation OK")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        run(Path(directory), stack, "--x11" in sys.argv)
