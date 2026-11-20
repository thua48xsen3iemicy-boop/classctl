"""XTEST input for a single, authorized X11 session."""

import json
import re
import subprocess
from pathlib import Path

from Xlib import X, display
from Xlib.ext import xtest

KEYS = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts/input-keys.json").read_text()
)


def position(monitors, target, u, v):
    selected = [
        m
        for m in monitors
        if target["scope"] == "desktop" or m["monitorId"] == target["monitorId"]
    ]
    if not selected or not (0 <= u < 1 and 0 <= v < 1):
        return None
    left = min(m["x"] for m in selected)
    top = min(m["y"] for m in selected)
    right = max(m["x"] + m["width"] for m in selected)
    bottom = max(m["y"] + m["height"] for m in selected)
    x, y = left + int(u * (right - left)), top + int(v * (bottom - top))
    if any(
        m["x"] <= x < m["x"] + m["width"] and m["y"] <= y < m["y"] + m["height"]
        for m in selected
    ):
        return x, y


class Controller:
    def __init__(self):
        self.display = display.Display()
        try:
            if not self.display.has_extension("XTEST"):
                raise ValueError("XTEST unavailable")
            xkb = subprocess.check_output(
                ["xkbcomp", "-xkb", self.display.get_display_name(), "-"],
                text=True,
                timeout=2,
            )
            self.codes = {
                k: int(v) for k, v in re.findall(r"<(\w+)>\s*=\s*(\d+);", xkb)
            }
        except BaseException:
            self.display.close()
            raise
        self.held = set()

    def event(self, data, monitors, target):
        kind = data["kind"]
        if kind == "release":
            self.release()
            return
        if kind in ("button", "move", "wheel"):
            point = position(monitors, target, data["u"], data["v"])
            if point:
                xtest.fake_input(self.display, X.MotionNotify, x=point[0], y=point[1])
            elif kind != "button" or data["down"]:
                return
        if kind == "wheel":
            steps = int(data["steps"])
            button = 4 if steps < 0 else 5
            for _ in range(abs(steps)):
                xtest.fake_input(self.display, X.ButtonPress, button)
                xtest.fake_input(self.display, X.ButtonRelease, button)
        if kind in ("button", "key"):
            code = (
                data["button"] + 1
                if kind == "button"
                else self.codes.get(KEYS.get(data["code"]))
            )
            if code is None:
                return
            key = (kind, code)
            if data["down"] != (key in self.held):
                if data["down"]:
                    self.held.add(key)
                else:
                    self.held.discard(key)
                event = (
                    (X.ButtonPress if data["down"] else X.ButtonRelease)
                    if kind == "button"
                    else (X.KeyPress if data["down"] else X.KeyRelease)
                )
                xtest.fake_input(self.display, event, code)
        self.display.sync()

    def release(self):
        for kind, code in self.held:
            xtest.fake_input(
                self.display, X.KeyRelease if kind == "key" else X.ButtonRelease, code
            )
        self.held.clear()
        self.display.sync()

    def close(self):
        try:
            self.release()
        finally:
            self.display.close()
