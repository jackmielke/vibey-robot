import Foundation

// MARK: - Models (only the fields the app reads; everything optional so a
// server-side rename degrades one label, not the whole screen)

struct Turn: Decodable, Hashable {
    let who: String
    let text: String
    let ts: Double
}

struct VibeyState: Decodable {
    var asleep: Bool?
    var off: Bool?
    var mode: String?
    var muted: Bool?
    var speaking: Bool?
    var listening: Bool?
    var voice_brain: String?
    var privacy: Bool?
    var incognito: Bool?
    var recording: Bool?
    var mic_label: String?
    var transcript: [Turn]?
}

struct Memory: Decodable, Identifiable, Hashable {
    let id: String
    let text: String
    let day: String?
    let time: String?
    let date: String?
    let mtime: Double?
}

private struct MemoriesResponse: Decodable { let memories: [Memory] }
private struct AskResponse: Decodable { let reply: String?; let error: String? }
private struct VolumeResponse: Decodable { let volume: Double? }
private struct DriveResponse: Decodable { let ok: Bool?; let result: String?; let error: String? }

enum APIError: LocalizedError {
    case unauthorized, server(String), unreachable(String)
    var errorDescription: String? {
        switch self {
        case .unauthorized: return "Token rejected (401). Check it in Settings."
        case .server(let m): return m
        case .unreachable(let m): return m
        }
    }
}

// MARK: - Client

struct VibeyAPI {
    var host: String
    var token: String

    private var chat: String { "http://\(host):8772" }
    private var viewer: String { "http://\(host):8770" }

    private func request(_ url: String, method: String = "GET", json: Any? = nil,
                         timeout: TimeInterval = 6) async throws -> Data {
        guard let u = URL(string: url) else { throw APIError.unreachable("Bad address: \(host)") }
        var req = URLRequest(url: u, timeoutInterval: timeout)
        req.httpMethod = method
        if !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let json {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await URLSession.shared.data(for: req)
        } catch {
            throw APIError.unreachable("Can't reach \(host). Same Wi-Fi as the Mac?")
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.unauthorized }
        if code >= 400 {
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError.server(msg ?? "HTTP \(code)")
        }
        return data
    }

    private func post(_ url: String, _ body: [String: Any] = [:], timeout: TimeInterval = 20) async throws {
        _ = try await request(url, method: "POST", json: body, timeout: timeout)
    }

    // Chat service (:8772)
    func state() async throws -> VibeyState {
        try JSONDecoder().decode(VibeyState.self, from: try await request("\(chat)/state", timeout: 4))
    }
    func wake() async throws { try await post("\(chat)/wake", timeout: 40) }
    func sleep() async throws { try await post("\(chat)/sleep", timeout: 40) }
    func setOff(_ off: Bool) async throws { try await post("\(chat)/off", ["off": off], timeout: 30) }
    func setPrivacy(_ on: Bool) async throws { try await post("\(chat)/privacy", ["on": on]) }
    func ask(_ text: String) async throws -> String {
        let data = try await request("\(chat)/ask", method: "POST",
                                     json: ["text": text, "channel": "telegram", "name": "Jack"],
                                     timeout: 120)
        let r = try JSONDecoder().decode(AskResponse.self, from: data)
        if let e = r.error { throw APIError.server(e) }
        return r.reply ?? ""
    }
    func drive(_ action: String, seconds: Double, speed: String) async throws -> String {
        let data = try await request("\(chat)/drive", method: "POST",
                                     json: ["action": action, "seconds": seconds, "speed": speed],
                                     timeout: 10)
        let r = try JSONDecoder().decode(DriveResponse.self, from: data)
        return r.result ?? r.error ?? "ok"
    }

    // Dashboard service (:8770)
    func volume() async throws -> Double? {
        try JSONDecoder().decode(VolumeResponse.self, from: try await request("\(viewer)/volume")).volume
    }
    func setVolume(_ v: Int) async throws { try await post("\(viewer)/volume", ["volume": v]) }
    func memories() async throws -> [Memory] {
        try JSONDecoder().decode(MemoriesResponse.self,
                                 from: try await request("\(viewer)/memories", timeout: 15)).memories
    }
    func addMemory(_ text: String) async throws { try await post("\(viewer)/memories", ["text": text]) }
    func editMemory(_ id: String, _ text: String) async throws {
        try await post("\(viewer)/memories", ["id": id, "text": text])
    }
    func deleteMemory(_ id: String) async throws {
        let q = id.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? id
        _ = try await request("\(viewer)/memories?id=\(q)", method: "DELETE", timeout: 15)
    }
}
