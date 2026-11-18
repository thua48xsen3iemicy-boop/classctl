"""Authenticated loopback coordinator for T06. Run separately from the T02 simulator."""

import argparse
import hashlib
import json
import secrets
import sqlite3
import ssl
import time
from http.server import HTTPServer
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from coordinator import CONTRACTS, Handler, Node, inventory, validate


preview_schema = json.loads((CONTRACTS / "preview-v1.schema.json").read_text())
preview_registry = Registry().with_resource(
    "urn:classctl:control:v1",
    Resource.from_contents(json.loads((CONTRACTS / "v1.schema.json").read_text())),
)
PREVIEW = Draft202012Validator(preview_schema, registry=preview_registry)


class AccessServer(HTTPServer):
    def policy(self):
        try:
            return json.loads(self.policy_path.read_text())
        except (OSError, ValueError):
            return {}

    def allowed(self, policy, fingerprint, request):
        teacher = policy.get("teachers", {}).get(fingerprint, {})
        preview = request["kind"] == "preview.subscribe"
        mode = "view" if preview else request["mode"]
        if mode not in teacher.get("rooms", {}).get(self.room["roomId"], []):
            return False
        node = self.nodes[request["nodeId"]]
        session = node.status["session"]
        target = (
            {"scope": "monitor", "monitorId": request["monitorId"]}
            if preview
            else request["target"]
        )
        feature = "preview.jpeg.v1" if preview else mode + "." + target["scope"]
        return bool(
            session
            and node.status["availability"] == "online"
            and session["sessionId"] == request["sessionId"]
            and session["layoutRevision"] == request["layoutRevision"]
            and feature in node.capabilities["features"]
            and (
                target["scope"] == "desktop"
                or any(
                    m["monitorId"] == target["monitorId"] for m in session["monitors"]
                )
            )
        )

    def audit(self, subject, event, target, outcome):
        with self.db:
            self.db.execute(
                "INSERT INTO audit VALUES (CURRENT_TIMESTAMP,?,?,?,?)",
                (subject, event, json.dumps(target), outcome),
            )

    def service_actions(self):
        now = time.monotonic()
        policy = self.policy()
        for node in self.nodes.values():
            node.expire(now, 2, 6)
        for token, grant in list(self.grants.items()):
            if (
                now >= grant["until"]
                or not self.allowed(policy, grant["teacher"], grant["request"])
                or policy.get("nodes", {}).get(grant["peer"])
                != grant["request"]["nodeId"]
            ):
                self.audit(
                    grant["teacher"], grant["stage"], grant["request"], "revoked"
                )
                del self.grants[token]


class SecureHandler(Handler):
    def route(self, method):
        server = self.server
        server.service_actions()
        subject = hashlib.sha256(
            self.connection.getpeercert(binary_form=True)
        ).hexdigest()
        policy = server.policy()
        teacher = policy.get("teachers", {}).get(subject, {})
        node_id = policy.get("nodes", {}).get(subject)
        room = server.room["roomId"]
        parts = self.path.strip("/").split("/")
        target = {"roomId": room}
        event = "unknown"
        try:
            if method == "GET" and parts == ["room"]:
                event = "subscribe"
                if "view" not in teacher.get("rooms", {}).get(room, []):
                    raise PermissionError
                result = server.room
            elif len(parts) == 3 and parts[0] == "nodes" and parts[1] in server.nodes:
                _, ident, event = parts
                target["nodeId"] = ident
                node = server.nodes[ident]
                if method == "GET" and event in ("status", "capabilities"):
                    if "view" not in teacher.get("rooms", {}).get(room, []):
                        raise PermissionError
                    result = getattr(node, event)
                elif method == "POST" and event in (
                    "status",
                    "heartbeat",
                    "capabilities",
                ):
                    if node_id != ident:
                        raise PermissionError
                    node.update(event, self.body(), time.monotonic())
                    server.service_actions()
                    result = node.status
                else:
                    return self.reply(404, "Unknown route")
            elif method == "POST" and parts in (["grant"], ["subscribe"]):
                event = parts[0]
                request = self.body()
                if event == "subscribe":
                    if (
                        not PREVIEW.is_valid(request)
                        or request["kind"] != "preview.subscribe"
                    ):
                        raise ValueError("Expected preview subscription")
                else:
                    validate(request, "access")
                target.update(request)
                ident = request["nodeId"]
                peers = [
                    key
                    for key, value in policy.get("nodes", {}).items()
                    if value == ident
                ]
                if (
                    ident not in server.nodes
                    or len(peers) != 1
                    or not server.allowed(policy, subject, request)
                ):
                    raise PermissionError
                token = secrets.token_urlsafe(32)
                server.grants[token] = dict(
                    teacher=subject,
                    peer=peers[0],
                    request=request,
                    stage="pending",
                    until=time.monotonic() + 10,
                )
                result = {"ticket": token}
            elif method == "POST" and parts in (["open"], ["active"]):
                event = parts[0]
                data = self.body()
                if not isinstance(data, dict) or not isinstance(data.get("token"), str):
                    raise ValueError("Expected token")
                token = data["token"]
                grant = server.grants.get(token)
                if not grant or grant["peer"] != subject:
                    raise PermissionError
                target.update(grant["request"], teacher=grant["teacher"])
                if event == "open" and grant["stage"] == "pending":
                    del server.grants[token]
                    handle = secrets.token_urlsafe(32)
                    grant.update(stage="active", until=time.monotonic() + 60)
                    server.grants[handle] = grant
                    result = {"handle": handle, "access": grant["request"]}
                elif event == "active" and grant["stage"] == "active":
                    result = grant["request"]
                else:
                    raise PermissionError
            else:
                return self.reply(404, "Unknown route")
        except (ValueError, PermissionError) as error:
            code = 403 if isinstance(error, PermissionError) else 400
            server.audit(subject, event, target, f"denied-{code}")
            return self.reply(code, "Denied")
        server.audit(subject, event, target, "allowed")
        self.reply(200, result)

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            raise ValueError("Invalid length")
        return json.loads(self.rfile.read(length))


def main():
    parser = argparse.ArgumentParser()
    for name in ("db", "cert", "key", "ca", "policy"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    context.load_verify_locations(args.ca)
    context.verify_mode = ssl.CERT_REQUIRED
    room = inventory(args.db, CONTRACTS / "examples/room.json")
    with AccessServer(("127.0.0.1", args.port), SecureHandler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.room = room
        server.nodes = {
            s["nodeId"]: Node(s["nodeId"]) for s in room["seats"] if s["nodeId"]
        }
        server.grants = {}
        server.policy_path = Path(args.policy)
        server.db = sqlite3.connect(args.db)
        server.db.execute(
            "CREATE TABLE IF NOT EXISTS audit (time TEXT, subject TEXT, event TEXT, target TEXT, outcome TEXT)"
        )
        print(server.server_port, flush=True)
        server.serve_forever(poll_interval=0.2)


if __name__ == "__main__":
    main()
