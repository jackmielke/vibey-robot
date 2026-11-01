import SwiftUI
import ARKit
import SceneKit
import AudioToolbox

/// Vibey's Haunted Candy Hunt, the ARKit version of services/halloween.html.
///
/// World tracking pins everything to the real room: you tap the robot once to
/// mark where it is, candy and ghosts float around the house, and at the
/// halfway mark a devil costume appears on the real Vibey for the boss fight.
/// Game events go to services/reachy_halloween.py (:8781 on the Mac), which
/// turns them into Vibey's voice lines, emotes, sounds and little wheel dodges.
struct HauntView: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @StateObject private var game = HauntGame()

    var body: some View {
        ZStack {
            HauntARView(game: game).ignoresSafeArea()
            hud
        }
        .statusBarHidden()
        .onAppear { game.host = store.host.trimmingCharacters(in: .whitespaces); game.checkLink() }
        .onDisappear { game.stop() }
    }

    private var hud: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top) {
                stat("CANDY", "\(game.score)")
                if game.phase == .hunt || game.phase == .boss { stat("TIME", "\(game.timeLeft)") }
                Spacer()
                VStack(alignment: .trailing, spacing: 8) {
                    Button { game.stop(); dismiss() } label: {
                        Image(systemName: "xmark").font(.headline.weight(.bold))
                            .frame(width: 38, height: 38).background(Circle().fill(.black.opacity(0.55)))
                    }
                    HStack(spacing: 6) {
                        Circle().fill(game.linked ? Color.green : Color.gray).frame(width: 8, height: 8)
                        Text(game.linked ? "Vibey linked" : "Solo mode")
                    }
                    .font(.system(.caption, design: .rounded).weight(.bold))
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(Capsule().fill(.black.opacity(0.55)))
                }
            }
            .foregroundStyle(.white)
            .padding(.horizontal, 18).padding(.top, 14)

            if game.phase == .boss {
                VStack(spacing: 4) {
                    Text("THE VIBEYGEIST").font(.system(.headline, design: .rounded).weight(.black))
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Capsule().fill(.black.opacity(0.6))
                            Capsule().fill(Color.red)
                                .frame(width: g.size.width * CGFloat(max(0, game.bossHP)) / CGFloat(HauntGame.bossMax))
                        }
                    }
                    .frame(height: 12)
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 50).padding(.top, 10)
            }

            Spacer()

            if let b = game.banner {
                Text(b)
                    .font(.system(size: 38, weight: .black, design: .rounded))
                    .multilineTextAlignment(.center)
                    .foregroundStyle(game.phase == .boss ? Color.red : Palette.beak)
                    .shadow(color: .black, radius: 8)
                    .padding(.horizontal, 24)
                    .transition(.scale.combined(with: .opacity))
            }

            Spacer()

            bottom
                .padding(.horizontal, 18).padding(.bottom, 30)
        }
        .animation(.spring(duration: 0.35), value: game.banner)
        .animation(.easeInOut, value: game.phase)
    }

    @ViewBuilder private var bottom: some View {
        switch game.phase {
        case .aim:
            VStack(spacing: 10) {
                Text("Vibey's Haunted Candy Hunt")
                    .font(.system(.title2, design: .rounded).weight(.black)).foregroundStyle(Palette.beak)
                Text(game.hint)
                    .font(.system(.subheadline, design: .rounded).weight(.semibold))
                    .multilineTextAlignment(.center).foregroundStyle(.white)
                Text("Then grab candy, zap ghosts twice, and free Vibey when it gets possessed. Pinch to resize the costume.")
                    .font(.system(.caption, design: .rounded)).multilineTextAlignment(.center)
                    .foregroundStyle(.white.opacity(0.75))
            }
            .padding(18)
            .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(.black.opacity(0.6)))
        case .over:
            VStack(spacing: 12) {
                Text(game.won ? "Vibey is free!" : "The Vibeygeist wins")
                    .font(.system(.title, design: .rounded).weight(.black)).foregroundStyle(Palette.beak)
                Text("\(game.score) candy").font(.system(size: 44, weight: .black, design: .rounded)).foregroundStyle(.white)
                Button("Play again") { game.restart?() }
                    .buttonStyle(PillButtonStyle(fill: Palette.beak))
            }
            .padding(20)
            .background(RoundedRectangle(cornerRadius: 22, style: .continuous).fill(.black.opacity(0.7)))
        default:
            if let t = game.toast {
                Text(t).font(.system(.subheadline, design: .rounded).weight(.bold)).foregroundStyle(.white)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(Capsule().fill(.black.opacity(0.6)))
            }
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(label).font(.system(size: 11, weight: .bold, design: .rounded)).foregroundStyle(.white.opacity(0.7))
            Text(value).font(.system(size: 28, weight: .black, design: .rounded)).monospacedDigit()
        }
        .padding(.trailing, 18)
        .shadow(color: .black.opacity(0.8), radius: 6)
    }
}

// MARK: - State the HUD reads

final class HauntGame: ObservableObject {
    enum Phase { case aim, hunt, boss, freed, over }
    static let round = 75
    static let bossAt = 35
    static let bossMax = 12

    @Published var phase: Phase = .aim
    @Published var score = 0
    @Published var timeLeft = HauntGame.round
    @Published var bossHP = HauntGame.bossMax
    @Published var banner: String?
    @Published var toast: String?
    @Published var hint = "Point at Vibey and tap the robot's face."
    @Published var won = false
    @Published var linked = false

    var host = ""
    var restart: (() -> Void)?
    var stopEngine: (() -> Void)?
    private var bannerJob: DispatchWorkItem?
    private var toastJob: DispatchWorkItem?

    func stop() { stopEngine?() }

    func show(_ text: String, for secs: Double = 2) {
        banner = text
        bannerJob?.cancel()
        let job = DispatchWorkItem { [weak self] in self?.banner = nil }
        bannerJob = job
        DispatchQueue.main.asyncAfter(deadline: .now() + secs, execute: job)
    }

    func say(_ text: String) {
        toast = text
        toastJob?.cancel()
        let job = DispatchWorkItem { [weak self] in self?.toast = nil }
        toastJob = job
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.6, execute: job)
    }

    private var base: URL? { URL(string: "http://\(host):8781") }

    func checkLink() {
        guard let u = base?.appendingPathComponent("state") else { return }
        var req = URLRequest(url: u, timeoutInterval: 3)
        req.cachePolicy = .reloadIgnoringLocalCacheData
        URLSession.shared.dataTask(with: req) { data, _, _ in
            let ok = (data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] })?["ok"] as? Bool ?? false
            DispatchQueue.main.async { self.linked = ok }
        }.resume()
    }

    /// Fire and forget: the robot reacting late (or not at all) never blocks play.
    func send(_ type: String, _ extra: [String: Any] = [:]) {
        guard linked, let u = base?.appendingPathComponent("event") else { return }
        var body = extra
        body["type"] = type
        body["score"] = score
        body["wheels"] = true
        var req = URLRequest(url: u, timeoutInterval: 4)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req).resume()
    }
}

// MARK: - AR view

struct HauntARView: UIViewRepresentable {
    @ObservedObject var game: HauntGame

    func makeCoordinator() -> HauntEngine { HauntEngine(game: game) }

    func makeUIView(context: Context) -> ARSCNView {
        let v = ARSCNView(frame: .zero)
        v.scene = SCNScene()
        v.autoenablesDefaultLighting = true
        v.automaticallyUpdatesLighting = true
        context.coordinator.attach(v)
        return v
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}

    static func dismantleUIView(_ uiView: ARSCNView, coordinator: HauntEngine) {
        coordinator.shutdown()
    }
}

/// The game itself: spawns, movement, taps, the boss. Runs on the main thread
/// off a display link; SceneKit nodes are safe to change from there.
final class HauntEngine: NSObject {
    private final class Thing {
        enum Kind { case candy, ghost, bat }
        let node: SCNNode
        let kind: Kind
        var hp: Int
        let speed: Float
        let born: CFTimeInterval
        let life: Double
        init(node: SCNNode, kind: Kind, hp: Int, speed: Float, born: CFTimeInterval, life: Double) {
            self.node = node; self.kind = kind; self.hp = hp; self.speed = speed; self.born = born; self.life = life
        }
    }

    let game: HauntGame
    private weak var view: ARSCNView?
    private var link: CADisplayLink?
    private var things: [Thing] = []
    private var vibey: SCNVector3?
    private var costume: SCNNode?
    private var costumeScale: Float = 1
    private var last: CFTimeInterval = 0
    private var clock: Double = 0
    private var spawnT = 0.0, ghostT = 3.0, batT = 2.5, dodgeT = 2.5
    private var lastHitSent = 0.0
    private var combo = 0
    private var comboT = 0.0
    private var dodgeOffset: Float = 0, dodgeTarget: Float = 0

    init(game: HauntGame) {
        self.game = game
        super.init()
        game.restart = { [weak self] in self?.reset(keepVibey: true) }
        game.stopEngine = { [weak self] in self?.shutdown() }
    }

    func attach(_ v: ARSCNView) {
        view = v
        let cfg = ARWorldTrackingConfiguration()
        cfg.planeDetection = [.horizontal, .vertical]
        v.session.run(cfg)
        v.addGestureRecognizer(UITapGestureRecognizer(target: self, action: #selector(tapped(_:))))
        v.addGestureRecognizer(UIPinchGestureRecognizer(target: self, action: #selector(pinched(_:))))
        let l = CADisplayLink(target: self, selector: #selector(step(_:)))
        l.add(to: .main, forMode: .common)
        link = l
    }

    func shutdown() {
        link?.invalidate(); link = nil
        view?.session.pause()
    }

    // MARK: camera helpers

    private var camTransform: simd_float4x4? { view?.session.currentFrame?.camera.transform }
    private var camPos: SIMD3<Float>? {
        guard let t = camTransform else { return nil }
        return SIMD3(t.columns.3.x, t.columns.3.y, t.columns.3.z)
    }
    private var camForward: SIMD3<Float>? {
        guard let t = camTransform else { return nil }
        return -SIMD3(t.columns.2.x, t.columns.2.y, t.columns.2.z)
    }
    private var camRight: SIMD3<Float>? {
        guard let t = camTransform else { return nil }
        return SIMD3(t.columns.0.x, t.columns.0.y, t.columns.0.z)
    }

    // MARK: input

    @objc private func tapped(_ g: UITapGestureRecognizer) {
        guard let v = view else { return }
        let p = g.location(in: v)
        switch game.phase {
        case .aim: placeVibey(at: p)
        case .hunt, .boss: hit(at: p)
        default: break
        }
    }

    @objc private func pinched(_ g: UIPinchGestureRecognizer) {
        guard let c = costume else { return }
        costumeScale = max(0.4, min(3, costumeScale * Float(g.scale)))
        g.scale = 1
        c.scale = SCNVector3(costumeScale, costumeScale, costumeScale)
    }

    private func placeVibey(at p: CGPoint) {
        guard let v = view else { return }
        var spot: SIMD3<Float>?
        if let q = v.raycastQuery(from: p, allowing: .estimatedPlane, alignment: .any),
           let r = v.session.raycast(q).first {
            spot = SIMD3(r.worldTransform.columns.3.x, r.worldTransform.columns.3.y, r.worldTransform.columns.3.z)
        }
        if spot == nil, let pos = camPos, let f = camForward {   // nothing to hit yet: assume ~1.2 m away
            spot = pos + f * 1.2
        }
        guard let s = spot else { return }
        vibey = SCNVector3(s.x, s.y, s.z)
        buildCostume()
        Haptics.ok()
        game.send("calibrated")
        reset(keepVibey: true)
    }

    private func hit(at p: CGPoint) {
        guard let v = view else { return }
        let hits = v.hitTest(p, options: [SCNHitTestOption.searchMode: SCNHitTestSearchMode.all.rawValue,
                                          SCNHitTestOption.ignoreHiddenNodes: true])
        // things first (a bat in front of the boss gets zapped first)
        for h in hits {
            if let t = thing(for: h.node) { touch(t); return }
        }
        if game.phase == .boss, hits.contains(where: { isCostume($0.node) }) { hitBoss() }
    }

    private func thing(for node: SCNNode) -> Thing? {
        var n: SCNNode? = node
        while let cur = n {
            if let t = things.first(where: { $0.node === cur }) { return t }
            n = cur.parent
        }
        return nil
    }

    private func isCostume(_ node: SCNNode) -> Bool {
        var n: SCNNode? = node
        while let cur = n { if cur === costume { return true }; n = cur.parent }
        return false
    }

    private func touch(_ t: Thing) {
        switch t.kind {
        case .candy:
            combo = comboT > 0 ? combo + 1 : 1
            comboT = 2.2
            let pts = 10 + min(combo - 1, 5) * 5
            game.score += pts
            AudioServicesPlaySystemSound(1104)
            Haptics.soft()
            if combo > 2 { game.say("\(combo)x combo! +\(pts)") }
            pop(t)
            game.send("candy", ["combo": combo])
        case .ghost, .bat:
            t.hp -= 1
            Haptics.tap()
            AudioServicesPlaySystemSound(1105)
            t.node.runAction(.sequence([.scale(to: 1.3, duration: 0.06), .scale(to: 1, duration: 0.1)]))
            if t.hp <= 0 {
                game.score += 5
                pop(t)
                game.send("ghost_zap")
            }
        }
    }

    private func pop(_ t: Thing) {
        things.removeAll { $0 === t }
        t.node.runAction(.sequence([.group([.scale(to: 1.8, duration: 0.18), .fadeOut(duration: 0.18)]), .removeFromParentNode()]))
    }

    private func hitBoss() {
        guard let c = costume else { return }
        game.bossHP -= 1
        Haptics.heavy()
        AudioServicesPlaySystemSound(1105)
        c.runAction(.sequence([.scale(to: CGFloat(costumeScale * 1.15), duration: 0.05),
                               .scale(to: CGFloat(costumeScale), duration: 0.12)]))
        if clock - lastHitSent > 0.7 { lastHitSent = clock; game.send("boss_hit", ["hp": game.bossHP]) }
        if game.bossHP <= 0 {
            game.score += 50
            game.phase = .freed
            game.show("Vibey is FREE!", for: 2.2)
            Haptics.ok()
            game.send("boss_defeated")
            c.runAction(.sequence([.group([.scale(to: CGFloat(costumeScale * 2), duration: 0.5), .fadeOut(duration: 0.5)]), .hide()]))
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.3) { [weak self] in self?.finish(won: true) }
        }
    }

    // MARK: flow

    private func reset(keepVibey: Bool) {
        for t in things { t.node.removeFromParentNode() }
        things = []
        clock = 0; spawnT = 0; ghostT = 3; batT = 2.5; dodgeT = 2.5; combo = 0
        dodgeOffset = 0; dodgeTarget = 0
        costume?.isHidden = true
        costume?.opacity = 1
        costume?.scale = SCNVector3(costumeScale, costumeScale, costumeScale)
        game.score = 0
        game.timeLeft = HauntGame.round
        game.bossHP = HauntGame.bossMax
        game.won = false
        game.phase = .hunt
        for _ in 0..<6 { spawnCandy() }
        game.show("Grab the candy!", for: 1.6)
        game.send("start")
    }

    private func possess() {
        game.phase = .boss
        for t in things where t.kind == .ghost { t.node.removeFromParentNode() }
        things.removeAll { $0.kind == .ghost }
        costume?.isHidden = false
        costume?.opacity = 0
        costume?.runAction(.fadeIn(duration: 0.6))
        Haptics.heavy()
        game.show("Vibey has been POSSESSED!", for: 2.6)
        game.say("Turn to Vibey and zap it free")
        game.send("boss_start")
    }

    private func finish(won: Bool) {
        guard game.phase != .over else { return }
        game.won = won
        game.phase = .over
        game.send("end", ["won": won])
    }

    // MARK: loop

    @objc private func step(_ l: CADisplayLink) {
        let now = l.timestamp
        let dt = last == 0 ? 0 : min(0.05, now - last)
        last = now
        guard game.phase == .hunt || game.phase == .boss, let cam = camPos else { return }
        clock += dt
        comboT -= dt
        let left = max(0, HauntGame.round - Int(clock))
        if left != game.timeLeft { game.timeLeft = left }
        if game.phase == .hunt && left <= HauntGame.bossAt { possess() }
        if game.phase == .boss && left <= 0 { finish(won: false); return }

        spawnT -= dt
        let candies = things.filter { $0.kind == .candy }.count
        if spawnT <= 0 && candies < (game.phase == .boss ? 4 : 9) { spawnCandy(); spawnT = Double.random(in: 0.5...1.2) }
        if game.phase == .hunt {
            ghostT -= dt
            if ghostT <= 0 { spawnGhost(); ghostT = Double.random(in: 2.6...4.2) }
        } else {
            batT -= dt
            if batT <= 0 { spawnBat(); batT = Double.random(in: 2.4...3.8) }
            bossMoves(dt)
        }

        for t in things {
            if t.kind == .candy {
                if now - t.born > t.life { pop(t) }
                continue
            }
            let p = SIMD3(t.node.position.x, t.node.position.y, t.node.position.z)
            let to = cam - p
            let d = simd_length(to)
            if d < 0.35 {
                things.removeAll { $0 === t }
                t.node.removeFromParentNode()
                let lost = min(game.score, t.kind == .bat ? 10 : 20)
                game.score -= lost
                Haptics.fail()
                game.say(t.kind == .bat ? "A bat grabbed \(lost) candy" : "A ghost stole \(lost) candy!")
                game.send("ghost_steal", ["lost": lost])
                continue
            }
            let wob = Float(sin(now * 3 + Double(t.speed) * 10)) * 0.15
            let dir = simd_normalize(to)
            let side = simd_normalize(simd_cross(dir, SIMD3<Float>(0, 1, 0)))
            let np = p + dir * t.speed * Float(dt) + side * wob * Float(dt)
            t.node.position = SCNVector3(np.x, np.y, np.z)
        }
    }

    private func bossMoves(_ dt: Double) {
        guard let c = costume, let base = vibey, let r = camRight else { return }
        dodgeT -= dt
        if dodgeT <= 0 {
            let left = Bool.random()
            dodgeTarget = left ? -0.14 : 0.14
            dodgeT = Double.random(in: 2.2...3.6)
            game.send("boss_dodge", ["dir": left ? "left" : "right"])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { [weak self] in self?.dodgeTarget = 0 }
        }
        dodgeOffset += (dodgeTarget - dodgeOffset) * Float(min(1, dt * 6))
        let flat = simd_normalize(SIMD3(r.x, 0, r.z))
        c.position = SCNVector3(base.x + flat.x * dodgeOffset, base.y, base.z + flat.z * dodgeOffset)
    }

    // MARK: spawning

    private func around(dist: ClosedRange<Float>, spread: Float, dy: ClosedRange<Float>) -> SCNVector3? {
        guard let cam = camPos, let f = camForward else { return nil }
        let heading = atan2(f.x, f.z) + Float.random(in: -spread...spread)
        let d = Float.random(in: dist)
        return SCNVector3(cam.x + sin(heading) * d, cam.y + Float.random(in: dy), cam.z + cos(heading) * d)
    }

    private func spawnCandy() {
        guard let pos = around(dist: 0.7...1.9, spread: 2.4, dy: -0.45...0.2) else { return }
        let node = HauntEngine.emoji(["🍬", "🍭", "🍫", "🍩", "🎃", "🍪", "🧁"].randomElement()!, size: 0.13)
        node.position = pos
        let bob = SCNAction.sequence([.moveBy(x: 0, y: 0.03, z: 0, duration: 0.7), .moveBy(x: 0, y: -0.03, z: 0, duration: 0.7)])
        bob.timingMode = .easeInEaseOut
        node.runAction(.repeatForever(bob))
        node.scale = SCNVector3(0.01, 0.01, 0.01)
        node.runAction(.scale(to: 1, duration: 0.25))
        add(Thing(node: node, kind: .candy, hp: 1, speed: 0, born: CACurrentMediaTime(), life: Double.random(in: 9...14)))
    }

    private func spawnGhost() {
        guard let pos = around(dist: 3.0...3.8, spread: 2.2, dy: -0.1...0.4) else { return }
        let node = HauntEngine.emoji("👻", size: 0.3)
        node.position = pos
        add(Thing(node: node, kind: .ghost, hp: 2, speed: Float.random(in: 0.3...0.45), born: CACurrentMediaTime(), life: 99))
    }

    private func spawnBat() {
        guard let v = vibey else { return }
        let node = HauntEngine.emoji("🦇", size: 0.18)
        node.position = SCNVector3(v.x, v.y + 0.1, v.z)
        add(Thing(node: node, kind: .bat, hp: 1, speed: 0.45, born: CACurrentMediaTime(), life: 99))
    }

    private func add(_ t: Thing) {
        view?.scene.rootNode.addChildNode(t.node)
        things.append(t)
    }

    // MARK: costume

    /// Horns, a flaming crown and glowing red eyes, sized for Reachy Mini's
    /// head (about 16 cm wide) around the spot you tapped on its face.
    private func buildCostume() {
        costume?.removeFromParentNode()
        guard let v = vibey else { return }
        let root = SCNNode()
        root.position = v
        let face = SCNBillboardConstraint()
        face.freeAxes = .Y
        root.constraints = [face]

        let red = SCNMaterial()
        red.diffuse.contents = UIColor(red: 0.85, green: 0.08, blue: 0.12, alpha: 1)
        red.emission.contents = UIColor(red: 0.6, green: 0.0, blue: 0.0, alpha: 1)
        for s: Float in [-1, 1] {
            let cone = SCNCone(topRadius: 0, bottomRadius: 0.022, height: 0.1)
            cone.materials = [red]
            let horn = SCNNode(geometry: cone)
            horn.position = SCNVector3(0.055 * s, 0.1, 0)
            horn.eulerAngles = SCNVector3(0, 0, -0.45 * s)
            root.addChildNode(horn)
        }
        let glow = SCNMaterial()
        glow.diffuse.contents = UIColor.red
        glow.emission.contents = UIColor(red: 1, green: 0.1, blue: 0.1, alpha: 1)
        glow.lightingModel = .constant
        for s: Float in [-1, 1] {
            let eye = SCNNode(geometry: SCNSphere(radius: 0.02))
            eye.geometry?.materials = [glow]
            eye.position = SCNVector3(0.042 * s, 0.0, 0.03)
            eye.runAction(.repeatForever(.sequence([.fadeOpacity(to: 0.55, duration: 0.25), .fadeOpacity(to: 1, duration: 0.25)])))
            root.addChildNode(eye)
        }
        let crown = HauntEngine.emoji("👑", size: 0.09, billboard: false)
        crown.position = SCNVector3(0, 0.1, 0.01)
        root.addChildNode(crown)
        let fire = HauntEngine.emoji("🔥", size: 0.07, billboard: false)
        fire.position = SCNVector3(0, 0.16, 0.0)
        root.addChildNode(fire)

        let aura = SCNNode(geometry: SCNPlane(width: 0.5, height: 0.5))
        let am = SCNMaterial()
        am.diffuse.contents = HauntEngine.auraImage()
        am.lightingModel = .constant
        am.writesToDepthBuffer = false
        am.isDoubleSided = true
        aura.geometry?.materials = [am]
        aura.position = SCNVector3(0, 0.02, -0.08)
        aura.renderingOrder = -1
        aura.runAction(.repeatForever(.sequence([.scale(to: 1.1, duration: 0.6), .scale(to: 0.95, duration: 0.6)])))
        root.addChildNode(aura)

        let light = SCNNode()
        light.light = SCNLight()
        light.light?.type = .omni
        light.light?.color = UIColor.red
        light.light?.intensity = 600
        light.position = SCNVector3(0, 0.05, 0.15)
        root.addChildNode(light)

        root.scale = SCNVector3(costumeScale, costumeScale, costumeScale)
        root.isHidden = true
        view?.scene.rootNode.addChildNode(root)
        costume = root
    }

    // MARK: art

    private static var emojiCache: [String: UIImage] = [:]

    static func emoji(_ e: String, size: CGFloat, billboard: Bool = true) -> SCNNode {
        let img = emojiCache[e] ?? {
            let r = UIGraphicsImageRenderer(size: CGSize(width: 256, height: 256))
            let i = r.image { _ in
                (e as NSString).draw(in: CGRect(x: 8, y: 16, width: 240, height: 240),
                                     withAttributes: [.font: UIFont.systemFont(ofSize: 200)])
            }
            emojiCache[e] = i
            return i
        }()
        let plane = SCNPlane(width: size, height: size)
        let m = SCNMaterial()
        m.diffuse.contents = img
        m.lightingModel = .constant
        m.isDoubleSided = true
        plane.materials = [m]
        let n = SCNNode(geometry: plane)
        if billboard { n.constraints = [SCNBillboardConstraint()] }
        return n
    }

    static func auraImage() -> UIImage {
        UIGraphicsImageRenderer(size: CGSize(width: 256, height: 256)).image { ctx in
            let colors = [UIColor(red: 1, green: 0.1, blue: 0.1, alpha: 0.55).cgColor,
                          UIColor(red: 1, green: 0, blue: 0, alpha: 0).cgColor] as CFArray
            if let g = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1]) {
                ctx.cgContext.drawRadialGradient(g, startCenter: CGPoint(x: 128, y: 128), startRadius: 0,
                                                 endCenter: CGPoint(x: 128, y: 128), endRadius: 128, options: [])
            }
        }
    }
}
