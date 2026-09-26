# mime_bot — plan

Status: **awaiting approval before scaffolding code**.

## What the user wants (my read)

A new, **deliberately minimal** JS Reachy Mini app:

- Open the Space URL on a phone or laptop.
- HF login → pick a robot → start session.
- Grant camera permission (front camera by default; usable on both phone
  and laptop).
- The robot mimics the user's face **live**, in real time, at ~20 Hz.
- A **tuner panel** with sliders exposing as many face-feature → robot-DOF
  knobs as are usefully tunable, drawn from MediaPipe's full output.

**Explicitly out of scope (per your message):**

- ❌ No video file upload.
- ❌ No YouTube ingest.
- ❌ No "record / save / replay" of moves. Live only.
- ❌ No pre-baked preset library beyond a sensible default.
- ❌ No persistence (no IndexedDB, no HF dataset upload).
- ❌ No annotated-video preview. (Live mesh overlay on the user's video
  stays in scope — different feature.)
- ❌ The Python `video_mimicry` app is left untouched.

## Tech choices

| Concern | Choice |
|---|---|
| App type | Static HF Space (`sdk: static`) |
| Folder | `reachy_mini_apps/mime_bot/` |
| HF Space slug | `mime_bot` (final URL: `https://<user>-mime_bot.static.hf.space/`) |
| SDK pin | `@v1.7.1` (latest tag as of today) |
| Face tracking | `@mediapipe/tasks-vision` `FaceLandmarker` (WebGL) |
| Audio | None — robot stays silent. User's mic is muted. |
| Robot send rate | ~20 Hz via `setTarget()` |
| State | All in memory, no storage |
| UI | Mobile-first, single page, three sections: video / mapping / debug |

## File layout

```
mime_bot/
  plan.md           ← this file
  README.md         ← Space YAML + short description
  index.html        ← scaffold copied from webrtc_example, trimmed hard
  style.css         ← copied from webrtc_example, trimmed
  main.js           ← all app logic (so index.html stays clean)
```

`main.js` is split out (rather than inline in `index.html` like
webrtc_example) because we have non-trivial logic (mapping, tuner state) and
this is the pattern `marionette_js` already uses.

## What MediaPipe FaceLandmarker gives us

Verified before scaffolding — these are the named ARKit-style blendshape
outputs (52 of them) plus the 4×4 head-pose matrix. We can expose any of
these to the tuner. Most useful subset for our purpose:

**Head pose (from `facialTransformationMatrixes`)**
- `head_roll`, `head_pitch`, `head_yaw` — radians

**Eyes**
- `eyeBlinkLeft`, `eyeBlinkRight`
- `eyeWideLeft`, `eyeWideRight`
- `eyeLookInLeft`, `eyeLookOutLeft`, `eyeLookInRight`, `eyeLookOutRight`,
  `eyeLookUpLeft`, `eyeLookDownLeft`, `eyeLookUpRight`, `eyeLookDownRight`
  → derived `gaze_h`, `gaze_v`
- `eyeSquintLeft`, `eyeSquintRight`

**Brows**
- `browInnerUp`, `browOuterUpLeft`, `browOuterUpRight`
- `browDownLeft`, `browDownRight` → derived `brow_furrow`

**Mouth**
- `jawOpen` → mouth_open
- `mouthSmileLeft`, `mouthSmileRight` → smile
- `mouthPucker`, `mouthFunnel`
- `mouthLeft`, `mouthRight` (lateral shift)
- `mouthUpperUpLeft/Right`, `mouthLowerDownLeft/Right` (sneer/frown)

**Cheeks / nose**
- `cheekPuff`, `cheekSquintLeft`, `cheekSquintRight`
- `noseSneerLeft`, `noseSneerRight`

This is **richer than what the Python app extracts** (the Python processor
hand-rolls 16 features; FaceLandmarker hands us 52 named expression scores
plus head pose for free).

## Robot output channels (7)

Same as the Python mapper:

- `head_roll_deg`, `head_pitch_deg`, `head_yaw_deg`
- `head_z_m` (subtle depth)
- `body_yaw_rad`
- `left_antenna_rad`, `right_antenna_rad`

## Tuner UI — split into "head" and "free-routing"

Per the user, head pose (roll/pitch/yaw and forward-back depth) is locked
to **direct geometric mapping** — the 6D pose from FaceLandmarker maps
straight to the robot head. The user only tunes **amplitude**, not which
input maps to which output.

For everything else (body yaw + both antennas), the user wants **full
freedom**: pick any of the ~52 ARKit blendshapes, route it to any of
those three channels, with a tunable weight, and as many rules as they
want.

### Section 1 — Head amplitudes (fixed routing)

```
HEAD (geometric, direct)
  Roll  amplitude            [────●────]   1.0   (× face roll → robot roll)
  Pitch amplitude            [────●────]   1.0
  Yaw   amplitude            [────●────]   1.0
  Forward/back amplitude     [──●──────]   1.0   (face Z depth → robot head z)

  Mirror mode (yaw flip)     [ off | ON  ]      ← default ON per user
  Robot enable (master)      [ off | on  ]
```

4 sliders + 2 toggles.

### Section 2 — Free-routing for body + antennas

For each of `body_yaw`, `left_antenna`, `right_antenna`, a **list of
mapping rules**. Each rule is `{ feature, weight }`. Channel value =
sum(weight × feature) + idle bias.

Each row in the UI:

```
[ feature dropdown ▾ ]  [ weight slider [-2.0 .. +2.0] ]  [ live: 0.42 → 0.30 ]  [×]
```

- Dropdown lists all available features grouped: `head pose`, `eyes`,
  `brows`, `mouth`, `cheeks/nose`, plus a few derived (`gaze_h`, `gaze_v`,
  `blink`, `wink_left`, `wink_right`).
- Weight slider: signed, default ±1.0 range — extends to ±2.0.
- Live readout shows current feature value × weight, plus the channel's
  combined value after clamping.
- `+ Add mapping` button per channel.
- `×` removes a rule.

Default rule set (reproduces Python `full` behaviour close enough):

| Channel | Default rules |
|---|---|
| `body_yaw` | `head_yaw × 0.6` |
| `left_antenna` | `+0.1745` rad bias, `browOuterUpLeft × 1.0`, `jawOpen × 0.4` |
| `right_antenna` | `−0.1745` rad bias, `browOuterUpRight × 1.0`, `jawOpen × 0.4` |

(Bias values use the **idle position from the SDK** —
`INIT_ANTENNAS_JOINT_POSITIONS = [-0.1745, 0.1745]` rad in
`reachy_mini.py`. Sign convention: index 0 = right, index 1 = left.)

A **"Reset to defaults"** button restores the rules above.
A **"STOP"** button (or master toggle off) returns the robot to neutral
and stops sending updates.

### Decisions locked

1. **Mirror mode**: ON by default (user will be side-by-side with robot
   for the demo; reversed feels cuter). Toggle exposed.
2. **Antenna idle bias**: `[-0.1745, +0.1745]` rad (right, left) per
   `INIT_ANTENNAS_JOINT_POSITIONS` in
   `reachy_mini/src/reachy_mini/reachy_mini.py:58` — confirmed reduces
   vertical-shake.
3. **Camera**: prefer `facingMode: "user"` (selfie) on phones, but no
   strict constraint — laptop webcams just work.
4. **Tuner**: head locked to geometric, body+antennas fully free-routed.
5. **HF user**: `RemiFabre` (confirmed via `hf auth whoami`). Push
   allowed.

## Open questions for you

1. **Mirror mode default — on or off?** When the user moves their head to
   their *left*, should the robot move to *its* left (mirror image, feels
   like looking in a mirror) or to its *right* (same direction in world
   space, feels like the robot is following you)? Mirror is more intuitive
   when the user is **facing** the robot (which is the demo). My default
   would be **mirror = ON**. Confirm or flip.
2. **Antenna idle behaviour.** When neither brow nor mouth is active,
   should antennas sit at 0 rad (laid down), or at a small positive rest
   angle so they're visibly upright? The Python `full` preset effectively
   sits at 0 with a -0.15 bias; I'd switch the JS default to a small
   positive rest (~0.1 rad / 6°) so the robot doesn't look "asleep" when
   the user's face is neutral. OK?
3. **Camera selection.** Default to the front-facing camera
   (`facingMode: "user"`) on phones — that's the selfie cam, which is
   what you want for the demo. On laptops there's only one camera, so it
   doesn't matter. Confirm.
4. **Anything else you want exposed in the tuner that I haven't listed?**
   For example: gaze → head yaw coupling, jaw-open → head pitch (looks
   "surprised"), cheek squint → smile-like body sway. Easy to add.
5. **HF username for the Space.** The README YAML doesn't need it but
   `hf repos create` does. I'll use `hf auth whoami` when we get to push
   time — just confirm you're logged in (or want me to skip the push and
   leave it local for you to push manually).

## Build order (small steps, commit each — per your "commit every change" rule)

1. Scaffold `README.md` (Space YAML), `index.html`, `style.css` from
   webrtc_example, trimmed to the essentials (auth → robot picker →
   single mimicry view). Commit.
2. Add `main.js` skeleton: connect / startSession / ensureAwake. Commit.
3. Wire up `<video>` showing user's webcam (not the robot camera).
   Commit.
4. Load FaceLandmarker, log head pose to console. Commit.
5. Implement the linear mapper + send `setTarget()` at 20 Hz with default
   gains, no UI tuner yet. **First on-robot test point.** Commit.
6. Add the tuner sliders + master toggle + reset button. Commit.
7. Mobile layout polish + iPhone Safari test. Commit.
8. Push to HF Space. Verify live. Commit final tweaks.

## What I will NOT do without asking

- Push to HF (requires explicit "ship it").
- Touch the existing `video_mimicry/` Python app.
- Add IndexedDB, recording, file upload, YouTube, or any of the other
  features explicitly cut from scope.
- Add features beyond what's listed above. If during implementation I
  notice something missing, I'll surface it as a question, not silently
  expand scope.

---

**Reply to me with:**
- Answers to the 5 open questions above (or "defaults are fine").
- "Go" to start at step 1 of the build order.
