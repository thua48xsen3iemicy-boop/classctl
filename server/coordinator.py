"""Loopback-only T02 inventory and node simulator."""
import argparse
import json
import sqlite3
import time
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
SCHEMA = Draft202012Validator(json.loads((CONTRACTS / "v1.schema.json").read_text()),
                              format_checker=FormatChecker())


def validate(message, kind):
    if not SCHEMA.is_valid(message) or message["kind"] != kind:
        raise ValueError(f"Expected valid v1 {kind}")


def unique(values):
    values = list(values)
    if len(set(values)) != len(values):
        raise ValueError("Duplicate inventory or monitor identifier")


def validate_room(room):
    validate(room, "room")
    seats = room["seats"]
    unique(s["seatId"] for s in seats)
    unique((s["row"], s["column"]) for s in seats)
    unique(s["nodeId"] for s in seats if s["nodeId"] is not None)


def append_nodes(room, desired):
    validate_room(desired)
    if desired["roomId"] != room["roomId"]:
        raise ValueError(f"Existing room is {room['roomId']}, requested {desired['roomId']}")
    seats = room["seats"]
    assigned = {s["nodeId"] for s in seats if s["nodeId"] is not None}
    requested = [s["nodeId"] for s in desired["seats"] if s["nodeId"] is not None]
    missing = assigned.difference(requested)
    if missing:
        raise ValueError("Keep existing nodes in the deployment inventory: " + ", ".join(sorted(missing)))

    used_ids = {s["seatId"] for s in seats}
    column = max((s["column"] for s in seats if s["row"] == 1), default=0) + 1
    for node in requested:
        if node in assigned:
            continue
        while f"desk-{column}" in used_ids:
            column += 1
        seats.append(dict(seatId=f"desk-{column}", row=1, column=column, nodeId=node))
        column += 1


def inventory(database, seed, *, extend=False):
    with sqlite3.connect(database) as db:
        if extend:
            db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS inventory (id INTEGER PRIMARY KEY CHECK(id=1), room TEXT NOT NULL)")
        row = db.execute("SELECT room FROM inventory WHERE id=1").fetchone()
        room = json.loads(row[0] if row else seed.read_text())
        validate_room(room)
        if not row:
            db.execute("INSERT INTO inventory VALUES (1, ?)", (json.dumps(room),))
        elif extend:
            previous_count = len(room["seats"])
            append_nodes(room, json.loads(seed.read_text()))
            if len(room["seats"]) != previous_count:
                validate_room(room)
                db.execute("UPDATE inventory SET room = ? WHERE id = 1", (json.dumps(room),))
        return room


class Node:
    def __init__(self, node_id):
        self.node_id = node_id
        self.disconnect()

    def disconnect(self):
        self.heartbeat = None
        self.verified = None
        self.status = dict(version=1, kind="status", nodeId=self.node_id, availability="offline",
                           session=None, preview=dict(state="pending", lastFrameAt=None, lastVerifiedAt=None))
        self.capabilities = dict(version=1, kind="capabilities", nodeId=self.node_id, features=[], actions=[])

    def expire(self, now, stale, offline):
        if self.heartbeat is not None and now - self.heartbeat >= offline:
            self.disconnect()
        elif self.verified is not None and now - self.verified >= stale:
            self.status["preview"]["state"] = "stale"

    def update(self, event, data, now):
        if event == "heartbeat":
            if data != {}:
                raise ValueError("Expected empty object")
        else:
            validate(data, event)
            if data["nodeId"] != self.node_id:
                raise ValueError("Wrong node")
        if event == "status":
            if data["availability"] == "offline":
                self.disconnect()
                return
            session = data["session"]
            if session:
                unique(m["monitorId"] for m in session["monitors"])
            if session != self.status["session"]:
                self.capabilities.update(features=[], actions=[])
            self.status = deepcopy(data)
            self.verified = now if data["preview"]["state"] in ("moving", "unchanged") else None
        elif event == "capabilities":
            self.capabilities = data
        self.heartbeat = now
        self.status["availability"] = "online"


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route(self, method):
        parts = self.path.strip("/").split("/")
        if method == "GET" and parts == ["room"]:
            return self.reply(200, self.server.room)
        if len(parts) != 3 or parts[0] != "nodes" or parts[1] not in self.server.nodes:
            return self.reply(404, "Unknown route or node")
        node = self.server.nodes[parts[1]]
        event = parts[2]
        now = time.monotonic()
        node.expire(now, self.server.stale, self.server.offline)
        if method == "GET" and event in ("status", "capabilities"):
            return self.reply(200, getattr(node, event))
        if method != "POST" or event not in ("status", "capabilities", "heartbeat"):
            return self.reply(404, "Unknown route")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Invalid body length")
            data = json.loads(self.rfile.read(length))
            node.update(event, data, time.monotonic())
        except ValueError as error:
            return self.reply(400, str(error))
        self.reply(200, node.status)

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--seed", type=Path, default=CONTRACTS / "examples/room.json")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--stale", type=int, default=2)
    parser.add_argument("--offline", type=int, default=6)
    args = parser.parse_args()
    if not 0 < args.stale < args.offline:
        parser.error("Require 0 < stale < offline")
    room = inventory(args.db, args.seed)
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.room = room
        server.nodes = {s["nodeId"]: Node(s["nodeId"]) for s in room["seats"] if s["nodeId"]}
        server.stale, server.offline = args.stale, args.offline
        print(server.server_port, flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
