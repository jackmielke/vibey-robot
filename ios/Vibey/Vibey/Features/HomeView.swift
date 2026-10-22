import SwiftUI

struct HomeView: View {
    @EnvironmentObject var store: Store
    @State private var volume: Double = 50
    @State private var volumeKnown = false
    @State private var cost: Cost?
    @State private var live: CostLive?
    @State private var added: String?
    @State private var pulse = false
    @State private var showSpend = false
    @State private var showBrain = false
    @State private var music: MacMusic?
    @State private var musicVol: Double = 50

    private var s: VibeyState? { store.state }
    private var privacy: Bool { s?.privacy ?? true }

    private var brainLabel: String {
        switch s?.voice_brain {
        case "live": return "GPT-Live"
        case "realtime": return "Realtime"
        case "basic": return "Basic"
        case "local": return "Local"
        case let b?: return b.capitalized
        default: return "—"
        }
    }

    private var activity: String {
        switch store.status {
        case .unreachable(let why): return why
        case .unknown: return "Looking for your Mac…"
        case .off: return "Switched off. Nothing can wake it."
        case .asleep: return "Sleeping. Say \"hey Vibey\" or tap Wake."
        case .awake:
            if s?.speaking == true { return "Talking" }
            if s?.recording == true { return "Hearing you" }
            return "Listening"
        }
    }

    private var statusColor: Color {
        switch store.status {
        case .awake: return Palette.live
        case .asleep: return Palette.duck
        case .unreachable: return Palette.bad
        default: return Palette.inkDim
        }
    }

    var body: some View {
        SpaceScreen {
            ScrollView {
                VStack(spacing: 18) {
                    hero
                    if cost?.budget?.level == "over" { budgetBanner }
                    primaryButton
                    if let pp = visibleProgress { powerProgress(pp) }
                    CameraCard()
                    privacyCard
                    volumeCard
                }
                .padding(.horizontal, 18)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
            .refreshable { await store.refresh(); await loadVolume() }
        }
        .task { await loadVolume() }
        .sheet(isPresented: $showBrain) {
            BrainSheet().environmentObject(store).presentationDetents([.large])
        }
        .sheet(isPresented: $showSpend) {
            SpendSheet().environmentObject(store).presentationDetents([.medium, .large])
        }
        .task {
            while !Task.isCancelled {
                if let c = try? await store.api.cost() { cost = c }
                try? await Task.sleep(for: .seconds(60))
            }
        }
        // The live meter: every new charge ticks the pill and flashes what it added.
        .task {
            while !Task.isCancelled {
                if let l = try? await store.api.costLive() {
                    if let was = live?.today, let now = l.today, now > was + 0.000001 {
                        let add = now - was
                        added = add < 0.01 ? String(format: "+%.1f¢", add * 100) : String(format: "+$%.2f", add)
                        Task { try? await Task.sleep(for: .seconds(2.5)); added = nil }
                    }
                    withAnimation(.easeOut(duration: 0.5)) { live = l }
                }
                try? await Task.sleep(for: .seconds(1.5))
            }
        }
    }

    /// Past the daily line: loud, but nothing stops. /sleep or the button does.
    private var budgetBanner: some View {
        HStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 22, weight: .bold))
            VStack(alignment: .leading, spacing: 2) {
                Text(String(format: "$%.2f today", cost?.today ?? 0))
                    .font(.system(.headline, design: .rounded).monospacedDigit())
                Text(String(format: "Past your $%.0f line. Still running — sleep it to stop.",
                            cost?.budget?.daily_cap ?? 3))
                    .font(.system(.caption, design: .rounded))
            }
            Spacer()
        }
        .foregroundStyle(.white)
        .padding(14)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Palette.bad))
    }

    private var hero: some View {
        VStack(spacing: 6) {
            RobotFace(status: store.status, eyesClosed: privacy, speaking: s?.speaking == true)
                .frame(maxWidth: 260)
                .padding(.top, 8)

            HStack(spacing: 8) {
                Circle().fill(statusColor)
                    .frame(width: 10, height: 10)
                    .shadow(color: statusColor, radius: 6)
                Text(store.status.title)
                    .font(.system(size: 40, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                    .contentTransition(.numericText())
            }
            Text(activity)
                .font(.system(.callout, design: .rounded))
                .foregroundStyle(.white.opacity(0.6))
                .multilineTextAlignment(.center)
                .lineLimit(2)

            HStack(spacing: 8) {
                Button { Haptics.tap(); showBrain = true } label: {
                    chip(icon: "waveform", text: brainLabel, on: store.status == .awake)
                }
                .buttonStyle(.plain)
                chip(icon: privacy ? "eye.slash.fill" : "eye.fill",
                     text: privacy ? "Eyes closed" : "Eyes open", on: !privacy)
                if s?.muted == true { chip(icon: "mic.slash.fill", text: "Muted", on: false) }
                if let today = live?.today ?? cost?.today {
                    Button { Haptics.tap(); showSpend = true } label: { spendChip(today) }
                        .buttonStyle(.plain)
                }
            }
            .padding(.top, 6)
        }
        .animation(.spring, value: store.status)
    }

    /// Green and breathing while a paid voice socket (Realtime or GPT-Live) is
    /// open; grey when nothing is billing. Tenths of a cent so each reply shows.
    private func spendChip(_ today: Double) -> some View {
        let open = live?.socket?.open == true
        return HStack(spacing: 6) {
            Circle().fill(open ? Palette.live : .white.opacity(0.35))
                .frame(width: 8, height: 8)
                .shadow(color: open ? Palette.live : .clear, radius: pulse ? 6 : 1)
                .scaleEffect(open && pulse ? 1.25 : 1)
            Text(String(format: "$%.3f today", today))
                .monospacedDigit()
                .contentTransition(.numericText(value: today))
            if let added {
                Text(added).foregroundStyle(Palette.live).transition(.opacity)
            }
        }
        .font(.system(.footnote, design: .rounded).weight(.semibold))
        .padding(.horizontal, 12).padding(.vertical, 7)
        .background(Capsule().fill(open ? Palette.live.opacity(0.16) : .white.opacity(0.08)))
        .overlay(Capsule().stroke(open ? Palette.live.opacity(0.6) : .white.opacity(0.15)))
        .foregroundStyle(open ? Palette.live : .white.opacity(0.75))
        .animation(.easeOut(duration: 0.3), value: added)
        .onAppear {
            withAnimation(.easeInOut(duration: 0.8).repeatForever(autoreverses: true)) { pulse = true }
        }
    }

    private func chip(icon: String, text: String, on: Bool) -> some View {
        Label(text, systemImage: icon)
            .font(.system(.footnote, design: .rounded).weight(.semibold))
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(Capsule().fill(on ? Palette.live.opacity(0.16) : .white.opacity(0.08)))
            .overlay(Capsule().stroke(on ? Palette.live.opacity(0.6) : .white.opacity(0.15)))
            .foregroundStyle(on ? Palette.live : .white.opacity(0.75))
    }

    @ViewBuilder
    private var primaryButton: some View {
        switch store.status {
        case .off:
            Button { store.setAwake(true) } label: {
                Label("Turn on", systemImage: "power")
            }
            .buttonStyle(PillButtonStyle())
            .disabled(store.busy)
        case .asleep:
            Button { store.setAwake(true) } label: {
                Label(store.busy ? "Waking…" : "Wake", systemImage: "sun.max.fill")
            }
            .buttonStyle(PillButtonStyle())
            .disabled(store.busy)
        case .awake:
            Button { store.setAwake(false) } label: {
                Label(store.busy ? "Settling…" : "Sleep", systemImage: "moon.zzz.fill")
            }
            .buttonStyle(PillButtonStyle(fill: .white))
            .disabled(store.busy)
        default:
            Button { Task { await store.refresh() } } label: {
                Label("Retry", systemImage: "arrow.clockwise")
            }
            .buttonStyle(PillButtonStyle(fill: .white.opacity(0.85)))
        }
    }

    /// Live while turning on; a failure stays up two minutes, a success a few seconds.
    private var visibleProgress: PowerProgress? {
        guard let pp = s?.power_progress else { return nil }
        let age = Date().timeIntervalSince1970 - pp.at
        if pp.error != nil { return age < 120 ? pp : nil }
        return (!pp.done || age < 8) ? pp : nil
    }

    private func powerProgress(_ pp: PowerProgress) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(pp.error != nil ? "Turning on stopped" : pp.done ? "Vibey is on" : "Turning on…")
                .font(.system(.headline, design: .rounded))
            ForEach(Array(pp.steps.enumerated()), id: \.offset) { _, step in
                HStack(spacing: 10) {
                    Group {
                        switch step.state {
                        case "ok": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                        case "fail": Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
                        case "doing": ProgressView().controlSize(.small)
                        default: Image(systemName: "circle").foregroundStyle(Palette.inkDim)
                        }
                    }
                    .frame(width: 20)
                    Text(step.label)
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(step.state == "todo" ? Palette.inkDim : Palette.ink)
                }
            }
            if let why = pp.error {
                Text(why)
                    .font(.system(.subheadline, design: .rounded).weight(.semibold))
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .shell()
    }

    private var privacyCard: some View {
        HStack(spacing: 14) {
            Image(systemName: privacy ? "eye.slash.fill" : "eye.fill")
                .font(.title2)
                .foregroundStyle(privacy ? Palette.ink : Palette.live)
                .frame(width: 44, height: 44)
                .background(Circle().fill(privacy ? Palette.duck : Palette.ink))
            VStack(alignment: .leading, spacing: 2) {
                Text("Privacy").font(.system(.headline, design: .rounded))
                Text(privacy ? "Camera off. No photos, no looking." : "Vibey can see the room.")
                    .font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(Palette.inkDim)
            }
            Spacer()
            Toggle("", isOn: Binding(
                get: { privacy },
                set: { on in store.run(on ? "Eyes closed" : "Eyes open") { try await $0.setPrivacy(on) } }))
                .labelsHidden()
                .tint(Palette.duck)
        }
        .shell()
        .disabled(isDown)
    }

    private var volumeCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Volume").font(.system(.headline, design: .rounded))
                Spacer()
                Text(volumeKnown ? "\(Int(volume))" : "–")
                    .font(.system(.headline, design: .rounded).monospacedDigit())
                    .foregroundStyle(Palette.inkDim)
            }
            HStack(spacing: 12) {
                Image(systemName: "speaker.fill").foregroundStyle(Palette.inkDim)
                Slider(value: $volume, in: 0...100, step: 1) { editing in
                    if !editing {
                        let v = Int(volume)
                        store.run { try await $0.setVolume(v) }
                    }
                }
                .tint(Palette.ink)
                Image(systemName: "speaker.wave.3.fill").foregroundStyle(Palette.inkDim)
            }
            Text("Robot speaker — Vibey's voice and sounds")
                .font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
            if let m = music, m.state == "playing" || m.state == "paused" {
                Divider().padding(.vertical, 4)
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Music on Mac").font(.system(.headline, design: .rounded))
                        Text([m.track, m.artist].compactMap { $0 }.joined(separator: " · "))
                            .font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
                            .lineLimit(1)
                    }
                    Spacer()
                    Button {
                        Haptics.tap()
                        Task { music = try? await store.api.macMusic(["action": m.state == "playing" ? "pause" : "play"]) }
                    } label: {
                        Image(systemName: m.state == "playing" ? "pause.fill" : "play.fill")
                            .frame(width: 36, height: 36)
                            .background(Circle().fill(Palette.ink.opacity(0.08)))
                    }
                    .buttonStyle(.plain)
                    Text("\(Int(musicVol))")
                        .font(.system(.headline, design: .rounded).monospacedDigit())
                        .foregroundStyle(Palette.inkDim)
                }
                HStack(spacing: 12) {
                    Image(systemName: "music.note").foregroundStyle(Palette.inkDim)
                    Slider(value: $musicVol, in: 0...100, step: 1) { editing in
                        if !editing {
                            let v = Int(musicVol)
                            Task { music = try? await store.api.macMusic(["volume": v]) }
                        }
                    }
                    .tint(Palette.ink)
                }
            }
        }
        .shell()
        .disabled(isDown)
        .task {
            while !Task.isCancelled {
                if let m = try? await store.api.macMusic() {
                    music = m
                    if let v = m.volume { musicVol = Double(v) }
                }
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    private var isDown: Bool {
        if case .unreachable = store.status { return true }
        return store.status == .unknown
    }

    private func loadVolume() async {
        if let v = try? await store.api.volume() {
            volume = v
            volumeKnown = true
        }
    }
}
