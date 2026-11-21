"""Execute only known power actions with a durable record on the target PC."""

import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone

from actions.power import PARAMETERS, REBOOT, SHUTDOWN, TTL, WIRE, result

METHODS = {SHUTDOWN: "PowerOff", REBOOT: "Reboot"}


class Power:
    def __init__(self, path, node_id):
        self.path = path
        self.node_id = node_id
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS power ("
                "request_id TEXT PRIMARY KEY, request TEXT NOT NULL, expires REAL NOT NULL, "
                "state TEXT NOT NULL, reason TEXT NOT NULL)"
            )

    def handle(self, request):
        if request.get("kind") != "action.request" or not WIRE.is_valid(request):
            return None
        if request["nodeIds"] != [self.node_id]:
            return None
        request_id = request["requestId"]
        try:
            state, reason = self.execute(request)
        except sqlite3.Error as error:
            print(f"Power journal: {error}", flush=True)
            state, reason = "unknown", "unconfirmed"
        return result(request_id, self.node_id, state, reason)

    def execute(self, request):
        method = METHODS.get(request["actionId"])
        if method is None or not PARAMETERS.is_valid(request["parameters"]):
            return "rejected", "unsupported"
        issued = datetime.fromisoformat(request["issuedAt"])
        expires = datetime.fromisoformat(request["expiresAt"])
        if not 0 < (expires - issued).total_seconds() <= TTL:
            return "rejected", "invalid-ttl"
        request_id = request["requestId"]
        serialized = json.dumps(request, sort_keys=True)
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                now = datetime.now(timezone.utc)
                db.execute("DELETE FROM power WHERE expires < ?", (now.timestamp() - 60,))
                previous = db.execute(
                    "SELECT request, state, reason FROM power WHERE request_id = ?", (request_id,)
                ).fetchone()
                if previous:
                    if previous[0] != serialized:
                        return "rejected", "conflict"
                    return previous[1], previous[2]
                if now >= expires:
                    return "expired", "expired"
                if now < issued:
                    return "rejected", "invalid-clock"
                if db.execute(
                    "SELECT 1 FROM power WHERE expires > ? AND state IN ('unknown', 'succeeded')",
                    (now.timestamp(),),
                ).fetchone():
                    return "rejected", "busy"
                db.execute(
                    "INSERT INTO power VALUES (?, ?, ?, 'unknown', 'unconfirmed')",
                    (request_id, serialized, expires.timestamp()),
                )
            now = datetime.now(timezone.utc)
            if now >= expires:
                outcome = "expired", "expired"
            elif now < issued:
                outcome = "rejected", "invalid-clock"
            else:
                outcome = self.invoke(method)
            with db:
                db.execute(
                    "UPDATE power SET state = ?, reason = ? WHERE request_id = ?",
                    (*outcome, request_id),
                )
        return outcome

    @staticmethod
    def invoke(method):
        try:
            subprocess.run(
                [
                    "busctl", "--system", "call", "org.freedesktop.login1",
                    "/org/freedesktop/login1", "org.freedesktop.login1.Manager",
                    method, "b", "false",
                ],
                check=True, stdout=subprocess.DEVNULL, timeout=3,
            )
        except subprocess.TimeoutExpired:
            return "unknown", "unconfirmed"
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"login1 {method}: {error}", flush=True)
            return "failed", "system-error"
        return "succeeded", "handed-off"
