import SwiftUI

/// Drive by eye and the room map, same as the Wheels panel on the Mac: type a
/// goal (or tap a thing on the map) and Vibey looks, makes a short move, looks
/// again. Scan room turns Vibey's head around and pins what it sees.
struct RoomMapView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var map: RoomMap?
    @State private var offline = false
    @State private var goal = ""
    @State private var pilot: String?
    @State private var piloting = false
    @FocusState private var typing: Bool

    var body: some View {
        SpaceScreen {
            ScrollView {
                VStack(spacing: 16) {
                    HStack {
                        ScreenTitle(title: "Room map", subtitle: "Tap a thing to drive there")
                        Spacer()
                        Button { dismiss() } label: {
                            Image(systemName: "xmark.circle.fill").font(.title).foregroundStyle(.white.opacity(0.6))
                        }
                        .accessibilityLabel("Close")
                    }
                    pilotCard
                    mapCard
                }
                .padding(.horizontal, 18)
                .padding(.vertical, 16)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .task {
            while !Task.isCancelled {
                await load()
                let busy = (map?.scanning ?? false) || piloting
                try? await Task.sleep(for: .milliseconds(busy ? 800 : 5000))
            }
        }
    }

    // MARK: Drive by eye

    private var pilotCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Drive by eye").font(.system(.headline, design: .rounded))
            HStack(spacing: 10) {
                TextField("drive to the couch", text: $goal)
                    .focused($typing)
                    .font(.system(.body, design: .rounded))
                    .padding(.horizontal, 14).padding(.vertical, 11)
                    .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Palette.ink.opacity(0.06)))
                    .submitLabel(.go)
                    .onSubmit { go(goal) }
                Button { go(goal) } label: {
                    Image(systemName: "eye.fill")
                        .font(.system(size: 18, weight: .bold))
                        .frame(width: 48, height: 48)
                        .background(Circle().fill(goal.isEmpty ? Palette.ink.opacity(0.15) : Palette.ink))
                        .foregroundStyle(goal.isEmpty ? .white : Palette.duck)
                }
                .buttonStyle(PressScale())
                .disabled(goal.trimmingCharacters(in: .whitespaces).isEmpty)
                .accessibilityLabel("Go")
            }
            if let pilot {
                Text(pilot).font(.system(.subheadline, design: .rounded)).foregroundStyle(Palette.inkDim)
            }
            if piloting {
                Button { stop() } label: {
                    Label("Stop", systemImage: "stop.fill").font(.system(.headline, design: .rounded))
                }
                .buttonStyle(PillButtonStyle(fill: Palette.bad, text: .white))
            }
        }
        .shell()
    }

    private func go(_ text: String) {
        let g = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !g.isEmpty else { return }
        typing = false
        Haptics.tap()
        pilot = "looking…"
        Task {
            do {
                let r = try await store.api.roverPilot(g)
                piloting = r.text == "driving"
                pilot = piloting ? "driving to \(g)" : r.text
            } catch {
                Haptics.fail()
                pilot = error.localizedDescription
            }
        }
    }

    private func stop() {
        Haptics.heavy()
        piloting = false
        pilot = "stopped"
        Task { try? await store.api.roverStop() }
    }

    // MARK: Map

    private var mapCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Room").font(.system(.headline, design: .rounded))
                Spacer()
                Text(mapNote).font(.system(.caption, design: .rounded)).foregroundStyle(Palette.inkDim)
            }
            RoomCanvas(map: map ?? RoomMap()) { thing in
                goal = "drive to the \(thing.name)"
                go(goal)
            }
            .frame(height: 300)
            HStack(spacing: 10) {
                Button { scan() } label: {
                    Label(map?.scanning == true ? "Scanning…" : "Scan room", systemImage: "viewfinder")
                        .font(.system(.headline, design: .rounded))
                }
                .buttonStyle(PillButtonStyle())
                .disabled(map?.scanning == true)
                Button { forget() } label: {
                    Image(systemName: "trash").font(.system(size: 18, weight: .bold)).frame(width: 48, height: 48)
                        .background(Circle().fill(Palette.ink.opacity(0.08))).foregroundStyle(Palette.ink)
                }
                .buttonStyle(PressScale())
                .accessibilityLabel("Forget the room map")
            }
        }
        .shell()
    }

    private var mapNote: String {
        guard let map else { return offline ? "chat service offline" : "loading…" }
        if map.scanning { return (map.step.isEmpty ? "looking around" : map.step) + "…" }
        if map.scans == 0 { return "nothing mapped yet" }
        return "\(map.things.count) things · \(map.scans) scan\(map.scans == 1 ? "" : "s")"
    }

    private func load() async {
        do {
            map = try await store.api.roomMap(); offline = false
        } catch { offline = true }
        guard piloting, let p = try? await store.api.roverInfo().pilot else { return }
        if p.running == true {
            pilot = "step \(p.step ?? 0): \(p.last?.isEmpty == false ? p.last! : "looking…")"
        } else {
            piloting = false
            pilot = "done: \(p.last?.isEmpty == false ? p.last! : p.goal ?? "")"
        }
    }

    private func scan() {
        Haptics.tap()
        Task {
            if let r = try? await store.api.roomScan(), !r.ok { pilot = r.text }
            try? await Task.sleep(for: .milliseconds(300))
            await load()
        }
    }

    private func forget() {
        Haptics.heavy()
        Task { try? await store.api.roomForget(); await load() }
    }
}

/// Top-down map: Vibey in the middle, things as dots, the rover as an arrow
/// (solid where a scan saw it, hollow while estimated from its own moves).
private struct RoomCanvas: View {
    let map: RoomMap
    let tap: (RoomMap.Thing) -> Void

    var body: some View {
        GeometryReader { geo in
            let W = Double(geo.size.width), H = Double(geo.size.height)
            let pts: [(Double, Double)] = [(0, 0)] + map.things.map { ($0.x, $0.y) } + (map.rover.map { [($0.x, $0.y)] } ?? [])
            let x0 = (pts.map { $0.0 }.min() ?? 0) - 0.8, x1 = (pts.map { $0.0 }.max() ?? 0) + 0.8
            let y0 = (pts.map { $0.1 }.min() ?? 0) - 0.8, y1 = (pts.map { $0.1 }.max() ?? 0) + 0.8
            let k: Double = min(W / max(3, x1 - x0), H / max(3, y1 - y0))
            let mx = (x0 + x1) / 2, my = (y0 + y1) / 2
            let P = { (x: Double, y: Double) -> CGPoint in CGPoint(x: W / 2 + (x - mx) * k, y: H / 2 - (y - my) * k) }

            ZStack {
                Canvas { cx, size in
                    var grid = Path()
                    var g = (mx - W / 2 / k).rounded(.down)
                    while g <= mx + W / 2 / k { let a = P(g, 0).x; grid.move(to: CGPoint(x: a, y: 0)); grid.addLine(to: CGPoint(x: a, y: size.height)); g += 1 }
                    g = (my - H / 2 / k).rounded(.down)
                    while g <= my + H / 2 / k { let b = P(0, g).y; grid.move(to: CGPoint(x: 0, y: b)); grid.addLine(to: CGPoint(x: size.width, y: b)); g += 1 }
                    cx.stroke(grid, with: .color(Palette.ink.opacity(0.08)), lineWidth: 1)
                }
                // Vibey
                VStack(spacing: 2) {
                    Image(systemName: "triangle.fill").font(.system(size: 9)).foregroundStyle(Palette.ink)
                    Circle().fill(Palette.ink).frame(width: 18, height: 18)
                    Text("Vibey").font(.system(size: 11, weight: .semibold, design: .rounded)).foregroundStyle(Palette.inkDim)
                }
                .position(P(0, 0))
                // things
                ForEach(map.things) { t in
                    Button { tap(t) } label: {
                        HStack(spacing: 5) {
                            Circle().fill(Palette.beak).frame(width: 14, height: 14)
                            Text(t.name).font(.system(size: 13, weight: .semibold, design: .rounded)).foregroundStyle(Palette.ink)
                        }
                        .padding(.vertical, 4).padding(.horizontal, 6)
                        .background(Capsule().fill(.white.opacity(0.7)))
                        .opacity(t.missed ? 0.45 : 1)
                    }
                    .buttonStyle(PressScale())
                    .fixedSize()
                    .position(P(t.x, t.y))
                    .accessibilityLabel("Drive to the \(t.name)")
                }
                // rover
                if let r = map.rover {
                    VStack(spacing: 1) {
                        Image(systemName: r.seen ? "location.north.fill" : "location.north")
                            .font(.system(size: 22, weight: .bold))
                            .foregroundStyle(Palette.live)
                            .rotationEffect(.degrees(r.heading))
                        Text("rover").font(.system(size: 11, weight: .semibold, design: .rounded)).foregroundStyle(Palette.inkDim)
                    }
                    .position(P(r.x, r.y))
                }
                if map.things.isEmpty {
                    Text("Nothing mapped yet. Press Scan room.")
                        .font(.system(.footnote, design: .rounded)).foregroundStyle(Palette.inkDim)
                        .position(x: W / 2, y: H - 18)
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Palette.ink.opacity(0.04)))
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}
