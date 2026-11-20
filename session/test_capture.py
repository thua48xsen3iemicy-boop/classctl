"""The direct capture keeps one input per track and falls back on read errors."""

import unittest
from unittest.mock import Mock, patch

import av
from aiortc.mediastreams import MediaStreamError
from PIL import Image

from live import ScreenCapture


class RegionCapture(unittest.TestCase):
    def test_region_read_failure_closes_input_and_falls_back(self):
        monitors = [dict(monitorId="DP-2", x=640, y=20, width=360, height=640)]
        container = Mock()
        direct_frame = av.VideoFrame(360, 640, "bgr0")
        container.decode.return_value = iter([direct_frame, direct_frame])
        grab = ScreenCapture(monitors, synthetic=False)
        with (
            patch.dict("os.environ", {"DISPLAY": ":1.0"}),
            patch("live.av.open", return_value=container) as open_input,
            patch("live.capture", return_value=Image.new("RGB", (360, 640))) as pillow,
        ):
            self.assertIs(grab.read(), direct_frame)
            self.assertIs(grab.read(), direct_frame)
            self.assertEqual(grab.read().format.name, "rgb24")
            self.assertEqual(grab.read().format.name, "rgb24")

        open_input.assert_called_once()
        options = open_input.call_args.kwargs["options"]
        self.assertEqual(
            (options["grab_x"], options["grab_y"], options["video_size"]),
            ("640", "20", "360x640"),
        )
        self.assertEqual(pillow.call_count, 2)
        container.close.assert_called_once()
        grab.close()
        container.close.assert_called_once()
        with self.assertRaises(MediaStreamError):
            grab.read()


if __name__ == "__main__":
    unittest.main()
