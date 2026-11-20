"""Browser coordinates refer to the oriented root-window crop from XRandR."""

import json
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch
from Xlib import X
from remote_input import Controller, position


class Coordinates(unittest.TestCase):
    def test_oriented_outputs_and_mirror(self):
        fixture = Path(__file__).resolve().parents[1] / "contracts/examples/status.json"
        monitors = json.loads(fixture.read_text())["session"]["monitors"]
        for m in monitors:
            target = {"scope": "monitor", "monitorId": m["monitorId"]}
            for u in (0, 0.5, 0.999999):
                for v in (0, 0.5, 0.999999):
                    self.assertEqual(
                        position(monitors, target, u, v),
                        (m["x"] + int(u * m["width"]), m["y"] + int(v * m["height"])),
                    )
        desktop = {"scope": "desktop"}
        self.assertIsNone(position(monitors, desktop, 0, 0.99))
        mirror = [monitors[0], dict(monitors[0], monitorId="mirror")]
        self.assertEqual(position(mirror, desktop, 0.5, 0.5), (960, 540))

    def test_wheel_clicks_follow_the_pointer(self):
        controller = Controller.__new__(Controller)
        controller.display = Mock()
        controller.held = set()
        monitors = [dict(monitorId="DP-2", x=1920, y=0, width=1920, height=1080)]
        target = dict(scope="monitor", monitorId="DP-2")
        wheel = dict(kind="wheel", u=0.5, v=0.5)
        with patch("remote_input.xtest.fake_input") as inject:
            controller.event(dict(wheel, steps=-2), monitors, target)
            controller.event(dict(wheel, steps=1), monitors, target)
            controller.event(dict(wheel, steps=1, u=1), monitors, target)
        d = controller.display
        move = call(d, X.MotionNotify, x=2880, y=540)
        up = [call(d, X.ButtonPress, 4), call(d, X.ButtonRelease, 4)]
        down = [call(d, X.ButtonPress, 5), call(d, X.ButtonRelease, 5)]
        self.assertEqual(inject.call_args_list, [move, *up, *up, move, *down])
        self.assertFalse(controller.held)

if __name__ == "__main__":
    unittest.main()
