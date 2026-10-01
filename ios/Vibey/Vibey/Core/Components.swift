import SwiftUI

/// Small uppercase label above a group of cards.
struct SectionLabel: View {
    let text: String
    var trailing: String? = nil
    var body: some View {
        HStack {
            Text(text)
                .font(.system(.caption, design: .rounded).weight(.heavy))
                .tracking(1.2)
                .textCase(.uppercase)
                .foregroundStyle(.white.opacity(0.55))
            Spacer()
            if let trailing {
                Text(trailing)
                    .font(.system(.caption, design: .rounded).weight(.semibold))
                    .foregroundStyle(.white.opacity(0.4))
            }
        }
        .padding(.horizontal, 6)
        .padding(.top, 6)
    }
}

/// One switch inside a shell card: icon puck, title, hint, toggle.
struct SwitchRow: View {
    let icon: String
    let title: String
    var hint: String? = nil
    var tint: Color = Palette.duck
    let isOn: Bool
    var enabled: Bool = true
    let set: (Bool) -> Void

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .bold))
                .frame(width: 34, height: 34)
                .background(Circle().fill(isOn ? Palette.ink : Palette.ink.opacity(0.07)))
                .foregroundStyle(isOn ? tint : Palette.inkDim)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.system(.body, design: .rounded).weight(.semibold))
                if let hint {
                    Text(hint).font(.system(.caption, design: .rounded))
                        .foregroundStyle(Palette.inkDim)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 8)
            Toggle("", isOn: Binding(get: { isOn }, set: { set($0) }))
                .labelsHidden()
                .tint(tint)
                .disabled(!enabled)
        }
        .padding(.vertical, 4)
    }
}

/// Hairline between rows inside a shell card.
struct RowDivider: View {
    var body: some View {
        Rectangle().fill(Palette.ink.opacity(0.07)).frame(height: 1).padding(.leading, 46)
    }
}

/// A row of pill choices (a nicer segmented control on the white shell).
struct ChoicePills<T: Hashable>: View {
    let options: [(T, String)]
    let selected: T?
    var tint: Color = Palette.duck
    let pick: (T) -> Void

    var body: some View {
        HStack(spacing: 6) {
            ForEach(options, id: \.0) { opt in
                let on = opt.0 == selected
                Button { if !on { pick(opt.0) } } label: {
                    Text(opt.1)
                        .font(.system(.subheadline, design: .rounded).weight(.bold))
                        .lineLimit(1)
                        .minimumScaleFactor(0.8)
                        .frame(maxWidth: .infinity, minHeight: 40)
                        .background(Capsule().fill(on ? Palette.ink : Palette.ink.opacity(0.06)))
                        .foregroundStyle(on ? tint : Palette.ink.opacity(0.75))
                }
                .buttonStyle(PressScale())
            }
        }
        .animation(.spring(response: 0.3), value: selected)
    }
}

/// Glowing status chip used on the dark background.
struct GlowChip: View {
    let icon: String
    let text: String
    var on: Bool = false
    var color: Color = Palette.live
    var body: some View {
        Label(text, systemImage: icon)
            .font(.system(.footnote, design: .rounded).weight(.semibold))
            .lineLimit(1)
            .padding(.horizontal, 12).padding(.vertical, 7)
            .background(Capsule().fill(on ? color.opacity(0.16) : .white.opacity(0.08)))
            .overlay(Capsule().stroke(on ? color.opacity(0.6) : .white.opacity(0.15)))
            .foregroundStyle(on ? color : .white.opacity(0.75))
    }
}

extension Error {
    /// Cancelled requests (tab switched, pull-to-refresh re-render) are not errors to show.
    var isCancellation: Bool {
        self is CancellationError || (self as? URLError)?.code == .cancelled
    }
}
