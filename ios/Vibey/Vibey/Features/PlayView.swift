import SwiftUI

/// The fun stuff: emotes, the soundboard, say-a-line, the DJ, and the wheels.
struct PlayView: View {
    @EnvironmentObject var store: Store
    @State private var emotes: [String] = Emotes.fallback
    @State private var sounds: [SoundFX] = []
    @State private var fired: String?
    @State private var line = ""
    @FocusState private var typing: Bool

    private let grid = [GridItem(.adaptive(minimum: 64), spacing: 8)]

    var body: some View {
        NavigationStack {
            SpaceScreen {
                ScrollView {
                    VStack(spacing: 14) {
                        ScreenTitle(title: "Play", subtitle: "Make Vibey do things")
                        sayCard
                        SectionLabel(text: "Emotes", trailing: "\(emotes.count)")
                        emoteGrid
                        SectionLabel(text: "Soundboard", trailing: sounds.isEmpty ? nil : "\(sounds.count - 1)")
                        soundboard
                        SectionLabel(text: "DJ")
                        DJCard()
                        SectionLabel(text: "Wheels")
                        NavigationLink { DriveView() } label: { driveRow }
                            .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 18)
                    .padding(.bottom, 24)
                }
                .scrollIndicators(.hidden)
                .scrollDismissesKeyboard(.interactively)
                .refreshable { await load() }
            }
            .toolbar(.hidden, for: .navigationBar)
        }
        .task { await load() }
    }

    // MARK: Say

    private var sayCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Say").font(.system(.headline, design: .rounded))
                Spacer()
                Text("word for word").font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
            }
            HStack(spacing: 10) {
                TextField("Hello from your phone", text: $line, axis: .vertical)
                    .focused($typing)
                    .lineLimit(1...4)
                    .font(.system(.body, design: .rounded))
                    .padding(.horizontal, 14).padding(.vertical, 11)
                    .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Palette.ink.opacity(0.06)))
                    .submitLabel(.send)
                    .onSubmit(say)
                Button(action: say) {
                    Image(systemName: "speaker.wave.2.fill")
                        .font(.system(size: 18, weight: .bold))
                        .frame(width: 48, height: 48)
                        .background(Circle().fill(canSay ? Palette.ink : Palette.ink.opacity(0.15)))
                        .foregroundStyle(canSay ? Palette.duck : .white)
                }
                .buttonStyle(PressScale())
                .disabled(!canSay)
                .accessibilityLabel("Speak it")
            }
        }
        .shell()
    }

    private var canSay: Bool { !line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    private func say() {
        let t = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        line = ""
        typing = false
        store.run("Saying it") { try await $0.say(t) }
    }

    // MARK: Emotes

    private var emoteGrid: some View {
        LazyVGrid(columns: grid, spacing: 8) {
            ForEach(emotes, id: \.self) { e in
                Tile(emoji: Emotes.emoji(e), title: Emotes.title(e), lit: fired == "e:" + e) {
                    fire("e:" + e) { try await $0.emote(e) }
                }
            }
        }
    }

    // MARK: Soundboard

    private var groups: [(String, [SoundFX])] {
        let order = ["droid", "mood", "space", "dark", "music", "bed"]
        let playable = sounds.filter { $0.name != "stop_audio" }
        let keys = Array(Set(playable.map { $0.group ?? "other" }))
            .sorted { (order.firstIndex(of: $0) ?? 99, $0) < (order.firstIndex(of: $1) ?? 99, $1) }
        return keys.map { k in (k, playable.filter { ($0.group ?? "other") == k }) }
    }

    @ViewBuilder
    private var soundboard: some View {
        if sounds.isEmpty {
            Text("Loading sounds…").font(.system(.footnote, design: .rounded))
                .foregroundStyle(.white.opacity(0.5)).frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 6)
        } else {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(groups, id: \.0) { g in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(Sounds.groupTitle(g.0))
                            .font(.system(.caption2, design: .rounded).weight(.bold))
                            .foregroundStyle(.white.opacity(0.4))
                            .padding(.leading, 6)
                        LazyVGrid(columns: grid, spacing: 8) {
                            ForEach(g.1) { s in
                                Tile(emoji: Sounds.emoji(s.name), title: s.label, lit: fired == "s:" + s.name,
                                     accent: Palette.live) {
                                    fire("s:" + s.name) { try await $0.sfx(s.name) }
                                }
                            }
                        }
                    }
                }
                Button { fire("s:stop") { try await $0.sfx("stop_audio") } } label: {
                    Label("Stop all sound", systemImage: "stop.fill")
                        .font(.system(.subheadline, design: .rounded).weight(.bold))
                        .frame(maxWidth: .infinity, minHeight: 46)
                        .background(Capsule().fill(.white.opacity(0.08)))
                        .overlay(Capsule().stroke(.white.opacity(0.18)))
                        .foregroundStyle(.white)
                }
                .buttonStyle(PressScale())
            }
        }
    }

    private var driveRow: some View {
        HStack(spacing: 14) {
            Image(systemName: "dpad.fill")
                .font(.title2)
                .frame(width: 44, height: 44)
                .background(Circle().fill(Palette.ink))
                .foregroundStyle(Palette.duck)
            VStack(alignment: .leading, spacing: 2) {
                Text("Drive").font(.system(.headline, design: .rounded))
                Text("D-pad for the rover wheels").font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(Palette.inkDim)
            }
            Spacer()
            Image(systemName: "chevron.right").foregroundStyle(Palette.inkDim)
        }
        .shell()
    }

    private func fire(_ id: String, _ action: @escaping (VibeyAPI) async throws -> Void) {
        withAnimation(.spring(response: 0.25)) { fired = id }
        store.run(nil, action)
        Task {
            try? await Task.sleep(for: .milliseconds(700))
            if fired == id { withAnimation(.easeOut) { fired = nil } }
        }
    }

    private func load() async {
        if let e = try? await store.api.emotes(), !e.isEmpty { emotes = e }
        if let s = try? await store.api.sounds() { sounds = s }
    }
}

/// Square emoji button with a glow when it fires.
struct Tile: View {
    let emoji: String
    let title: String
    var lit: Bool = false
    var accent: Color = Palette.duck
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 4) {
                Text(emoji).font(.system(size: 26))
                Text(title)
                    .font(.system(size: 11, weight: .bold, design: .rounded))
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
                    .minimumScaleFactor(0.85)
                    .foregroundStyle(Palette.ink)
            }
            .frame(maxWidth: .infinity, minHeight: 74)
            .padding(.horizontal, 4)
            .background(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom],
                                         startPoint: .top, endPoint: .bottom))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 22, style: .continuous)
                    .stroke(accent, lineWidth: lit ? 3 : 0)
            )
            .shadow(color: (lit ? accent : .white).opacity(lit ? 0.7 : 0.16), radius: lit ? 16 : 10)
            .scaleEffect(lit ? 1.04 : 1)
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(title)
    }
}

/// The DJ deck: reachy_dj.py through :8772/dj/*.
struct DJCard: View {
    @EnvironmentObject var store: Store
    @Environment(\.scenePhase) private var phase
    @State private var st: DJStatus?
    @State private var tracks: [String] = []
    @State private var pick: String?
    @State private var pct: Double = 100
    @State private var sliding = false
    @State private var offline: String?

    private var playing: Bool { st?.playing == true }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: "opticaldisc.fill")
                    .font(.system(size: 26))
                    .foregroundStyle(playing ? Palette.duck : Palette.inkDim)
                    .rotationEffect(.degrees(playing ? 360 : 0))
                    .animation(playing ? .linear(duration: 2).repeatForever(autoreverses: false) : .default, value: playing)
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(Palette.ink))
                VStack(alignment: .leading, spacing: 2) {
                    Text(st?.track ?? (offline == nil ? "Nothing loaded" : "DJ offline"))
                        .font(.system(.headline, design: .rounded)).lineLimit(1)
                    Text(subtitle).font(.system(.caption, design: .rounded).monospacedDigit())
                        .foregroundStyle(Palette.inkDim).lineLimit(1)
                }
                Spacer()
                Menu {
                    ForEach(tracks, id: \.self) { t in
                        Button { pick = t; Task { await send("load", ["track": t]) } } label: {
                            if t == (pick ?? st?.track) { Label(t, systemImage: "checkmark") } else { Text(t) }
                        }
                    }
                    if tracks.isEmpty { Text("Drop songs in ~/Music/vibey") }
                } label: {
                    Image(systemName: "music.note.list")
                        .font(.system(size: 17, weight: .bold))
                        .frame(width: 40, height: 40)
                        .background(Circle().fill(Palette.ink.opacity(0.07)))
                        .foregroundStyle(Palette.ink)
                }
            }

            HStack(spacing: 10) {
                Button {
                    Haptics.tap()
                    Task {
                        if playing { await send("pause") }
                        else if let p = pick ?? tracks.first, p != st?.track { await send("play", ["track": p]) }
                        else { await send("play") }
                    }
                } label: {
                    Label(playing ? "Pause" : "Play", systemImage: playing ? "pause.fill" : "play.fill")
                        .font(.system(.headline, design: .rounded))
                        .frame(maxWidth: .infinity, minHeight: 48)
                        .background(Capsule().fill(Palette.duck))
                        .foregroundStyle(Palette.ink)
                }
                .buttonStyle(PressScale())
                Button { Haptics.tap(); Task { await send("stop") } } label: {
                    Image(systemName: "stop.fill")
                        .font(.system(size: 17, weight: .bold))
                        .frame(width: 56, height: 48)
                        .background(Capsule().fill(Palette.ink))
                        .foregroundStyle(.white)
                }
                .buttonStyle(PressScale())
            }

            VStack(spacing: 6) {
                HStack {
                    Text("Tempo").font(.system(.subheadline, design: .rounded).weight(.semibold))
                    Spacer()
                    Text(tempoLabel).font(.system(.subheadline, design: .rounded).monospacedDigit())
                        .foregroundStyle(Palette.inkDim)
                }
                HStack(spacing: 10) {
                    nudge(-4)
                    Slider(value: $pct, in: 80...125, step: 1) { editing in
                        sliding = editing
                        if !editing, let bpm = st?.bpm {
                            Task { await send("tempo", ["bpm": bpm * pct / 100]) }
                        }
                    }
                    .tint(Palette.ink)
                    nudge(4)
                }
            }
            .disabled(st?.bpm == nil)
            .opacity(st?.bpm == nil ? 0.45 : 1)
        }
        .shell()
        .task(id: phase == .active) { await poll() }
    }

    private var subtitle: String {
        if let offline { return offline }
        guard let st, st.track != nil else { return "Pick a track to start" }
        let pos = Int(st.position ?? 0), dur = Int(st.duration ?? 0)
        return String(format: "%d:%02d / %d:%02d", pos / 60, pos % 60, dur / 60, dur % 60)
    }

    private var tempoLabel: String {
        guard let t = st?.target_bpm ?? st?.bpm else { return "–" }
        return "\(Int(t.rounded())) BPM · \(Int(pct))%"
    }

    private func nudge(_ p: Int) -> some View {
        Button { Haptics.soft(); Task { await send("nudge", ["percent": p]) } } label: {
            Text(p > 0 ? "+\(p)%" : "\(p)%")
                .font(.system(.caption, design: .rounded).weight(.bold).monospacedDigit())
                .frame(width: 48, height: 34)
                .background(Capsule().fill(Palette.ink.opacity(0.07)))
                .foregroundStyle(Palette.ink)
        }
        .buttonStyle(PressScale())
    }

    private func apply(_ s: DJStatus) {
        st = s
        offline = nil
        if !sliding { pct = ((s.rate ?? 1) * 100).rounded() }
    }

    private func send(_ action: String, _ body: [String: Any] = [:]) async {
        do { apply(try await store.api.dj(action, body)) }
        catch where error.isCancellation {}
        catch { Haptics.fail(); store.flash(error.localizedDescription) }
    }

    private func poll() async {
        guard phase == .active else { return }
        tracks = (try? await store.api.djTracks()) ?? tracks
        while !Task.isCancelled {
            do { apply(try await store.api.djStatus()) }
            catch where error.isCancellation { return }
            catch { offline = error.localizedDescription }
            try? await Task.sleep(for: .seconds(playing ? 1 : 3))
        }
    }
}

enum Emotes {
    static let fallback = ["wave", "dance", "laugh", "whistle", "nod", "happy", "excited", "curious"]

    static let emojis: [String: String] = [
        "happy": "😄", "whistle": "🎶", "excited": "🤩", "curious": "🧐", "sad": "😢",
        "smug": "😏", "thinking": "🤔", "victory": "🏆", "wave": "👋", "nod": "👍",
        "shake": "🙅", "wave_left": "🫲", "wave_right": "🫱", "peace": "✌️", "smile": "😊",
        "tilt_left": "🐶", "tilt_right": "🐱", "tilt_left_big": "🙃", "tilt_right_big": "🫠",
        "laugh": "😂", "laugh_wheeze": "🤣", "laugh_cackle": "😆", "appalled": "😱",
        "confused": "😵‍💫", "frown": "☹️", "surprised": "😮", "no_no_no": "🚫",
        "yes_yes_yes": "🙌", "shy": "🙈", "wink": "😉", "shrug": "🤷", "shy_nod": "☺️",
        "dance": "💃",
    ]
    static let titles: [String: String] = [
        "tilt_left_big": "Big tilt left", "tilt_right_big": "Big tilt right",
        "no_no_no": "No no no", "yes_yes_yes": "Yes yes yes", "laugh_wheeze": "Wheeze",
        "laugh_cackle": "Cackle", "shy_nod": "Shy nod",
    ]
    static func emoji(_ n: String) -> String { emojis[n] ?? "✨" }
    static func title(_ n: String) -> String {
        titles[n] ?? n.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

enum Sounds {
    static let emojis: [String: String] = [
        "laser_pew": "⚡️", "laser_burst": "💥", "saber_on": "🗡️", "saber_swing": "💨",
        "hyperjump": "🚀", "shield_up": "🛡️", "tractor_beam": "🛸", "airlock": "🚪",
        "saber_off": "🌑", "saber_hum": "🎛️", "saber_clash": "⚔️", "blaster_stun": "😵",
        "ion_scream": "✈️", "dark_breath": "😮‍💨", "dark_sting": "🌘", "droid_yes": "✅",
        "droid_no": "❌", "droid_gossip": "💬", "scanner": "📡", "cantina": "🎷",
        "success": "🏆", "fail": "🎺", "mischief": "😈", "robot_boop": "🤖",
        "cartoon_boing": "🪀", "sparkle_up": "✨", "playful_cartoon": "🎠",
        "spacey_march": "🪐", "dreamy_drift": "🌙", "chase_scene": "🏃",
    ]
    static func emoji(_ n: String) -> String { emojis[n] ?? "🔊" }
    static func groupTitle(_ g: String) -> String {
        ["droid": "DROID", "mood": "MOOD", "space": "SPACE", "dark": "DARK SIDE",
         "music": "MUSIC", "bed": "MUSIC BEDS · LOOPS",
         "astromech": "ASTROMECH · R2 STYLE", "robots": "FAMOUS ROBOTS", "scifi": "SCI-FI"][g] ?? g.uppercased()
    }
}
