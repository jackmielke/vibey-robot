// Vibey.app — native shell around the localhost:8770 dashboard plus a menu-bar
// status item driven by GET localhost:8772/state.
import Cocoa
import WebKit

let dashboardURL = URL(string: "http://localhost:8770")!
let chatBase = "http://localhost:8772"
let repoPath = NSString(string: "~/dev/vibey-robot").expandingTildeInPath

enum VibeyState: String { case awake, asleep, off, down }

// MARK: - Shell helpers

/// Runs a command through a login zsh so PATH/fnm match Terminal.
func runLoginShell(_ command: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/zsh")
    p.arguments = ["-lc", command]
    p.currentDirectoryURL = URL(fileURLWithPath: repoPath)
    p.standardInput = FileHandle.nullDevice
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    try? p.run()
}

func startVibey() {
    runLoginShell("nohup zsh '\(repoPath)/start_wonder.sh' > /tmp/start_wonder.log 2>&1 < /dev/null &!")
}

func stopVibey() {
    runLoginShell("'\(repoPath)/vibey' stop > /tmp/vibey_stop.log 2>&1")
}

func request(_ path: String, method: String = "GET", json: [String: Any]? = nil,
             timeout: TimeInterval = 4,
             done: @escaping (Int, Data?) -> Void) {
    var req = URLRequest(url: URL(string: chatBase + path)!)
    req.httpMethod = method
    req.timeoutInterval = timeout
    if let json = json {
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: json)
    }
    URLSession.shared.dataTask(with: req) { data, resp, _ in
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        DispatchQueue.main.async { done(code, data) }
    }.resume()
}

// MARK: - Asleep placeholder view

final class AsleepView: NSView {
    var onStart: (() -> Void)?
    private let status = NSTextField(labelWithString: "")

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let icon = NSImageView()
        let cfg = NSImage.SymbolConfiguration(pointSize: 64, weight: .light)
        icon.image = NSImage(systemSymbolName: "moon.zzz.fill", accessibilityDescription: nil)?
            .withSymbolConfiguration(cfg)
        icon.contentTintColor = .secondaryLabelColor

        let title = NSTextField(labelWithString: "Vibey's asleep")
        title.font = .systemFont(ofSize: 26, weight: .semibold)
        let sub = NSTextField(labelWithString: "The dashboard on localhost:8770 isn't answering.")
        sub.textColor = .secondaryLabelColor
        let button = NSButton(title: "Start Vibey", target: self, action: #selector(start))
        button.bezelStyle = .rounded
        button.controlSize = .large
        button.keyEquivalent = "\r"
        status.textColor = .tertiaryLabelColor

        let stack = NSStackView(views: [icon, title, sub, button, status])
        stack.orientation = .vertical
        stack.spacing = 12
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: centerYAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    @objc private func start() {
        status.stringValue = "Starting… (log: /tmp/start_wonder.log)"
        onStart?()
    }
    func reset() { status.stringValue = "" }
}

// MARK: - Main window

final class MainWindowController: NSWindowController, WKNavigationDelegate {
    let web = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
    let asleep = AsleepView(frame: .zero)
    private var showingDashboard = false

    init() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "Vibey"
        w.minSize = NSSize(width: 480, height: 360)
        w.center()
        w.setFrameAutosaveName("VibeyMainWindow")
        w.isReleasedWhenClosed = false
        super.init(window: w)
        web.navigationDelegate = self
        asleep.onStart = { [weak self] in
            startVibey()
            self?.waitForDashboard(tries: 60)
        }
        w.contentView = asleep
    }
    required init?(coder: NSCoder) { fatalError() }

    /// Probe the dashboard; load it if up, otherwise show the asleep view.
    func refresh() {
        var req = URLRequest(url: dashboardURL)
        req.timeoutInterval = 3
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            let up = (resp as? HTTPURLResponse) != nil
            DispatchQueue.main.async { up ? self.showDashboard() : self.showAsleep() }
        }.resume()
    }

    func showDashboard() {
        if !showingDashboard {
            window?.contentView = web
            showingDashboard = true
            web.load(URLRequest(url: dashboardURL))
        }
    }

    func showAsleep() {
        showingDashboard = false
        asleep.reset()
        window?.contentView = asleep
    }

    func reload() {
        if showingDashboard { web.reload() } else { refresh() }
    }

    private func waitForDashboard(tries: Int) {
        guard tries > 0 else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            var req = URLRequest(url: dashboardURL)
            req.timeoutInterval = 2
            URLSession.shared.dataTask(with: req) { _, resp, _ in
                DispatchQueue.main.async {
                    if resp != nil { self.showDashboard() } else { self.waitForDashboard(tries: tries - 1) }
                }
            }.resume()
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showAsleep()
    }
}

// MARK: - App delegate / menu bar

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    var statusItem: NSStatusItem!
    var main: MainWindowController!
    var state: VibeyState = .down
    var privacyAvailable = false
    var privacyOn = false
    var timer: Timer?

    let stateItem = NSMenuItem(title: "Vibey: …", action: nil, keyEquivalent: "")
    lazy var wakeItem = NSMenuItem(title: "Wake", action: #selector(wake), keyEquivalent: "")
    lazy var sleepItem = NSMenuItem(title: "Sleep", action: #selector(sleep), keyEquivalent: "")
    lazy var privacyItem = NSMenuItem(title: "Privacy Mode", action: #selector(togglePrivacy), keyEquivalent: "")

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMainMenu()
        main = MainWindowController()
        main.showWindow(nil)
        main.refresh()
        NSApp.activate(ignoringOtherApps: true)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.autoenablesItems = false
        menu.delegate = self
        stateItem.isEnabled = false
        menu.addItem(stateItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open Dashboard", action: #selector(openDashboard), keyEquivalent: "d")
        menu.addItem(.separator())
        menu.addItem(wakeItem)
        menu.addItem(sleepItem)
        menu.addItem(privacyItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Start Vibey", action: #selector(start), keyEquivalent: "")
        menu.addItem(withTitle: "Stop Vibey", action: #selector(stop), keyEquivalent: "")
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        for item in menu.items where item.action != nil && item.target == nil
            && item.action != #selector(NSApplication.terminate(_:)) { item.target = self }
        wakeItem.target = self; sleepItem.target = self; privacyItem.target = self
        statusItem.menu = menu
        apply()

        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.poll() }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ s: NSApplication, hasVisibleWindows: Bool) -> Bool {
        openDashboard(); return true
    }

    func buildMainMenu() {
        let bar = NSMenu()
        let appItem = NSMenuItem(); bar.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit Vibey", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let editItem = NSMenuItem(); bar.addItem(editItem)
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        let viewItem = NSMenuItem(); bar.addItem(viewItem)
        let view = NSMenu(title: "View")
        let r = view.addItem(withTitle: "Reload", action: #selector(reloadPage), keyEquivalent: "r")
        r.target = self
        viewItem.submenu = view
        let winItem = NSMenuItem(); bar.addItem(winItem)
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winItem.submenu = win
        NSApp.mainMenu = bar
        NSApp.windowsMenu = win
    }

    // MARK: polling

    func poll() {
        request("/state", timeout: 3) { code, data in
            guard code == 200, let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                self.state = .down; self.apply(); return
            }
            if (obj["off"] as? Bool) == true { self.state = .off }
            else if (obj["asleep"] as? Bool) == true { self.state = .asleep }
            else { self.state = .awake }
            self.apply()
        }
        // Privacy lives in /dials (not /state). No "privacy" key there means the
        // endpoint isn't deployed, so the menu item is grayed out.
        request("/dials", timeout: 3) { code, data in
            if code == 200, let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let p = obj["privacy"] as? Bool {
                self.privacyAvailable = true; self.privacyOn = p
            } else {
                self.privacyAvailable = false; self.privacyOn = false
            }
            self.apply()
        }
    }

    func apply() {
        let symbol: String
        switch state {
        case .awake: symbol = privacyOn ? "eye.slash" : "antenna.radiowaves.left.and.right"
        case .asleep: symbol = privacyOn ? "eye.slash" : "moon.zzz"
        case .off: symbol = "power"
        case .down: symbol = "antenna.radiowaves.left.and.right.slash"
        }
        let img = NSImage(systemSymbolName: symbol, accessibilityDescription: "Vibey \(state.rawValue)")
        img?.isTemplate = true
        statusItem?.button?.image = img
        statusItem?.button?.toolTip = "Vibey: \(state.rawValue)"
        stateItem.title = "Vibey: \(state.rawValue)" + (privacyOn ? " · privacy on" : "")
        let reachable = state != .down
        wakeItem.isEnabled = reachable && state != .awake
        sleepItem.isEnabled = reachable && state == .awake
        privacyItem.isEnabled = reachable && privacyAvailable
        privacyItem.state = privacyOn ? .on : .off
        privacyItem.title = privacyAvailable ? "Privacy Mode" : "Privacy Mode (unavailable)"
    }

    func menuWillOpen(_ menu: NSMenu) { poll() }

    // MARK: actions

    @objc func openDashboard() {
        NSApp.activate(ignoringOtherApps: true)
        main.showWindow(nil)
        main.window?.makeKeyAndOrderFront(nil)
        main.refresh()
    }
    @objc func reloadPage() { main.reload() }
    @objc func wake() { request("/wake", method: "POST", timeout: 15) { _, _ in self.poll() } }
    @objc func sleep() { request("/sleep", method: "POST", timeout: 15) { _, _ in self.poll() } }
    @objc func togglePrivacy() {
        let want = !privacyOn
        request("/privacy", method: "POST", json: ["on": want]) { code, _ in
            if code == 404 { self.privacyAvailable = false }
            else if (200..<300).contains(code) { self.privacyOn = want }
            self.apply(); self.poll()
        }
    }
    @objc func start() {
        startVibey()
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) { self.poll(); self.main.refresh() }
    }
    @objc func stop() {
        let a = NSAlert()
        a.messageText = "Stop Vibey?"
        a.informativeText = "Puts the robot to sleep and stops every Vibey service."
        a.addButton(withTitle: "Stop"); a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        stopVibey()
        DispatchQueue.main.asyncAfter(deadline: .now() + 6) { self.poll(); self.main.refresh() }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
