"""Classroom coordinator. One authenticated source socket per inventory node."""

import argparse
import asyncio
import hashlib
import json
import secrets
import signal
import sqlite3
import ssl
import sys
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from aiohttp import WSMsgType, web
from secure import AccessPolicy, CONTRACTS, Node, PREVIEW, inventory, validate

ROOT = CONTRACTS.parent
sys.path.insert(0, str(ROOT))
from actions import power
from ops.ssh.nextcloud import nextcloud_origin

UI_HOST = "127.0.0.1"
LOCAL_SUBJECT = "local"

INPUT = Draft202012Validator(
    json.loads((CONTRACTS / "input-v1.schema.json").read_text())
)
SESSION_IDENTITY = Draft202012Validator(
    json.loads((CONTRACTS / "session-identity-v1.schema.json").read_text())
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
        self.labels = {}
        self.channels = {}
        self.pending = {}
        self.power_requests = {}
        self.ice = json.loads(Path(args.ice).read_text()) if args.ice else []

    def require_view(self, policy=None):
        if policy is None:
            policy = self.policy()
        permissions = self.permissions(policy, LOCAL_SUBJECT)
        if "view" not in permissions:
            raise web.HTTPForbidden()
        return permissions

    async def state(self, request):
        policy = self.policy()
        permissions = self.require_view(policy)
        power_nodes = None
        if power.PERMISSION in permissions:
            power_nodes = [
                ident for ident in self.nodes if self.power_rejection(policy, ident) is None
            ]
        labels = {}
        for ident, label in self.labels.items():
            session = self.nodes[ident].status["session"]
            active = session and session["sessionId"] == label["sessionId"]
            labels[ident] = dict(
                hostname=label["hostname"],
                user=label["user"] if active else None,
                displayName=label["displayName"] if active else None,
            )
        return web.json_response(
            dict(
                room=self.room,
                nodes={k: n.status for k, n in self.nodes.items()},
                labels=labels,
                iceServers=self.ice,
                controlNodes=[
                    ident
                    for ident, node in self.nodes.items()
                    if "control.monitor" in node.capabilities["features"]
                    and "control" in permissions
                ],
                wheelNodes=[
                    ident
                    for ident, node in self.nodes.items()
                    if "control.wheel.v1" in node.capabilities["features"]
                    and "control" in permissions
                ],
                powerNodes=power_nodes,
            ),
            headers={"Cache-Control": "no-store"},
        )

    def power_rejection(self, policy, node_id):
        node = self.nodes[node_id]
        source = self.sources.get(node_id)
        if (
            source is None or source.closed or node.status["availability"] != "online"
            or policy.get("nodes", {}).get(source["subject"]) != node_id
        ):
            return "offline"
        available = {
            action["actionId"] for action in node.capabilities["actions"]
            if action["permission"] == power.PERMISSION
            and action["context"] == "system"
            and action["parameterSchema"] == power.PARAMETER_SCHEMA
            and action["maxTtlSeconds"] >= power.TTL
        }
        if not {power.SHUTDOWN, power.REBOOT} <= available:
            return "unsupported"
        return None

    async def power_action(self, request):
        if request.headers.get("Origin") != "http://" + request.host:
            raise web.HTTPForbidden()
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType()
        try:
            command = await request.json()
        except ValueError:
            raise web.HTTPBadRequest() from None
        if power.PORTAL_GROUP.is_valid(command):
            targets = command["nodeIds"]
        elif power.PORTAL.is_valid(command):
            targets = [command["nodeId"]]
        else:
            raise web.HTTPBadRequest()
        policy = self.policy()
        if power.PERMISSION not in self.require_view(policy):
            raise web.HTTPForbidden()
        if any(node_id not in self.nodes for node_id in targets):
            raise web.HTTPNotFound()
        request_id = secrets.token_hex(16)
        # Allow a small clock difference while keeping both clocks inside the TTL.
        issued = datetime.now(timezone.utc) - timedelta(seconds=2)
        operation = dict(
            version=1, kind="action.request", requestId=request_id,
            actionId=command["actionId"], issuedAt=issued.isoformat(),
            expiresAt=(issued + timedelta(seconds=power.TTL)).isoformat(), parameters={},
        )
        outcomes = await asyncio.gather(*(
            self.dispatch_power(policy, node_id, operation) for node_id in targets
        ))
        return web.json_response(
            power.result(request_id, outcomes), headers={"Cache-Control": "no-store"}
        )

    async def dispatch_power(self, policy, node_id, operation):
        request_id = operation["requestId"]
        target = dict(actionId=operation["actionId"], nodeId=node_id, requestId=request_id)
        rejection = self.power_rejection(policy, node_id)
        if rejection or node_id in self.power_requests:
            outcome = power.outcome(node_id, "rejected", rejection or "busy")
        else:
            source = self.sources[node_id]
            self.audit(LOCAL_SUBJECT, "power", target, "issued")
            pending = asyncio.get_running_loop().create_future()
            self.power_requests[node_id] = (request_id, source, pending)
            try:
                async with asyncio.timeout(6):
                    await source.send_json(dict(operation, nodeIds=[node_id]))
                    outcome = await pending
            except (ConnectionError, TimeoutError):
                outcome = power.outcome(node_id, "unknown", "unconfirmed")
            finally:
                self.power_requests.pop(node_id, None)
        self.audit(
            LOCAL_SUBJECT, "power", dict(target, reason=outcome["reason"]), outcome["state"]
        )
        return outcome

    def receive_power_result(self, node_id, source, data):
        pending = self.power_requests.get(node_id)
        if not pending or pending[1] is not source or pending[2].done():
            return
        request_id, _, future = pending
        if not power.WIRE.is_valid(data) or data["requestId"] != request_id:
            return
        outcomes = data["outcomes"]
        if (
            len(outcomes) == 1
            and outcomes[0]["nodeId"] == node_id
            and outcomes[0]["state"] != "accepted"
        ):
            future.set_result(outcomes[0])

    async def browser(self, request):
        self.require_view()
        subject = LOCAL_SUBJECT
        if request.headers.get("Origin") != "http://" + request.host:
            raise web.HTTPForbidden()
        # A monitor switch opens its next channel after the previous socket closes.
        # Acknowledge the close only after releasing the channel in finally.
        ws = web.WebSocketResponse(
            protocols=["classctl.input.v1", "classctl.preview.v1"],
            heartbeat=5,
            autoclose=False,
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
                    self.require_view()
                    features = self.nodes[node_id].capabilities["features"]
                    if (
                        key in self.pending
                        or not self.allowed(self.policy(), subject, req)
                        or not INPUT.is_valid(ack)
                        or (
                            ack["kind"] == "wheel"
                            and "control.wheel.v1" not in features
                        )
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
            try:
                if channel:
                    self.audit(subject, "close", channel[3], "closed")
                    if not channel[1].closed:
                        await channel[1].send_json(dict(key=key, op="close"))
            finally:
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
                    if data.get("kind") == "action.result":
                        self.receive_power_result(ident, ws, data)
                        continue
                    if data.get("kind") in ("status", "capabilities"):
                        self.nodes[ident].update(data["kind"], data, time.monotonic())
                        session = self.nodes[ident].status["session"]
                        if (
                            data["kind"] == "capabilities"
                            and "session.identity.v1" in data["features"]
                            and session
                            and ws.get("described") != session["sessionId"]
                        ):
                            ws["described"] = session["sessionId"]
                            await ws.send_json(dict(key="", op="describe"))
                        continue
                    if data.get("op") == "identity":
                        session = self.nodes[ident].status["session"]
                        if (
                            session
                            and session["sessionId"] == data.get("sessionId")
                            and SESSION_IDENTITY.is_valid(data)
                        ):
                            self.labels[ident] = dict(
                                sessionId=session["sessionId"],
                                hostname=data["hostname"],
                                user=data.get("user"),
                                displayName=data.get("displayName"),
                            )
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
            pending = self.power_requests.get(ident)
            if pending and pending[1] is ws and not pending[2].done():
                pending[2].set_result(power.outcome(ident, "unknown", "unconfirmed"))
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
                        and "view" in self.permissions(policy, teacher)
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


def matches(message, request):
    return all(message.get(k) == v for k, v in request.items() if k != "kind")


def identity(request):
    cert = request.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
    return hashlib.sha256(cert).hexdigest()


@web.middleware
async def check_ui_host(request, handler):
    port = request.transport.get_extra_info("sockname")[1]
    host = UI_HOST if port == 80 else f"{UI_HOST}:{port}"
    if request.headers.get("Host") != host:
        raise web.HTTPForbidden()
    return await handler(request)


def ui_app(relay):
    app = web.Application(middlewares=[check_ui_host])
    app.cleanup_ctx.append(relay.watch)
    app.add_routes(
        [
            web.get("/state", relay.state),
            web.get("/stream", relay.browser),
            web.post("/power", relay.power_action),
        ]
    )

    def serve(path, page=False):
        async def file(request):
            policy = relay.policy()
            relay.require_view(policy)
            ancestor = None
            if page:
                try:
                    ancestor = nextcloud_origin(policy.get("nextcloud_origin"))
                except ValueError:
                    pass
            ancestors = ancestor or "'none'"
            headers = {"Content-Security-Policy": f"frame-ancestors {ancestors}"}
            if page:
                headers["Cache-Control"] = "no-store"
            return web.FileResponse(path, headers=headers)

        return file

    for url, path in {
        "/": "web/live.html",
        "/live.js": "web/live.js",
        "/theme.js": "web/theme.js",
        "/input.js": "web/input.js",
        "/input-keys.json": "contracts/input-keys.json",
        "/live.css": "web/live.css",
        "/receiver.mjs": "contracts/preview/receiver.mjs",
    }.items():
        app.router.add_get(url, serve(ROOT / path, page=url == "/"))
    return app


async def run(args):
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    tls.load_verify_locations(args.ca)
    tls.verify_mode = ssl.CERT_REQUIRED
    relay = Relay(args)
    sources = web.Application()
    sources.router.add_get("/source", relay.source)
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    async with AsyncExitStack() as stack:
        stack.callback(relay.db.close)
        source_runner = web.AppRunner(sources, shutdown_timeout=5)
        stack.push_async_callback(source_runner.cleanup)
        await source_runner.setup()
        ui_runner = web.AppRunner(ui_app(relay), shutdown_timeout=5)
        stack.push_async_callback(ui_runner.cleanup)
        await ui_runner.setup()
        await web.TCPSite(source_runner, args.host, args.port, ssl_context=tls).start()
        await web.TCPSite(ui_runner, UI_HOST, args.ui_port).start()
        print(f"Портал преподавателя: http://{UI_HOST}:{args.ui_port}", flush=True)
        await stop.wait()


def main():
    p = argparse.ArgumentParser()
    for name in ("db", "policy", "cert", "key", "ca"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--ice")
    p.add_argument("--host", default=UI_HOST, help="Agent listener address")
    p.add_argument("--port", type=int, default=8767, help="Agent mTLS port")
    p.add_argument("--ui-port", type=int, default=8768, help="Loopback HTTP port")
    args = p.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
