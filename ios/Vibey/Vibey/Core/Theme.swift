import SwiftUI
import UIKit

enum Palette {
    static let space = Color(red: 0.020, green: 0.024, blue: 0.047)
    static let duck = Color(red: 1.0, green: 0.824, blue: 0.247)        // #FFD23F
    static let beak = Color(red: 1.0, green: 0.478, blue: 0.11)
    static let live = Color(red: 0.357, green: 0.890, blue: 1.0)         // #5BE3FF
    static let ink = Color(red: 0.078, green: 0.078, blue: 0.11)
    static let inkDim = Color(red: 0.42, green: 0.43, blue: 0.52)
    static let shellTop = Color.white
    static let shellBottom = Color(red: 0.886, green: 0.894, blue: 0.957)
    static let antenna = Color(red: 0.80, green: 0.84, blue: 1.0)
    static let bad = Color(red: 1.0, green: 0.42, blue: 0.42)
    static let starDim = Color.white.opacity(0.55)
}

/// The robot-shell look: white to lavender, soft halo, big radius.
struct ShellCard: ViewModifier {
    var padding: CGFloat = 18
    var glow: Color = .white
    func body(content: Content) -> some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 28, style: .continuous)
                    .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom],
                                         startPoint: .top, endPoint: .bottom))
            )
            .shadow(color: glow.opacity(0.22), radius: 22)
            .shadow(color: glow.opacity(0.10), radius: 4)
            .foregroundStyle(Palette.ink)
            .environment(\.colorScheme, .light)   // controls read dark-on-white
    }
}

extension View {
    func shell(padding: CGFloat = 18, glow: Color = .white) -> some View {
        modifier(ShellCard(padding: padding, glow: glow))
    }
}

enum Haptics {
    static func tap() { UIImpactFeedbackGenerator(style: .medium).impactOccurred() }
    static func soft() { UIImpactFeedbackGenerator(style: .soft).impactOccurred() }
    static func heavy() { UIImpactFeedbackGenerator(style: .heavy).impactOccurred() }
    static func ok() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func fail() { UINotificationFeedbackGenerator().notificationOccurred(.error) }
}

/// Big pill button used for the primary actions.
struct PillButtonStyle: ButtonStyle {
    var fill: Color = Palette.duck
    var text: Color = Palette.ink
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(.title3, design: .rounded).weight(.bold))
            .frame(maxWidth: .infinity, minHeight: 64)
            .background(Capsule().fill(fill))
            .foregroundStyle(text)
            .shadow(color: fill.opacity(0.45), radius: configuration.isPressed ? 6 : 18)
            .scaleEffect(configuration.isPressed ? 0.97 : 1)
            .animation(.spring(response: 0.25, dampingFraction: 0.7), value: configuration.isPressed)
    }
}

struct ScreenTitle: View {
    let title: String
    var subtitle: String? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.system(size: 34, weight: .heavy, design: .rounded))
                .foregroundStyle(.white)
            if let subtitle {
                Text(subtitle)
                    .font(.system(.subheadline, design: .rounded))
                    .foregroundStyle(.white.opacity(0.55))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
