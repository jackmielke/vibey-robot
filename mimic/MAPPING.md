# mime_bot — current mapping & demo ideas

Each robot DOF is computed as `geometric_pose × amp + sum(rules)`, then
clamped to a safe envelope. Multiple rules on the same channel sum
cleanly so any feature can co-drive any DOF.

## Robot DOFs (output channels)

| Channel       | Range          | Notes                              |
|---------------|----------------|------------------------------------|
| head_roll     | ±40°           | geometric + rules                  |
| head_pitch    | ±40°           | geometric + rules                  |
| head_yaw      | ±90°           | geometric + rules                  |
| head_x        | ±0.04 m        | forward(+) / back(-)               |
| head_y        | ±0.04 m        | left(+) / right(-)                 |
| head_z        | ±0.04 m        | up(+) / down(-)                    |
| body_yaw      | ±90°           | rules only                         |
| left_antenna  | ±60° (≈±1.05r) | rules only, idle bias ≈ +0.1745 r  |
| right_antenna | ±60° (≈±1.05r) | rules only, idle bias ≈ −0.1745 r  |

Axes match the Reachy Mini SDK convention (head[0][3]=X forward,
head[1][3]=Y left, head[2][3]=Z up).

## Current default mapping

### Head — direct geometric × amp (rotations only, default amp = 1.5×)

| Face feature                  | Robot channel        |
|-------------------------------|----------------------|
| face roll                     | head_roll            |
| face pitch                    | head_pitch           |
| face yaw (mirror-flipped)     | head_yaw             |

Translations (head_x / head_y / head_z) are **rule-only** — the user's
face position is intentionally not coupled to the robot's position, so
moving toward/away from the camera no longer drags the robot around.

### Head — additive rules

| Channel    | Rule                                                                       |
|------------|----------------------------------------------------------------------------|
| head_roll  | −5° bias (calibrate camera-tilt offset) + jawOpen × +25° (speech wobble — head tilts on each syllable) |
| head_pitch | −14° bias (calibrated from snapshot — cancels the +14° geometric contribution so rest = 0°) + jawOpen × −25° (speech wobble — head nods down on each syllable) |
| head_yaw   | −9° bias (calibrate the small natural face-yaw offset; eye-difference yaw was removed — it kept rotating the head during unrelated demos) |
| head_x     | eye openness SUM (squint = +X / forward, wide = −X / back, ±0.028 each) − 0.03 m bias to cancel resting eyeSquint baseline |
| head_y     | mouthPucker × +0.04 (sideways)                                             |
| head_z     | mouthSmile{Left,Right} × +0.008 (small lift on smile, leaves Stewart-platform travel for other axes) − 0.0016 m bias for resting smile baseline. (mouthShrugLower → −Z is currently commented out — fired in too many false-positive situations) |

### Body + antennas

| Channel       | Rules                                                            |
|---------------|------------------------------------------------------------------|
| body_yaw      | LOCKED to head_yaw (same numeric angle). Rules ignored — see note below. |
| left_antenna  | bias +0.29, browOuterUpLeft × +1.2, browDownLeft × −0.92, brow_asym_left × +0.5, jawOpen × +0.45 |
| right_antenna | bias −0.29, browOuterUpRight × −1.2, browDownRight × +0.92, brow_asym_right × −0.5, jawOpen × −0.45 |

The antenna math is calibrated to three poses, derived from snapshot 1:
- **Rest** (browOuterUp ≈ 0.025, browDown ≈ 0.155): antennas at ±10°, the SDK idle bias.
- **Both brows up** (browOuterUp ≈ 0.6): antennas at ±58°, matches the previous "perfect" feel.
- **Frown** (browDown ≈ 0.5): antennas at ∓10°, slightly crossed inward.

`brow_asym_left` and `brow_asym_right` are derived features:
`max(0, browOuterUp_self − browOuterUp_other)`. They read 0 when both
brows match and rise toward 0.6 when one brow is dominantly raised, so
the moving-side antenna gets a continuous +0.5 × Δ kicker on top of
the linear response — single-brow gestures are visibly stronger than
half a both-brows gesture.

### Note: head_yaw / body_yaw locked together

`body_yaw` is forced equal to `head_yaw` (same numeric angle) inside
`mapToTargets`. Reason: when the Stewart head platform was asked to
yaw alone, the IK leaked yaw into parasitic roll/pitch near its
workspace edge. Driving head and body together keeps the Stewart away
from that edge. Any rules added to `body_yaw` via the UI are currently
ignored — the body_yaw panel exists but has no effect.

## Unused MediaPipe features — demo ideas

The 52 ARKit blendshapes are listed in `main.js` (`FEATURE_GROUPS`).
Below are the unused ones, grouped by the kind of expression they
naturally support, with starter ideas for each.

**Smile / happy** — `mouthSmileLeft`, `mouthSmileRight`, `cheekSquintLeft`, `cheekSquintRight`
Idea: smile → antennas wave outward (additive on top of jawOpen-spread),
plus a body-yaw oscillation if both sides smile equally — "happy sway".

**Sad / concerned** — `mouthFrownLeft`, `mouthFrownRight`, `browInnerUp`, `browDownLeft`, `browDownRight`
Idea: frown + browDown → antennas droop downward (override the +0.1745
idle bias with a negative sum), head pitches down. browInnerUp pinches
both antennas inward (compassion).

**Surprise** — combine the existing `jawOpen` (used) with `browInnerUp` (unused)
Idea: high browInnerUp + jawOpen → antennas spread *more* + head leans
back (negative head_x rule on browInnerUp). Big "oh!" pose.

**Disgust** — `noseSneerLeft`, `noseSneerRight`
Idea: sneer → head pitches up + body yaws away. Optionally: asymmetric
sneer biases the body yaw direction.

**Playful** — `tongueOut`, `cheekPuff`, `mouthDimpleLeft`, `mouthDimpleRight`
Idea: tongueOut → both antennas point forward (silly bunny-look).
cheekPuff → fast body-yaw oscillation (giggle wobble).

**Gaze (eye-only direction)** — `eyeLookInLeft/OutLeft`, `eyeLookInRight/OutRight`, `eyeLookUp{L,R}`, `eyeLookDown{L,R}`
Idea: derived `gaze_h = (eyeLookOutRight - eyeLookInRight) − (eyeLookOutLeft - eyeLookInLeft)` and `gaze_v` similar → bias head_yaw and head_pitch. The robot follows your *eyes* even when your head doesn't move — very alive.

**Jaw motion** — `jawForward`, `jawLeft`, `jawRight`
Idea: jawForward → strong additive head_x (lean in further).
jawLeft / jawRight → small body_yaw bias (cocky asymmetric pose).

**Lip dynamics** — `mouthFunnel`, `mouthClose`, `mouthLeft`, `mouthRight`, `mouthRollUpper/Lower`, `mouthShrugUpper/Lower`, `mouthStretchLeft/Right`, `mouthPressLeft/Right`, `mouthUpperUp{L,R}`, `mouthLowerDown{L,R}`
Idea: a long tail of subtle lip shapes. Most useful as **derived "mood"
features** — e.g. `tense = mouthPress + mouthRoll`, `pursed = mouthFunnel + mouthClose` — then route those to antennas or body for fine expressivity.

## Quick combos worth trying

- **"Yes" nod loop**: jawOpen high + head pitch sweep → emphatic nodding.
- **"No" shake**: large eye-yaw differential while talking.
- **Curious tilt**: asymmetric mouthSmile → small head_roll bias (cute head-tilt cocking).
- **Stink-eye**: noseSneer + browDown on one side → head pitch up + slight body yaw.
- **Awkward shrug**: mouthShrugUpper + mouthShrugLower → antennas pop up + body yaw oscillate.
