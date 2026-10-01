import SwiftUI

/// Near-black sky with a few hundred stars that drift very slowly to the left.
/// Static when Reduce Motion is on.
struct Starfield: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private struct Star { let x: Double; let y: Double; let r: Double; let a: Double; let depth: Double }

    private static let stars: [Star] = {
        var g = SeededRandom(seed: 0x5EED_B0B)
        return (0..<220).map { _ in
            let depth = g.next()
            return Star(x: g.next(), y: g.next(),
                        r: 0.4 + depth * 1.3,
                        a: 0.25 + g.next() * 0.65,
                        depth: depth)
        }
    }()

    var body: some View {
        TimelineView(.periodic(from: .now, by: reduceMotion ? 3600 : 1.0 / 20)) { ctx in
            Canvas { gc, size in
                let t = reduceMotion ? 0 : ctx.date.timeIntervalSinceReferenceDate
                gc.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Palette.space))
                // A faint nebula so the black has some depth.
                gc.fill(Path(CGRect(origin: .zero, size: size)),
                        with: .radialGradient(Gradient(colors: [Color(red: 0.16, green: 0.18, blue: 0.42).opacity(0.30), .clear]),
                                              center: CGPoint(x: size.width * 0.2, y: size.height * 0.28),
                                              startRadius: 0, endRadius: max(size.width, size.height) * 0.7))
                for s in Self.stars {
                    let speed = 0.002 + s.depth * 0.006           // fraction of width per second
                    var x = (s.x - t * speed).truncatingRemainder(dividingBy: 1)
                    if x < 0 { x += 1 }
                    let twinkle = reduceMotion ? 1 : 0.75 + 0.25 * sin(t * (0.6 + s.depth) + s.x * 40)
                    let rect = CGRect(x: x * size.width, y: s.y * size.height, width: s.r * 2, height: s.r * 2)
                    gc.fill(Path(ellipseIn: rect), with: .color(.white.opacity(s.a * twinkle)))
                }
            }
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }
}

struct SeededRandom {
    private var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> Double {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        return Double(state >> 11) / Double(1 << 53)
    }
}
