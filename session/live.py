"""Authorized input and capture in the logged-in user's physical X11 session."""

import argparse
import asyncio
import hashlib
import io
import json
import os
import pwd
import socket
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone

from aiohttp import ClientError, ClientSession, UnixConnector, WSMsgType
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from av import VideoFrame
from remote_input import Controller
from PIL import Image, ImageDraw, ImageGrab


def stamp():
    return datetime.now(timezone.utc).isoformat()


def outputs(fake):
    if fake:
        rows = [("DP-1", 640, 360, 0, 0, 0), ("DP-2", 360, 640, 640, 0, 90)]
    else:
        listing = subprocess.check_output(["xrandr", "--query"], text=True, timeout=2)
        rows = []
        for line in listing.splitlines():
            m = re.match(
                r"^(\S+) connected (?:primary )?(\d+)x(\d+)([+-]\d+)([+-]\d+)\s+(?:\([^)]*\)\s+)?(left|right|inverted|normal)?",
                line,
            )
            if m:
                name, w, h, x, y, rotation = m.groups()
                rows.append(
                    (
                        name,
                        int(w),
                        int(h),
                        int(x),
                        int(y),
                        {
                            None: 0,
                            "normal": 0,
                            "right": 90,
                            "inverted": 180,
                            "left": 270,
                        }[rotation],
                    )
                )
    monitors = []
    for name, w, h, x, y, r in rows:
        mirror = (
            f"mirror-{x}-{y}"
            if sum(row[1:5] == (w, h, x, y) for row in rows) > 1
            else None
        )
        monitors.append(
            dict(
                monitorId=name,
                x=x,
                y=y,
                width=w,
                height=h,
                rotation=r,
                mirrorGroup=mirror,
            )
        )
    return monitors


def capture(monitors, target, fake):
    selected = [m for m in monitors if target is None or m["monitorId"] == target]
    x = min(m["x"] for m in selected)
    y = min(m["y"] for m in selected)
    right = max(m["x"] + m["width"] for m in selected)
    bottom = max(m["y"] + m["height"] for m in selected)
    if not fake:
        return ImageGrab.grab(
            bbox=(x, y, right, bottom), xdisplay=os.environ["DISPLAY"]
        ).convert("RGB")
    image = Image.new("RGB", (right - x, bottom - y))
    draw = ImageDraw.Draw(image)
    for m in selected:
        a, b = m["x"] - x, m["y"] - y
        draw.rectangle(
            (a, b, a + m["width"] - 1, b + m["height"] - 1),
            fill="#345265" if m["monitorId"] == "DP-1" else "#735682",
        )
        draw.text(
            (a + 20, b + 20),
            f"{m['monitorId']}  {time.monotonic():.1f}",
            fill="white",
            font_size=24,
        )
    return image


class ScreenTrack(VideoStreamTrack):
    def __init__(self, source, channel):
        super().__init__()
        self.source = source
        self.channel = channel

    async def recv(self):
        pts, base = await self.next_timestamp()
        self.source.check(self.channel)
        image = await asyncio.to_thread(self.source.image, self.channel)
        self.source.check(self.channel)
        frame = VideoFrame.from_image(image)
        frame.pts, frame.time_base = pts, base
        return frame


class Source:
    def __init__(self, args, ws):
        self.args, self.ws = args, ws
        self.session = uuid.uuid4().hex
        self.rev = 0
        self.monitors = []
        self.channels = {}
        self.control_features = []
        if not args.synthetic:
            try:
                probe = Controller()
                probe.close()
                self.control_features = ["control.monitor", "control.desktop"]
            except Exception as error:
                print(f"Ввод X11 недоступен: {error}", flush=True)

    def check(self, c):
        req = c["req"]
        if (
            time.monotonic() >= c["until"]
            or req["sessionId"] != self.session
            or req["layoutRevision"] != self.rev
            or req["nodeId"] != self.args.node
        ):
            raise ValueError("Expired capture binding")

    def image(self, c):
        req = c["req"]
        monitor = req.get("monitorId") or req.get("target", {}).get("monitorId")
        return capture(self.monitors, monitor, self.args.synthetic)

    async def close(self, key):
        c = self.channels.pop(key, None)
        if c:
            c["until"] = 0
            if "controller" in c:
                c["controller"].close()
            if "peer" in c:
                await c["peer"].close()

    def freshness(self):
        previews = [
            c for c in self.channels.values() if c["req"]["kind"] == "preview.subscribe"
        ]
        if not previews or any("frameAt" not in c for c in previews):
            return dict(state="pending", lastFrameAt=None, lastVerifiedAt=None)
        stale = any(time.monotonic() - c["checked"] > 2 for c in previews)
        state = "moving" if any(c["changed"] for c in previews) else "unchanged"
        return dict(
            state="stale" if stale else state,
            lastFrameAt=min(c["frameAt"] for c in previews),
            lastVerifiedAt=min(c["checkedAt"] for c in previews),
        )

    async def update(self):
        while True:
            monitors = await asyncio.to_thread(outputs, self.args.synthetic)
            if monitors != self.monitors:
                self.rev += 1
                self.monitors = monitors
                for key in list(self.channels):
                    await self.close(key)
            base = dict(version=1, nodeId=self.args.node)
            await self.ws.send_json(
                dict(
                    base,
                    kind="status",
                    availability="online",
                    session=dict(
                        sessionId=self.session,
                        layoutRevision=self.rev,
                        monitors=monitors,
                    )
                    if monitors
                    else None,
                    preview=self.freshness(),
                )
            )
            await self.ws.send_json(
                dict(
                    base,
                    kind="capabilities",
                    features=[
                        "view.monitor",
                        "view.desktop",
                        "preview.jpeg.v1",
                        "session.identity.v1",
                    ]
                    + self.control_features,
                    actions=[],
                )
            )
            await asyncio.sleep(0.6)

    async def frames(self):
        while True:
            for key, c in list(self.channels.items()):
                if (
                    min(c["until"], c.get("inputUntil", float("inf")))
                    <= time.monotonic()
                ):
                    await self.close(key)
                    await self.ws.send_json(dict(key=key, op="ended"))
                    continue
                if "peer" in c or c["awaiting"]:
                    continue
                captured = stamp()
                image = await asyncio.to_thread(self.image, c)
                if self.channels.get(key) is not c:
                    continue
                self.check(c)
                image.thumbnail((320, 180))
                buf = io.BytesIO()
                image.save(buf, "JPEG", quality=75)
                data = buf.getvalue()
                digest = hashlib.sha256(data).digest()
                req = c["req"]
                c.update(
                    checked=time.monotonic(),
                    checkedAt=captured,
                    changed=digest != c.get("digest"),
                )
                if not c["changed"]:
                    await self.ws.send_json(
                        dict(
                            key=key,
                            message=dict(
                                req,
                                kind="preview.verified",
                                seq=c["seq"],
                                verifiedAt=captured,
                            ),
                        )
                    )
                else:
                    c["seq"] += 1
                    c.update(digest=digest, awaiting=True, frameAt=captured)
                    header = json.dumps(
                        dict(
                            req,
                            kind="preview.frame",
                            seq=c["seq"],
                            mime="image/jpeg",
                            byteLength=len(data),
                            capturedAt=captured,
                        )
                    ).encode()
                    await self.ws.send_bytes(
                        key.encode() + len(header).to_bytes(4, "big") + header + data
                    )
            await asyncio.sleep(0.12)

    async def listen(self):
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            key, op = data["key"], data["op"]
            c = self.channels.get(key)
            if op == "describe":
                try:
                    user = pwd.getpwuid(os.getuid()).pw_name
                except KeyError:
                    user = None
                await self.ws.send_json(
                    dict(
                        op="identity",
                        sessionId=self.session,
                        hostname=socket.gethostname(),
                        user=user,
                    )
                )
            elif op == "ticket":
                await self.ws.send_json(dict(key=key, op="redeem"))
            elif op == "open" and c is None:
                req = data["request"]
                c = dict(req=req, seq=0, awaiting=False, until=time.monotonic() + 3)
                self.check(c)
                self.channels[key] = c
                if req["kind"] == "preview.subscribe":
                    await self.ws.send_json(
                        dict(key=key, message=dict(req, kind="preview.ready"))
                    )
                elif req["kind"] == "access":
                    if req["mode"] == "control":
                        if not self.control_features or any(
                            "controller" in ch for ch in self.channels.values()
                        ):
                            await self.close(key)
                            await self.ws.send_json(dict(key=key, op="ended"))
                            continue
                        try:
                            c["controller"] = Controller()
                        except Exception:
                            await self.close(key)
                            await self.ws.send_json(dict(key=key, op="ended"))
                            continue
                        c["inputUntil"] = time.monotonic() + 5
                    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                    c["peer"] = peer
                    await peer.setRemoteDescription(
                        RTCSessionDescription(**data["offer"])
                    )
                    peer.addTrack(ScreenTrack(self, c))
                    await peer.setLocalDescription(await peer.createAnswer())
                    await self.ws.send_json(
                        dict(
                            key=key,
                            message=dict(type="answer", sdp=peer.localDescription.sdp),
                        )
                    )
            elif c and op == "input" and "controller" in c:
                if min(c["until"], c["inputUntil"]) <= time.monotonic():
                    await self.close(key)
                    continue
                self.check(c)
                c["inputUntil"] = time.monotonic() + 3
                if data["event"]["kind"] != "ping":
                    c["controller"].event(
                        data["event"], self.monitors, c["req"]["target"]
                    )
            elif c and op == "renew":
                c["until"] = time.monotonic() + 3
            elif c and op == "ack" and c["seq"] == data["seq"]:
                c["awaiting"] = False
            elif c and op == "close":
                await self.close(key)


async def run(args):
    async with ClientSession(connector=UnixConnector(path=args.socket)) as client:
        async with client.ws_connect("http://localhost/session") as ws:
            s = Source(args, ws)
            tasks = [asyncio.create_task(fn()) for fn in (s.update, s.frames, s.listen)]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for key in list(s.channels):
                    await s.close(key)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--socket", required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--synthetic", action="store_true")
    args = p.parse_args()

    async def reconnect():
        while True:
            try:
                await run(args)
            except (OSError, ClientError, subprocess.SubprocessError):
                pass
            await asyncio.sleep(1)

    asyncio.run(reconnect())
