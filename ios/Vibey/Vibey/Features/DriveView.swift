import SwiftUI

/// D-pad for the wheels: POST :8772/drive {action, seconds, speed}.
struct DriveView: View {
    @EnvironmentObject var store: Store
    @State private var speed = "medium"
    @State private var seconds = 1.0
    @State private var last: String?
    @State private var lastOK = true
    @State private var sending: String?

    var body: some View {
        SpaceScreen {
            ScrollView {
                VStack(spacing: 22) {
                    ScreenTitle(title: "Drive", subtitle: "Short hops. STOP always wins.")

                    if let last {
                        Label(last, systemImage: lastOK ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                            .font(.system(.subheadline, design: .rounded).weight(.semibold))
                            .padding(.horizontal, 14).padding(.vertical, 9)
                            .background(Capsule().fill((lastOK ? Palette.live : Palette.duck).opacity(0.15)))
                            .overlay(Capsule().stroke((lastOK ? Palette.live : Palette.duck).opacity(0.5)))
                            .foregroundStyle(lastOK ? Palette.live : Palette.duck)
                            .transition(.scale.combined(with: .opacity))
                    }

                    pad
                        .padding(.vertical, 6)

                    Button { go("stop") } label: {
                        Label("STOP", systemImage: "stop.fill")
                            .font(.system(size: 26, weight: .black, design: .rounded))
                    }
                    .buttonStyle(PillButtonStyle(fill: Palette.bad, text: .white))
                    .accessibilityLabel("Stop")

                    settings
                }
                .padding(.horizontal, 18)
                .padding(.bottom, 24)
                .animation(.spring, value: last)
            }
            .scrollIndicators(.hidden)
        }
    }

    private var pad: some View {
        let size: CGFloat = 86
        return VStack(spacing: 12) {
            HStack(spacing: 12) {
                key("spin_left", "arrow.counterclockwise", size, small: true)
                key("forward", "arrowtriangle.up.fill", size)
                key("spin_right", "arrow.clockwise", size, small: true)
            }
            HStack(spacing: 12) {
                key("left", "arrowtriangle.left.fill", size)
                Circle()
                    .fill(RadialGradient(colors: [Palette.duck.opacity(0.35), .clear], center: .center,
                                         startRadius: 2, endRadius: size * 0.5))
                    .overlay(Image(systemName: "circle.grid.cross.fill").foregroundStyle(.white.opacity(0.35)))
                    .frame(width: size, height: size)
                key("right", "arrowtriangle.right.fill", size)
            }
            HStack(spacing: 12) {
                Color.clear.frame(width: size, height: size)
                key("back", "arrowtriangle.down.fill", size)
                Color.clear.frame(width: size, height: size)
            }
        }
    }

    private func key(_ action: String, _ icon: String, _ size: CGFloat, small: Bool = false) -> some View {
        Button { go(action) } label: {
            Image(systemName: icon)
                .font(.system(size: small ? 24 : 30, weight: .bold))
                .frame(width: size, height: size)
                .background(
                    RoundedRectangle(cornerRadius: 26, style: .continuous)
                        .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom], startPoint: .top, endPoint: .bottom))
                )
                .foregroundStyle(sending == action ? Palette.live : Palette.ink)
                .shadow(color: (sending == action ? Palette.live : .white).opacity(0.3), radius: 14)
                .opacity(small ? 0.92 : 1)
        }
        .buttonStyle(PressScale())
        .accessibilityLabel(action.replacingOccurrences(of: "_", with: " "))
    }

    private var settings: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Speed").font(.system(.headline, design: .rounded))
            Picker("Speed", selection: $speed) {
                Text("Slow").tag("slow"); Text("Medium").tag("medium"); Text("Fast").tag("fast")
            }
            .pickerStyle(.segmented)
            HStack {
                Text("Each tap").font(.system(.headline, design: .rounded))
                Spacer()
                Text(String(format: "%.1fs", seconds))
                    .font(.system(.headline, design: .rounded).monospacedDigit())
                    .foregroundStyle(Palette.inkDim)
            }
            Slider(value: $seconds, in: 0.3...3, step: 0.1).tint(Palette.ink)
        }
        .shell()
    }

    private func go(_ action: String) {
        action == "stop" ? Haptics.heavy() : Haptics.tap()
        sending = action
        Task {
            do {
                let r = try await store.api.drive(action, seconds: seconds, speed: speed)
                last = r; lastOK = true
            } catch {
                Haptics.fail()
                last = error.localizedDescription; lastOK = false
            }
            sending = nil
        }
    }
}

struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .animation(.spring(response: 0.2, dampingFraction: 0.6), value: configuration.isPressed)
    }
}
