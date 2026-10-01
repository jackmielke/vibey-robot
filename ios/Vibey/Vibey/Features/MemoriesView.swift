import SwiftUI

/// Vibey's own memories (GET/POST/DELETE :8770/memories), newest first.
struct MemoriesView: View {
    @EnvironmentObject var store: Store
    @State private var items: [Memory] = []
    @State private var loading = false
    @State private var error: String?
    @State private var editing: Editing?

    struct Editing: Identifiable { let id = UUID(); let memory: Memory? }

    var body: some View {
        SpaceScreen {
            ScrollView {
                VStack(spacing: 14) {
                    HStack(alignment: .bottom) {
                        ScreenTitle(title: "Memories",
                                    subtitle: items.isEmpty ? nil : "\(items.count) things Vibey knows")
                        Button { Haptics.tap(); editing = Editing(memory: nil) } label: {
                            Image(systemName: "plus")
                                .font(.system(size: 20, weight: .bold))
                                .frame(width: 48, height: 48)
                                .background(Circle().fill(Palette.duck))
                                .foregroundStyle(Palette.ink)
                                .shadow(color: Palette.duck.opacity(0.5), radius: 12)
                        }
                        .accessibilityLabel("Add memory")
                    }
                    if let error {
                        Text(error).font(.system(.callout, design: .rounded))
                            .foregroundStyle(Palette.bad).frame(maxWidth: .infinity, alignment: .leading)
                    }
                    if loading && items.isEmpty { ProgressView().tint(.white).padding(.top, 40) }
                    ForEach(items) { m in
                        Button { Haptics.soft(); editing = Editing(memory: m) } label: { row(m) }
                            .buttonStyle(.plain)
                            .contextMenu {
                                Button("Edit", systemImage: "pencil") { editing = Editing(memory: m) }
                                Button("Delete", systemImage: "trash", role: .destructive) { delete(m) }
                            }
                    }
                }
                .padding(.horizontal, 18)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
            .refreshable { await load() }
        }
        .task { await load() }
        .sheet(item: $editing) { e in
            MemoryEditor(memory: e.memory) { text in
                save(e.memory, text)
            } onDelete: {
                if let m = e.memory { delete(m) }
            }
            .presentationDetents([.medium, .large])
            .presentationBackground(.ultraThinMaterial)
        }
    }

    private func row(_ m: Memory) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(Self.when(m))
                .font(.system(.caption, design: .rounded).weight(.bold))
                .textCase(.uppercase)
                .foregroundStyle(Palette.inkDim)
            Text(m.text)
                .font(.system(.body, design: .rounded))
                .multilineTextAlignment(.leading)
                .lineLimit(6)
        }
        .shell(padding: 16)
    }

    static func when(_ m: Memory) -> String {
        guard let day = m.day, !day.isEmpty else { return m.date ?? "" }
        let inF = DateFormatter(); inF.dateFormat = "yyyy-MM-dd HH:mm"
        let dayF = DateFormatter(); dayF.dateFormat = "yyyy-MM-dd"
        let t = (m.time ?? "")
        if !t.isEmpty, let d = inF.date(from: "\(day) \(t)") {
            let out = DateFormatter(); out.dateFormat = "EEE, MMM d · h:mm a"
            return out.string(from: d)
        }
        if let d = dayF.date(from: day) {
            let out = DateFormatter(); out.dateFormat = "EEE, MMM d, yyyy"
            return out.string(from: d)
        }
        return day
    }

    private func load() async {
        loading = true
        do {
            items = try await store.api.memories().reversed()
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
        loading = false
    }

    private func save(_ m: Memory?, _ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }
        Task {
            do {
                if let m { try await store.api.editMemory(m.id, t) } else { try await store.api.addMemory(t) }
                Haptics.ok()
                store.flash(m == nil ? "Remembered" : "Updated")
                await load()
            } catch {
                Haptics.fail(); store.flash(error.localizedDescription)
            }
        }
    }

    private func delete(_ m: Memory) {
        Task {
            do {
                try await store.api.deleteMemory(m.id)
                Haptics.heavy()
                withAnimation { items.removeAll { $0.id == m.id } }
                store.flash("Forgotten")
            } catch {
                Haptics.fail(); store.flash(error.localizedDescription)
            }
        }
    }
}

struct MemoryEditor: View {
    let memory: Memory?
    var onSave: (String) -> Void
    var onDelete: () -> Void
    @State private var text = ""
    @State private var confirmDelete = false
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Button("Cancel") { dismiss() }.foregroundStyle(.white.opacity(0.7))
                Spacer()
                Text(memory == nil ? "New memory" : "Edit memory")
                    .font(.system(.headline, design: .rounded))
                Spacer()
                Button("Save") { onSave(text); dismiss() }
                    .font(.system(.headline, design: .rounded))
                    .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            if let memory {
                Text(MemoriesView.when(memory))
                    .font(.system(.caption, design: .rounded).weight(.bold))
                    .textCase(.uppercase)
                    .foregroundStyle(.white.opacity(0.5))
            }
            TextEditor(text: $text)
                .focused($focused)
                .font(.system(.body, design: .rounded))
                .scrollContentBackground(.hidden)
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(.white.opacity(0.08)))
                .frame(minHeight: 140)
            if memory != nil {
                Button(role: .destructive) { confirmDelete = true } label: {
                    Label("Forget this", systemImage: "trash")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(Palette.bad)
                .confirmationDialog("Forget this memory?", isPresented: $confirmDelete, titleVisibility: .visible) {
                    Button("Forget", role: .destructive) { onDelete(); dismiss() }
                }
            }
            Spacer(minLength: 0)
        }
        .padding(20)
        .onAppear { text = memory?.text ?? ""; focused = memory == nil }
    }
}
