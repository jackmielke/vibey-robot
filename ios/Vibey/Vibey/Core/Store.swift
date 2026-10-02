import Foundation
import SwiftUI

enum RobotStatus: Equatable {
    case unknown, unreachable(String), off, asleep, awake

    var title: String {
        switch self {
        case .unknown: return "Connecting"
        case .unreachable: return "Unreachable"
        case .off: return "Off"
        case .asleep: return "Asleep"
        case .awake: return "Awake"
        }
    }
}

/// One poller for the whole app: GET /state every 2s while in the foreground.
@MainActor
final class Store: ObservableObject {
    @Published var host: String
    @Published var token: String
    @Published var state: VibeyState?
    @Published var status: RobotStatus = .unknown
    @Published var busy = false
    @Published var toast: String?
    @Published var events: [VibeEvent] = []
    private var eventsLast = 0

    private var pollTask: Task<Void, Never>?

    init() {
        host = Keychain.get("host") ?? Secrets.host
        token = Keychain.get("token") ?? Secrets.token
    }

    /// Privacy is assumed ON until the Mac says otherwise: no frames on a guess.
    var privacy: Bool { state?.privacy ?? true }

    var api: VibeyAPI { VibeyAPI(host: host.trimmingCharacters(in: .whitespaces),
                                 token: token.trimmingCharacters(in: .whitespacesAndNewlines)) }

    func save(host: String, token: String) {
        self.host = host.trimmingCharacters(in: .whitespaces)
        self.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        Keychain.set("host", self.host)
        Keychain.set("token", self.token)
        Task { await refresh() }
    }

    func startPolling() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stopPolling() { pollTask?.cancel(); pollTask = nil }

    func refresh() async {
        do {
            let s = try await api.state()
            state = s
            status = (s.off ?? false) ? .off : (s.asleep ?? true) ? .asleep : .awake
            await refreshEvents()
        } catch {
            if error.isCancellation { return }
            status = .unreachable(error.localizedDescription)
        }
    }

    /// Run an action with haptics and a toast on failure, then re-poll.
    func run(_ label: String? = nil, _ action: @escaping (VibeyAPI) async throws -> Void) {
        Haptics.tap()
        busy = true
        Task {
            do {
                try await action(api)
                Haptics.ok()
                if let label { flash(label) }
            } catch where !error.isCancellation {
                Haptics.fail()
                flash(error.localizedDescription)
            } catch {}
            busy = false
            await refresh()
        }
    }

    /// The power button. One request at a time, and the target is checked
    /// against a FRESH /state rather than the status painted 2s ago, so a stale
    /// screen can't send the opposite of what you meant.
    func setAwake(_ want: Bool) {
        guard !busy else { return }
        run(want ? "Waking up" : "Goodnight") { api in
            let s = try await api.state()
            let off = s.off ?? false, asleep = s.asleep ?? true
            if want {
                if off { try await api.setOff(false); return }
                try await api.wake()      // awake already: re-arms a limp body
            } else if !off && !asleep {
                try await api.sleep()
            }
        }
    }

    /// Vibey's stream of consciousness, appended; kept to the last 300.
    func refreshEvents() async {
        guard let r = try? await api.events(since: eventsLast) else { return }
        if r.last < eventsLast { eventsLast = 0; return }   // chat service restarted
        guard !r.events.isEmpty else { return }
        let have = Set(events.map(\.id))
        events.append(contentsOf: r.events.filter { !have.contains($0.id) })
        if events.count > 300 { events.removeFirst(events.count - 300) }
        eventsLast = max(eventsLast, r.last)
    }

    func flash(_ msg: String) {
        withAnimation(.spring) { toast = msg }
        Task {
            try? await Task.sleep(for: .seconds(2.6))
            if toast == msg { withAnimation(.easeOut) { toast = nil } }
        }
    }
}
