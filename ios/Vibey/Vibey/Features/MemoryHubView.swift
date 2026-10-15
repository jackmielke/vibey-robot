import SwiftUI
import AVKit

/// Everything Vibey keeps: written memories, the faces it knows, and clips.
struct MemoryHubView: View {
    enum Pane: String, CaseIterable { case memories = "Memories", friends = "Friends", clips = "Clips" }
    @State private var pane: Pane = Self.initialPane

    private static var initialPane: Pane {
        let a = ProcessInfo.processInfo.arguments
        guard let i = a.firstIndex(of: "-pane"), i + 1 < a.count else { return .memories }
        return Pane(rawValue: a[i + 1].capitalized) ?? .memories
    }

    var body: some View {
        SpaceScreen {
            VStack(spacing: 12) {
                ScreenTitle(title: "Memory")
                    .padding(.horizontal, 18)
                HStack(spacing: 6) {
                    ForEach(Pane.allCases, id: \.self) { p in
                        Button { Haptics.soft(); withAnimation(.spring(response: 0.3)) { pane = p } } label: {
                            Text(p.rawValue)
                                .font(.system(.subheadline, design: .rounded).weight(.bold))
                                .frame(maxWidth: .infinity, minHeight: 38)
                                .background(Capsule().fill(pane == p ? Palette.duck : .white.opacity(0.07)))
                                .foregroundStyle(pane == p ? Palette.ink : .white.opacity(0.75))
                        }
                        .buttonStyle(PressScale())
                    }
                }
                .padding(.horizontal, 18)

                switch pane {
                case .memories: MemoriesView()
                case .friends: FriendsView()
                case .clips: ClipsView()
                }
            }
        }
    }
}

/// Inline error with a retry, used by every list.
struct ErrorNote: View {
    let text: String
    var retry: (() -> Void)? = nil
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(Palette.bad)
            Text(text).font(.system(.footnote, design: .rounded))
                .foregroundStyle(.white.opacity(0.85))
                .frame(maxWidth: .infinity, alignment: .leading)
            if let retry {
                Button("Retry", action: retry)
                    .font(.system(.footnote, design: .rounded).weight(.bold))
                    .foregroundStyle(Palette.duck)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Palette.bad.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(Palette.bad.opacity(0.35)))
    }
}

// MARK: - Friends

struct FriendsView: View {
    @EnvironmentObject var store: Store
    @State private var friends: [Friend] = []
    @State private var loading = false
    @State private var error: String?
    @State private var naming: Friend?
    @State private var newName = ""
    @State private var opened: Friend?

    private let cols = [GridItem(.adaptive(minimum: 100), spacing: 12)]

    var body: some View {
        ScrollView {
            VStack(spacing: 14) {
                HStack {
                    Text(summary).font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.white.opacity(0.55))
                    Spacer()
                }
                if let error { ErrorNote(text: error) { Task { await load() } } }
                if loading && friends.isEmpty { ProgressView().tint(.white).padding(.top, 40) }
                LazyVGrid(columns: cols, spacing: 12) {
                    ForEach(friends) { f in
                        Button { Haptics.soft(); opened = f } label: { card(f) }
                            .buttonStyle(PressScale())
                            .contextMenu {
                                Button(f.name == nil ? "Name them" : "Rename", systemImage: "pencil") {
                                    newName = f.name ?? ""; naming = f
                                }
                            }
                    }
                }
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
        .refreshable { await load() }
        .task { await load() }
        .sheet(item: $opened) { f in
            FriendProfileView(friendID: f.id, seed: f, others: friends.filter { $0.id != f.id }) {
                Task { await load() }
            }
            .presentationDetents([.large])
            .presentationBackground(Palette.space)
        }
        .alert(naming?.name == nil ? "Who is this?" : "Rename", isPresented: Binding(
            get: { naming != nil }, set: { if !$0 { naming = nil } })) {
            TextField("Name", text: $newName)
            Button("Save") { if let f = naming { rename(f, newName) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Vibey greets people by this name.")
        }
    }

    private var summary: String {
        if friends.isEmpty { return loading ? "Loading…" : "No faces yet" }
        let named = friends.filter { $0.name?.isEmpty == false }.count
        return "\(named) friends · \(friends.count - named) strangers"
    }

    private func card(_ f: Friend) -> some View {
        VStack(spacing: 8) {
            FaceThumb(dataURI: f.snapshot)
                .frame(width: 76, height: 76)
                .clipShape(Circle())
                .overlay(Circle().stroke(f.name == nil ? Palette.inkDim.opacity(0.3) : Palette.duck, lineWidth: 3))
            Text(f.name ?? "Stranger")
                .font(.system(.subheadline, design: .rounded).weight(.bold))
                .foregroundStyle(f.name == nil ? Palette.inkDim : Palette.ink)
                .lineLimit(1)
            Text("seen \(f.times_seen ?? 0)×")
                .font(.system(.caption2, design: .rounded))
                .foregroundStyle(Palette.inkDim)
        }
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 24, style: .continuous)
            .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom], startPoint: .top, endPoint: .bottom)))
        .shadow(color: .white.opacity(0.16), radius: 12)
    }

    private func load() async {
        loading = true
        do { friends = try await store.api.friends(); error = nil }
        catch where error.isCancellation {}
        catch { self.error = error.localizedDescription }
        loading = false
    }

    private func rename(_ f: Friend, _ name: String) {
        let n = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty, n != f.name else { return }
        Task {
            do {
                try await store.api.nameFriend(f.id, n)
                Haptics.ok(); store.flash("Hi, \(n)")
                await load()
            } catch where !error.isCancellation {
                Haptics.fail(); store.flash(error.localizedDescription)
            } catch {}
        }
    }
}

/// Decodes a `data:image/jpeg;base64,…` snapshot once.
struct FaceThumb: View {
    let dataURI: String?
    @State private var img: UIImage?
    var body: some View {
        ZStack {
            Palette.ink.opacity(0.08)
            if let img { Image(uiImage: img).resizable().scaledToFill() }
            else { Image(systemName: "person.fill").font(.system(size: 30)).foregroundStyle(Palette.inkDim.opacity(0.5)) }
        }
        .task(id: dataURI) {
            guard let s = dataURI, let comma = s.firstIndex(of: ",") else { return }
            let b64 = String(s[s.index(after: comma)...])
            img = await Task.detached { Data(base64Encoded: b64).flatMap(UIImage.init(data:)) }.value
        }
    }
}

// MARK: - Clips

struct ClipsView: View {
    @EnvironmentObject var store: Store
    @State private var clips: [Capture] = []
    @State private var loading = false
    @State private var error: String?
    @State private var opening: String?
    @State private var player: PlayerItem?

    struct PlayerItem: Identifiable { let id = UUID(); let url: URL }

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                HStack {
                    Text(clips.isEmpty ? (loading ? "Loading…" : "No clips yet. Ask Vibey on Telegram for /clip.")
                         : "\(clips.count) clips and photos")
                        .font(.system(.subheadline, design: .rounded))
                        .foregroundStyle(.white.opacity(0.55))
                    Spacer()
                }
                if let error { ErrorNote(text: error) { Task { await load() } } }
                ForEach(clips) { c in
                    Button { open(c) } label: { row(c) }.buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
        .refreshable { await load() }
        .task { await load() }
        .fullScreenCover(item: $player) { p in
            ZStack(alignment: .topTrailing) {
                Color.black.ignoresSafeArea()
                if p.url.pathExtension.lowercased() == "mp4" {
                    VideoPlayer(player: AVPlayer(url: p.url)).ignoresSafeArea()
                } else if let img = UIImage(contentsOfFile: p.url.path) {
                    Image(uiImage: img).resizable().scaledToFit()
                }
                Button { player = nil } label: {
                    Image(systemName: "xmark").font(.system(size: 16, weight: .bold))
                        .frame(width: 44, height: 44)
                        .background(Circle().fill(.ultraThinMaterial)).foregroundStyle(.white)
                }
                .padding(18)
            }
        }
    }

    private func row(_ c: Capture) -> some View {
        HStack(spacing: 14) {
            Image(systemName: c.name.hasSuffix(".mp4") ? "film.fill" : "photo.fill")
                .font(.system(size: 18, weight: .bold))
                .frame(width: 44, height: 44)
                .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Palette.ink))
                .foregroundStyle(Palette.live)
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.title(c.name)).font(.system(.headline, design: .rounded))
                Text(Self.subtitle(c)).font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
            }
            Spacer()
            if opening == c.name { ProgressView() }
            else { Image(systemName: "play.circle.fill").font(.title2).foregroundStyle(Palette.ink) }
        }
        .shell(padding: 12)
    }

    /// clip_20260926_140542.mp4 → "Sat, Sep 26 · 2:05 PM"
    static func title(_ name: String) -> String {
        let digits = name.components(separatedBy: CharacterSet.decimalDigits.inverted).filter { !$0.isEmpty }
        if digits.count >= 2 {
            let f = DateFormatter(); f.dateFormat = "yyyyMMddHHmmss"
            if let d = f.date(from: digits[0] + digits[1].prefix(6)) {
                let o = DateFormatter(); o.dateFormat = "EEE, MMM d · h:mm a"
                return o.string(from: d)
            }
        }
        return name
    }

    static func subtitle(_ c: Capture) -> String {
        let kind = c.name.hasPrefix("timelapse") ? "Timelapse" : c.name.hasSuffix(".mp4") ? "Clip" : "Photo"
        guard let s = c.size else { return kind }
        return "\(kind) · " + ByteCountFormatter.string(fromByteCount: Int64(s), countStyle: .file)
    }

    private func load() async {
        loading = true
        do { clips = try await store.api.captures(); error = nil }
        catch where error.isCancellation {}
        catch { self.error = error.localizedDescription }
        loading = false
    }

    private func open(_ c: Capture) {
        Haptics.soft()
        opening = c.name
        Task {
            do { player = PlayerItem(url: try await store.api.captureFile(c.name)) }
            catch where !error.isCancellation { Haptics.fail(); store.flash(error.localizedDescription) }
            catch {}
            opening = nil
        }
    }
}
