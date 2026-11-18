"""Synthetic pupil and classroom relay, each run in a separate process."""

import asyncio
import io
import json
import ssl
import struct
import sys
import time
from fractions import Fraction
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web
from aioice import ice
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCSessionDescription,
)
from av import VideoFrame
from PIL import Image, ImageDraw

ice.get_host_addresses = lambda use_ipv4, use_ipv6: ["127.0.0.1"]

role, config_path = sys.argv[1:]
cfg = json.loads(Path(config_path).read_text())
app = web.Application()
tracks = []
peers = []
latest = {}
busy = set()
stats = dict(rx=0, tx=0, replaced=0, queuePeak=0, captures=[], confirmations=0)
subscriber = None
active = False


class Track(MediaStreamTrack):
    kind = "video"

    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue(1)

    async def recv(self):
        return await self.queue.get()


pace_lock = asyncio.Lock()


async def pace(size):
    if cfg["mbps"]:
        async with pace_lock:
            await asyncio.sleep(size * 8 / (cfg["mbps"] * 1e6))


async def offer(request):
    body = await request.json()
    if peers:
        await peers.pop().close()
        tracks[:] = [Track() for _ in tracks]
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peers.append(peer)
    await peer.setRemoteDescription(RTCSessionDescription(**body))
    for track in tracks:
        peer.addTrack(track)
    # Pinned aiortc exposes the shared BUNDLE media transport here.
    for transport in {sender.transport for sender in peer.getSenders()}:
        send = transport._send_rtp

        async def limited(packet, send=send):
            await pace(len(packet))
            await send(packet)

        transport._send_rtp = limited
    await peer.setLocalDescription(await peer.createAnswer())
    return web.json_response(dict(type="answer", sdp=peer.localDescription.sdp))


async def generate(app):
    width, height = cfg["source"]
    tw, th = cfg["thumb"]
    async with (
        ClientSession() as client,
        client.ws_connect(cfg["relay"] + "/up", ssl=False) as upstream,
    ):
        seq = 0
        start = None
        while True:
            if not active:
                await asyncio.sleep(0.02)
                continue
            if start is None:
                start = time.monotonic()
            elapsed = time.monotonic() - start
            stale_test = (
                cfg["scenario"] == "outage"
                and cfg["duration"] / 3 <= elapsed < 2 * cfg["duration"] / 3
            )
            tick = time.monotonic()
            if not stale_test:
                await upstream.send_str("ok")
                stats["confirmations"] += 1
                stats["tx"] += 2
                if cfg["scenario"] != "static" or seq < 3:
                    for index, track in enumerate(tracks):
                        captured = time.time_ns() / 1e6
                        frame = Image.new(
                            "RGB", (width, height), ("#304854", "#485038")[index % 2]
                        )
                        draw = ImageDraw.Draw(frame)
                        for y in range(40, height, 64):
                            draw.rectangle((32, y, width // 2, y + 9), fill="#acb5ad")
                        x = seq * 17 % width
                        draw.rectangle(
                            (x, height // 3, x + width // 6, height // 2),
                            fill="#eeb651",
                        )
                        image = frame.resize((tw, th), Image.Resampling.BILINEAR)
                        draw = ImageDraw.Draw(image)
                        for bit in range(16):
                            draw.rectangle(
                                (bit * 8, 0, bit * 8 + 7, 15),
                                fill="white" if seq & (1 << bit) else "black",
                            )
                        stats["captures"].append([index, seq, captured])
                        if cfg["transport"] == "jpeg":
                            buf = io.BytesIO()
                            image.save(buf, "JPEG", quality=75)
                            packet = struct.pack("!H", index) + buf.getvalue()
                            await pace(len(packet))
                            await upstream.send_bytes(packet)
                            stats["tx"] += len(packet)
                        else:
                            video = VideoFrame.from_image(image)
                            video.pts = round(elapsed * 90000)
                            video.time_base = Fraction(1, 90000)
                            if track.queue.full():
                                track.queue.get_nowait()
                                stats["replaced"] += 1
                            track.queue.put_nowait(video)
                            stats["queuePeak"] = 1
            seq += 1
            await asyncio.sleep(max(0, 1 / cfg["fps"] - (time.monotonic() - tick)))


async def flush(index):
    if (
        subscriber is not None
        and not subscriber.closed
        and index not in busy
        and index in latest
    ):
        packet = latest.pop(index)
        busy.add(index)
        await subscriber.send_bytes(packet)
        stats["tx"] += len(packet)


async def socket(request):
    global subscriber
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    upstream = request.path == "/up"
    if not upstream:
        latest.clear()
        busy.clear()
        subscriber = ws
    async for msg in ws:
        size = len(msg.data)
        stats["rx"] += size
        if upstream and msg.type == WSMsgType.BINARY:
            index = int.from_bytes(msg.data[:2], "big")
            stats["replaced"] += int(index in latest)
            latest[index] = msg.data
            stats["queuePeak"] = max(stats["queuePeak"], len(latest))
            await flush(index)
        elif upstream and subscriber is not None and not subscriber.closed:
            await subscriber.send_str("ok")
            stats["tx"] += 2
        elif not upstream:
            index = int(msg.data)
            busy.discard(index)
            await flush(index)
    if subscriber is ws:
        subscriber = None
    return ws


async def control(request):
    global active
    active = True
    return web.Response()


async def metrics(request):
    if peers:
        for report in (await peers[-1].getStats()).values():
            if report.type == "transport":
                stats["rtcTx"] = report.bytesSent
                stats["rtcRx"] = report.bytesReceived
    return web.json_response(stats)


async def home(request):
    return web.FileResponse(Path(__file__).with_name("receiver.html"))


async def startup(app):
    if role == "source":
        tracks.extend(Track() for _ in range(cfg["visible"] * cfg["outputs"]))
        app["generator"] = asyncio.create_task(generate(app))


async def cleanup(app):
    if role == "source":
        app["generator"].cancel()
        await asyncio.gather(app["generator"], return_exceptions=True)
        await asyncio.gather(*(p.close() for p in peers))


app.on_startup.append(startup)
app.on_cleanup.append(cleanup)
app.add_routes([web.get("/", home), web.get("/metrics", metrics)])
if role == "relay":
    app.add_routes([web.get("/up", socket), web.get("/down", socket)])
else:
    app.add_routes([web.post("/start", control), web.post("/offer", offer)])
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
tls.load_cert_chain(cfg["cert"], cfg["key"])
web.run_app(
    app,
    host="127.0.0.1",
    port=cfg[role + "Port"],
    ssl_context=tls,
    print=None,
    access_log=None,
)
