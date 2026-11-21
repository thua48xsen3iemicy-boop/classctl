"""Forward the active local X11 session over the node's outgoing mTLS connection."""

import argparse
import asyncio
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, WSMsgType, web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from actions.power import CAPABILITIES
from node.power import Power


async def forward(msg, dst):
    if msg.type == WSMsgType.TEXT:
        await dst.send_str(msg.data)
    elif msg.type == WSMsgType.BINARY:
        await dst.send_bytes(msg.data)


def active_seat(pid, uid):
    if uid <= 0:
        return None
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

        if (
            prop("User").startswith(f'(uo) {uid} "')
            and prop("Class") == 's "user"'
            and prop("Active") == "b true"
            and prop("Remote") == "b false"
            and prop("Type") == 's "x11"'
            and prop("Seat").startswith('(so) "seat0" ')
        ):
            return path
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return None


async def main(args):
    if args.test_session and (
        urlsplit(args.server).hostname not in ("localhost", "127.0.0.1")
        or args.uid != os.getuid()
    ):
        raise ValueError("Test sessions require loopback and the current uid")
    tls = ssl.create_default_context(cafile=args.ca)
    tls.load_cert_chain(args.cert, args.key)
    os.umask(0o077)
    power = None
    if args.power_db:
        if args.test_session or os.geteuid() != 0:
            raise ValueError("System power actions require a production node running as root")
        power = Power(args.power_db, args.node)
    power_actions = CAPABILITIES if power else []
    upstream = None
    local = None
    local_peer = None
    checking_session = asyncio.Lock()

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
        if power:
            await ws.send_json(
                dict(version=1, kind="capabilities", nodeId=args.node,
                     features=[], actions=power_actions)
            )

    async def session(request):
        nonlocal local, local_peer
        sock = request.transport.get_extra_info("socket")
        pid, uid, _ = struct.unpack(
            "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if args.uid is not None and uid != args.uid:
            raise web.HTTPForbidden()
        if local is not None or upstream is None or checking_session.locked():
            raise web.HTTPServiceUnavailable()
        session_path = None
        if not args.test_session:
            async with checking_session:
                session_path = await asyncio.to_thread(active_seat, pid, uid)
            if session_path is None:
                raise web.HTTPForbidden()
            if local is not None or upstream is None:
                raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse()
        local, local_peer = ws, (pid, uid, session_path)
        try:
            await ws.prepare(request)
            async for msg in ws:
                if upstream is None:
                    break
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = msg.json()
                    except json.JSONDecodeError:
                        break
                    if not isinstance(data, dict):
                        break
                    if data.get("kind") in ("action.request", "action.result"):
                        continue
                    if data.get("kind") == "capabilities":
                        data["actions"] = power_actions
                        await upstream.send_json(data)
                        continue
                await forward(msg, upstream)
        finally:
            try:
                if upstream and not upstream.closed:
                    await idle(upstream)
            finally:
                local, local_peer = None, None
        return ws

    async def monitor(ws):
        while True:
            if local is None:
                await idle(ws)
            elif not args.test_session:
                current = local
                pid, uid, session_path = local_peer
                if await asyncio.to_thread(active_seat, pid, uid) != session_path:
                    await current.close()
            await asyncio.sleep(0.7)

    async def connect():
        nonlocal upstream
        previous_error = None
        async with ClientSession() as client:
            while True:
                try:
                    async with client.ws_connect(
                        args.server + "/source", ssl=tls, heartbeat=5
                    ) as ws:
                        previous_error = None
                        print(f"Соединение с {args.server}/source установлено", flush=True)
                        upstream = ws
                        check = asyncio.create_task(monitor(ws))
                        try:
                            async for msg in ws:
                                if msg.type == WSMsgType.TEXT:
                                    data = msg.json()
                                    if data.get("kind") == "action.request":
                                        if power:
                                            response = await asyncio.to_thread(
                                                power.handle, data
                                            )
                                            if response and not ws.closed:
                                                await ws.send_json(response)
                                        continue
                                if local is not None and not local.closed:
                                    await forward(msg, local)
                        finally:
                            upstream = None
                            check.cancel()
                            await asyncio.gather(check, return_exceptions=True)
                            if local is not None:
                                await local.close()
                except (OSError, ClientError) as error:
                    detail = f"{type(error).__name__}: {error}"
                    if detail != previous_error:
                        print(f"Ошибка подключения: {detail}. Повтор через 1 с.", flush=True)
                        previous_error = detail
                await asyncio.sleep(1)

    app = web.Application()
    app.router.add_get("/session", session)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.UnixSite(runner, args.socket).start()
    if args.uid is None:
        # The parent directory is owned by the service. The handler authenticates
        # local peers using kernel credentials and the corresponding login1 session.
        os.chmod(args.socket, 0o666)
    else:
        os.chown(args.socket, args.uid, -1)
    print(f"Слушаю Unix-сокет {args.socket}", flush=True)
    try:
        await connect()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for name in ("socket", "cert", "key", "ca", "server"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--power-db", help="Enable system power actions with this persistent SQLite journal")
    p.add_argument("--test-session", action="store_true")
    p.add_argument("--uid", type=int, help="Restrict to one UID (required with --test-session)")
    asyncio.run(main(p.parse_args()))
