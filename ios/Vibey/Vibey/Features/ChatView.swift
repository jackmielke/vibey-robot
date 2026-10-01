import SwiftUI

/// The live room transcript (GET /state every 2s via Store) plus anything typed
/// here, which goes to POST /ask as Jack on the Telegram channel.
struct ChatView: View {
    @EnvironmentObject var store: Store
    @State private var draft = ""
    @State private var local: [Line] = []
    @State private var thinking = false
    @FocusState private var focused: Bool

    struct Line: Identifiable, Hashable {
        let id: String
        let mine: Bool          // right side: Jack (typed) or people in the room
        let typed: Bool         // came from this phone
        let text: String
        let ts: Double
    }

    private var lines: [Line] {
        let room = (store.state?.transcript ?? []).map { t in
            Line(id: "r\(t.ts)-\(t.who)", mine: t.who != "wonder", typed: false, text: t.text, ts: t.ts / 1000)
        }
        // A typed reply may also show up in the room transcript; keep one copy.
        let extra = local.filter { l in !room.contains { $0.text == l.text && abs($0.ts - l.ts) < 180 } }
        return (room + extra).sorted { $0.ts < $1.ts }
    }

    var body: some View {
        SpaceScreen {
            VStack(spacing: 0) {
                ScreenTitle(title: "Chat", subtitle: "Live from the room · type to talk as Jack")
                    .padding(.horizontal, 18)
                    .padding(.bottom, 8)
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 10) {
                            if lines.isEmpty {
                                Text("Nothing said yet.")
                                    .font(.system(.callout, design: .rounded))
                                    .foregroundStyle(.white.opacity(0.5))
                                    .padding(.top, 60)
                            }
                            ForEach(lines) { bubble($0) }
                            if thinking { typingBubble }
                            Color.clear.frame(height: 1).id("bottom")
                        }
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                    }
                    .scrollIndicators(.hidden)
                    .scrollDismissesKeyboard(.interactively)
                    .onChange(of: lines.count) { _, _ in
                        withAnimation(.easeOut) { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                    .onChange(of: thinking) { _, _ in
                        withAnimation(.easeOut) { proxy.scrollTo("bottom", anchor: .bottom) }
                    }
                    .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                composer
            }
        }
    }

    @ViewBuilder
    private func bubble(_ l: Line) -> some View {
        if !l.mine && l.text.hasPrefix("(") && l.text.hasSuffix(")") {
            // stage directions like "(OpenAI Realtime mode ...)": a quiet note, not a message
            Text(l.text.dropFirst().dropLast())
                .font(.system(.caption, design: .rounded))
                .foregroundStyle(.white.opacity(0.45))
                .multilineTextAlignment(.center)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 2)
        } else {
            message(l)
        }
    }

    private func message(_ l: Line) -> some View {
        HStack(alignment: .bottom) {
            if l.mine { Spacer(minLength: 48) }
            VStack(alignment: l.mine ? .trailing : .leading, spacing: 3) {
                Text(l.text)
                    .font(.system(.body, design: .rounded))
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(
                        RoundedRectangle(cornerRadius: 20, style: .continuous)
                            .fill(l.mine
                                  ? AnyShapeStyle(l.typed ? Palette.duck : Palette.duck.opacity(0.85))
                                  : AnyShapeStyle(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom],
                                                                 startPoint: .top, endPoint: .bottom)))
                    )
                    .shadow(color: (l.mine ? Palette.duck : .white).opacity(0.18), radius: 10)
                    .foregroundStyle(Palette.ink)
                    .textSelection(.enabled)
                Text("\(l.mine ? (l.typed ? "You" : "Room") : "Vibey") · \(Self.clock(l.ts))")
                    .font(.system(.caption2, design: .rounded))
                    .foregroundStyle(.white.opacity(0.4))
                    .padding(.horizontal, 6)
            }
            if !l.mine { Spacer(minLength: 48) }
        }
    }

    private var typingBubble: some View {
        HStack {
            HStack(spacing: 5) {
                ForEach(0..<3) { i in
                    Circle().fill(Palette.inkDim).frame(width: 7, height: 7)
                        .phaseAnimator([0.3, 1.0]) { c, p in c.opacity(p) } animation: { _ in
                            .easeInOut(duration: 0.5).delay(Double(i) * 0.15)
                        }
                }
            }
            .padding(.horizontal, 16).padding(.vertical, 14)
            .background(RoundedRectangle(cornerRadius: 20, style: .continuous).fill(Palette.shellBottom))
            Spacer()
        }
    }

    private var composer: some View {
        HStack(spacing: 10) {
            TextField("Say something to Vibey…", text: $draft, axis: .vertical)
                .lineLimit(1...4)
                .focused($focused)
                .font(.system(.body, design: .rounded))
                .padding(.horizontal, 16).padding(.vertical, 12)
                .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(.white.opacity(0.1)))
                .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).stroke(.white.opacity(0.15)))
                .foregroundStyle(.white)
                .submitLabel(.send)
                .onSubmit(send)
            Button(action: send) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 18, weight: .bold))
                    .frame(width: 46, height: 46)
                    .background(Circle().fill(canSend ? Palette.duck : .white.opacity(0.15)))
                    .foregroundStyle(canSend ? Palette.ink : .white.opacity(0.4))
                    .shadow(color: Palette.duck.opacity(canSend ? 0.5 : 0), radius: 10)
            }
            .disabled(!canSend)
            .accessibilityLabel("Send")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var canSend: Bool { !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !thinking }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !thinking else { return }
        Haptics.tap()
        draft = ""
        let now = Date().timeIntervalSince1970
        local.append(Line(id: "me\(now)", mine: true, typed: true, text: text, ts: now))
        thinking = true
        Task {
            do {
                let reply = try await store.api.ask(text)
                if !reply.isEmpty {
                    local.append(Line(id: "re\(now)", mine: false, typed: true, text: reply,
                                      ts: Date().timeIntervalSince1970))
                }
                Haptics.soft()
            } catch {
                Haptics.fail()
                store.flash(error.localizedDescription)
            }
            thinking = false
        }
    }

    static func clock(_ ts: Double) -> String {
        let f = DateFormatter()
        f.dateFormat = Calendar.current.isDateInToday(Date(timeIntervalSince1970: ts)) ? "h:mm a" : "MMM d, h:mm a"
        return f.string(from: Date(timeIntervalSince1970: ts))
    }
}
