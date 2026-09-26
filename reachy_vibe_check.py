#!/usr/bin/env python3
"""
reachy_vibe_check.py — Vibey looks at you, scores the vibe, and posts it to the
Vibe Check app.

    python3 reachy_vibe_check.py               # capture, score, post, say it out loud
    python3 reachy_vibe_check.py --dry-run     # capture + score only, nothing published
    python3 reachy_vibe_check.py --quiet       # skip the spoken verdict
    python3 reachy_vibe_check.py --caption "hackathon vibe check"

The frame comes from the MJPEG bridge (`reachy_camera.py`, port 8771), because the
Reachy daemon only shares camera frames over WebRTC. Scoring and publishing go
through the same edge functions the iOS app uses, so a robot post is an ordinary
post: same image-safety gate, same rate limit, and it shows up in the feed with no
app change.

The robot has no Apple ID, so it cannot use `auth-session`. `robot-session` is the
second door: it trades VIBEY_ROBOT_KEY for a Vibe session signed server-side.

.env keys (in ~/dev/vibey-robot/.env):
    VIBEY_ROBOT_KEY     the shared secret set as a Supabase function secret
    VIBE_API_KEY        the app's public apikey (the project's legacy anon JWT)
    VIBE_BASE_URL       optional, defaults to the Vibe Check project
    CAM_URL             optional, defaults to http://localhost:8771/frame.jpg
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

from reachy_voice import load_env

load_env()

BASE_URL = os.environ.get(
    "VIBE_BASE_URL", "https://hkakpvfytmuqpjympmwx.supabase.co"
).rstrip("/")
CAM_URL = os.environ.get("CAM_URL", "http://localhost:8771/frame.jpg")
ROBOT_KEY = os.environ.get("VIBEY_ROBOT_KEY", "")
API_KEY = os.environ.get("VIBE_API_KEY", "")

# The posting endpoint rejects anything over 1.1 MB of raw JPEG, so a frame that
# somehow arrives bigger is downscaled rather than silently failing at the server.
MAX_JPEG_BYTES = 1_000_000


def _post(path: str, payload: dict, token: str | None = None, timeout: int = 60) -> dict:
    body = json.dumps(payload).encode()
    headers = {"apikey": API_KEY, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{BASE_URL}/functions/v1/{path}", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace")[:300]
        raise RuntimeError(f"{path} failed ({e.code}): {detail}") from None


def grab_frame() -> bytes:
    """One JPEG from the camera bridge. A dead bridge is the single most likely
    failure here, so it says so instead of surfacing a bare connection error."""
    try:
        with urllib.request.urlopen(CAM_URL, timeout=10) as response:
            jpeg = response.read()
    except Exception as e:  # noqa: BLE001 - any transport failure means the same thing
        raise RuntimeError(
            f"no frame from {CAM_URL} ({e}). Start the camera bridge: "
            "`vibey` (or `source reachy_env/bin/activate && python3 reachy_camera.py`)"
        ) from None
    if len(jpeg) < 4 or jpeg[:3] != b"\xff\xd8\xff":
        raise RuntimeError(f"{CAM_URL} did not return a JPEG ({len(jpeg)} bytes)")
    return shrink(jpeg)


def shrink(jpeg: bytes) -> bytes:
    if len(jpeg) <= MAX_JPEG_BYTES:
        return jpeg
    try:
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(jpeg))
        image.thumbnail((1600, 1600))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, "JPEG", quality=85)
        return buffer.getvalue()
    except Exception as e:  # noqa: BLE001 - Pillow is optional in this venv
        raise RuntimeError(
            f"frame is {len(jpeg)} bytes, over the {MAX_JPEG_BYTES} limit, and it "
            f"could not be downscaled ({e}). `pip install pillow`."
        ) from None


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def score(image: str) -> dict:
    # analyze-selfie sits behind the gateway's JWT check (unlike the community endpoints, which
    # verify the Vibe session themselves), so the apikey has to be presented as a bearer too.
    result = _post("analyze-selfie", {"imageData": image}, token=API_KEY)
    if not isinstance(result.get("score"), (int, float)) or not result.get("analysis"):
        raise RuntimeError(f"analyze-selfie returned no verdict: {result}")
    return result


def session() -> str:
    if not ROBOT_KEY:
        raise RuntimeError("VIBEY_ROBOT_KEY is not set in .env")
    token = _post("robot-session", {}, token=ROBOT_KEY).get("session_token")
    if not token:
        raise RuntimeError("robot-session returned no token")
    return token


def publish(token: str, image: str, caption: str, verdict: dict) -> dict:
    return _post(
        "community-write",
        {
            "action": "create_post",
            "request_id": str(uuid.uuid4()),
            "name": caption,
            "score": round(float(verdict["score"])),
            "analysis": verdict["analysis"],
            "image_data": image,
        },
        token=token,
    )


def name_the_robot(token: str, display_name: str, handle: str) -> dict:
    """Once is enough, but it is idempotent, so the post path just calls it."""
    return _post(
        "community-write",
        {"action": "set_profile", "display_name": display_name, "handle": handle},
        token=token,
    )


def speak(line: str) -> None:
    """Best effort. A robot that is asleep, offline, or busy holding a realtime
    conversation must not cost us the post that already succeeded."""
    try:
        from reachy_voice import say

        say(line)
    except Exception as e:  # noqa: BLE001 - robot offline/mic-locked is routine
        print(f"[vibe-check] could not speak ({e})", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vibey scans a vibe and posts it.")
    parser.add_argument("--caption", default="vibe checked by Vibey 🤖")
    parser.add_argument("--dry-run", action="store_true", help="score only, do not post")
    parser.add_argument("--quiet", action="store_true", help="do not speak the verdict")
    parser.add_argument("--save", help="also write the captured frame to this path")
    args = parser.parse_args()

    if not API_KEY:
        print("VIBE_API_KEY is not set in .env", file=sys.stderr)
        return 2

    jpeg = grab_frame()
    if args.save:
        with open(args.save, "wb") as handle:
            handle.write(jpeg)
    image = data_url(jpeg)
    print(f"[vibe-check] frame {len(jpeg)} bytes, scoring…", flush=True)

    verdict = score(image)
    line = verdict["analysis"]
    print(f"[vibe-check] {round(float(verdict['score']))}/100 — {line}", flush=True)

    if not args.quiet:
        speak(f"Vibe check. {round(float(verdict['score']))} out of 100. {line}")

    if args.dry_run:
        print("[vibe-check] dry run, nothing posted", flush=True)
        return 0

    token = session()
    name_the_robot(token, "Vibey", "vibey")
    posted = publish(token, image, args.caption, verdict)
    print(f"[vibe-check] posted {posted.get('id')} → {posted.get('image_url')}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(f"[vibe-check] {error}", file=sys.stderr)
        sys.exit(1)
