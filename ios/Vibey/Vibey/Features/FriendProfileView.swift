import SwiftUI

/// One friend: basics, the notes Vibey keeps about them, and every photo the
/// face service holds. Photos are recognition samples, so deleting one or
/// filing it under someone else is how you correct who Vibey thinks is who.
///
/// Notes are ordinary memories (the Memories tab) that mention this friend by
/// name, so the brain already reads them. Adding a note writes a memory that
/// starts with their name.
struct FriendProfileView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    let friendID: String
    var seed: Friend? = nil
    var others: [Friend] = []
    var onChange: () -> Void = {}

    @State private var profile: FriendProfile?
    @State private var notes: [Memory] = []
    @State private var error: String?
    @State private var loading = false
    @State private var renaming = false
    @State private var newName = ""
    @State private var addingNote = false
    @State private var noteText = ""
    @State private var picked: FacePhoto?

    private let cols = [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8),
                        GridItem(.flexible(), spacing: 8)]

    private var name: String? {
        let n = profile?.name ?? seed?.name
        return (n?.isEmpty == false) ? n : nil
    }
    private var photos: [FacePhoto] { profile?.samples ?? [] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark").font(.system(size: 14, weight: .bold))
                            .frame(width: 36, height: 36)
                            .background(Circle().fill(.white.opacity(0.1)))
                            .foregroundStyle(.white)
                    }
                    .accessibilityLabel("Close")
                }
                header
                if let error { ErrorNote(text: error) { Task { await load() } } }
                stats
                photosSection
                notesSection
            }
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .padding(.bottom, 30)
        }
        .scrollIndicators(.hidden)
        .refreshable { await load() }
        .task { await load() }
        .alert(name == nil ? "Who is this?" : "Rename", isPresented: $renaming) {
            TextField("Name", text: $newName)
            Button("Save") { rename(newName) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Vibey greets people by this name.")
        }
        .alert("Note about \(name ?? "them")", isPresented: $addingNote) {
            TextField("Loves oat milk lattes", text: $noteText)
            Button("Save") { addNote(noteText) }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Vibey keeps this as a memory and brings it up with them.")
        }
        .sheet(item: $picked) { p in
            PhotoActionsSheet(photo: p, current: name, others: others) { action in
                apply(action, to: p)
            }
            .presentationDetents([.medium, .large])
            .presentationBackground(.ultraThinMaterial)
        }
    }

    // MARK: Sections

    private var header: some View {
        HStack(spacing: 16) {
            FaceThumb(dataURI: profile?.snapshot ?? photos.first?.snapshot ?? seed?.snapshot)
                .frame(width: 92, height: 92)
                .clipShape(Circle())
                .overlay(Circle().stroke(name == nil ? Palette.inkDim.opacity(0.4) : Palette.duck, lineWidth: 3))
            VStack(alignment: .leading, spacing: 8) {
                Text(name ?? "Stranger")
                    .font(.system(size: 30, weight: .heavy, design: .rounded))
                    .foregroundStyle(.white)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
                Button {
                    Haptics.soft(); newName = name ?? ""; renaming = true
                } label: {
                    Label(name == nil ? "Name them" : "Rename", systemImage: "pencil")
                        .font(.system(.subheadline, design: .rounded).weight(.bold))
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(Capsule().fill(name == nil ? Palette.duck : .white.opacity(0.1)))
                        .foregroundStyle(name == nil ? Palette.ink : .white)
                }
                .buttonStyle(PressScale())
            }
            Spacer(minLength: 0)
        }
    }

    private var stats: some View {
        HStack(spacing: 0) {
            stat("Seen", "\(profile?.times_seen ?? seed?.times_seen ?? 0)×")
            Divider().frame(height: 34)
            stat("First", Self.day(profile?.first_seen))
            Divider().frame(height: 34)
            stat("Last", Self.ago(profile?.last_seen))
        }
        .shell(padding: 14)
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(spacing: 3) {
            Text(value).font(.system(.headline, design: .rounded)).lineLimit(1).minimumScaleFactor(0.7)
            Text(label).font(.system(.caption2, design: .rounded).weight(.bold))
                .textCase(.uppercase).foregroundStyle(Palette.inkDim)
        }
        .frame(maxWidth: .infinity)
    }

    private var notesSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                SectionLabel(text: "Notes", trailing: notes.isEmpty ? nil : "\(notes.count)")
                if name != nil {
                    Button { Haptics.tap(); noteText = ""; addingNote = true } label: {
                        Label("Add", systemImage: "plus")
                            .font(.system(.caption, design: .rounded).weight(.bold))
                            .padding(.horizontal, 12).padding(.vertical, 7)
                            .background(Capsule().fill(Palette.duck))
                            .foregroundStyle(Palette.ink)
                    }
                    .accessibilityLabel("Add note")
                    .padding(.top, 6)
                }
            }
            if name == nil {
                hint("Name them first, then Vibey can keep notes about them.")
            } else if notes.isEmpty {
                hint(loading ? "Loading…" : "Nothing yet. Things Vibey remembers that mention \(name!) show up here.")
            }
            ForEach(notes) { m in
                VStack(alignment: .leading, spacing: 6) {
                    Text(MemoriesView.when(m))
                        .font(.system(.caption2, design: .rounded).weight(.bold))
                        .textCase(.uppercase).foregroundStyle(Palette.inkDim)
                    Text(m.text).font(.system(.callout, design: .rounded))
                        .multilineTextAlignment(.leading)
                        .lineLimit(5)
                }
                .shell(padding: 14)
                .contextMenu {
                    Button("Delete note", systemImage: "trash", role: .destructive) { deleteNote(m) }
                }
            }
            if !notes.isEmpty {
                Text("Long-press a note to delete it.")
                    .font(.system(.caption2, design: .rounded)).foregroundStyle(.white.opacity(0.4))
                    .padding(.horizontal, 6)
            }
        }
    }

    private var photosSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            SectionLabel(text: "Photos", trailing: photos.isEmpty ? nil : "\(photos.count)")
            if photos.isEmpty {
                hint(loading ? "Loading…" : "No photos on file.")
            } else {
                Text("Tap a photo that isn't \(name ?? "them") to delete it or file it under the right person. Vibey recognizes people from these.")
                    .font(.system(.caption, design: .rounded)).foregroundStyle(.white.opacity(0.5))
                    .padding(.horizontal, 6)
            }
            LazyVGrid(columns: cols, spacing: 8) {
                ForEach(photos) { p in
                    Button { Haptics.soft(); picked = p } label: {
                        // Square cell first, photo fills it: crops vary in shape.
                        Color.clear.aspectRatio(1, contentMode: .fit)
                            .overlay(FaceThumb(dataURI: p.snapshot))
                            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
                                .stroke(.white.opacity(0.15)))
                    }
                    .buttonStyle(PressScale())
                    .accessibilityLabel("Photo from \(Self.day(p.created_at))")
                }
            }
        }
    }

    private func hint(_ s: String) -> some View {
        Text(s).font(.system(.footnote, design: .rounded)).foregroundStyle(.white.opacity(0.55))
            .padding(.horizontal, 6)
    }

    // MARK: Data

    private func load() async {
        loading = true
        do {
            profile = try await store.api.friendProfile(friendID)
            error = nil
        } catch where error.isCancellation {
        } catch {
            self.error = error.localizedDescription
        }
        await loadNotes()
        loading = false
    }

    private func loadNotes() async {
        guard let n = name else { notes = []; return }
        if let all = try? await store.api.memories() {
            notes = all.filter { Self.mentions($0.text, n) }.reversed()
        }
    }

    /// Whole-word, case-insensitive: "Kai" matches "kai's" but not "Kaiser".
    static func mentions(_ text: String, _ name: String) -> Bool {
        let p = "\\b" + NSRegularExpression.escapedPattern(for: name) + "\\b"
        return text.range(of: p, options: [.regularExpression, .caseInsensitive]) != nil
    }

    private func rename(_ raw: String) {
        let n = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty, n != name else { return }
        Task {
            do {
                try await store.api.nameFriend(friendID, n)
                Haptics.ok(); store.flash("Hi, \(n)")
                onChange()
                // Naming someone after an existing friend folds them in; this
                // id is gone then, so close instead of showing an error.
                if (try? await store.api.friendProfile(friendID)) == nil { dismiss() } else { await load() }
            } catch where !error.isCancellation {
                Haptics.fail(); self.error = error.localizedDescription
            } catch {}
        }
    }

    private func addNote(_ raw: String) {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, let n = name else { return }
        // The note must name them, or it would not show up here (or mean
        // anything to the brain) later.
        let text = Self.mentions(t, n) ? t : "\(n): \(t)"
        Task {
            do {
                try await store.api.addMemory(text)
                Haptics.ok()
                await loadNotes()
            } catch where !error.isCancellation {
                Haptics.fail(); self.error = error.localizedDescription
            } catch {}
        }
    }

    private func deleteNote(_ m: Memory) {
        Task {
            do {
                try await store.api.deleteMemory(m.id)
                Haptics.heavy()
                withAnimation { notes.removeAll { $0.id == m.id } }
            } catch where !error.isCancellation {
                Haptics.fail(); self.error = error.localizedDescription
            } catch {}
        }
    }

    private func apply(_ action: PhotoActionsSheet.Action, to p: FacePhoto) {
        Task {
            do {
                switch action {
                case .delete:
                    try await store.api.deletePhoto(p.id)
                    Haptics.heavy()
                case .moveTo(let f):
                    let r = try await store.api.reassignPhoto(p.id, toFace: f.id)
                    Haptics.ok(); store.flash("Moved to \(r.name ?? f.name ?? "that person")")
                case .name(let n):
                    let r = try await store.api.reassignPhoto(p.id, name: n)
                    Haptics.ok(); store.flash("Moved to \(r.name ?? n)")
                }
                withAnimation { profile?.samples?.removeAll { $0.id == p.id } }
                onChange()
                // Moving a stranger's only photo folds them into the other person.
                if (try? await store.api.friendProfile(friendID)) == nil { dismiss() } else { await load() }
            } catch where !error.isCancellation {
                Haptics.fail(); self.error = error.localizedDescription
            } catch {}
        }
    }

    // MARK: Dates (the face service stores ISO 8601 UTC with microseconds)

    private static func date(_ s: String?) -> Date? {
        guard let s else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }

    static func day(_ s: String?) -> String {
        guard let d = date(s) else { return "—" }
        let f = DateFormatter(); f.dateFormat = "MMM d, yyyy"
        return f.string(from: d)
    }

    static func ago(_ s: String?) -> String {
        guard let d = date(s) else { return "—" }
        if Date().timeIntervalSince(d) < 60 { return "just now" }
        let f = RelativeDateTimeFormatter(); f.unitsStyle = .short
        return f.localizedString(for: d, relativeTo: Date())
    }
}

/// Tap a photo: see it big, then delete it or say who it really is.
struct PhotoActionsSheet: View {
    enum Action { case delete, moveTo(Friend), name(String) }

    let photo: FacePhoto
    let current: String?
    let others: [Friend]
    var onAction: (Action) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var choosing = false
    @State private var query = ""
    @State private var confirmDelete = false

    private var matches: [Friend] {
        let named = others.filter { $0.name?.isEmpty == false }
        let q = query.trimmingCharacters(in: .whitespaces)
        return q.isEmpty ? named : named.filter { $0.name!.localizedCaseInsensitiveContains(q) }
    }
    private var typed: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var typedIsNew: Bool {
        !typed.isEmpty && !others.contains { $0.name?.caseInsensitiveCompare(typed) == .orderedSame }
            && current?.caseInsensitiveCompare(typed) != .orderedSame
    }

    var body: some View {
        VStack(spacing: 14) {
            HStack {
                Button("Cancel") { dismiss() }.foregroundStyle(.white.opacity(0.7))
                Spacer()
                Text(choosing ? "Who is this?" : "Photo").font(.system(.headline, design: .rounded))
                Spacer()
                Button("Cancel") {}.hidden()
            }
            if choosing { chooser } else { actions }
        }
        .padding(20)
    }

    private var actions: some View {
        VStack(spacing: 14) {
            FaceThumb(dataURI: photo.snapshot)
                .frame(width: 180, height: 180)
                .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
            Text("Captured \(FriendProfileView.day(photo.created_at))")
                .font(.system(.caption, design: .rounded).weight(.bold))
                .textCase(.uppercase).foregroundStyle(.white.opacity(0.5))
            Button { Haptics.soft(); withAnimation { choosing = true } } label: {
                Label("This is someone else…", systemImage: "person.crop.circle.badge.questionmark")
            }
            .buttonStyle(PillButtonStyle())
            Button(role: .destructive) { confirmDelete = true } label: {
                Label("Delete photo", systemImage: "trash").frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .tint(Palette.bad)
            .confirmationDialog("Delete this photo?", isPresented: $confirmDelete, titleVisibility: .visible) {
                Button("Delete", role: .destructive) { onAction(.delete); dismiss() }
            } message: {
                Text("Vibey stops using it to recognize \(current ?? "them").")
            }
            Spacer(minLength: 0)
        }
    }

    private var chooser: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.white.opacity(0.5))
                TextField("Name", text: $query)
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled()
                    .submitLabel(.done)
                    .onSubmit { if typedIsNew { pick(.name(typed)) } }
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(.white.opacity(0.08)))
            ScrollView {
                VStack(spacing: 8) {
                    if typedIsNew {
                        Button { pick(.name(typed)) } label: {
                            row(nil, title: "New person: \(typed)", icon: "plus.circle.fill")
                        }
                        .buttonStyle(.plain)
                    }
                    ForEach(matches) { f in
                        Button { pick(.moveTo(f)) } label: { row(f.snapshot, title: f.name ?? "", icon: nil) }
                            .buttonStyle(.plain)
                    }
                    if matches.isEmpty && !typedIsNew {
                        Text("Type a name to file it under someone new.")
                            .font(.system(.footnote, design: .rounded)).foregroundStyle(.white.opacity(0.5))
                            .padding(.top, 10)
                    }
                }
            }
            .scrollIndicators(.hidden)
        }
    }

    private func row(_ snapshot: String?, title: String, icon: String?) -> some View {
        HStack(spacing: 12) {
            if let icon {
                Image(systemName: icon).font(.system(size: 26)).foregroundStyle(Palette.duck)
                    .frame(width: 40, height: 40)
            } else {
                FaceThumb(dataURI: snapshot).frame(width: 40, height: 40).clipShape(Circle())
            }
            Text(title).font(.system(.body, design: .rounded).weight(.semibold)).foregroundStyle(.white)
            Spacer()
            Image(systemName: "chevron.right").font(.caption.weight(.bold)).foregroundStyle(.white.opacity(0.35))
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(.white.opacity(0.06)))
        .contentShape(Rectangle())
    }

    private func pick(_ a: Action) {
        Haptics.tap()
        onAction(a)
        dismiss()
    }
}
