"""Opt-in exercise on the disposable two-output Xorg used by live_acceptance."""

import json
import os
import subprocess
import time
from Xlib import X, XK, display


def exercise(page, tmp, monitor):
    d = display.Display()
    root = d.screen().root
    shift = d.keysym_to_keycode(XK.string_to_keysym("Shift_L"))

    def held():
        return bool(d.query_keymap()[shift // 8] & (1 << (shift % 8)))

    def wait(predicate):
        end = time.monotonic() + 7
        while time.monotonic() < end:
            if predicate():
                return
            page.wait_for_timeout(50)
        raise AssertionError("Unexpected X11 input state")

    policy = tmp / "policy"
    config = json.loads(policy.read_text())
    rooms = config["teachers"]["local"]["rooms"]
    page.get_by_role("button", name="desk-1 · DUMMY1").click()
    page.wait_for_function("document.querySelector('video')?.readyState>=2")
    assert not page.get_by_text("Включить управление").count()
    page.evaluate(
        """sockets.at(-1).send(JSON.stringify({kind:'key',down:true,code:'ShiftLeft'}))"""
    )
    page.wait_for_function("pcs.at(-1).connectionState==='closed'")
    assert not held()
    rooms["lab-a"].append("control")
    policy.write_text(json.dumps(config))
    page.wait_for_timeout(800)
    page.get_by_role("button", name="desk-1 · DUMMY1").click()
    page.wait_for_function(
        "document.querySelector('#mode').textContent==='Управление включено'"
    )
    # The drawer owns its own pointer events while remote input is enabled.
    page.locator("#handle").hover()
    page.wait_for_function("!document.querySelector('#drawer').inert")
    page.evaluate("""()=>{window.toolbarClicks=[];const ws=sockets.at(-1),send=ws.send.bind(ws);
      ws.send=data=>{const msg=JSON.parse(data);if(msg.kind==='button')toolbarClicks.push(msg);send(data)}}""")
    page.locator("#target").click()
    assert page.evaluate("toolbarClicks.length===0")
    page.evaluate("delete sockets.at(-1).send")
    page.mouse.move(10, 10)
    # A second control channel from the same portal must also be refused.
    assert await_second_denial(page, monitor)
    video = page.locator("video")
    box = video.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    wait(
        lambda: (
            abs(root.query_pointer().root_x - monitor["x"] - monitor["width"] // 2) <= 2
            and abs(root.query_pointer().root_y - monitor["y"] - monitor["height"] // 2)
            <= 2
        )
    )
    assert page.evaluate("""async()=>{const {coordinates:f}=await import('/input.js');
        const rect={left:0,top:0,width:1000,height:1000};
        return f(rect,1920,1080,10,10)===null && f(rect,1920,1080,0,218.75).u===0 &&
               f(rect,1080,1920,500,500).v===.5 && f(rect,1080,1920,999,500)===null}""")
    # xev translates the injected physical key through the active XKB layout.
    win = root.create_window(0, 0, 100, 100, 0, d.screen().root_depth)
    win.map()
    win.set_input_focus(X.RevertToParent, X.CurrentTime)
    d.sync()
    saved = tmp / "saved.xkb"
    subprocess.run(
        ["xkbcomp", "-w", "0", "-xkb", os.environ["DISPLAY"], saved], check=True
    )
    try:
        with (
            (tmp / "keys").open("w") as log,
            subprocess.Popen(
                ["stdbuf", "-oL", "xev", "-id", hex(win.id), "-event", "keyboard"],
                stdout=log,
            ) as observer,
        ):
            try:
                for layout, symbol in [("us", "keysym 0x63, c"), ("ru", "Cyrillic_es")]:
                    subprocess.run(["setxkbmap", "-layout", layout], check=True)
                    page.wait_for_timeout(150)
                    page.keyboard.press("Control+KeyC")
                    wait(lambda: symbol in (tmp / "keys").read_text())
                assert "state 0x4" in (tmp / "keys").read_text()
            finally:
                observer.terminate()
    finally:
        subprocess.run(["xkbcomp", "-w", "0", saved, os.environ["DISPLAY"]], check=True)
        win.destroy()
        d.sync()
    page.keyboard.down("Shift")
    page.mouse.down()
    wait(lambda: held() and root.query_pointer().mask & X.Button1Mask)
    # Stop network delivery from the browser without dispatching a blur/release.
    page.evaluate("WebSocket.prototype.send=function(){}")
    wait(lambda: not held() and not root.query_pointer().mask & X.Button1Mask)
    page.keyboard.up("Shift")
    page.mouse.up()
    page.reload()
    page.get_by_role("button", name="desk-1 · DUMMY1").wait_for()
    d.close()
    print(
        "T07: explicit control, physical pointer, Shift/button release on lost browser heartbeat OK"
    )


def await_second_denial(page, monitor):
    return page.evaluate(
        """async id=>{
      const state=await (await fetch('/state')).json(), s=state.nodes['pc-01'].session;
      const ws=new WebSocket('/stream','classctl.input.v1');
      return new Promise(resolve=>{
        const timer=setTimeout(()=>{ws.close();resolve(false)},2000);
        ws.onopen=()=>ws.send(JSON.stringify({request:{version:1,kind:'access',mode:'control',
          nodeId:'pc-01',sessionId:s.sessionId,layoutRevision:s.layoutRevision,target:{scope:'monitor',monitorId:id}}}));
        ws.onclose=()=>{clearTimeout(timer);resolve(true)};
      });
    }""",
        monitor["monitorId"],
    )
