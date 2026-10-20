# Vibey + Wave Rover game plan

Hardware: Waveshare WAVE ROVER (ESP32, JSON over HTTP at `/js?json=...`),
driven by `reachy_rover.py`. Vibey's voice brain gets a `drive` tool whenever
`ROVER_URL` is set; Telegram has `/drive`; the chat service has `POST /drive`.

## Phase 1: Vibey drives the rover from the table (now)
- Vibey stays put. You talk to it, it remote-controls the rover with short
  timed moves (max 4 s each, rover auto-stops if it hears nothing for 3 s).
- Setup: rover joins the home Wi-Fi instead of its own "UGV" hotspot
  (`./vibey rover wifi "SSID"` while the Mac is on UGV), then
  `./vibey rover find --save`, then restart Vibey.
- `ROVER_RIDING` unset: the tool describes the rover as a separate car.

## Phase 2: Vibey watches the rover
- Point Vibey's camera at the floor area. Between moves Vibey calls `look`
  to see where the rover went, so "drive it to the couch" becomes
  move, look, correct, repeat.

## Phase 3: Vibey rides on top
- Build a flat, low mount with a wide base; strap Vibey down. Check it stays
  upright at "fast" on a gentle stop before trusting it.
- Power: Vibey's own battery (Reachy Mini wireless) or a USB-C pack.
- Set `ROVER_RIDING=1` (slower speeds, "my wheels" wording).

## Phase 4: camera-guided movement
- Vibey's head camera now faces forward from the rover. A loop grabs a frame
  from the camera service (:8771), asks a vision model for a next move
  (forward/turn/stop plus a short reason), drives one short step, repeats.
- Safety first: stop on any "stop", on low battery (`status` voltage), and
  when the frame shows something close. Start with "come to me" and "follow me"
  using the face tracker that already exists.
