import SwiftUI

/// Vibey, drawn from the app icon: white shell head, round shades, two
/// antennas with duck-yellow tips, and the duck. Eyes shut when asleep or in
/// privacy mode; the whole thing dims when off or unreachable.
struct RobotFace: View {
    var status: RobotStatus
    var eyesClosed: Bool
    var speaking: Bool

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var bob = false

    private var lit: Bool { status == .awake }
    private var dim: Bool {
        if case .unreachable = status { return true }
        return status == .off || status == .unknown
    }
    private var shut: Bool { status == .asleep || eyesClosed || dim }

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            ZStack {
                // halo
                Circle()
                    .fill(RadialGradient(colors: [(lit ? Palette.live : .white).opacity(dim ? 0.05 : 0.28), .clear],
                                         center: .center, startRadius: 0, endRadius: w * 0.55))
                    .frame(width: w * 1.1, height: w * 1.1)
                    .offset(y: w * 0.08)

                // antennas
                antenna(w: w, angle: -12).offset(x: -w * 0.22, y: -w * 0.30)
                antenna(w: w, angle: 12).offset(x: w * 0.22, y: -w * 0.30)

                // body
                UnevenRoundedRectangle(topLeadingRadius: w * 0.06, bottomLeadingRadius: w * 0.25,
                                       bottomTrailingRadius: w * 0.25, topTrailingRadius: w * 0.06,
                                       style: .continuous)
                    .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom], startPoint: .top, endPoint: .bottom))
                    .frame(width: w * 0.48, height: w * 0.26)
                    .offset(y: w * 0.30)

                // head
                RoundedRectangle(cornerRadius: w * 0.14, style: .continuous)
                    .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom], startPoint: .top, endPoint: .bottom))
                    .frame(width: w * 0.70, height: w * 0.43)
                    .shadow(color: (lit ? Palette.live : .white).opacity(dim ? 0 : 0.5), radius: w * 0.06)
                    .offset(y: w * 0.04)

                // shades
                HStack(spacing: w * 0.06) { eye(w: w); eye(w: w) }
                    .overlay(Rectangle().fill(Palette.ink).frame(width: w * 0.08, height: w * 0.014))
                    .offset(y: w * 0.04)

                duck(w: w).offset(y: -w * 0.205)

                if status == .asleep {
                    Text("z z z")
                        .font(.system(size: w * 0.07, weight: .heavy, design: .rounded))
                        .foregroundStyle(.white.opacity(0.7))
                        .offset(x: w * 0.40, y: -w * 0.22)
                }
            }
            .frame(width: w, height: w)
            .saturation(dim ? 0 : 1)
            .opacity(dim ? 0.45 : 1)
            .offset(y: bob ? -4 : 4)
        }
        .aspectRatio(1, contentMode: .fit)
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.easeInOut(duration: 2.6).repeatForever(autoreverses: true)) { bob = true }
        }
        .accessibilityElement()
        .accessibilityLabel("Vibey is \(status.title.lowercased())")
    }

    private func antenna(w: CGFloat, angle: Double) -> some View {
        VStack(spacing: 0) {
            Circle().fill(Palette.duck)
                .frame(width: w * 0.055, height: w * 0.055)
                .shadow(color: (lit ? Palette.live : Palette.duck).opacity(dim ? 0 : 0.9), radius: lit ? 10 : 4)
            Capsule().fill(Palette.antenna).frame(width: w * 0.016, height: w * 0.19)
        }
        .rotationEffect(.degrees(angle), anchor: .bottom)
    }

    @ViewBuilder
    private func eye(w: CGFloat) -> some View {
        let d = w * 0.25
        ZStack {
            Circle()
                .fill(RadialGradient(colors: [Color(white: 0.22), Color(white: 0.06)],
                                     center: UnitPoint(x: 0.35, y: 0.3), startRadius: 0, endRadius: d * 0.7))
            if shut {
                // closed lid: a soft smile-shaped line across the lens
                Path { p in
                    p.move(to: CGPoint(x: d * 0.22, y: d * 0.50))
                    p.addQuadCurve(to: CGPoint(x: d * 0.78, y: d * 0.50), control: CGPoint(x: d * 0.5, y: d * 0.66))
                }
                .stroke(.white.opacity(0.85), style: StrokeStyle(lineWidth: d * 0.06, lineCap: .round))
            } else {
                Circle().fill(.white.opacity(0.88))
                    .frame(width: d * 0.22, height: d * 0.22)
                    .offset(x: -d * 0.2, y: -d * 0.2)
                if speaking {
                    Circle().stroke(Palette.live.opacity(0.8), lineWidth: 2)
                        .frame(width: d * 0.9, height: d * 0.9)
                }
            }
        }
        .frame(width: d, height: d)
    }

    private func duck(w: CGFloat) -> some View {
        ZStack {
            Ellipse().fill(Palette.duck).frame(width: w * 0.2, height: w * 0.085).offset(y: w * 0.045)
            Circle().fill(Palette.duck).frame(width: w * 0.12, height: w * 0.12).offset(y: -w * 0.01)
            Capsule().fill(Palette.ink).frame(width: w * 0.075, height: w * 0.022).offset(y: -w * 0.015)
            Capsule().fill(Palette.beak).frame(width: w * 0.05, height: w * 0.022).offset(x: w * 0.07, y: 0)
        }
    }
}
