"""Exercise the coordinator across two process lifetimes."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from coordinator import inventory

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parent / "contracts/examples"


@contextmanager
def running(db, *options):
    with subprocess.Popen([sys.executable, str(HERE / "coordinator.py"), "--db", db, "--port", "0",
                           "--stale", "1", "--offline", "4", *options], stdout=subprocess.PIPE,
                          text=True) as proc:
        try:
            port = int(proc.stdout.readline())
            def call(path, message=None):
                body = None if message is None else json.dumps(message).encode()
                with urlopen(Request(f"http://127.0.0.1:{port}/{path}", data=body), timeout=3) as resp:
                    return json.load(resp)
            yield call
        finally:
            proc.terminate()


def accept():
    prefix = "nodes/pc-01/"
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "test.sqlite")
        with running(db) as call:
            room = call("room")
            assert room == json.loads((EXAMPLES / "room.json").read_text())
            status = json.loads((EXAMPLES / "status.json").read_text())
            assert call(prefix+"status", status) == status
            assert len(status["session"]["monitors"]) == 2
            caps = json.loads((EXAMPLES / "capabilities.json").read_text())
            call(prefix+"capabilities", caps)
            assert call(prefix+"capabilities") == caps
            time.sleep(1.2)
            stale = call(prefix+"heartbeat", {})
            assert stale["availability"] == "online" and stale["preview"]["state"] == "stale"
            time.sleep(4.1)
            offline = call(prefix+"status")
            assert offline["availability"] == "offline" and offline["session"] is None
            assert not call(prefix+"capabilities")["features"]
            assert call("room") == room
            try:
                call(prefix+"control", {})
            except HTTPError as err:
                assert err.code == 404
            else:
                raise AssertionError("Unexpected control endpoint")
        with running(db, "--seed", str(Path(tmp) / "absent")) as call:
            assert call("room") == room and call(prefix+"status") == offline

        seed = Path(tmp) / "expanded.json"
        desired = dict(room, seats=[
            dict(seatId="desk-1", row=1, column=1, nodeId="pc-03"),
            dict(seatId="desk-2", row=1, column=2, nodeId="pc-02"),
            dict(seatId="desk-3", row=1, column=3, nodeId="pc-01"),
        ])
        seed.write_text(json.dumps(desired))
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE audit (event TEXT)")
            conn.execute("INSERT INTO audit VALUES ('before deployment')")
        expanded = inventory(db, seed, extend=True)
        assert expanded["seats"] == room["seats"] + [
            dict(seatId="desk-3", row=1, column=3, nodeId="pc-03"),
        ]
        assert inventory(db, seed, extend=True) == expanded
        seed.write_text(json.dumps(room))
        try:
            inventory(db, seed, extend=True)
        except ValueError as err:
            assert "pc-03" in str(err)
        else:
            raise AssertionError("Unexpected removal from the inventory")
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT event FROM audit").fetchall() == [("before deployment",)]
        with running(db) as call:
            assert call("room") == expanded and call("nodes/pc-03/status")["availability"] == "offline"
    print("OK: two monitors, stale/online, offline, inventory after restart, control disabled")
    print("OK: deployment appends seats, preserves audit, and accepts a repeat with the same nodes")


if __name__ == "__main__":
    accept()
