import SwiftUI

/// The rover's wheels, same as the Wheels panel on the Mac: gas and steering
/// sliders (Car), a hold-to-drive pad, or a joystick. Let go to stop. While a finger is down the
/// move is resent every 250 ms (150 ms for the stick); the chat service keeps
/// the rover going only ~0.6 s per call, so a dropped request, leaving the
/// screen or locking the phone all end in a stop.
struct DriveView: View {
    @EnvironmentObject var store: Store
    @Environment(\.scenePhase) private var phase
    @StateObject private var driver = RoverDriver()
    @AppStorage("vibey.wspeed") private var speed = "medium"
    @AppStorage("vibey.wmode") private var mode = "car"
    @State private var info: RoverInfo?
    @State private var reachable = true
    @State private var showMap = false

    private var off: Bool { !(info?.connected ?? false) }

    var body: some View {
        SpaceScreen {
            VStack(spacing: 18) {
                ScreenTitle(title: "Drive", subtitle: hint)

                HStack {
                    status
                    Spacer()
                    Button { driver.letGo(); showMap = true } label: {
                        Label("Map", systemImage: "map.fill")
                            .font(.system(.subheadline, design: .rounded).weight(.bold))
                            .padding(.horizontal, 14).padding(.vertical, 8)
                            .background(Capsule().fill(Palette.duck))
                            .foregroundStyle(Palette.ink)
                    }
                    .buttonStyle(PressScale())
                    .accessibilityLabel("Room map and drive by eye")
                }

                Picker("Controls", selection: $mode) {
                    Text("Car").tag("car"); Text("Pad").tag("pad"); Text("Stick").tag("stick")
                }
                .pickerStyle(.segmented)
                .onChange(of: mode) { _, _ in driver.letGo() }

                Spacer(minLength: 0)
                switch mode {
                case "pad": pad
                case "stick": Joystick(driver: driver, disabled: off)
                default: carControls
                }
                Spacer(minLength: 0)

                Button { driver.halt() } label: {
                    Label("STOP", systemImage: "stop.fill")
                        .font(.system(size: 26, weight: .black, design: .rounded))
                }
                .buttonStyle(PillButtonStyle(fill: Palette.bad, text: .white))
                .accessibilityLabel("Stop")

                VStack(alignment: .leading, spacing: 10) {
                    Text("Top speed").font(.system(.headline, design: .rounded))
                    Picker("Speed", selection: $speed) {
                        Text("Slow").tag("slow"); Text("Medium").tag("medium"); Text("Fast").tag("fast")
                    }
                    .pickerStyle(.segmented)
                }
                .shell()
            }
            .padding(.horizontal, 18)
            .padding(.bottom, 16)
        }
        .onAppear { driver.api = store.api; driver.speed = speed }
        .onChange(of: speed) { _, s in driver.speed = s }
        .onChange(of: phase) { _, p in if p != .active { driver.letGo() } }
        .onDisappear { driver.letGo() }
        .sheet(isPresented: $showMap) { RoomMapView().environmentObject(store) }
        .task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: .seconds(10))
            }
        }
    }

    private var hint: String {
        switch mode {
        case "pad": return "Hold a button. Let go to stop."
        case "stick": return "Push to drive, sideways to spin."
        default: return "Gas on the left, steering on the right."
        }
    }

    // MARK: Status

    private var status: some View {
        let (text, good): (String, Bool) = {
            if let msg = driver.note { return (msg, false) }
            guard reachable, let info else { return (reachable ? "checking…" : "chat service offline", false) }
            if !info.connected { return ("no rover set (ROVER_URL)", false) }
            if info.online == false { return ("rover not answering", false) }
            let v = info.battery.map { "🔋 " + $0 } ?? "connected"
            return ((info.riding == true ? "riding · " : "") + v, true)
        }()
        return Label(text, systemImage: good ? "bolt.car.fill" : "exclamationmark.triangle.fill")
            .font(.system(.subheadline, design: .rounded).weight(.semibold))
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(Capsule().fill((good ? Palette.live : Palette.duck).opacity(0.15)))
            .overlay(Capsule().stroke((good ? Palette.live : Palette.duck).opacity(0.5)))
            .foregroundStyle(good ? Palette.live : Palette.duck)
    }

    private func refresh() async {
        driver.api = store.api
        do { info = try await store.api.roverInfo(); reachable = true }
        catch { info = nil; reachable = false }
    }

    // MARK: Car (gas and steering apart, use both thumbs)

    private var carControls: some View {
        HStack(alignment: .center) {
            CarTrack(vertical: true, label: "GAS", driver: driver, disabled: off)
            Spacer(minLength: 12)
            CarTrack(vertical: false, label: "STEER", driver: driver, disabled: off)
        }
        .padding(.horizontal, 8)
    }

    // MARK: Pad (same layout as the Mac panel)

    private var pad: some View {
        let size: CGFloat = 86
        return VStack(spacing: 12) {
            HStack(spacing: 12) {
                HoldKey(action: "left", icon: "arrow.up.left", size: size, driver: driver, disabled: off)
                HoldKey(action: "forward", icon: "arrowtriangle.up.fill", size: size, driver: driver, disabled: off)
                HoldKey(action: "right", icon: "arrow.up.right", size: size, driver: driver, disabled: off)
            }
            HStack(spacing: 12) {
                HoldKey(action: "spin_left", icon: "arrow.counterclockwise", label: "spin", size: size, driver: driver, disabled: off)
                Circle()
                    .fill(RadialGradient(colors: [Palette.duck.opacity(0.35), .clear], center: .center,
                                         startRadius: 2, endRadius: size * 0.5))
                    .overlay(Image(systemName: "circle.grid.cross.fill").foregroundStyle(.white.opacity(0.35)))
                    .frame(width: size, height: size)
                HoldKey(action: "spin_right", icon: "arrow.clockwise", label: "spin", size: size, driver: driver, disabled: off)
            }
            HStack(spacing: 12) {
                Color.clear.frame(width: size, height: size)
                HoldKey(action: "back", icon: "arrowtriangle.down.fill", size: size, driver: driver, disabled: off)
                Color.clear.frame(width: size, height: size)
            }
        }
    }
}

// MARK: - Driver

/// Owns the resend loop so every way of letting go ends the same way.
@MainActor
final class RoverDriver: ObservableObject {
    @Published private(set) var action: String?
    @Published private(set) var steering = false
    @Published var note: String?
    var api = VibeyAPI(host: "", token: "")
    var speed = "medium"
    private var wheels: (Double, Double)?
    private var gas = 0.0, turn = 0.0
    private var loop: Task<Void, Never>?

    func hold(_ a: String) {
        guard action != a else { return }
        Haptics.tap()
        wheels = nil; steering = false; action = a
        run(every: 0.25)
    }

    /// Joystick vector, -1...1 each way (y up is forward). nil = centred.
    func steer(x: Double, y: Double) {
        guard let v = Self.mix(x, y) else {
            if steering { letGo() }
            return
        }
        let fresh = !steering
        wheels = v; action = nil; steering = true
        if fresh { Haptics.soft(); run(every: 0.15) }
    }

    /// Car mode. Gas alone drives straight, steering alone spins in place,
    /// both together arc, and in reverse the steering flips so it backs up
    /// like a car (same mix as the Mac panel).
    func car(gas g: Double? = nil, steer s: Double? = nil) {
        if let g { gas = g }
        if let s { turn = s }
        let moving = abs(gas) > 0.08
        steer(x: (moving ? 0.7 : 1) * (gas < -0.08 ? -turn : turn), y: gas)
    }

    /// Let go of `only` (or anything). Eases to a stop on the Mac side.
    func letGo(_ only: String? = nil) {
        if let only, action != only { return }
        guard action != nil || steering else { return }
        end()
        let api = api
        Task { try? await api.roverRelease() }
    }

    func halt() {
        Haptics.heavy()
        end()
        let api = api
        Task {
            do { try await api.roverStop(); note = nil }
            catch { note = error.localizedDescription; Haptics.fail() }
        }
    }

    private func end() {
        loop?.cancel(); loop = nil
        action = nil; wheels = nil; steering = false
        gas = 0; turn = 0
    }

    private func run(every: Double) {
        loop?.cancel()
        loop = Task { [weak self] in
            while !Task.isCancelled {
                self?.sendOnce()
                try? await Task.sleep(for: .milliseconds(Int(every * 1000)))
            }
        }
    }

    /// Fire and forget: a slow reply must not delay the next keep-alive.
    private func sendOnce() {
        let api = api, speed = speed, a = action, w = wheels
        Task {
            do {
                let r: DriveResult
                if let w { r = try await api.roverWheels(left: w.0, right: w.1, speed: speed) }
                else if let a { r = try await api.roverHold(a, speed: speed) }
                else { return }
                note = r.ok ? nil : r.text
            } catch {
                note = error.localizedDescription
            }
        }
    }

    /// Arcade mix: each wheel = forward ± turn, with a small dead zone.
    /// Pivots get the extra spin power on the Mac side (SPIN_MIN).
    static func mix(_ x: Double, _ y: Double) -> (Double, Double)? {
        if (x * x + y * y).squareRoot() < 0.12 { return nil }
        let l = y + x, r = y - x
        let k = max(1, abs(l), abs(r))
        return (l / k, r / k)
    }
}

// MARK: - Controls

/// A pad button that drives for as long as it is held.
private struct HoldKey: View {
    let action: String
    let icon: String
    var label: String? = nil
    let size: CGFloat
    @ObservedObject var driver: RoverDriver
    let disabled: Bool
    @GestureState private var down = false

    private var held: Bool { driver.action == action }

    var body: some View {
        VStack(spacing: 2) {
            Image(systemName: icon).font(.system(size: label == nil ? 30 : 24, weight: .bold))
            if let label {
                Text(label).font(.system(size: 11, weight: .heavy, design: .rounded)).textCase(.uppercase)
            }
        }
        .frame(width: size, height: size)
        .background(
            RoundedRectangle(cornerRadius: 26, style: .continuous)
                .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom], startPoint: .top, endPoint: .bottom))
        )
        .foregroundStyle(held ? Palette.live : Palette.ink)
        .shadow(color: (held ? Palette.live : .white).opacity(held ? 0.6 : 0.3), radius: 14)
        .scaleEffect(held ? 0.92 : 1)
        .animation(.spring(response: 0.2, dampingFraction: 0.6), value: held)
        .opacity(disabled ? 0.35 : 1)
        .contentShape(Rectangle())
        .gesture(
            DragGesture(minimumDistance: 0).updating($down) { _, s, _ in s = true },
            including: disabled ? .none : .all
        )
        // GestureState resets on lift and on cancel (a call coming in, etc.).
        .onChange(of: down) { _, d in d ? driver.hold(action) : driver.letGo(action) }
        .accessibilityLabel(action.replacingOccurrences(of: "_", with: " "))
        .accessibilityAddTraits(.isButton)
    }
}

/// One Car slider: vertical is gas (up forward, down reverse), horizontal is
/// steering. Each has its own gesture, so both thumbs work at once.
private struct CarTrack: View {
    let vertical: Bool
    let label: String
    @ObservedObject var driver: RoverDriver
    let disabled: Bool
    @State private var knob: CGFloat = 0
    @GestureState private var down = false
    private let long: CGFloat = 230, short: CGFloat = 76

    var body: some View {
        let reach = long / 2 - 34
        VStack(spacing: 8) {
            ZStack {
                Capsule()
                    .fill(Palette.ink.opacity(0.5))
                    .overlay(Capsule().stroke(.white.opacity(down ? 0.5 : 0.2), lineWidth: 2))
                Image(systemName: vertical ? "arrow.up" : "arrow.left")
                    .offset(x: vertical ? 0 : -long / 2 + 18, y: vertical ? -long / 2 + 18 : 0)
                Image(systemName: vertical ? "arrow.down" : "arrow.right")
                    .offset(x: vertical ? 0 : long / 2 - 18, y: vertical ? long / 2 - 18 : 0)
                Circle()
                    .fill(down ? AnyShapeStyle(Palette.live)
                               : AnyShapeStyle(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom],
                                                              startPoint: .top, endPoint: .bottom)))
                    .frame(width: 60, height: 60)
                    .shadow(color: (down ? Palette.live : .white).opacity(0.5), radius: 12)
                    .offset(x: vertical ? 0 : knob, y: vertical ? knob : 0)
            }
            .font(.system(size: 14, weight: .bold))
            .foregroundStyle(.white.opacity(0.4))
            .frame(width: vertical ? short : long, height: vertical ? long : short)
            .contentShape(Capsule())
            .gesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .local)
                    .updating($down) { _, s, _ in s = true }
                    .onChanged { g in
                        let d = max(-reach, min(reach, (vertical ? g.location.y : g.location.x) - long / 2))
                        knob = d
                        if vertical { driver.car(gas: -d / reach) } else { driver.car(steer: d / reach) }
                    },
                including: disabled ? .none : .all
            )
            Text(label).font(.system(size: 11, weight: .heavy, design: .rounded)).foregroundStyle(Palette.inkDim)
        }
        .opacity(disabled ? 0.35 : 1)
        .onChange(of: down) { _, d in
            if !d {
                withAnimation(.spring(response: 0.25, dampingFraction: 0.6)) { knob = 0 }
                if vertical { driver.car(gas: 0) } else { driver.car(steer: 0) }
            }
        }
        .onChange(of: driver.steering) { _, s in
            if !s && !down { withAnimation(.spring(response: 0.25)) { knob = 0 } }
        }
        .accessibilityLabel(vertical ? "Gas" : "Steering")
    }
}

/// Drag anywhere in the circle: up is forward, sideways steers or spins.
private struct Joystick: View {
    @ObservedObject var driver: RoverDriver
    let disabled: Bool
    @State private var knob: CGSize = .zero
    @GestureState private var down = false
    private let size: CGFloat = 250

    var body: some View {
        let reach = size / 2 - 36
        ZStack {
            Circle()
                .fill(RadialGradient(colors: [Palette.duck.opacity(driver.steering ? 0.3 : 0.15), Palette.ink.opacity(0.5)],
                                     center: .center, startRadius: 4, endRadius: size / 2))
                .overlay(Circle().stroke(.white.opacity(driver.steering ? 0.5 : 0.2), lineWidth: 2))
            Image(systemName: "arrow.up").offset(y: -size / 2 + 22)
            Image(systemName: "arrow.counterclockwise").offset(x: -size / 2 + 22)
            Image(systemName: "arrow.clockwise").offset(x: size / 2 - 22)
            Image(systemName: "arrow.down").offset(y: size / 2 - 22)
            Circle()
                .fill(LinearGradient(colors: [Palette.shellTop, Palette.shellBottom], startPoint: .top, endPoint: .bottom))
                .frame(width: 72, height: 72)
                .shadow(color: (driver.steering ? Palette.live : .white).opacity(0.5), radius: 14)
                .offset(knob)
        }
        .font(.system(size: 14, weight: .bold))
        .foregroundStyle(.white.opacity(0.4))
        .frame(width: size, height: size)
        .opacity(disabled ? 0.35 : 1)
        .contentShape(Circle())
        .gesture(
            DragGesture(minimumDistance: 0, coordinateSpace: .local)
                .updating($down) { _, s, _ in s = true }
                .onChanged { g in
                    var dx = g.location.x - size / 2, dy = g.location.y - size / 2
                    let m = (dx * dx + dy * dy).squareRoot()
                    if m > reach { dx *= reach / m; dy *= reach / m }
                    knob = CGSize(width: dx, height: dy)
                    driver.steer(x: dx / reach, y: -dy / reach)
                },
            including: disabled ? .none : .all
        )
        .onChange(of: down) { _, d in
            if !d {
                withAnimation(.spring(response: 0.25, dampingFraction: 0.6)) { knob = .zero }
                driver.steer(x: 0, y: 0)
            }
        }
        .onChange(of: driver.steering) { _, s in
            if !s && !down { withAnimation(.spring(response: 0.25)) { knob = .zero } }
        }
        .accessibilityLabel("Joystick")
    }
}

struct PressScale: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.92 : 1)
            .animation(.spring(response: 0.2, dampingFraction: 0.6), value: configuration.isPressed)
    }
}
