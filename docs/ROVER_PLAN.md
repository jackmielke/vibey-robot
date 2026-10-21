# Vibey + Wave Rover game plan

Hardware: Waveshare WAVE ROVER (ESP32, JSON over HTTP at `/js?json=...`),
driven by `reachy_rover.py`. Vibey's voice brain gets a `drive` tool whenever
`ROVER_URL` is set; Telegram has `/drive`; the chat service has `POST /drive`.

Status (2026-10-21): phase 1 works on the real rover, phase 2 has started,
phases 3 and 4 are planned.

## Phase 1: Vibey drives the rover from the table (working)
- Vibey stays put. You talk to it, it remote-controls the rover with short
  timed moves (max 4 s each, rover auto-stops if it hears nothing for 3 s).
- Setup: rover joins the home Wi-Fi instead of its own "UGV" hotspot
  (`./vibey rover wifi "SSID"` while the Mac is on UGV), then
  `./vibey rover find --save`, then restart Vibey. The rover's Wi-Fi is
  2.4 GHz only. After a power cycle it may get a new address; a failed send
  makes Vibey sweep the subnet and save the new `ROVER_URL` (at most once a
  minute).
- `ROVER_RIDING` unset: the tool describes the rover as a separate car.
- V0 manual driving: the Wheels panel in the Vibey app (control.html on
  :8770) has a hold-to-drive pad, a joystick, WASD/arrow keys, Q/E to spin,
  game controller support, a speed picker and a stop. The iPhone app's Drive
  screen has the same pad and joystick.
  It goes through the chat service (`/rover`), which owns the rover, so the
  pad and Vibey's voice never drive it at the same time.
- Tuned on the real rover: 0.15 power only hums, so moves get at least 0.22;
  spins in place scrub the tyres and get at least 0.4, and still top out
  around 25 deg/s. Battery is 3x 18650 (about 1 to 2 hours of driving).

## Phase 2: Vibey watches the rover (started)
- `reachy_rover_pilot.py`: frame from :8771, a vision model picks ONE short
  move toward a goal, drive it, look again, up to 10 steps. Voice tool
  `drive_to` ("drive to the couch"), and the "Drive by eye" box in the Wheels
  panel. Any stop or manual press ends it. Same camera/privacy rules as `look`.
- Point Vibey's camera at the floor area. Between moves Vibey calls `look`
  to see where the rover went, so "drive it to the couch" becomes
  move, look, correct, repeat.

## Phase 3: Vibey rides on top
- Build a flat, low mount with a wide base; strap Vibey down. Check it stays
  upright at "fast" on a gentle stop before trusting it.
- Heads up: Vibey (about 1.5 kg) is nearly double the rover's rated 0.8 kg
  payload, and the wheels sit only about 11 cm apart front to back. Add
  anti-tip ball casters front and back, a few mm off the floor.
- `drive` ramps speed up and down over 0.5 s when riding, so stops don't
  pitch Vibey forward. Keep head pitch small while moving.
- Power: Vibey's own battery (Reachy Mini wireless) or a USB-C pack.
- Set `ROVER_RIDING=1` (slower speeds, "my wheels" wording).

## Phase 4: camera-guided movement
- Vibey's head camera now faces forward from the rover. A loop grabs a frame
  from the camera service (:8771), asks a vision model for a next move
  (forward/turn/stop plus a short reason), drives one short step, repeats.
- Safety first: stop on any "stop", on low battery (`status` voltage), and
  when the frame shows something close. Start with "come to me" and "follow me"
  using the face tracker that already exists.
