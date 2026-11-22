import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from live import active_seat
from node.power import Power


class SeatAdmission(unittest.TestCase):
    def test_each_active_local_account_is_admitted(self):
        properties = {
            "User": '(uo) 1001 "/org/freedesktop/login1/user/_1001"',
            "Class": 's "user"',
            "Active": "b true",
            "Remote": "b false",
            "Type": 's "x11"',
            "Seat": '(so) "seat0" "/org/freedesktop/login1/seat/seat0"',
        }
        session = "/org/freedesktop/login1/session/c12"

        def busctl(args, **kwargs):
            if "GetSessionByPID" in args:
                return f'o "{session}"'
            return properties[args[-1]]

        with patch("live.subprocess.check_output", side_effect=busctl):
            self.assertEqual(active_seat(26504, 1001), session)
            self.assertIsNone(active_seat(26504, 0))
            self.assertIsNone(active_seat(26504, 1002))
            for key, value in [
                ("Seat", '(so) "seat1" "/org/freedesktop/login1/seat/seat1"'),
                ("Active", "b false"),
                ("Remote", "b true"),
                ("Type", 's "tty"'),
                ("Class", 's "greeter"'),
            ]:
                with self.subTest(key=key), patch.dict(properties, {key: value}):
                    self.assertIsNone(active_seat(26504, 1001))

            properties.update(
                Active="b true", User='(uo) 1002 "/org/freedesktop/login1/user/_1002"'
            )
            session = "/org/freedesktop/login1/session/c13"
            self.assertEqual(active_seat(26505, 1002), session)

    def test_unmapped_peer_is_denied(self):
        with patch("live.subprocess.check_output", side_effect=OSError):
            self.assertIsNone(active_seat(26504, 1001))


class PowerActions(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "power.sqlite"
        self.power = Power(self.path, "pc-01")
        example = Path(__file__).resolve().parents[1] / "contracts/examples/power-request.json"
        now = datetime.now(timezone.utc)
        self.request = dict(
            json.loads(example.read_text()),
            issuedAt=(now - timedelta(seconds=1)).isoformat(),
            expiresAt=(now + timedelta(seconds=9)).isoformat(),
        )

    def test_replay_after_restart_and_conflict(self):
        with patch.object(Power, "invoke", return_value=("succeeded", "handed-off")) as invoke:
            first = self.power.handle(self.request)
            self.assertEqual(Power(self.path, "pc-01").handle(self.request), first)
            conflict = self.power.handle(dict(self.request, actionId="system.reboot"))
            self.assertEqual(conflict["outcomes"][0]["reason"], "conflict")
            concurrent = self.power.handle(dict(self.request, requestId="next"))
            self.assertEqual(concurrent["outcomes"][0]["reason"], "busy")
            invoke.assert_called_once_with("PowerOff")

    def test_rejects_expired_or_out_of_scope_before_call(self):
        expired = dict(
            self.request, issuedAt="2020-01-01T00:00:00Z", expiresAt="2020-01-01T00:00:10Z",
        )
        with patch.object(Power, "invoke") as invoke:
            self.assertEqual(self.power.handle(expired)["outcomes"][0]["state"], "expired")
            self.assertIsNone(self.power.handle(dict(self.request, nodeIds=["pc-02"])))
            self.assertIsNone(self.power.handle(dict(self.request, nodeIds=["pc-01", "pc-02"])))
            extra = self.power.handle(dict(self.request, parameters={"force": True}))
            self.assertEqual(extra["outcomes"][0]["reason"], "unsupported")
            invoke.assert_not_called()

    def test_timeout_stays_unconfirmed_across_replay(self):
        with patch("node.power.subprocess.run", side_effect=subprocess.TimeoutExpired("busctl", 3)) as run:
            first = self.power.handle(self.request)
            self.assertEqual(first["outcomes"][0]["state"], "unknown")
            self.assertEqual(self.power.handle(self.request), first)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
