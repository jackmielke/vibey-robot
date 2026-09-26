// mime_bot — live face mimicry for Reachy Mini.
//
// Build status:
//   ✓ HF auth + connect + robot picker + startSession lifecycle.
//   ✗ Webcam, FaceLandmarker, mapping, tuner — coming next.

import { ReachyMini, rpyToMatrix } from "./reachy-local.js";

// MediaPipe Tasks-Vision is a multi-megabyte ESM bundle. Importing it
// at the top of the module would block DOMContentLoaded (and therefore
// bootstrap → authenticate → connect) until the whole bundle plus its
// transitive WASM downloads resolve — a 5-30 s hang where the page just
// sits at "Checking sign-in…". We import it lazily inside
// initFaceLandmarker() instead, so auth and the robot picker run
// immediately and the model only loads once the user has actually
// started a session.
let FaceLandmarker = null;
let FilesetResolver = null;
let DrawingUtils = null;

// We don't pipe the user's mic into the robot — the demo is silent on the
// robot side, audio (if any) plays from the user's own device.
const robot = new ReachyMini({
    appName: "mime_bot",
    enableMicrophone: false,
});

let selectedRobotId = null;
let cameraStream = null;
let faceLandmarker = null;
let trackingActive = false;
let lastTrackingLog = 0;
let lastFeatures = null;       // most recent feature snapshot from onFrame
let lastFaceMatrix = null;     // most recent raw 4x4 face matrix (flat[16] row-major) — kept for the debug-snapshot button
let sendIntervalId = null;     // setInterval handle for the 20 Hz robot loop
let baselineZ = null;          // head-Z reference set on first detected frame
const SEND_HZ = 20;

// ─── Pose constants & smooth-return helpers ────────────────
// All ported from marionette_js (which itself mirrors the Python SDK).
// One primitive — softReturnToBase — is the universal "go home softly"
// move: pin goal=current → enable torque (no jerk) → daemon-side
// interpolation to base over a distance-scaled duration.
const INIT_HEAD_POSE = [
    [1, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
];
// [right, left] in radians; ~10° outward tilt damps mechanical resonance
// at exact-vertical (matches reachy_mini.py:INIT_ANTENNAS_JOINT_POSITIONS).
const INIT_ANTENNAS = [-0.1745, 0.1745];

const MIN_RETURN_S = 0.2;
const MAX_RETURN_S = 1.5;
const SECS_PER_MAGIC_MM = 0.02;  // mirrors Python `_scaled_duration`

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function deltaAngleBetweenRot(P, Q) {
    const trace = (
        P[0][0] * Q[0][0] + P[0][1] * Q[0][1] + P[0][2] * Q[0][2] +
        P[1][0] * Q[1][0] + P[1][1] * Q[1][1] + P[1][2] * Q[1][2] +
        P[2][0] * Q[2][0] + P[2][1] * Q[2][1] + P[2][2] * Q[2][2]
    );
    const cos = clamp((trace - 1) / 2, -1, 1);
    return Math.acos(cos);
}

function distanceBetweenPoses(p1, p2) {
    const dx = p1[0][3] - p2[0][3];
    const dy = p1[1][3] - p2[1][3];
    const dz = p1[2][3] - p2[2][3];
    const transM = Math.hypot(dx, dy, dz);
    const angleRad = deltaAngleBetweenRot(
        [p1[0].slice(0, 3), p1[1].slice(0, 3), p1[2].slice(0, 3)],
        [p2[0].slice(0, 3), p2[1].slice(0, 3), p2[2].slice(0, 3)],
    );
    return transM * 1000 + angleRad * 180 / Math.PI;  // magic-mm
}

function scaledDuration(currentHead, targetHead) {
    if (!currentHead || !targetHead) return MAX_RETURN_S;
    const d = distanceBetweenPoses(currentHead, targetHead) * SECS_PER_MAGIC_MM;
    return Math.min(Math.max(d, MIN_RETURN_S), MAX_RETURN_S);
}

const Motor = {
    // Wait one round-trip for an up-to-date state event so we don't pin
    // to a stale cached value (causes a snap when torque comes back on).
    _waitFreshState({ timeoutMs = 300 } = {}) {
        return new Promise((resolve) => {
            let done = false;
            const onState = () => {
                if (done) return;
                done = true;
                robot.removeEventListener("state", onState);
                clearTimeout(t);
                resolve();
            };
            robot.addEventListener("state", onState);
            robot.requestState();
            const t = setTimeout(onState, timeoutMs);
        });
    },

    // Pin goal=current → enable torque (no jerk) → daemon-side smooth
    // interp to INIT_HEAD_POSE over scaledDuration. Fire-and-forget;
    // the daemon keeps interpolating on its own clock even if we stop
    // sending. Returns the duration so callers can pace follow-ups.
    async softReturnToBase({ freshen = false } = {}) {
        if (freshen) await this._waitFreshState();
        const rs = robot.robotState;
        if (rs?.headMatrix) {
            robot.setFullTarget({
                head: rs.headMatrix,
                antennas: rs.antennasRad,
                bodyYaw: rs.bodyYaw ?? 0,
            });
        }
        robot.setMotorMode("enabled");
        const duration = scaledDuration(rs?.headMatrix, INIT_HEAD_POSE);
        robot.gotoTarget({
            head: INIT_HEAD_POSE,
            antennas: INIT_ANTENNAS,
            bodyYaw: 0,
            duration,
        });
        return duration;
    },

    // App startup: wait briefly for the first state, then smooth goto base.
    // Whatever pose the robot is in is preserved, torque is safe-enabled,
    // then it interpolates to base. No wakeUp animation (we don't want
    // the SDK's built-in wake sound + 2 s wiggle here).
    async startup() {
        robot.requestState();
        const t0 = performance.now();
        while (!robot.robotState?.headMatrix && performance.now() - t0 < 1000) {
            await sleep(50);
        }
        return this.softReturnToBase();
    },
};

// ─── Robot DOF safety envelope ─────────────────────────────
// AXES (per Reachy Mini SDK notebook): in the head matrix's translation
// column, head[0][3] = X = forward(+)/back(-), head[1][3] = Y = left(+)/
// right(-), head[2][3] = Z = up(+)/down(-). We had this wrong before:
// the old code routed "forward/back" to head[2][3] (= up/down), so any
// rule emitting a positive value would lift the head off baseline and
// burn through the vertical envelope.
//
// Limits per the SDK / AGENTS.md:
//   - Head pitch/roll: ±40°  - Head yaw: ±180°  - Body yaw: ±160°
//   - Translation: SDK demos use ±2 cm comfortably; widening to ±4 cm
//     here for big expressive motion in simulation. Daemon clamps to
//     whatever the actual workspace is, so this is safe.
const HEAD_ROLL_MAX_DEG = 40.0;
const HEAD_PITCH_MAX_DEG = 40.0;
const HEAD_YAW_MAX_DEG = 90.0;       // face-following rarely exceeds 90°
const HEAD_X_MIN = -0.04;
const HEAD_X_MAX = 0.04;
const HEAD_Y_MIN = -0.04;
const HEAD_Y_MAX = 0.04;
const HEAD_Z_MIN = -0.04;
const HEAD_Z_MAX = 0.04;
const BODY_YAW_MAX_RAD = (90.0 * Math.PI) / 180.0;
// Antennas: SDK accepts a wide range; we cap at ±60° (~±1.05 rad) as a
// generous safe envelope around the ±10° idle bias.
const ANTENNA_MAX_RAD = 1.05;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// ─── Mapping config (in-memory, no persistence per spec) ───
//
// Three layers, all additive at the channel level:
//   1. headAmp: head_roll/pitch/yaw/z geometric pose × amplitude. Direct
//      mimicry of the user's head — usually the dominant signal.
//   2. headRules: per-head-channel free-routing. Sum of {feature, weight}
//      rules ADDS on top of the geometric value (then clamped to safe).
//      Lets blendshapes drive head DOFs alongside the geometric pose —
//      e.g. eye-squint sum → +z forward lean, eye openness diff → yaw.
//   3. rules: free-routing for body_yaw + left/right antennas (no
//      geometric counterpart — these are pure rule outputs).
//
// "_bias" is a constant 1.0 feature for setting idle / rest offsets
// without occupying a real input.
//
// BASE_CONFIG keeps the original (pre-iteration) defaults for reference;
// nothing loads it automatically. DEFAULT_CONFIG is what the app starts
// with and what "Reset tuner" restores.
//
// headAmp keys map to the same robot frame the SDK uses:
//   x = forward/back (head[0][3]), y = left/right, z = up/down,
//   plus the rotation amplitudes roll/pitch/yaw.
// Only `x` has a default geometric input wired up (face depth → forward
// lean); y/z geometric amps are present so the user can wire face_x and
// face_y to robot Y/Z later if they want.
const BASE_CONFIG = {
    headAmp: { roll: 1.0, pitch: 1.0, yaw: 1.0 },
    headRules: {
        head_roll: [], head_pitch: [], head_yaw: [],
        head_x: [], head_y: [], head_z: [],
    },
    rules: {
        body_yaw: [
            { feature: "head_yaw", weight: 0.6 },
            { feature: "nostril_flare", weight: 0.2 },
        ],
        left_antenna: [
            { feature: "_bias", weight: 0.1745 },
            { feature: "browOuterUpLeft", weight: 0.6 },
            { feature: "jawOpen", weight: 0.3 },
        ],
        right_antenna: [
            { feature: "_bias", weight: -0.1745 },
            { feature: "browOuterUpRight", weight: -0.6 },
            { feature: "jawOpen", weight: -0.3 },
        ],
    },
};

// Iteration from base, with the X/Z axis confusion fixed:
//   - All head amplitudes × 1.5, rule weights × 1.5 (bias values stay).
//   - Eye-squint SUM → +x (lean forward), eye-wide SUM → -x (lean back).
//     Each eye contributes independently so partial squint scales.
//   - Eye openness DIFFERENCE → yaw (differential-drive style): one eye
//     squint + other wide turns the head; both same cancels.
//   - mouthPucker → +y sideways translation.
//   - Translation rules now reach the full ±0.04 m clamp at one full
//     extreme feature (was ±0.025 m, was hitting baseline noise).
const DEFAULT_CONFIG = {
    headAmp: { roll: 1.5, pitch: 1.5, yaw: 1.5 },
    headRules: {
        // _bias shifts roll to compensate the user's setup (camera not
        // perfectly level). Calibrated from a "looking straight at the
        // camera" snapshot so the rest pose lands at 0°.
        // jawOpen × +30° = speech-wobble roll component (combined with
        // the pitch nod below, mouth-open spikes produce a two-axis
        // bob). Bumped from ±20 → ±30 for more lively talking.
        head_roll: [
            { feature: "_bias",   weight: -5.0 },
            { feature: "jawOpen", weight: 25.0 },
        ],
        // _bias of −14° calibrated from a "looking straight at the
        // camera" snapshot — at rest the geometric pitch contribution
        // is +14°, so this lands the rest pose at exactly 0°.
        // jawOpen × −30° = speech-wobble pitch component.
        head_pitch: [
            { feature: "_bias",   weight: -14.0 },
            { feature: "jawOpen", weight: -25.0 },
        ],
        // _bias offsets a small natural face-yaw the user holds at rest
        // (camera off-axis). The eye-difference yaw rules that used to
        // live here were removed: they made the head rotate whenever
        // the eyes squinted asymmetrically, which fired during
        // unrelated demos (e.g. raising one eyebrow), and overall the
        // coupling was confusing.
        head_yaw: [
            { feature: "_bias", weight: -9.0 },
        ],
        // Eye-sum forward/back (X = forward axis on the robot).
        // Squint pushes forward (suspicious lean), wide pulls back.
        // Each eye contributes 0.028 m/feature; _bias of −0.03 cancels
        // the resting eyeSquint baseline (~0.5 each from snapshots) so
        // the head sits near 0 at rest.
        head_x: [
            { feature: "_bias",          weight: -0.03 },
            { feature: "eyeSquintLeft",  weight:  0.028 },
            { feature: "eyeSquintRight", weight:  0.028 },
            { feature: "eyeWideLeft",    weight: -0.028 },
            { feature: "eyeWideRight",   weight: -0.028 },
        ],
        // mouthPucker → +y sideways. ±0.04 m clamp; full pucker hits it.
        head_y: [
            { feature: "mouthPucker", weight: 0.04 },
        ],
        // Smile lifts the head (Z = up). Weights kept small because a
        // full grin saturating +Z eats most of the Stewart platform's
        // travel and kills expressivity on the other axes — full grin
        // (sum ≈ 2.0) now lifts the head ~0.014 m, well inside the
        // ±0.04 m envelope. _bias cancels the resting smile baseline
        // (~0.10 each).
        head_z: [
            { feature: "_bias",            weight: -0.0016 },
            { feature: "mouthSmileLeft",   weight:  0.008 },
            { feature: "mouthSmileRight",  weight:  0.008 },
            // Temporarily disabled — mouthShrugLower fired in too many
            // unintended situations (e.g. when looking up). Re-enable
            // by un-commenting if a cleaner trigger lands.
            // { feature: "mouthShrugLower",  weight: -0.05 },
        ],
    },
    rules: {
        // body_yaw is intentionally rule-less — it's locked to mirror
        // head_yaw (the same numeric angle) inside mapToTargets, per
        // user request to avoid the parasitic roll/pitch coupling
        // observed when the Stewart head platform handled yaw alone.
        body_yaw: [],
        // Left antenna mapping derived from snapshot 1 baselines so:
        //   - rest (browOuterUp ≈ 0.025, browDown ≈ 0.155) → +0.1745 rad (+10°)
        //   - both brows up (browOuterUp = 0.6) → +1.014 rad (+58°, current "perfect" pose)
        //   - frown (browDown = 0.5) → −0.175 rad (−10°, antennas slightly crossed inward)
        // The brow_asym_left rule kicks in only when this side's brow
        // is dominantly raised — gives the moving antenna a +0.5 × Δ
        // boost on top of the linear response so single-brow gestures
        // are visibly stronger than half a both-brows gesture.
        left_antenna: [
            { feature: "_bias",            weight:  0.29 },
            { feature: "browOuterUpLeft",  weight:  1.2 },
            { feature: "browDownLeft",     weight: -0.92 },
            { feature: "brow_asym_left",   weight:  0.5 },
            { feature: "jawOpen",          weight:  0.45 },
        ],
        right_antenna: [
            { feature: "_bias",            weight: -0.29 },
            { feature: "browOuterUpRight", weight: -1.2 },
            { feature: "browDownRight",    weight:  0.92 },
            { feature: "brow_asym_right",  weight: -0.5 },
            { feature: "jawOpen",          weight: -0.45 },
        ],
    },
};

const config = JSON.parse(JSON.stringify(DEFAULT_CONFIG));

// ─── Feature catalog (for tuner dropdowns) ─────────────────
// Order within each group is the order shown in the dropdown.
const FEATURE_GROUPS = [
    { label: "Special", features: ["_bias"] },
    { label: "Head pose (raw)", features: [
        "head_roll", "head_pitch", "head_yaw",
        "head_x", "head_y", "head_z",
    ]},
    { label: "Brows", features: [
        "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
        "browDownLeft", "browDownRight",
    ]},
    { label: "Eyes", features: [
        "eyeBlinkLeft", "eyeBlinkRight",
        "eyeWideLeft", "eyeWideRight",
        "eyeSquintLeft", "eyeSquintRight",
        "eyeLookInLeft", "eyeLookOutLeft",
        "eyeLookInRight", "eyeLookOutRight",
        "eyeLookUpLeft", "eyeLookDownLeft",
        "eyeLookUpRight", "eyeLookDownRight",
    ]},
    { label: "Mouth", features: [
        "jawOpen", "jawForward", "jawLeft", "jawRight",
        "mouthClose", "mouthFunnel", "mouthPucker",
        "mouthLeft", "mouthRight",
        "mouthSmileLeft", "mouthSmileRight",
        "mouthFrownLeft", "mouthFrownRight",
        "mouthDimpleLeft", "mouthDimpleRight",
        "mouthStretchLeft", "mouthStretchRight",
        "mouthPressLeft", "mouthPressRight",
        "mouthRollUpper", "mouthRollLower",
        "mouthShrugUpper", "mouthShrugLower",
        "mouthUpperUpLeft", "mouthUpperUpRight",
        "mouthLowerDownLeft", "mouthLowerDownRight",
    ]},
    { label: "Cheeks / nose / tongue", features: [
        "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
        "noseSneerLeft", "noseSneerRight",
        "tongueOut",
    ]},
    // Derived = computed from raw 478-point landmarks rather than from
    // the ARKit blendshape vector. Experimental — calibration is rough
    // and may need tuning per face. The blendshape monitor includes
    // these too so users can see if they track their movement at all.
    { label: "Derived (experimental)", features: [
        "nostril_flare",
        // brow asymmetry: max(0, browOuterUp{Side} - browOuterUp{OtherSide}).
        // Reads ~0 when both brows match, climbs to ~0.6 when only one
        // brow is fully raised. Used to give the moving-side antenna an
        // extra kicker on single-brow gestures.
        "brow_asym_left",
        "brow_asym_right",
    ]},
];
const ALL_FEATURES = FEATURE_GROUPS.flatMap((g) => g.features);

function isMaster() { return $("toggleMaster")?.checked ?? false; }
function isMirror() { return $("toggleMirror")?.checked ?? true; }

// ─── DOM helpers ───────────────────────────────────────────
const $ = (id) => document.getElementById(id);

// ─── Login screen states ───────────────────────────────────
//
// The same #loginView is reused for three things:
//   - "Checking sign-in…"  on page load while authenticate() resolves.
//   - "Sign in with HF"    if authenticate() returned false.
//   - "Couldn't connect"   if connect() threw after auth (rare).
//
// Hiding the OAuth button when not needed prevents a double-click during
// the brief "checking" window from triggering a redundant OAuth redirect.
function setLoginMessage(msg, { showButton = false } = {}) {
    $("loginMessage").textContent = msg;
    $("btnLogin").classList.toggle("hidden", !showButton);
}

function showLogin() {
    $("loginView").classList.remove("hidden");
    $("mainApp").classList.add("hidden");
}
function showMain() {
    $("loginView").classList.add("hidden");
    $("mainApp").classList.remove("hidden");
    $("username").textContent = "@" + (robot.username || "user");
}

// View toggling between picker and mimicry inside the main app.
function showPicker() {
    $("robotSelector").classList.remove("hidden");
    $("mimicryView").classList.add("hidden");
}
function showMimicry() {
    $("robotSelector").classList.add("hidden");
    $("mimicryView").classList.remove("hidden");
}

function setPickerHeader(text) {
    const el = $("pickerHeader");
    if (el) el.textContent = text;
}

// ─── Webcam ────────────────────────────────────────────────
// First call: no deviceId — we ask for `facingMode: "user"` to get
// the selfie cam on phones and fall back to whatever the browser
// chooses on laptops. Once permission resolves we can `enumerateDevices`
// for the camera-picker dropdown; on subsequent calls (camera switch)
// we pass an explicit deviceId.
// ── handsfree MJPEG fallback ────────────────────────────────
//
// MediaPipe's detectForVideo takes any ImageSource, so an <img> fed by an
// MJPEG multipart stream works — but only via a canvas: the <img> has no
// currentTime and the tracker's frame bookkeeping wants something it can
// sample. captureStream() on that canvas then yields a MediaStream, so
// everything downstream (the two <video> elements, the mesh overlay) stays
// exactly as it was.
const HANDSFREE_URL = "/mimic/handsfree.mjpg";   // same-origin proxy; see reachy_viewer.py

async function startHandsfreeCamera() {
    const img = new Image();
    const ok = await new Promise((resolve) => {
        const t = setTimeout(() => resolve(false), 4000);
        img.onload = () => { clearTimeout(t); resolve(true); };
        img.onerror = () => { clearTimeout(t); resolve(false); };
        img.src = HANDSFREE_URL;
    });
    if (!ok || !img.naturalWidth) return null;

    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    const pump = () => {
        if (img.naturalWidth) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        requestAnimationFrame(pump);
    };
    pump();

    cameraStream = canvas.captureStream(30);
    for (const id of ["localVideo", "localVideo2"]) {
        const v = $(id);
        if (!v) continue;
        v.srcObject = cameraStream;
        await v.play().catch(() => {});
    }
    console.log("[camera] using handsfree MJPEG at " + HANDSFREE_URL);
    return cameraStream;
}

async function startCamera(deviceId = null) {
    if (cameraStream) return cameraStream;
    const video = deviceId
        ? { deviceId: { exact: deviceId }, width: { ideal: 640 }, height: { ideal: 480 } }
        : { facingMode: "user",            width: { ideal: 640 }, height: { ideal: 480 } };
    try {
        cameraStream = await navigator.mediaDevices.getUserMedia({ video, audio: false });
    } catch (e) {
        // A camera can be permitted and still unavailable, because something
        // else on the machine already has it — here, the handsfree project's
        // browser_viewer.py holds the MacBook camera from login onward. The
        // browser does not re-prompt in that case (permission was already
        // granted), so this reads as "the page just does nothing".
        //
        // handsfree already publishes what it sees as MJPEG, so instead of the
        // two fighting over the device, one owns it and the other reads its
        // stream. Both can run at once, which is the point: keep controlling
        // the computer by head and play with the robot at the same time.
        console.warn("getUserMedia failed, trying handsfree MJPEG:", e);
        const viaHandsfree = await startHandsfreeCamera();
        if (viaHandsfree) return viaHandsfree;
        console.error("no camera available:", e);
        alert("No camera. Something else may be using it — check handsfree "
              + "(localhost:8765) or grant camera access.");
        return null;
    }
    // Same MediaStream feeds both <video> elements — left = raw,
    // right = the same stream with a face-mesh canvas overlaid on top.
    for (const id of ["localVideo", "localVideo2"]) {
        const v = $(id);
        if (!v) continue;
        v.srcObject = cameraStream;
        await v.play().catch(() => {});
    }
    populateCameraSelect();
    return cameraStream;
}

// Populate the camera-picker dropdown. Browsers hide device LABELS
// until camera permission is granted, so this is only meaningful
// after a successful getUserMedia. If there's only one camera, hide
// the picker — no point showing a one-option dropdown.
async function populateCameraSelect() {
    const select = $("cameraSelect");
    const wrap = $("cameraSelectWrap");
    if (!select || !wrap) return;
    let devices;
    try {
        devices = await navigator.mediaDevices.enumerateDevices();
    } catch (e) {
        console.warn("enumerateDevices failed:", e);
        return;
    }
    const cams = devices.filter((d) => d.kind === "videoinput");
    if (cams.length <= 1) {
        wrap.classList.add("hidden");
        return;
    }
    wrap.classList.remove("hidden");

    const currentId = cameraStream?.getVideoTracks?.()[0]?.getSettings?.().deviceId;
    select.innerHTML = "";
    for (const d of cams) {
        const opt = document.createElement("option");
        opt.value = d.deviceId;
        opt.textContent = d.label || `Camera ${d.deviceId.slice(0, 8)}`;
        if (d.deviceId === currentId) opt.selected = true;
        select.appendChild(opt);
    }
}

// Swap the active camera mid-session. The tracking loop and 20 Hz
// send loop don't care — they read from the same <video> element,
// which gets its srcObject reattached transparently.
async function switchCamera(deviceId) {
    if (!deviceId) return;
    stopCamera();
    await startCamera(deviceId);
}

function stopCamera() {
    if (cameraStream) {
        for (const t of cameraStream.getTracks()) t.stop();
        cameraStream = null;
    }
    for (const id of ["localVideo", "localVideo2"]) {
        const v = $(id);
        if (v) v.srcObject = null;
    }
    // Wipe the mesh canvas so a frozen frame doesn't linger.
    const canvas = $("overlayCanvas");
    if (canvas) {
        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
}

// ─── FaceLandmarker ────────────────────────────────────────
// MediaPipe Tasks-Vision FaceLandmarker. Returns 52 ARKit-style
// blendshapes + a 4x4 facialTransformationMatrix (head pose) per
// frame. Runs on a WASM/WebGL backend on-device — no upload.
async function initFaceLandmarker() {
    if (faceLandmarker) return faceLandmarker;
    if (!FaceLandmarker) {
        const mod = await import("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/+esm");
        FaceLandmarker = mod.FaceLandmarker;
        FilesetResolver = mod.FilesetResolver;
        DrawingUtils = mod.DrawingUtils;
    }
    const fileset = await FilesetResolver.forVisionTasks(
        "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35/wasm",
    );
    faceLandmarker = await FaceLandmarker.createFromOptions(fileset, {
        baseOptions: {
            modelAssetPath:
                "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
            delegate: "GPU",
        },
        runningMode: "VIDEO",
        numFaces: 1,
        outputFaceBlendshapes: true,
        outputFacialTransformationMatrixes: true,
    });
    return faceLandmarker;
}

function startTracking() {
    if (trackingActive) return;
    trackingActive = true;
    const video = $("localVideo");
    let lastTimestamp = -1;

    const tick = () => {
        if (!trackingActive) return;
        if (video.readyState >= 2 && video.currentTime !== lastTimestamp && faceLandmarker) {
            lastTimestamp = video.currentTime;
            // detectForVideo wants a monotonic timestamp in ms.
            const result = faceLandmarker.detectForVideo(video, performance.now());
            onFrame(result);
        }
        requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
}

function stopTracking() {
    trackingActive = false;
}

// Per-frame callback: build a feature dict from the FaceLandmarker
// result and stash it for the 20 Hz send loop to consume.
//
// Features available to mapping rules:
//   - head_roll, head_pitch, head_yaw : radians (from transformation matrix)
//   - head_x, head_y, head_z          : metres-ish, baseline-subtracted
//   - <52 ARKit blendshape names>     : 0..1 each
//   - _bias                           : constant 1.0 (for idle offsets)
function onFrame(result) {
    // Draw the mesh overlay on the right-hand video regardless of whether
    // we actually built features this frame — a clean canvas is the
    // right "no face" state.
    drawMesh(result.faceLandmarks?.[0]);

    const hasFace = result.faceBlendshapes?.length > 0
        && result.facialTransformationMatrixes?.length > 0;

    if (!hasFace) {
        lastFeatures = null;
        lastFaceMatrix = null;
        if (performance.now() - lastTrackingLog > 1000) {
            lastTrackingLog = performance.now();
            console.log("[face] no face");
        }
        return;
    }

    const features = { _bias: 1.0 };

    // Blendshapes — 52 named scores in [0, 1].
    for (const c of result.faceBlendshapes[0].categories) {
        features[c.categoryName] = c.score;
    }

    // Head pose from facialTransformationMatrix (column-major 4x4 in
    // some MediaPipe builds; for tasks-vision it's row-major flat[16]).
    // Decompose the upper-left 3x3 into intrinsic XYZ (roll/pitch/yaw).
    const m = result.facialTransformationMatrixes[0].data;
    lastFaceMatrix = m;
    const rpy = matrixToRollPitchYaw(m);
    features.head_roll = rpy.roll;
    features.head_pitch = rpy.pitch;
    features.head_yaw = rpy.yaw;

    // Translation — units are roughly cm in the canonical face model.
    // We baseline on first detection; downstream mapping scales.
    const tx = m[3], ty = m[7], tz = m[11];
    if (baselineZ === null) baselineZ = tz;
    features.head_x = tx * 0.01;            // cm → m
    features.head_y = ty * 0.01;
    features.head_z = (tz - baselineZ) * 0.01;

    // Derived: nostril flare (experimental).
    // Lateral distance between the alae (the widest points of the
    // nostrils), normalised by inter-outer-eye-corner distance to
    // cancel out face size and head depth. MediaPipe FaceMesh indices:
    //   64  = right alar lateral
    //   294 = left alar lateral
    //   33  = right eye outer corner
    //   263 = left eye outer corner
    // Calibration is a guess — at neutral the ratio sits around 0.42,
    // a strong flare bumps it to ~0.48. The user can tune by adjusting
    // the rule weight in the tuner; the blendshape monitor will show
    // whether the raw signal moves at all.
    const lms = result.faceLandmarks?.[0];
    if (lms && lms.length > 294) {
        const nostrilW = Math.abs(lms[64].x - lms[294].x);
        const eyeW = Math.abs(lms[33].x - lms[263].x);
        if (eyeW > 0.01) {
            const ratio = nostrilW / eyeW;
            const NOSTRIL_REST = 0.42;
            const NOSTRIL_FLARE = 0.48;
            features.nostril_flare = clamp(
                (ratio - NOSTRIL_REST) / (NOSTRIL_FLARE - NOSTRIL_REST),
                0, 1,
            );
        } else {
            features.nostril_flare = 0;
        }
    } else {
        features.nostril_flare = 0;
    }

    // Brow asymmetry. max() introduces a kink at delta = 0 but the
    // brow signals are noisy enough below ~0.05 that the kink is
    // imperceptible in practice — and it lets us route the boost only
    // to the moving-side antenna with no conditional in the rule.
    const bL = features.browOuterUpLeft  ?? 0;
    const bR = features.browOuterUpRight ?? 0;
    features.brow_asym_left  = Math.max(0, bL - bR);
    features.brow_asym_right = Math.max(0, bR - bL);

    lastFeatures = features;

    // Periodic console summary so it's easy to confirm tracking works
    // without console-spam at 30 fps.
    const now = performance.now();
    if (now - lastTrackingLog > 1000) {
        lastTrackingLog = now;
        const top = result.faceBlendshapes[0].categories
            .filter((c) => c.score > 0.15)
            .sort((a, b) => b.score - a.score)
            .slice(0, 4)
            .map((c) => `${c.categoryName}=${c.score.toFixed(2)}`)
            .join(" ");
        console.log(
            `[face] yaw=${(rpy.yaw * 180 / Math.PI).toFixed(0)}° pitch=${(rpy.pitch * 180 / Math.PI).toFixed(0)}° roll=${(rpy.roll * 180 / Math.PI).toFixed(0)}° z=${features.head_z.toFixed(3)}m | ${top}`,
        );
    }
}

// Draw the face mesh + emphasised features onto the overlay canvas.
// Resizes the canvas's internal bitmap to match the source video so
// landmarks (which are normalised 0..1) project correctly.
function drawMesh(landmarks) {
    const canvas = $("overlayCanvas");
    const video = $("localVideo2");
    if (!canvas || !video) return;
    const ctx = canvas.getContext("2d");

    const w = video.videoWidth || 640;
    const h = video.videoHeight || 480;
    if (canvas.width !== w) canvas.width = w;
    if (canvas.height !== h) canvas.height = h;

    ctx.clearRect(0, 0, w, h);
    if (!landmarks || !DrawingUtils) return;

    const du = new DrawingUtils(ctx);
    // Tessellation in low-opacity white = the gauzy "mesh" look.
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_TESSELATION, {
        color: "#FFFFFF22", lineWidth: 0.6,
    });
    // Highlights for the features the mapping cares about.
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_RIGHT_EYE,    { color: "#FF6B35", lineWidth: 1.2 });
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_LEFT_EYE,     { color: "#FF6B35", lineWidth: 1.2 });
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_RIGHT_EYEBROW,{ color: "#FF8A5C", lineWidth: 1.4 });
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_LEFT_EYEBROW, { color: "#FF8A5C", lineWidth: 1.4 });
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_LIPS,         { color: "#FF6B35", lineWidth: 1.2 });
    du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_FACE_OVAL,    { color: "#FFFFFF66", lineWidth: 1.0 });
    if (FaceLandmarker.FACE_LANDMARKS_RIGHT_IRIS) {
        du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_RIGHT_IRIS, { color: "#48BB78", lineWidth: 1.4 });
        du.drawConnectors(landmarks, FaceLandmarker.FACE_LANDMARKS_LEFT_IRIS,  { color: "#48BB78", lineWidth: 1.4 });
    }
}

// Decompose a flat 4x4 rotation matrix (row-major as MediaPipe's
// tasks-vision returns it) into intrinsic XYZ Euler (roll, pitch, yaw)
// in radians, matching the convention used by reachy_mini's
// rpyToMatrix(roll, pitch, yaw).
function matrixToRollPitchYaw(m) {
    // m is flat[16] row-major: rows R0=m[0..3], R1=m[4..7], R2=m[8..11].
    // Upper-left 3x3:
    //   m00 m01 m02
    //   m10 m11 m12
    //   m20 m21 m22
    const m00 = m[0], m01 = m[1], m02 = m[2];
    const m10 = m[4], m11 = m[5], m12 = m[6];
    const m20 = m[8], m21 = m[9], m22 = m[10];

    // Intrinsic XYZ (R = Rx · Ry · Rz):
    //   pitch = atan2(-m12, m22)   (rotation about X, "nod")
    //   yaw   = asin( m02)         (rotation about Y, "turn")
    //   roll  = atan2(-m01, m00)   (rotation about Z, "tilt")
    // Confirmed empirically:
    //   - User testing: yaw and roll feel correct as-is.
    //   - Pitch was inverted (looking down made the robot look up), so
    //     we flip its sign — reachy_mini's rpyToMatrix interprets
    //     +pitch as "tilt forward / chin down" in this build.
    const pitch = -Math.atan2(-m12, m22);
    const yaw = -Math.asin(clamp(m02, -1, 1));
    const roll = -Math.atan2(-m01, m00);
    return { roll, pitch, yaw };
}

// ─── Mapping: features → robot DOF targets ─────────────────
// Returns null if there's nothing to send (no face detected, or master
// off). The caller is responsible for zeroing the robot back to
// neutral when master is off.
function mapToTargets(features) {
    if (!features) return null;

    // Apply mirror by flipping yaw of head, which transitively affects
    // any rule that uses head_yaw.
    const mirror = isMirror() ? -1 : 1;
    const f = { ...features, head_yaw: features.head_yaw * mirror };

    // Head — rotations are geometric pose × amplitude PLUS rule
    // contributions; translations are rule-only. Translations were
    // previously driven by the face's frame-Z (depth from camera) but
    // that meant moving toward/away from the camera shoved the robot
    // around — disruptive and pointless. Rotations only for geometric;
    // X/Y/Z translation is a free expressive axis driven by rules.
    const hr = config.headRules;
    const headRollDeg = clamp(
        (f.head_roll * 180 / Math.PI) * config.headAmp.roll + sumRules(hr.head_roll, f),
        -HEAD_ROLL_MAX_DEG, HEAD_ROLL_MAX_DEG);
    const headPitchDeg = clamp(
        (f.head_pitch * 180 / Math.PI) * config.headAmp.pitch + sumRules(hr.head_pitch, f),
        -HEAD_PITCH_MAX_DEG, HEAD_PITCH_MAX_DEG);
    const headYawDeg = clamp(
        (f.head_yaw * 180 / Math.PI) * config.headAmp.yaw + sumRules(hr.head_yaw, f),
        -HEAD_YAW_MAX_DEG, HEAD_YAW_MAX_DEG);
    const headX = clamp(sumRules(hr.head_x, f), HEAD_X_MIN, HEAD_X_MAX);
    const headY = clamp(sumRules(hr.head_y, f), HEAD_Y_MIN, HEAD_Y_MAX);
    const headZ = clamp(sumRules(hr.head_z, f), HEAD_Z_MIN, HEAD_Z_MAX);

    // body_yaw is locked to head_yaw (same numeric angle, in radians).
    // Mechanical reason: when the Stewart head platform was asked to
    // yaw alone it leaked into roll/pitch as the IK approached its
    // workspace edge. Driving head and body together keeps the Stewart
    // away from that limit. config.rules.body_yaw is intentionally
    // ignored here.
    const bodyYaw = clamp(headYawDeg * Math.PI / 180,
        -BODY_YAW_MAX_RAD, BODY_YAW_MAX_RAD);
    const leftAnt = clamp(sumRules(config.rules.left_antenna, f),
        -ANTENNA_MAX_RAD, ANTENNA_MAX_RAD);
    const rightAnt = clamp(sumRules(config.rules.right_antenna, f),
        -ANTENNA_MAX_RAD, ANTENNA_MAX_RAD);

    return { headRollDeg, headPitchDeg, headYawDeg,
             headX, headY, headZ,
             bodyYaw, leftAnt, rightAnt };
}

function sumRules(rules, features) {
    let sum = 0;
    for (const r of rules) {
        const v = features[r.feature];
        if (v != null) sum += r.weight * v;
    }
    return sum;
}

// ─── Tuner UI rendering ────────────────────────────────────

function renderHeadAmpPanel() {
    const panel = $("headAmpPanel");
    if (!panel) return;
    const rows = [
        ["roll",  "Roll amp"],
        ["pitch", "Pitch amp"],
        ["yaw",   "Yaw amp"],
    ];
    panel.innerHTML = "";
    for (const [key, label] of rows) {
        const row = document.createElement("div");
        row.className = "slider-row";
        row.innerHTML = `
            <span class="slider-label">${label}</span>
            <input type="range" class="slider" min="0" max="3" step="0.05"
                   value="${config.headAmp[key]}" data-headamp="${key}">
            <span class="slider-value" data-headamp-val="${key}">${config.headAmp[key].toFixed(2)}</span>
        `;
        panel.appendChild(row);
    }
    panel.querySelectorAll("input[data-headamp]").forEach((input) => {
        input.addEventListener("input", () => {
            const key = input.dataset.headamp;
            const v = parseFloat(input.value);
            config.headAmp[key] = v;
            panel.querySelector(`[data-headamp-val="${key}"]`).textContent = v.toFixed(2);
        });
    });
}

function buildFeatureSelect(selected) {
    const select = document.createElement("select");
    for (const group of FEATURE_GROUPS) {
        const og = document.createElement("optgroup");
        og.label = group.label;
        for (const f of group.features) {
            const opt = document.createElement("option");
            opt.value = f;
            opt.textContent = f;
            if (f === selected) opt.selected = true;
            og.appendChild(opt);
        }
        select.appendChild(og);
    }
    return select;
}

function renderChannel(channelKey) {
    const list = $(`rules_${channelKey}`);
    if (!list) return;
    list.innerHTML = "";
    config.rules[channelKey].forEach((rule, idx) => {
        list.appendChild(buildRuleRow(channelKey, rule, idx));
    });
}

function buildRuleRow(channelKey, rule, idx) {
    const row = document.createElement("div");
    row.className = "rule-row";
    row.dataset.channel = channelKey;
    row.dataset.idx = String(idx);

    // Feature dropdown
    const select = buildFeatureSelect(rule.feature);
    select.addEventListener("change", () => {
        config.rules[channelKey][idx].feature = select.value;
    });
    row.appendChild(select);

    // Weight slider + value
    const weightBlock = document.createElement("div");
    weightBlock.className = "weight-block";
    weightBlock.innerHTML = `
        <input type="range" class="slider" min="-2" max="2" step="0.01" value="${rule.weight}">
        <span class="slider-value">${rule.weight.toFixed(2)}</span>
    `;
    const weightSlider = weightBlock.querySelector("input");
    const weightLabel = weightBlock.querySelector(".slider-value");
    weightSlider.addEventListener("input", () => {
        const v = parseFloat(weightSlider.value);
        config.rules[channelKey][idx].weight = v;
        weightLabel.textContent = v.toFixed(2);
    });

    // Live contribution readout
    const live = document.createElement("span");
    live.className = "live-readout";
    live.textContent = "—";

    // Remove
    const removeBtn = document.createElement("button");
    removeBtn.className = "btn-remove";
    removeBtn.textContent = "×";
    removeBtn.title = "Remove this mapping";
    removeBtn.addEventListener("click", () => {
        config.rules[channelKey].splice(idx, 1);
        renderChannel(channelKey);
    });

    // Layout: [select] [weight-block + live] [×]
    // (CSS grid handles wrapping on narrow viewports.)
    const middle = document.createElement("div");
    middle.style.display = "flex";
    middle.style.flexDirection = "column";
    middle.style.gap = "4px";
    middle.appendChild(weightBlock);
    middle.appendChild(live);
    row.appendChild(middle);
    row.appendChild(removeBtn);
    return row;
}

function renderAllChannels() {
    for (const ch of ["body_yaw", "left_antenna", "right_antenna"]) {
        renderChannel(ch);
    }
}

function renderTuner() {
    renderHeadAmpPanel();
    renderBlendshapeMonitor();
    renderAllChannels();
}

// Build the 52 bar rows once. Subsequent updates only mutate the
// `width` of each fill div + the inner text of the value — no DOM
// churn (52 rows × 15 Hz would be a lot).
function renderBlendshapeMonitor() {
    const root = $("blendshapeMonitor");
    if (!root) return;
    if (root.dataset.built === "1") return;
    root.dataset.built = "1";

    // 52 blendshapes from the ARKit-aligned set, alphabetised so the
    // user can find one without hunting.
    const allBlendshapes = FEATURE_GROUPS
        .filter((g) => g.label !== "Special" && g.label !== "Head pose (raw)")
        .flatMap((g) => g.features)
        .sort();

    const grid = document.createElement("div");
    grid.className = "bs-grid";
    for (const name of allBlendshapes) {
        const row = document.createElement("div");
        row.className = "bs-row dim";
        row.dataset.feature = name;
        row.innerHTML = `
            <span class="bs-label" title="${name}">${name}</span>
            <div class="bs-bar"><div class="bs-fill"></div></div>
            <span class="bs-val">0.00</span>
        `;
        grid.appendChild(row);
    }
    root.innerHTML = "";
    root.appendChild(grid);
}

// 15 Hz updater for both blendshape bars and rule live-readouts.
const ACTIVE_THRESHOLD = 0.05;  // below this, dim the row
function tickLiveDisplay() {
    if (!lastFeatures) return;

    // Blendshape bars
    const bars = document.querySelectorAll("#blendshapeMonitor .bs-row");
    bars.forEach((row) => {
        const v = lastFeatures[row.dataset.feature];
        if (v == null) return;
        const fill = row.querySelector(".bs-fill");
        const val = row.querySelector(".bs-val");
        const pct = Math.max(0, Math.min(1, v)) * 100;
        fill.style.width = pct + "%";
        val.textContent = v.toFixed(2);
        row.classList.toggle("dim", v < ACTIVE_THRESHOLD);
    });

    // Rule rows — same loop body as the previous separate ticker.
    for (const channelKey of ["body_yaw", "left_antenna", "right_antenna"]) {
        const list = $(`rules_${channelKey}`);
        if (!list) continue;
        list.querySelectorAll(".rule-row").forEach((row) => {
            const idx = parseInt(row.dataset.idx, 10);
            const rule = config.rules[channelKey][idx];
            if (!rule) return;
            const v = lastFeatures[rule.feature];
            const live = row.querySelector(".live-readout");
            if (v == null) {
                live.textContent = "—";
            } else {
                const contrib = rule.weight * v;
                live.textContent = `${v.toFixed(2)} → ${contrib >= 0 ? "+" : ""}${contrib.toFixed(2)}`;
            }
        });
    }
}

// ─── 20 Hz send loop ───────────────────────────────────────
function startSendLoop() {
    if (sendIntervalId) return;
    sendIntervalId = setInterval(() => {
        if (!isMaster()) return;
        const targets = mapToTargets(lastFeatures);
        if (!targets) return;
        try {
            // setFullTarget accepts the nested 4x4 directly and packs it
            // into one set_full_target wire command, atomic with the
            // antenna and body fields.
            const head = rpyToMatrix(
                targets.headRollDeg,
                targets.headPitchDeg,
                targets.headYawDeg,
            );
            head[0][3] = targets.headX;
            head[1][3] = targets.headY;
            head[2][3] = targets.headZ;
            robot.setFullTarget({
                head,
                antennas: [targets.rightAnt, targets.leftAnt],
                bodyYaw: targets.bodyYaw,
            });
        } catch (e) {
            // Don't drop the loop on a single transient failure.
            console.warn("setFullTarget failed:", e);
        }
    }, 1000 / SEND_HZ);
}

function stopSendLoop() {
    if (sendIntervalId) {
        clearInterval(sendIntervalId);
        sendIntervalId = null;
    }
}

// One-shot neutral pose: identity head, idle antennas, body straight.
// Used on master-toggle-off — for full session-stop, prefer
// Motor.softReturnToBase() (synchronous fire-and-forget pattern, also
// safe inside pagehide).
function sendNeutral() {
    if (robot.state !== "streaming") return;
    try {
        robot.setFullTarget({
            head: INIT_HEAD_POSE,
            antennas: INIT_ANTENNAS,
            bodyYaw: 0,
        });
    } catch (e) {
        console.warn("sendNeutral failed:", e);
    }
}

// ─── Debug snapshot ────────────────────────────────────────
// Dumps the full tracking state to a JSON download so the user can
// share it back for offline debugging (calibration, axis questions,
// etc). Includes everything the mapper sees this frame plus the
// targets it would produce, so a snapshot is enough to reconstruct
// the exact robot command without rerunning the camera.
function saveSnapshot() {
    if (!lastFeatures || !lastFaceMatrix) {
        alert("No face tracked yet — point the camera at your face first.");
        return;
    }
    const snapshot = {
        schema: "mime_bot/snapshot/v1",
        captured_at: new Date().toISOString(),
        perf_ms: Math.round(performance.now()),
        flags: {
            mirror: isMirror(),
            master: isMaster(),
        },
        raw: {
            // Row-major flat[16] as MediaPipe's tasks-vision returns it.
            // Translation lives at indices 3, 7, 11 (tx, ty, tz in cm).
            face_matrix_row_major_flat16: Array.from(lastFaceMatrix),
            // The first detected tz (in cm) used as a baseline so the
            // depth feature reads ~0 at startup. Useful to interpret
            // features.head_z below.
            baselineZ_cm: baselineZ,
        },
        // The dict the mapping rules consume — 52 ARKit blendshapes,
        // head_roll/pitch/yaw (rad), head_x/y/z (m, baseline-subtracted),
        // plus derived (nostril_flare, etc.) and `_bias = 1.0`.
        features: lastFeatures,
        // What mapToTargets would produce for this frame.
        targets: mapToTargets(lastFeatures),
        // The active mapper configuration so the snapshot is
        // self-contained — no need to also remember which weights
        // were live when it was taken.
        config: {
            headAmp: { ...config.headAmp },
            headRules: JSON.parse(JSON.stringify(config.headRules)),
            rules: JSON.parse(JSON.stringify(config.rules)),
        },
    };

    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const blob = new Blob([JSON.stringify(snapshot, null, 2)],
                         { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `mime_bot_snapshot_${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function applyMirrorClass() {
    const container = document.querySelector("#mimicryView .video-container");
    if (!container) return;
    if ($("toggleMirror").checked) container.classList.add("mirror-preview");
    else container.classList.remove("mirror-preview");
}

// ─── Robot picker ──────────────────────────────────────────
// One-click picker (mirrors marionette_js): tapping a robot card starts
// the session immediately. No intermediate "Start" button.
function renderRobotList(robots) {
    const list = $("robotList");
    list.innerHTML = "";
    if (!robots?.length) {
        setPickerHeader("Looking for your robot");
        list.innerHTML = '<div class="hint">Power one on and make sure it\'s signed in to your HF account.</div>';
        return;
    }
    setPickerHeader("Pick your robot");
    for (const r of robots) {
        const div = document.createElement("div");
        div.className = "robot-card";
        div.innerHTML = `<div class="name">${r.meta?.name || "Reachy Mini"}</div>
                         <div class="id">${r.id.slice(0, 12)}…</div>`;
        div.onclick = () => pickRobot(r);
        list.appendChild(div);
    }
}

async function pickRobot(r) {
    selectedRobotId = r.id;
    $("robotName").textContent = r.meta?.name || "Reachy Mini";
    setPickerHeader(`Starting session with ${r.meta?.name || "robot"}…`);
    try {
        await robot.startSession(r.id);
        // The 'streaming' handler takes over view + camera + tracking.
    } catch (e) {
        console.error("startSession failed:", e);
        setPickerHeader("Pick your robot");
        const msg = e.reason?.startsWith("robot_busy")
            ? `Robot busy — "${e.activeApp || "another app"}" is connected`
            : `Failed: ${e.message || e}`;
        alert(msg);
    }
}

// ─── Lifecycle event wiring ────────────────────────────────
robot.addEventListener("robotsChanged", (e) => renderRobotList(e.detail.robots));

robot.addEventListener("streaming", async () => {
    showMimicry();
    applyMirrorClass();

    // Smooth, jerk-free entry: pin current → enable torque → daemon
    // interpolates to base. Fire-and-forget — daemon does the interp
    // on its own clock. We then wait that duration before letting the
    // live 20 Hz stream start, so we don't fight the goto.
    const settleStart = performance.now();
    let gotoMs = MIN_RETURN_S * 1000;
    try {
        const dur = await Motor.startup();
        gotoMs = Math.round((dur ?? MIN_RETURN_S) * 1000);
    } catch (e) {
        console.warn("Motor.startup failed:", e);
    }

    // Camera + FaceLandmarker init in parallel with the goto (they take
    // ~1 s combined the first time, similar to the goto duration).
    await startCamera();
    try {
        await initFaceLandmarker();
    } catch (e) {
        console.error("FaceLandmarker init failed:", e);
        alert("Failed to load the face tracker — check your network and reload.");
        return;
    }

    // Wait for whichever is longer: goto settle, or "we're already past it".
    const elapsed = performance.now() - settleStart;
    if (elapsed < gotoMs) await sleep(gotoMs - elapsed);

    startTracking();
    startSendLoop();
});

robot.addEventListener("sessionStopped", (e) => {
    stopSendLoop();
    stopTracking();
    baselineZ = null;
    stopCamera();
    showPicker();
    if (e.detail?.message) setStatus("connected", e.detail.message);
});

robot.addEventListener("disconnected", () => {
    // Signaling/SSE went away. Drop back to the login screen with a
    // prompt to re-auth (covers token expiry as well as network drops).
    setLoginMessage("Disconnected — sign in again to reconnect.", { showButton: true });
    showLogin();
});

robot.addEventListener("sessionRejected", (e) => {
    const active = e.detail?.activeApp;
    alert(active ? `Robot busy — "${active}" is already connected` : "Robot busy");
});

robot.addEventListener("error", (e) => {
    console.error(`[${e.detail.source}]`, e.detail.error);
});

// ─── Button wiring ─────────────────────────────────────────
$("btnLogin").addEventListener("click", () => robot.login());
$("btnLogout").addEventListener("click", () => {
    robot.logout();
    setLoginMessage("Sign in to use Mime Bot.", { showButton: true });
    showLogin();
});

$("btnSnapshot").addEventListener("click", saveSnapshot);

$("btnStop").addEventListener("click", async () => {
    // Stop our local commands FIRST so they don't fight the goto.
    stopSendLoop();
    stopTracking();
    baselineZ = null;
    stopCamera();
    // Hand the daemon a smooth interp back to base; it'll keep going on
    // its own clock even after stopSession tears the data channel down.
    if (robot.state === "streaming") {
        try { await Motor.softReturnToBase(); } catch (e) { console.warn("softReturnToBase:", e); }
    }
    try {
        await robot.stopSession();
    } catch (e) {
        console.warn("stopSession:", e);
    }
});

// ─── Clean shutdown on tab close / navigation ─────────────
//
// pagehide and beforeunload give us a *very* short synchronous window.
// We can't await sleeps. The trick (per marionette_js): we don't need to
// — we push three sync commands to the WebRTC data channel and let the
// daemon execute them on its own clock after the page is gone:
//   1. setFullTarget(currentPose) — pin goal=current, no jerk
//   2. setMotorMode("enabled")    — torque on
//   3. gotoTarget(INIT)           — daemon interps to base
let _shuttingDown = false;
function shutdown() {
    if (_shuttingDown) return;
    _shuttingDown = true;
    try {
        stopSendLoop();
        stopTracking();
        stopCamera();
        if (robot.state === "streaming") {
            // Sync version of softReturnToBase — no awaits, just fire.
            const rs = robot.robotState;
            if (rs?.headMatrix) {
                robot.setFullTarget({
                    head: rs.headMatrix,
                    antennas: rs.antennasRad,
                    bodyYaw: rs.bodyYaw ?? 0,
                });
            }
            robot.setMotorMode("enabled");
            const duration = scaledDuration(rs?.headMatrix, INIT_HEAD_POSE);
            robot.gotoTarget({
                head: INIT_HEAD_POSE,
                antennas: INIT_ANTENNAS,
                bodyYaw: 0,
                duration,
            });
        }
    } catch {}
}
window.addEventListener("pagehide", shutdown);
window.addEventListener("beforeunload", shutdown);

// Mirror toggle is purely visual on the local preview (face landmark
// extraction always reads the un-flipped frame; the mapper flips
// head_yaw at the output). Wired here so the CSS class applies
// immediately on click, even before streaming.
document.addEventListener("DOMContentLoaded", () => {
    const t = $("toggleMirror");
    if (t) t.addEventListener("change", applyMirrorClass);

    // Toggling master off → return to neutral immediately.
    // Toggling on → loop resumes naturally on next tick.
    $("toggleMaster").addEventListener("change", () => {
        if (!isMaster()) sendNeutral();
    });

    // Camera picker — switch to the selected device on change.
    $("cameraSelect").addEventListener("change", (e) => {
        switchCamera(e.target.value);
    });

    // +Add mapping: appends a neutral rule to the channel.
    document.querySelectorAll(".btn-add[data-channel]").forEach((btn) => {
        btn.addEventListener("click", () => {
            const ch = btn.dataset.channel;
            config.rules[ch].push({ feature: "_bias", weight: 0.0 });
            renderChannel(ch);
        });
    });

    // Reset to defaults: deep-copy and re-render all panels.
    $("btnReset").addEventListener("click", () => {
        const fresh = JSON.parse(JSON.stringify(DEFAULT_CONFIG));
        config.headAmp = fresh.headAmp;
        config.headRules = fresh.headRules;
        config.rules = fresh.rules;
        $("toggleMirror").checked = true;
        applyMirrorClass();
        renderTuner();
    });

    // Initial render so the panels look populated even before the
    // first session (the data is in #mimicryView which stays hidden,
    // but the content is ready when it shows).
    renderTuner();

    // ~15 Hz ticker drives both the blendshape monitor and the rule
    // live-readouts off the same lastFeatures snapshot.
    setInterval(tickLiveDisplay, 70);
});

// ─── Bootstrap ─────────────────────────────────────────────
//
// Mirrors marionette_js's flow: try to silently authenticate from the
// stored OAuth state (or freshly returned URL parameters), and if that
// succeeds, auto-connect to the signaling server. The user only ever
// sees the login screen if they're not signed in.
async function bootstrap() {
    showLogin();
    setLoginMessage("Checking sign-in…");

    let authed = false;
    try {
        authed = await robot.authenticate();
    } catch (e) {
        console.warn("authenticate threw:", e);
    }

    if (!authed) {
        setLoginMessage("Sign in to use Mime Bot.", { showButton: true });
        return;
    }

    showMain();
    showPicker();
    setPickerHeader("Connecting…");

    try {
        await robot.connect();
        setPickerHeader("Looking for your robot");
        // One robot, on this wifi, already chosen by virtue of being the only
        // one — showing a picker with a single row in it is a click for nothing.
        if (selectedRobotId === null) {
            await pickRobot({ id: "local", meta: { name: "Vibey" } });
        }
    } catch (e) {
        console.error("connect failed:", e);
        setLoginMessage(
            `Couldn't reach the signaling server: ${e.message || e}. Reload to retry.`,
            { showButton: true },
        );
        showLogin();
    }
}

document.addEventListener("DOMContentLoaded", bootstrap);
