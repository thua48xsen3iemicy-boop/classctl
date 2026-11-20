import unittest
from unittest.mock import patch

from live import active_seat


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


if __name__ == "__main__":
    unittest.main()
