"""Forward one operator-selected local session over the node's outgoing mTLS connection."""

import argparse
import asyncio
import os
import subprocess
from urllib.parse import urlsplit
import socket
import ssl
import struct

from aiohttp import ClientError, ClientSession, WSMsgType, web


async def forward(msg, dst):
    if msg.type == WSMsgType.TEXT:
        await dst.send_str(msg.data)
    elif msg.type == WSMsgType.BINARY:
        await dst.send_bytes(msg.data)


def active_seat(pid):
    # Resolve the kernel-reported peer PID through logind/elogind.
    base = ["busctl", "--system"]
    try:
        path = subprocess.check_output(
            base
            + [
                "call",
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "GetSessionByPID",
                "u",
                str(pid),
            ],
            text=True,
            timeout=1,
        ).split('"')[1]

        def prop(name):
            return subprocess.check_output(
                base
                + [
                    "get-property",
                    "org.freedesktop.login1",
                    path,
                    "org.freedesktop.login1.Session",
                    name,
                ],
                text=True,
                timeout=1,
            ).strip()

        return (
            prop("Active") == "b true"
            and prop("Remote") == "b false"
            and prop("Type") == 's "x11"'
            and prop("Seat").startswith('so "seat0" ')
        )
    except (OSError, subprocess.SubprocessError, IndexError):
        return False


async def main(args):
    if args.test_session and (
        urlsplit(args.server).hostname not in ("localhost", "127.0.0.1")
        or args.uid != os.getuid()
    ):
        raise ValueError("Test sessions require loopback and the current uid")
    tls = ssl.create_default_context(cafile=args.ca)
    tls.load_cert_chain(args.cert, args.key)
    upstream = None
    local = None
    local_pid = None

    async def idle(ws):
        await ws.send_json(
            dict(
                version=1,
                kind="status",
                nodeId=args.node,
                availability="online",
                session=None,
                preview=dict(state="pending", lastFrameAt=None, lastVerifiedAt=None),
            )
        )

    async def session(request):
        nonlocal local, local_pid
        sock = request.transport.get_extra_info("socket")
        pid, uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != args.uid or not (
            args.test_session or await asyncio.to_thread(active_seat, pid)
        ):
            raise web.HTTPForbidden()
        if local is not None or upstream is None:
            raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse()
        local, local_pid = ws, pid
        try:
            await ws.prepare(request)
            async for msg in ws:
                if upstream is None:
                    break
                await forward(msg, upstream)
        finally:
            local, local_pid = None, None
            if upstream and not upstream.closed:
                await idle(upstream)
        return ws

    async def monitor(ws):
        while True:
            if local is None:
                await idle(ws)
            elif not args.test_session and not await asyncio.to_thread(
                active_seat, local_pid
            ):
                await local.close()
            await asyncio.sleep(0.7)

    async def connect():
        nonlocal upstream
        async with ClientSession() as client:
            while True:
                try:
                    async with client.ws_connect(
                        args.server + "/source", ssl=tls, heartbeat=5
                    ) as ws:
                        upstream = ws
                        check = asyncio.create_task(monitor(ws))
                        try:
                            async for msg in ws:
                                if local is not None and not local.closed:
                                    await forward(msg, local)
                        finally:
                            upstream = None
                            check.cancel()
                            await asyncio.gather(check, return_exceptions=True)
                            if local is not None:
                                await local.close()
                except (OSError, ClientError):
                    pass
                await asyncio.sleep(1)

    app = web.Application()
    app.router.add_get("/session", session)
    runner = web.AppRunner(app)
    await runner.setup()
    os.umask(0o077)
    await web.UnixSite(runner, args.socket).start()
    os.chown(args.socket, args.uid, -1)
    try:
        await connect()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for name in ("socket", "cert", "key", "ca", "server"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--test-session", action="store_true")
    p.add_argument("--uid", type=int, required=True)
    asyncio.run(main(p.parse_args()))
