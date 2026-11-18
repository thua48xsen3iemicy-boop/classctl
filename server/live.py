"""Classroom coordinator. One authenticated source socket per inventory node."""

import argparse
import asyncio
import hashlib
import json
import secrets
import sqlite3
import ssl
import time
from pathlib import Path

from jsonschema import Draft202012Validator
from aiohttp import WSMsgType, web
from secure import AccessPolicy, CONTRACTS, Node, PREVIEW, inventory, validate

ROOT = CONTRACTS.parent

INPUT = Draft202012Validator(
    json.loads((CONTRACTS / "input-v1.schema.json").read_text())
)


class Relay(AccessPolicy):
    def __init__(self, args):
        self.policy_path = Path(args.policy)
        self.room = inventory(args.db, CONTRACTS / "examples/room.json")
        self.nodes = {
            s["nodeId"]: Node(s["nodeId"]) for s in self.room["seats"] if s["nodeId"]
        }
        self.db = sqlite3.connect(args.db)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS audit (time TEXT, subject TEXT, event TEXT, target TEXT, outcome TEXT)"
        )
        self.sources = {}
        self.channels = {}
        self.pending = {}
        self.ice = json.loads(Path(args.ice).read_text()) if args.ice else []

    def teacher(self, request):
        subject = identity(request)
        if "view" not in self.policy().get("teachers", {}).get(subject, {}).get(
            "rooms", {}
        ).get(self.room["roomId"], []):
            raise web.HTTPForbidden()
        return subject

    async def state(self, request):
        subject = self.teacher(request)
        return web.json_response(
            dict(
                room=self.room,
                nodes={k: n.status for k, n in self.nodes.items()},
                iceServers=self.ice,
                controlNodes=[
                    ident
                    for ident, node in self.nodes.items()
                    if "control.monitor" in node.capabilities["features"]
                    and "control"
                    in self.policy()
                    .get("teachers", {})
                    .get(subject, {})
                    .get("rooms", {})
                    .get(self.room["roomId"], [])
                ],
            ),
            headers={"Cache-Control": "no-store"},
        )

    async def browser(self, request):
        subject = self.teacher(request)
        if request.headers.get("Origin") != "https://" + request.host:
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(
            protocols=["classctl.input.v1", "classctl.preview.v1"], heartbeat=5
        )
        await ws.prepare(request)
        key = secrets.token_urlsafe(24)
        try:
            first = await ws.receive_json(timeout=5)
            req = first["request"]
            preview = req.get("kind") == "preview.subscribe"
            if preview:
                if not PREVIEW.is_valid(req) or ws.ws_protocol != "classctl.preview.v1":
                    raise ValueError("Invalid subscription")
            else:
                validate(req, "access")
                if req["mode"] == "control":
                    if ws.ws_protocol != "classctl.input.v1" or any(
                        r.get("mode") == "control" and r["nodeId"] == req["nodeId"]
                        for _, _, _, r in self.channels.values()
                    ):
                        self.audit(subject, "control", req, "denied")
                        raise ValueError("Control unavailable")
            node_id = req["nodeId"]
            if node_id not in self.sources or not self.allowed(
                self.policy(), subject, req
            ):
                self.audit(subject, "open", req, "denied")
                raise ValueError("Denied")
            source = self.sources[node_id]
            self.channels[key] = (ws, source, subject, req)
            self.audit(subject, "open", req, "issued")
            self.pending[key] = (time.monotonic() + 10, first.get("offer"))
            await source.send_json(dict(key=key, op="ticket"))
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                ack = json.loads(msg.data)
                if req.get("mode") == "control":
                    self.teacher(request)
                    if (
                        key in self.pending
                        or not self.allowed(self.policy(), subject, req)
                        or not INPUT.is_valid(ack)
                    ):
                        break
                    await source.send_json(dict(key=key, op="input", event=ack))
                    continue
                if (
                    not preview
                    or not PREVIEW.is_valid(ack)
                    or ack["kind"] != "preview.ack"
                    or not matches(ack, req)
                ):
                    break
                await source.send_json(dict(key=key, op="ack", seq=ack["seq"]))
        except (ValueError, KeyError, TypeError, TimeoutError, ConnectionError):
            pass
        finally:
            self.pending.pop(key, None)
            channel = self.channels.pop(key, None)
            if channel:
                self.audit(subject, "close", channel[3], "closed")
                if not channel[1].closed:
                    await channel[1].send_json(dict(key=key, op="close"))
            await ws.close()
        return ws

    async def source(self, request):
        subject = identity(request)
        ident = self.policy().get("nodes", {}).get(subject)
        if ident not in self.nodes or ident in self.sources:
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=5)
        await ws.prepare(request)
        ws["subject"] = subject
        self.sources[ident] = ws
        try:
            async for msg in ws:
                if self.policy().get("nodes", {}).get(subject) != ident:
                    break
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    if data.get("kind") in ("status", "capabilities"):
                        self.nodes[ident].update(data["kind"], data, time.monotonic())
                        continue
                    if data.get("op") == "redeem":
                        key = data["key"]
                        channel = self.channels.get(key)
                        if channel and channel[1] is ws:
                            ticket = self.pending.pop(key, None)
                            dest, _, teacher, req = channel
                            if (
                                ticket
                                and time.monotonic() < ticket[0]
                                and self.allowed(self.policy(), teacher, req)
                            ):
                                self.audit(teacher, "establish", req, "allowed")
                                await ws.send_json(
                                    dict(
                                        key=key, op="open", request=req, offer=ticket[1]
                                    )
                                )
                            else:
                                await dest.close()
                        continue
                    if data.get("op") == "ended":
                        channel = self.channels.get(data["key"])
                        if channel and channel[1] is ws:
                            await channel[0].close()
                        continue
                    key, payload = data["key"], data["message"]
                elif msg.type == WSMsgType.BINARY:
                    key = msg.data[:32].decode("ascii")
                    packet = msg.data[32:]
                    n = int.from_bytes(packet[:4], "big")
                    if not 0 < n <= 4096:
                        break
                    payload = json.loads(packet[4 : 4 + n])
                    if (
                        not PREVIEW.is_valid(payload)
                        or payload["kind"] != "preview.frame"
                        or len(packet) - 4 - n != payload["byteLength"]
                    ):
                        break
                else:
                    break
                channel = self.channels.get(key)
                if not channel or key in self.pending or channel[1] is not ws:
                    continue
                dest, _, teacher, req = channel
                if not self.allowed(self.policy(), teacher, req):
                    await dest.close()
                    continue
                if req["kind"] == "preview.subscribe":
                    if not PREVIEW.is_valid(payload) or not matches(payload, req):
                        continue
                elif msg.type != WSMsgType.TEXT or payload.get("type") != "answer":
                    continue
                if not dest.closed:
                    if msg.type == WSMsgType.BINARY:
                        await dest.send_bytes(packet)
                    else:
                        await dest.send_json(payload)
        finally:
            self.sources.pop(ident, None)
            self.nodes[ident].disconnect()
            for dest, source, _, _ in list(self.channels.values()):
                if source is ws:
                    await dest.close()
        return ws

    async def watch(self, app):
        async def poll():
            while True:
                await asyncio.sleep(0.4)
                for node in self.nodes.values():
                    node.expire(time.monotonic(), 2, 5)
                policy = self.policy()
                for ident, ws in list(self.sources.items()):
                    if policy.get("nodes", {}).get(ws["subject"]) != ident:
                        await ws.close()
                for key, (dest, source, teacher, req) in list(self.channels.items()):
                    if key in self.pending:
                        if time.monotonic() >= self.pending[key][0]:
                            await dest.close()
                        continue
                    if (
                        self.allowed(policy, teacher, req)
                        and not source.closed
                        and "view"
                        in policy.get("teachers", {})
                        .get(teacher, {})
                        .get("rooms", {})
                        .get(self.room["roomId"], [])
                    ):
                        try:
                            await asyncio.wait_for(
                                source.send_json(dict(key=key, op="renew")), timeout=0.5
                            )
                        except (ConnectionError, TimeoutError):
                            await dest.close()
                    else:
                        await dest.close()

        task = asyncio.create_task(poll())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.db.close()


def matches(message, request):
    return all(message.get(k) == v for k, v in request.items() if k != "kind")


def identity(request):
    cert = request.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
    return hashlib.sha256(cert).hexdigest()


def main():
    p = argparse.ArgumentParser()
    for name in ("db", "policy", "cert", "key", "ca"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--ice")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8767)
    args = p.parse_args()
    relay = Relay(args)
    app = web.Application()
    app.cleanup_ctx.append(relay.watch)
    app.add_routes(
        [
            web.get("/state", relay.state),
            web.get("/source", relay.source),
            web.get("/stream", relay.browser),
        ]
    )

    def serve(path):
        async def file(request):
            relay.teacher(request)
            return web.FileResponse(path)

        return file

    for url, path in {
        "/": "web/live.html",
        "/live.js": "web/live.js",
        "/input.js": "web/input.js",
        "/input-keys.json": "contracts/input-keys.json",
        "/style.css": "web/style.css",
        "/receiver.mjs": "contracts/preview/receiver.mjs",
    }.items():
        app.router.add_get(url, serve(ROOT / path))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    tls.load_verify_locations(args.ca)
    tls.verify_mode = ssl.CERT_REQUIRED
    web.run_app(app, host=args.host, port=args.port, ssl_context=tls)


if __name__ == "__main__":
    main()
