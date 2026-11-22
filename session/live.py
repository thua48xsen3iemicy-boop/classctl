"""Authorized input and capture in the logged-in user's physical X11 session."""

import argparse
import asyncio
import hashlib
import io
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from aiohttp import ClientError, ClientSession, UnixConnector, WSMsgType
from aiortc import (
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.codecs import vpx
from aiortc.mediastreams import MediaStreamError
from av import VideoFrame
from remote_input import Controller
from PIL import Image, ImageDraw, ImageGrab


def stamp():
    return datetime.now(timezone.utc).isoformat()


def display_name(uid):
    try:
        # getpwuid uses NSS, including SSSD on FreeIPA clients.
        name = pwd.getpwuid(uid).pw_gecos.split(",", 1)[0].strip()
    except (KeyError, OSError):
        return None
    return name if 0 < len(name) <= 255 else None


def outputs(fake):
    if fake:
        rows = [("DP-1", 960, 540, 0, 0, 0), ("DP-2", 360, 640, 960, 0, 90)]
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


def bounds(selected):
    x = min(m["x"] for m in selected)
    y = min(m["y"] for m in selected)
    right = max(m["x"] + m["width"] for m in selected)
    bottom = max(m["y"] + m["height"] for m in selected)
    return x, y, right, bottom


def capture(monitors, target, fake):
    selected = [m for m in monitors if target is None or m["monitorId"] == target]
    x, y, right, bottom = bounds(selected)
    if not fake:
        return ImageGrab.grab(
            bbox=(x, y, right, bottom), xdisplay=os.environ["DISPLAY"]
        )
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


def capture_previews(monitors, fake, previous):
    desktop = capture(monitors, None, fake)
    left, top, _, _ = bounds(monitors)
    previews = {}
    for monitor in monitors:
        x, y = monitor["x"] - left, monitor["y"] - top
        image = desktop
        if len(monitors) > 1:
            image = desktop.crop((x, y, x + monitor["width"], y + monitor["height"]))
        image.thumbnail((800, 450))
        pixels = image.size, image.tobytes()
        monitor_id = monitor["monitorId"]
        cached = previous.get(monitor_id)
        if cached is not None and cached[0] == pixels:
            previews[monitor_id] = cached
            continue
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
        previews[monitor_id] = pixels, data, hashlib.sha256(data).digest()
    return previews


class ScreenTrack(VideoStreamTrack):
    frame_interval = 1 / 15
    rtp_time_base = Fraction(1, 90_000)

    def __init__(self, source, channel):
        super().__init__()
        self.source = source
        self.channel = channel
        self.started_at = None
        self.last_capture_at = None

    async def recv(self):
        now = time.monotonic()
        if self.last_capture_at is not None:
            delay = self.last_capture_at + self.frame_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
        if self.readyState != "live":
            raise MediaStreamError
        self.source.check(self.channel)
        self.last_capture_at = time.monotonic()
        if self.started_at is None:
            self.started_at = self.last_capture_at
        elapsed = self.last_capture_at - self.started_at
        image = await asyncio.to_thread(self.source.image, self.channel)
        self.source.check(self.channel)
        frame = VideoFrame.from_image(image)
        frame.pts = round(elapsed / self.rtp_time_base)
        frame.time_base = self.rtp_time_base
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
                self.control_features = [
                    "control.monitor",
                    "control.desktop",
                    "control.wheel.v1",
                ]
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
        previews = {}
        revision = self.rev
        next_capture = 0.0
        while True:
            capture_due = time.monotonic() >= next_capture
            pending = []
            for key, c in list(self.channels.items()):
                if self.channels.get(key) is not c:
                    continue
                if (
                    min(c["until"], c.get("inputUntil", float("inf")))
                    <= time.monotonic()
                ):
                    await self.close(key)
                    await self.ws.send_json(dict(key=key, op="ended"))
                    continue
                if not capture_due or "peer" in c or c["awaiting"]:
                    continue
                pending.append((key, c))
            if revision != self.rev:
                previews = {}
                revision = self.rev
            pending = [
                (key, c)
                for key, c in pending
                if self.channels.get(key) is c and c["req"]["layoutRevision"] == revision
            ]
            if pending:
                targets = {c["req"]["monitorId"] for _, c in pending}
                monitors = [m for m in self.monitors if m["monitorId"] in targets]
                next_capture = time.monotonic() + 0.5
                captured = stamp()
                previews = await asyncio.to_thread(
                    capture_previews, monitors, self.args.synthetic, previews
                )
            for key, c in pending:
                if (
                    revision != self.rev
                    or self.channels.get(key) is not c
                    or time.monotonic() >= c["until"]
                ):
                    continue
                self.check(c)
                req = c["req"]
                _, data, digest = previews[req["monitorId"]]
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
            await asyncio.sleep(0.17)

    async def listen(self):
        async for msg in self.ws:
            if msg.type != WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            key, op = data["key"], data["op"]
            c = self.channels.get(key)
            if op == "describe":
                await self.ws.send_json(
                    dict(
                        op="identity",
                        sessionId=self.session,
                        hostname=socket.gethostname(),
                        displayName=await asyncio.to_thread(display_name, os.getuid()),
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


async def reconnect(args):
    while True:
        try:
            await run(args)
        except (OSError, ClientError, subprocess.SubprocessError):
            pass
        await asyncio.sleep(1)


async def wait_for_release(link, current):
    while True:
        try:
            release = link.resolve(strict=True)
        except (OSError, RuntimeError):
            pass
        else:
            if release != current:
                return release
        await asyncio.sleep(1)


async def run_until_update(args):
    if args.release_link is None:
        await reconnect(args)
        return None
    current = Path(__file__).resolve().parents[1]
    connection = asyncio.create_task(reconnect(args))
    update = asyncio.create_task(wait_for_release(args.release_link, current))
    try:
        done, _ = await asyncio.wait((connection, update), return_when=asyncio.FIRST_COMPLETED)
        if connection in done:
            connection.result()
        return update.result()
    finally:
        connection.cancel()
        update.cancel()
        await asyncio.gather(connection, update, return_exceptions=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--socket", required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--synthetic", action="store_true")
    p.add_argument("--release-link", type=Path)
    args = p.parse_args()

    # aiortc 1.14.0 has no public bitrate setting for its VP8 sender.
    # Apply the LAN bitrate range to the screen capture process only.
    vpx.DEFAULT_BITRATE = 4_000_000
    vpx.MIN_BITRATE = 2_000_000
    vpx.MAX_BITRATE = 8_000_000

    release = asyncio.run(run_until_update(args))
    if release is not None:
        python = release / ".venv/bin/python"
        script = release / "session/live.py"
        os.execv(python, [str(python), str(script), *sys.argv[1:]])
