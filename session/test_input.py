"""Browser coordinates refer to the oriented root-window crop from XRandR."""

import json
import unittest
from pathlib import Path
from remote_input import position


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


if __name__ == "__main__":
    unittest.main()
