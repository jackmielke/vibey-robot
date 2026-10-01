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

    private var pollTask: Task<Void, Never>?

    init() {
        host = Keychain.get("host") ?? Secrets.host
        token = Keychain.get("token") ?? Secrets.token
    }

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
        } catch {
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
            } catch {
                Haptics.fail()
                flash(error.localizedDescription)
            }
            busy = false
            await refresh()
        }
    }

    func flash(_ msg: String) {
        withAnimation(.spring) { toast = msg }
        Task {
            try? await Task.sleep(for: .seconds(2.6))
            if toast == msg { withAnimation(.easeOut) { toast = nil } }
        }
    }
}
