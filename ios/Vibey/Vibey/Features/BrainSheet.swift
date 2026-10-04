import SwiftUI

/// GET :8772/brain — every voice brain, the models behind it, and where
/// everything is running.
struct BrainInfo: Decodable {
    struct Option: Decodable {
        var label: String
        var blurb: String?
        var models: [String]?
    }
    var brain: String
    var options: [String: Option]
    var in_use: String?
    var awake: Bool?
    var stage: Int?
    var mic_source: String?
    var speaker_source: String?
    var vision_model: String?
    var text_model: String?
}

extension VibeyAPI {
    func brainInfo() async throws -> BrainInfo {
        guard let u = URL(string: "http://\(host):8772/brain") else {
            throw APIError.unreachable("Bad address: \(host)")
        }
        var req = URLRequest(url: u, timeoutInterval: 10)
        if !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, resp) = try await URLSession.shared.data(for: req)
        if (resp as? HTTPURLResponse)?.statusCode == 401 { throw APIError.unauthorized }
        return try JSONDecoder().decode(BrainInfo.self, from: data)
    }
}

/// Tap the brain pill: pick the voice brain, pick the stage, and see which
/// model is doing what right now.
struct BrainSheet: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var info: BrainInfo?
    @State private var problem: String?
    @State private var switching: String?

    private let order = ["basic", "realtime", "live"]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if let i = info {
                        section("Voice brain")
                        ForEach(order.filter { i.options[$0] != nil }, id: \.self) { key in
                            brainRow(key, i.options[key]!, current: i.brain == key)
                        }
                        section("Where it runs")
                        stages(i)
                        section("Right now")
                        now(i)
                    } else if let problem {
                        Text(problem).foregroundStyle(Palette.bad)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
                    }
                }
                .padding(18)
            }
            .navigationTitle("Brain")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task { await load() }
            .refreshable { await load() }
        }
    }

    private func load() async {
        do { info = try await store.api.brainInfo(); problem = nil }
        catch { problem = error.localizedDescription }
    }

    private func section(_ t: String) -> some View {
        Text(t.uppercased()).font(.caption.weight(.heavy)).foregroundStyle(.secondary).padding(.top, 4)
    }

    private func brainRow(_ key: String, _ o: BrainInfo.Option, current: Bool) -> some View {
        Button {
            guard !current else { return }
            Haptics.tap()
            switching = key
            Task {
                do { try await store.api.setBrain(key); Haptics.ok() }
                catch { Haptics.fail(); store.flash(error.localizedDescription) }
                await load(); await store.refresh()
                switching = nil
            }
        } label: {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: current ? "largecircle.fill.circle" : "circle")
                    .font(.title3).foregroundStyle(current ? Palette.live : .secondary)
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text(o.label).font(.headline)
                        if switching == key { ProgressView().controlSize(.small) }
                    }
                    if let b = o.blurb { Text(b).font(.subheadline).foregroundStyle(.secondary) }
                    if let m = o.models, !m.isEmpty {
                        Text(m.joined(separator: " + "))
                            .font(.caption.monospaced())
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(Capsule().fill(Color.secondary.opacity(0.15)))
                    }
                }
                Spacer()
            }
            .padding(14)
            .background(RoundedRectangle(cornerRadius: 16)
                .fill(current ? Palette.live.opacity(0.12) : Color.secondary.opacity(0.08)))
            .overlay(RoundedRectangle(cornerRadius: 16)
                .stroke(current ? Palette.live.opacity(0.6) : .clear))
        }
        .buttonStyle(.plain)
    }

    private func stages(_ i: BrainInfo) -> some View {
        let all: [(Int, String, String)] = [
            (1, "Robot alone", "Body, camera and the robot's own face tracking. Nothing on the Mac, no cloud."),
            (2, "+ Mac, offline", "Wake word, hearing, faces and memory run on the Mac. No cloud voice, so no spend."),
            (3, "+ Cloud", "Full conversation with the voice brain above. Texts too."),
        ]
        return VStack(spacing: 8) {
            ForEach(all, id: \.0) { n, title, hint in
                let on = (i.stage ?? 3) == n
                Button {
                    guard !on else { return }
                    Haptics.tap()
                    store.run("Stage \(n)") { try await $0.setStage(n) }
                    Task { try? await Task.sleep(for: .seconds(2)); await load() }
                } label: {
                    HStack(alignment: .top, spacing: 12) {
                        Text("\(n)").font(.headline.monospacedDigit())
                            .frame(width: 28, height: 28)
                            .background(Circle().fill(on ? Palette.live : Color.secondary.opacity(0.2)))
                            .foregroundStyle(on ? Palette.ink : .primary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(title).font(.subheadline.weight(.semibold))
                            Text(hint).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .padding(12)
                    .background(RoundedRectangle(cornerRadius: 14)
                        .fill(on ? Palette.live.opacity(0.12) : Color.secondary.opacity(0.08)))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func now(_ i: BrainInfo) -> some View {
        let rows: [(String, String)] = [
            ("Status", (i.awake ?? false) ? "awake · \(i.options[i.in_use ?? i.brain]?.label ?? i.brain)" : "asleep"),
            ("Hears with", (i.mic_source ?? "?") == "laptop" ? "Mac mic" : "robot mic"),
            ("Speaks through", (i.speaker_source ?? "?") == "laptop" ? "Mac speaker" : "robot speaker"),
            ("Texts answered by", i.text_model ?? "?"),
            ("Looks with", i.vision_model ?? "?"),
            ("Wake word", "whisper, on the Mac (local)"),
        ]
        return VStack(spacing: 0) {
            ForEach(rows, id: \.0) { k, v in
                HStack {
                    Text(k).font(.subheadline).foregroundStyle(.secondary)
                    Spacer()
                    Text(v).font(.subheadline.monospaced()).multilineTextAlignment(.trailing)
                }
                .padding(.vertical, 8)
                Divider()
            }
        }
    }
}
