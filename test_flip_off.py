#!/usr/bin/env python3
"""Geometry check for the flip-off detector in reachy_gestures.

Runs without mediapipe, a camera, or the robot: it feeds hand-shaped
landmark sets straight into the ratio test. That covers the part that is
actually easy to get wrong — a threshold that separates "extended" from
"curled" for one hand orientation and not the others — which is why every
pose here is also replayed rotated and scaled.

    python3 test_flip_off.py
"""
import importlib.util
import math
import os
import sys

os.environ.setdefault("GESTURE_ON", "0")
_here = os.path.dirname(os.path.abspath(__file__))


class P:
    def __init__(self, x, y):
        self.x, self.y = x, y


def _load():
    """Import the module's pure-geometry half without cv2/mediapipe.

    reachy_gestures imports the whole gesture venv at module scope, which the
    system python does not have. Dropping those import lines and everything
    from the frame grabber down leaves the constants and the ratio maths,
    which is all this test is about.
    """
    drop = ("import cv2", "import mediapipe", "import numpy",
            "from mediapipe", "import reachy_emotes")
    lines = []
    for line in open(os.path.join(_here, "reachy_gestures.py")):
        if line.startswith("def _frame("):
            break
        if line.startswith(drop):
            continue
        lines.append(line)
    mod = {}
    exec(compile("".join(lines), "reachy_gestures.py", "exec"), mod)
    return mod


def hand(extended, *, angle=0.0, scale=1.0, cx=0.5, cy=0.9):
    """A 21-point hand with the named fingers out and the rest curled.

    Wrist at the origin, fingers pointing up the -y axis, then rotated by
    `angle` so the same pose can be tested lying on its side.
    """
    pts = [(0.0, 0.0)]  # 0 wrist
    # thumb 1-4, roughly out to the side and irrelevant to the test
    pts += [(0.05 * i, -0.04 * i) for i in range(1, 5)]
    for finger, spread in (("index", -0.06), ("middle", -0.02),
                           ("ring", 0.02), ("pinky", 0.06)):
        out = finger in extended
        mcp = (spread, -0.20)
        pip = (spread, -0.30)
        if out:
            dip, tip = (spread, -0.42), (spread, -0.52)
        else:
            # curled: the tip folds back toward the palm
            dip, tip = (spread, -0.26), (spread, -0.17)
        pts += [mcp, pip, dip, tip]
    out = []
    for x, y in pts:
        xr = x * math.cos(angle) - y * math.sin(angle)
        yr = x * math.sin(angle) + y * math.cos(angle)
        out.append(P(cx + xr * scale, cy + yr * scale))
    return out


def main() -> int:
    m = _load()
    is_flip = m["_is_flip_off"]
    ext = m["_finger_extension"]

    cases = [
        ("flip-off",             {"middle"},                            True),
        ("fist",                 set(),                                 False),
        ("open palm",            {"index", "middle", "ring", "pinky"},  False),
        ("peace sign",           {"index", "middle"},                   False),
        ("pointing",             {"index"},                             False),
        ("middle + pinky",       {"middle", "pinky"},                   False),
    ]
    fails = 0
    for name, fingers, want in cases:
        # same pose upright, on its side, upside down, near and far
        for angle in (0.0, math.pi / 2, math.pi, -math.pi / 3):
            for scale in (0.5, 1.0, 2.2):
                lm = hand(fingers, angle=angle, scale=scale)
                got = is_flip(lm)
                if got != want:
                    fails += 1
                    print(f"FAIL {name}: angle={angle:.2f} scale={scale} "
                          f"→ {got}, wanted {want}")
        lm = hand(fingers)
        print(f"{'ok ' if not fails else '   '}{name:<16} "
              f"mid={ext(lm, 'middle'):.2f} "
              f"idx={ext(lm, 'index'):.2f} "
              f"ring={ext(lm, 'ring'):.2f} "
              f"pinky={ext(lm, 'pinky'):.2f} → {is_flip(lm)}")

    print("PASS" if not fails else f"{fails} FAILURES")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
