import unittest
from unittest.mock import patch

from live import active_seat


class SeatAdmission(unittest.TestCase):
    def test_busctl_session_properties(self):
        properties = {"Active": "b true", "Remote": "b false", "Type": 's "x11"',
                      "Seat": '(so) "seat0" "/org/freedesktop/login1/seat/seat0"'}

        def busctl(args, **kwargs):
            if "GetSessionByPID" in args:
                return 'o "/org/freedesktop/login1/session/c12"'
            return properties[args[-1]]

        with patch("live.subprocess.check_output", side_effect=busctl):
            self.assertTrue(active_seat(26504))
            for key, value in [("Seat", '(so) "seat1" "/org/freedesktop/login1/seat/seat1"'),
                               ("Active", "b false"), ("Remote", "b true"), ("Type", 's "tty"')]:
                with self.subTest(key=key), patch.dict(properties, {key: value}):
                    self.assertFalse(active_seat(26504))


if __name__ == "__main__":
    unittest.main()
