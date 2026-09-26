/**
 * reachy-local.js — a drop-in replacement for reachy-mini.js that talks to the
 * robot on the LAN instead of through Hugging Face.
 *
 * The upstream client (RemiFabre/mime_bot, built on pollen-robotics/reachy-mini)
 * reaches the robot over WebRTC via a relay at cduss-reachy-mini-central.hf.space,
 * behind an HF OAuth login. That is the right design for a Space a stranger opens
 * on their phone. It is the wrong one here: Vibey's robot is on the same wifi as
 * the browser, the rest of this repo already drives it over plain REST on :8000,
 * and routing head poses through a datacentre to reach a robot on the desk adds
 * a round trip to every frame of a 30fps mimic loop.
 *
 * So this exposes the same surface main.js already calls — same method names,
 * same payload shapes, same 'state'/'streaming'/'disconnected' events — and puts
 * REST underneath. main.js changes by one import line.
 *
 * The daemon echoes Origin back in access-control-allow-origin, so the browser
 * can POST to it directly and no proxy is needed.
 *
 *   import { ReachyMini, rpyToMatrix } from "./reachy-local.js";
 */

export function degToRad(deg) { return deg * Math.PI / 180; }
export function radToDeg(rad) { return rad * 180 / Math.PI; }

// Same construction as upstream, kept here so main.js's import is satisfied
// from one module.
export function rpyToMatrix(rollDeg, pitchDeg, yawDeg) {
    const r = degToRad(rollDeg), p = degToRad(pitchDeg), y = degToRad(yawDeg);
    const cr = Math.cos(r), sr = Math.sin(r);
    const cp = Math.cos(p), sp = Math.sin(p);
    const cy = Math.cos(y), sy = Math.sin(y);
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, 0],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, 0],
        [-sp, cp * sr, cp * cr, 0],
        [0, 0, 0, 1],
    ];
}

export function matrixToRpy(m) {
    const a = Array.isArray(m[0]) ? m : [m.slice(0, 4), m.slice(4, 8), m.slice(8, 12), m.slice(12, 16)];
    return {
        roll: radToDeg(Math.atan2(a[2][1], a[2][2])),
        pitch: radToDeg(-Math.asin(Math.max(-1, Math.min(1, a[2][0])))),
        yaw: radToDeg(Math.atan2(a[1][0], a[0][0])),
    };
}

const flat16 = (head) => (Array.isArray(head[0]) ? head.flat() : head);

// {x,y,z,roll,pitch,yaw} in metres/radians -> 4x4 row-major, the convention the
// SDK and main.js both use (head[0][3]=X forward, [1][3]=Y left, [2][3]=Z up).
function rpyRadToMatrix({ x = 0, y = 0, z = 0, roll = 0, pitch = 0, yaw = 0 }) {
    const cr = Math.cos(roll), sr = Math.sin(roll);
    const cp = Math.cos(pitch), sp = Math.sin(pitch);
    const cy = Math.cos(yaw), sy = Math.sin(yaw);
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr, x],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr, y],
        [-sp, cp * sr, cp * cr, z],
        [0, 0, 0, 1],
    ];
}

export class ReachyMini extends EventTarget {
    constructor(options = {}) {
        super();
        // Default to the same host the page came from, so opening the dashboard
        // from another machine on the wifi still points at the right robot.
        // Resolved properly in connect() from /mimic/robot.json — this is only
        // the fallback for opening the page outside the dashboard.
        this.baseUrl = (options.baseUrl
            || new URLSearchParams(location.search).get("robot")
            || `http://${location.hostname}:8000`).replace(/\/$/, "");
        this._baseResolved = Boolean(options.baseUrl
            || new URLSearchParams(location.search).get("robot"));
        this._state = "disconnected";
        this._robotState = { headMatrix: null, antennas: null, bodyYaw: 0 };
        this._pollMs = options.pollMs || 500;
        this._poller = null;

        // One command in flight at a time.
        //
        // The mimic loop fires a target per animation frame (~60/s). Letting
        // those queue means the robot is always executing a pose from a second
        // ago and the lag grows without bound — it looks like the robot is
        // mimicking someone else's face. Dropping frames while one is in flight
        // is correct here: a newer target strictly supersedes an older one, so
        // the one worth sending is always the latest.
        this._inFlight = false;
        this._pending = null;
    }

    get state() { return this._state; }
    get robotState() { return this._robotState; }
    get username() { return "local"; }

    // ── auth: all no-ops. There is no login on the LAN. ─────────────────────
    async login() { return true; }
    async logout() { return true; }
    async authenticate() { return true; }

    async connect() {
        if (!this._baseResolved) {
            try {
                const r = await fetch("/mimic/robot.json", { cache: "no-store" });
                const j = await r.json();
                if (j.url) this.baseUrl = j.url.replace(/\/$/, "");
            } catch { /* fall back to the guess */ }
            this._baseResolved = true;
        }
        const ok = await this._ping();
        this._setState(ok ? "connected" : "disconnected");
        if (ok) {
            // main.js is built around picking from a list of robots on an
            // account. On the LAN there is exactly one and it is the one this
            // page was served next to, so announce it and let the UI skip
            // straight past the picker.
            this.dispatchEvent(new CustomEvent("robotsChanged", {
                detail: { robots: [{ id: "local", meta: { name: "Vibey" } }] },
            }));
        }
        if (!ok) {
            this.dispatchEvent(new CustomEvent("error", {
                detail: { message: `No robot at ${this.baseUrl}` },
            }));
        }
        return ok;
    }

    async startSession() {
        if (!(await this._ping())) {
            this._setState("disconnected");
            return false;
        }
        // Face tracking OFF for the duration.
        //
        // Vibey's onboard face-follow and this mimic loop both drive the same
        // neck, ~30 times a second, from different ideas of where it should be.
        // Run them together and the head fights itself. Tracking is one of the
        // best things Vibey does, so it is suspended rather than dropped —
        // stopSession turns it straight back on.
        await this._post("/api/media/tracking/disable").catch(() => {});
        await this._post("/api/motors/set_mode/enabled").catch(() => {});

        this._setState("streaming");
        this.requestState();
        clearInterval(this._poller);
        this._poller = setInterval(() => this.requestState(), this._pollMs);
        this.dispatchEvent(new CustomEvent("streaming", { detail: { active: true } }));
        return true;
    }

    stopSession() {
        clearInterval(this._poller);
        this._poller = null;
        // Hand the neck back to the face-follower.
        this._post("/api/media/tracking/enable", { weight: 0.6 }).catch(() => {});
        this._setState("connected");
        this.dispatchEvent(new CustomEvent("streaming", { detail: { active: false } }));
        return true;
    }

    _setState(s) {
        if (s === this._state) return;
        this._state = s;
        if (s === "disconnected") {
            this.dispatchEvent(new CustomEvent("disconnected", {}));
        }
    }

    async _ping() {
        try {
            const r = await fetch(`${this.baseUrl}/api/daemon/status`, { cache: "no-store" });
            return r.ok;
        } catch { return false; }
    }

    // Is Vibey switched off? Cached for a second — _post runs at 20 Hz during a
    // mimic session and this must not become 20 extra requests a second.
    //
    // This page talks STRAIGHT to the daemon on :8000, so it knew nothing about
    // the off switch on the dashboard: switching Vibey off disabled the motors,
    // and the next mimic frame re-enabled them and carried on driving the neck.
    // From the room, the off button simply did not work. The dashboard's state
    // is the authority, so it gets asked.
    async _isOff() {
        const now = Date.now();
        if (now - (this._offAt || 0) < 1000) return this._offCache === true;
        this._offAt = now;
        try {
            const r = await fetch("/chatstate", { cache: "no-store" });
            this._offCache = r.ok ? !!(await r.json()).off : false;
        } catch { this._offCache = false; }   // dashboard unreachable: don't block
        return this._offCache === true;
    }

    async _post(path, body) {
        if (await this._isOff()) {
            // Silent: at 20 Hz a thrown error per frame would bury the console
            // and tell the user nothing they can't see on the dashboard.
            throw new Error("vibey-off");
        }
        const r = await fetch(`${this.baseUrl}${path}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (!r.ok) throw new Error(`${path} -> ${r.status}`);
        return r.status === 204 ? {} : r.json().catch(() => ({}));
    }

    async requestState() {
        try {
            const r = await fetch(`${this.baseUrl}/api/state/full`, { cache: "no-store" });
            if (!r.ok) throw new Error(r.status);
            const s = await r.json();
            // /api/state/full reports the head as {x,y,z,roll,pitch,yaw} in
            // METRES and RADIANS, but main.js only ever handles headMatrix — it
            // feeds it straight back to gotoTarget and diffs it against
            // INIT_HEAD_POSE. Convert here so the shape main.js sees matches
            // what the WebRTC client used to hand it.
            const head = s.head_pose ?? s.present_head_pose ?? null;
            if (head) {
                this._robotState.headMatrix = (head.m || Array.isArray(head))
                    ? head
                    : rpyRadToMatrix(head);
            }
            const ant = s.antennas_position ?? s.antennas ?? null;
            if (ant) this._robotState.antennas = ant;
            const yaw = s.body_yaw ?? s.present_body_yaw;
            if (yaw !== undefined && yaw !== null) this._robotState.bodyYaw = yaw;
            this.dispatchEvent(new CustomEvent("state", { detail: this._robotState }));
            return this._robotState;
        } catch (e) {
            this._setState("disconnected");
            return null;
        }
    }

    _headPayload(head) {
        return { m: flat16(head) };
    }

    // The hot path: one target per frame, coalesced.
    setFullTarget({ head, antennas, bodyYaw } = {}) {
        const body = {};
        if (head) body.target_head_pose = this._headPayload(head);
        if (antennas) body.target_antennas = antennas;
        if (bodyYaw !== undefined && bodyYaw !== null) body.target_body_yaw = bodyYaw;

        this._pending = body;
        if (this._inFlight) return true;

        const pump = () => {
            const next = this._pending;
            this._pending = null;
            if (!next) { this._inFlight = false; return; }
            this._inFlight = true;
            this._post("/api/move/set_target", next)
                .catch(() => { /* a dropped frame is not worth a log line 30x/s */ })
                .finally(pump);
        };
        pump();
        return true;
    }

    gotoTarget({ head, antennas, bodyYaw, duration } = {}) {
        const body = { duration: Number(duration) || 0.5 };
        if (head) body.head_pose = this._headPayload(head);
        if (antennas) body.antennas = antennas;
        if (bodyYaw !== undefined && bodyYaw !== null) body.body_yaw = bodyYaw;
        return this._post("/api/move/goto", body).then(() => true).catch(() => false);
    }

    setMotorMode(mode) {
        return this._post(`/api/motors/set_mode/${mode}`).then(() => true).catch(() => false);
    }

    wakeUp() { return this._post("/api/move/play/wake_up").catch(() => false); }
    gotoSleep() { return this._post("/api/move/play/goto_sleep").catch(() => false); }
    setVolume(v) { return this._post("/api/volume/set", { volume: v }).catch(() => false); }
}

export default ReachyMini;
