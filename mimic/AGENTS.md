# mime_bot — agent guide

A short brief for any agent picking this app up. **Read these first**, in order:

1. **[`plan.md`](plan.md)** — the design spec the user signed off on (scope, locked decisions, layout, default mappings). The source of truth for *what this app is*.
2. **[`main.js`](main.js)** — single-file app logic, top to bottom: SDK lifecycle → camera → FaceLandmarker → mapping → 20 Hz send loop → tuner UI → blendshape monitor → shutdown. The file is heavily commented at decision points.
3. **`../reachy_mini/AGENTS.md`** — the **authoritative** doc for everything daemon/SDK/JS-Space related (auth flow, signaling, command shapes, safety limits, deployment). When in doubt about robot interaction, that doc wins. Don't duplicate its content here.

The Space is live at **https://remifabre-mime-bot.static.hf.space/** (note: HF lowercases the username and hyphenates the slug — the underscore form `mime_bot` only appears in `git remote`).

---

## App at a glance

Live face mimicry over WebRTC. Webcam → MediaPipe FaceLandmarker (52 ARKit blendshapes + 4×4 head transformation matrix) → mapping → robot at 20 Hz. Head pose is locked to direct geometric mapping (amplitude-only tuning); body yaw + L/R antennas are free-routed via a `{feature, weight}` rule editor. There's a live 52-bar blendshape monitor flush under the video pair — that's the demo hero view.

## Locked decisions (do not silently change)

- **Vendored `reachy-mini.js`** is copied from `../marionette_js/`, *not* imported from the upstream tag. It adds `setFullTarget` / `gotoTarget` / enriched `robotState` (`headMatrix`, `antennasRad`, `bodyYaw`, `motorMode`). The upstream `@v1.7.x` SDK does *not* have these; do not switch back without porting equivalents.
- **MediaPipe pinned to `@mediapipe/tasks-vision@0.10.35`**. `0.10.22` was a guess and 404s on jsdelivr. If bumping, verify both `/+esm` and `/wasm/` directory exist on jsdelivr first.
- **MediaPipe import is lazy** (inside `initFaceLandmarker()`), *not* at module top. Top-level import was blocking `DOMContentLoaded` for seconds → the page hung at "Checking sign-in…". Keep it lazy.
- **Auth flow mirrors marionette_js**: `authenticate()` → auto-`connect()` → robotsChanged renders picker → tap a card calls `startSession()` directly. No intermediate Connect or Start button.
- **Smooth startup uses `Motor.softReturnToBase`** (pin current → enable torque → daemon-side scaled goto). Same primitive used on Stop and on `pagehide` / `beforeunload`. The shutdown handler is *synchronous* on purpose — see the comment block above `function shutdown()`.
- **Pitch sign is empirically flipped** in `matrixToRollPitchYaw` (`pitch = -atan2(-m12, m22)`). User tested: looking down nods the robot down with this sign. Don't flip it back without re-testing.
- **Mirror mode default = ON, applied at the mapper** by negating `head_yaw` in the feature dict. Any rule using `head_yaw` (e.g. body yaw) inherits the flip. The CSS `.mirror-preview` class only flips the *visual* preview.
- **Antenna idle bias = `[-0.1745, +0.1745]` rad** ([right, left]). Matches `INIT_ANTENNAS_JOINT_POSITIONS` in `reachy_mini/src/reachy_mini/reachy_mini.py:58`. The ±10° tilt damps a vertical-resonance shake.
- **Head pose channels are now ADDITIVELY free-routable** (in `config.headRules`). Geometric pose × `config.headAmp` is computed first, then per-channel rules are summed onto it before clamping. The head channels are `head_roll/pitch/yaw` (degrees) and `head_x/y/z` (metres). Body yaw + 2 antennas remain pure rule outputs (no geometric base). Free routing for head is in code only — no UI for it yet; the user iterates by asking for code edits.
- **20 Hz send / 15 Hz monitor tick / 30 fps face tracking**. The send loop is `setInterval`; the tracker loop is `requestAnimationFrame`-driven; live UI is a single `setInterval` shared by chart and rule readouts.
- **No persistence**. No IndexedDB, no localStorage for tuning state, no HF dataset upload. Reset = in-memory deep-copy of `DEFAULT_CONFIG`.

## Demo invariants — do not break

- **Webcams + blendshape chart fit in one viewport "slot"**. The chart sits *flush* under the `.video-pair` with nothing between. Controls (Stop, master toggle, mirror, reset) + tuning panels live below the chart. The user films this layout.
- **Both videos share the same `MediaStream`**. Don't fan out into two `getUserMedia` calls. `localVideo` is raw, `localVideo2` has the mesh canvas overlaid.
- **Stop / tab-close must return to base smoothly**. The 3-command sync-send pattern (`setFullTarget(current)` → `setMotorMode("enabled")` → `gotoTarget(INIT)`) is the hard-won shutdown handshake — don't replace with `await`s in `pagehide`.

## Out of scope (explicit user cuts)

- ❌ Video file upload, YouTube ingest, screen capture
- ❌ Move recording / save / replay / library
- ❌ Multi-preset library (only the default rule set + tuner)
- ❌ Persistence of any kind
- ❌ Touching the sibling Python app `../video_mimicry/` — leave it alone

## Experimental / rough edges

- **`nostril_flare`** (derived feature in `onFrame`) — calibration `(NOSTRIL_REST=0.42, NOSTRIL_FLARE=0.48)` is a guess. Goal is just "see the bar move" in the monitor. Tune the constants if real users report it sitting at 0 or pinning to 1.
- **Pitch sign** held for one user. If a different face/camera angle reads inverted, the math may be camera-frame-dependent rather than universally correct — re-test before generalising.
- **iPhone Safari** has not been verified end-to-end. Camera permissions on iOS Safari are the most fragile path; if a user reports a black `localVideo`, that's the first place to look.
- **Ears**: not detectable. MediaPipe FaceMesh has zero landmarks past the face oval, so independent ear movement is invisible to us. Don't try.

## Pushing changes

The remote is the HF Space (already wired):

```bash
git push   # pushes to https://huggingface.co/spaces/RemiFabre/mime_bot, live in ~10 s
```

The user (`RemiFabre`, `pollen-robotics` org) is logged in via `hf auth whoami`. Push is pre-authorised. Commits go straight to `main`. Style: short imperative subjects, no Co-Authored-By trailer.
