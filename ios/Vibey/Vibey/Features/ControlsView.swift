import SwiftUI

/// Every switch the desktop control center has, grouped the same way:
/// brain, stage, hearing, talking, seeing, wake, privacy, spend, connection.
/// Privacy (the camera) stays on Home on purpose.
struct ControlsView: View {
    @EnvironmentObject var store: Store
    @State private var d: Dials?
    @State private var cost: Cost?
    @State private var startVol: Double = 60
    @State private var loadError: String?

    private var s: VibeyState? { store.state }
    private var off: Bool { s?.off ?? false }
    private var scribeOn: Bool { s?.scribe?.on ?? false }
    private var frontDeskTrailing: String? {
        guard let f = s?.frontdesk, (f.guests ?? 0) > 0 else { return nil }
        return "\(f.checked_in ?? 0)/\(f.guests ?? 0) in"
    }

    var body: some View {
        NavigationStack {
            SpaceScreen {
                ScrollView {
                    VStack(spacing: 12) {
                        ScreenTitle(title: "Controls", subtitle: off ? "Vibey is off. Switch it on from Home." : "Everything about how Vibey runs")
                        if let loadError {
                            Text(loadError).font(.system(.footnote, design: .rounded))
                                .foregroundStyle(Palette.bad).frame(maxWidth: .infinity, alignment: .leading)
                        }

                        SectionLabel(text: "Brain")
                        VStack(alignment: .leading, spacing: 10) {
                            ChoicePills(options: [("basic", "Basic"), ("realtime", "Realtime"), ("live", "GPT-Live")],
                                        selected: d?.voice_brain ?? s?.voice_brain) { b in
                                let label = ["basic": "Basic", "realtime": "Realtime", "live": "GPT-Live"][b] ?? b
                                store.run("\(label) brain") { try await $0.setBrain(b) }
                                d?.voice_brain = b
                            }
                            Text({
                                switch d?.voice_brain ?? s?.voice_brain {
                                case "basic": return "Realtime with no tools, memory or extras. Just talks — and can look."
                                case "realtime": return "One model hears, thinks and speaks. All the tools."
                                default: return "Voice layer plus a smarter backend brain."
                                }
                            }())
                                .font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
                            RowDivider().padding(.leading, -46)
                            SwitchRow(icon: "text.bubble.fill", title: "Think aloud",
                                      hint: "Says what it's doing as it goes",
                                      isOn: d?.think_aloud ?? false) { dial("think_aloud", $0) }
                        }
                        .shell()

                        SectionLabel(text: "Stage", trailing: "now \(s?.stage ?? 3)")
                        VStack(alignment: .leading, spacing: 10) {
                            ChoicePills(options: [(1, "1 Robot"), (2, "2 + Mac"), (3, "3 + Cloud")],
                                        selected: s?.stage) { n in
                                store.run("Stage \(n)") { try await $0.setStage(n) }
                            }
                            Text(stageHint).font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
                        }
                        .shell()
                        .disabled(off)

                        SectionLabel(text: "Hearing")
                        VStack(spacing: 6) {
                            HStack {
                                Text("Ears").font(.system(.body, design: .rounded).weight(.semibold))
                                Spacer()
                                ChoicePills(options: [("robot", "Robot"), ("laptop", "MacBook")],
                                            selected: d?.mic_source) { dial("mic_source", $0) }
                                    .frame(width: 190)
                            }
                            .padding(.vertical, 4)
                            RowDivider()
                            SwitchRow(icon: "ear.fill", title: "Listening",
                                      hint: "Hears you and answers out loud",
                                      isOn: d?.listening ?? false) { dial("listening", $0) }
                            RowDivider()
                            SwitchRow(icon: "mic.slash.fill", title: "Mute mic",
                                      hint: "Hears nothing at all", tint: Palette.bad,
                                      isOn: d?.muted ?? false) { dial("muted", $0) }
                            RowDivider()
                            SwitchRow(icon: "pencil.and.list.clipboard", title: "Take notes",
                                      hint: scribeOn ? "Scribing for \(Int(s?.scribe?.minutes ?? 0)) min. Summary texts you when off."
                                                     : "Stays silent, writes everything down",
                                      isOn: scribeOn) { on in
                                store.run(on ? "Taking notes" : "Notes on their way") { try await $0.setScribe(on) }
                            }
                        }
                        .shell(padding: 14)
                        .disabled(off)

                        SectionLabel(text: "Talking")
                        VStack(spacing: 6) {
                            SwitchRow(icon: "waveform", title: "Voice",
                                      hint: "Talk back out loud (off = stage 2)",
                                      isOn: d?.voice ?? true) { dial("voice", $0) }
                            RowDivider()
                            HStack {
                                Text("Mouth").font(.system(.body, design: .rounded).weight(.semibold))
                                Spacer()
                                ChoicePills(options: [("robot", "Robot"), ("laptop", "MacBook")],
                                            selected: d?.speaker_source) { dial("speaker_source", $0) }
                                    .frame(width: 190)
                            }
                            .padding(.vertical, 4)
                            RowDivider()
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Text("Starts at").font(.system(.body, design: .rounded).weight(.semibold))
                                    Spacer()
                                    Text("\(Int(startVol))").font(.system(.body, design: .rounded).monospacedDigit())
                                        .foregroundStyle(Palette.inkDim)
                                }
                                Slider(value: $startVol, in: 0...100, step: 5) { editing in
                                    if !editing { dial("start_volume", Int(startVol)) }
                                }
                                .tint(Palette.ink)
                                Text("Volume Vibey wakes up at").font(.system(.caption, design: .rounded))
                                    .foregroundStyle(Palette.inkDim)
                            }
                            .padding(.vertical, 4)
                        }
                        .shell(padding: 14)
                        .disabled(off)

                        SectionLabel(text: "Seeing & waking")
                        VStack(spacing: 6) {
                            SwitchRow(icon: "video.fill", title: "Camera",
                                      hint: "Off = no video at all, on the robot or the Mac",
                                      isOn: d?.camera ?? store.cameraOn) { dial("camera", $0) }
                            RowDivider()
                            SwitchRow(icon: "face.smiling.inverse", title: "Face tracking",
                                      hint: "Turns to follow you",
                                      isOn: d?.face_tracking ?? false) { dial("face_tracking", $0) }
                            RowDivider()
                            SwitchRow(icon: "quote.bubble.fill", title: "Wake word",
                                      hint: "\"Hey Vibey\" wakes it",
                                      isOn: s?.switches?["wake"] ?? false) { sw("wake", $0) }
                            RowDivider()
                            SwitchRow(icon: "hands.clap.fill", title: "Clap to wake",
                                      hint: "Two claps wakes it (doors can too)",
                                      isOn: s?.switches?["claps"] ?? false) { sw("claps", $0) }
                        }
                        .shell(padding: 14)
                        .disabled(off)

                        SectionLabel(text: "Front desk", trailing: frontDeskTrailing)
                        SwitchRow(icon: "qrcode.viewfinder", title: "Front desk",
                                  hint: store.privacy ? "Needs the camera: turn privacy off on Home first"
                                                      : "Scans Luma ticket QRs at the door",
                                  tint: Palette.live, isOn: s?.frontdesk?.on ?? false) { on in
                            store.run(on ? "Front desk on" : "Front desk off") { try await $0.setFrontDesk(on) }
                        }
                        .shell(padding: 14)
                        .disabled(off)

                        SectionLabel(text: "Memory")
                        SwitchRow(icon: "theatermasks.fill", title: "Incognito",
                                  hint: "Remembers nothing tonight, no new faces",
                                  tint: Palette.live, isOn: d?.incognito ?? s?.incognito ?? false) { on in
                            store.run(on ? "Incognito on" : "Incognito off") { try await $0.setIncognito(on) }
                            d?.incognito = on
                        }
                        .shell(padding: 14)

                        SectionLabel(text: "Spend")
                        costCard

                        SectionLabel(text: "Connection")
                        NavigationLink { SettingsView() } label: {
                            HStack(spacing: 12) {
                                Image(systemName: "antenna.radiowaves.left.and.right")
                                    .font(.system(size: 15, weight: .bold))
                                    .frame(width: 34, height: 34)
                                    .background(Circle().fill(Palette.ink))
                                    .foregroundStyle(Palette.live)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text("Mac & token").font(.system(.body, design: .rounded).weight(.semibold))
                                    Text(store.host).font(.system(.caption, design: .monospaced))
                                        .foregroundStyle(Palette.inkDim)
                                }
                                Spacer()
                                Image(systemName: "chevron.right").foregroundStyle(Palette.inkDim)
                            }
                            .shell(padding: 14)
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 18)
                    .padding(.bottom, 24)
                }
                .scrollIndicators(.hidden)
                .refreshable { await load() }
            }
            .toolbar(.hidden, for: .navigationBar)
        }
        .task { await load() }
    }

    private var stageHint: String {
        switch s?.stage ?? 3 {
        case 1: return "Robot alone: body, camera and onboard tracking. No Mac brain, no cloud."
        case 2: return "Plus the Mac: wake word, faces, moves, notes. No cloud voice."
        default: return "Everything: cloud voice, Telegram, the lot."
        }
    }

    private var costCard: some View {
        let b = cost?.budget
        let level = b?.level ?? "ok"
        let tint: Color = level == "over" ? Palette.bad : level == "warn" ? Palette.beak : Palette.live
        let money: (Double?) -> String = { $0.map { String(format: "$%.2f", $0) } ?? "–" }
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(money(cost?.today))
                        .font(.system(size: 34, weight: .heavy, design: .rounded).monospacedDigit())
                        .foregroundStyle(level == "over" ? Palette.bad : Palette.ink)
                    Text(level == "over" ? "past your \(money(b?.daily_cap)) line — still running"
                         : "today of \(money(b?.daily_cap))")
                        .font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 2) {
                    Text(money(cost?.week))
                        .font(.system(.title3, design: .rounded).weight(.bold).monospacedDigit())
                    Text("7 days").font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
                    Text("\(money(cost?.month)) / \(money(b?.monthly_cap)) month")
                        .font(.system(.caption, design: .rounded).monospacedDigit())
                        .foregroundStyle(Palette.inkDim)
                }
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Palette.inkDim.opacity(0.18))
                    Capsule().fill(tint)
                        .frame(width: g.size.width * min(1, max(0, b?.fraction ?? 0)))
                }
            }
            .frame(height: 6)
            let rows = (cost?.by_source_today ?? [:]).filter { $0.value >= 0.005 }
                .sorted { $0.value > $1.value }
            ForEach(rows, id: \.key) { k, v in
                HStack {
                    Text(Cost.labels[k] ?? k).font(.system(.subheadline, design: .rounded))
                    Spacer()
                    Text(money(v)).font(.system(.subheadline, design: .rounded).monospacedDigit())
                }
            }
            if let n = cost?.unpriced_today, n > 0 {
                Text("+\(n) calls with no known price")
                    .font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
            }
            if let bl = cost?.billed {
                if bl.available == true {
                    Text("OpenAI billed, all apps: \(money(bl.today)) today · \(money(bl.month)) month")
                        .font(.system(.caption, design: .rounded).monospacedDigit())
                        .foregroundStyle(Palette.inkDim)
                } else if let hint = bl.hint {
                    Text(hint).font(.system(.caption2, design: .rounded)).foregroundStyle(Palette.inkDim)
                }
            }
            if b?.guard_off == true {
                Text("Budget guard off today").font(.system(.caption, design: .rounded))
                    .foregroundStyle(Palette.beak)
            }
        }
        .shell()
    }

    private func dial(_ key: String, _ value: Any) {
        Haptics.tap()
        Task {
            do {
                d = try await store.api.setDials([key: value])
                Haptics.ok()
            } catch where !error.isCancellation {
                Haptics.fail(); store.flash(error.localizedDescription)
            } catch {}
            await store.refresh()
        }
    }

    private func sw(_ name: String, _ on: Bool) {
        store.run(nil) { try await $0.setSwitch(name, on) }
    }

    private func load() async {
        do {
            let fresh = try await store.api.dials()
            d = fresh
            if let v = fresh.start_volume { startVol = Double(v) }
            loadError = nil
        } catch where error.isCancellation {
        } catch {
            loadError = error.localizedDescription
        }
        cost = try? await store.api.cost()
    }
}
