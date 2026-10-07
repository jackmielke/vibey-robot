import Foundation

/// Everything behind the "$ today" pill (GET :8772/cost/detail).
struct SpendDetail: Decodable {
    struct Day: Decodable, Identifiable {
        var day: String
        var date: String
        var usd: Double
        var calls: Int
        var minutes: Double
        var id: String { date }
    }
    struct Hour: Decodable, Identifiable {
        var hour: Int
        var usd: Double
        var id: Int { hour }
    }
    struct Model: Decodable, Identifiable {
        var model: String
        var source: String
        var usd_today: Double
        var usd_week: Double
        var calls_today: Int
        var calls_week: Int
        var minutes_today: Double
        var minutes_week: Double
        var id: String { model + source }
    }
    struct Charge: Decodable, Identifiable {
        var at: Double
        var source: String
        var model: String
        var usd: Double
        var id: String { "\(at)-\(model)" }
    }
    var today: Double
    var days: [Day]
    var hours_today: [Hour]
    var models: [Model]
    var voice_minutes_today: Double
    var voice_replies_today: Int
    var recent: [Charge]
    var budget: Cost.Budget?
}

extension VibeyAPI {
    func spendDetail() async throws -> SpendDetail {
        guard let u = URL(string: "http://\(host):8772/cost/detail") else {
            throw APIError.unreachable("Bad address: \(host)")
        }
        var req = URLRequest(url: u, timeoutInterval: 15)
        if !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let (data, resp) = try await URLSession.shared.data(for: req)
        if (resp as? HTTPURLResponse)?.statusCode == 401 { throw APIError.unauthorized }
        return try JSONDecoder().decode(SpendDetail.self, from: data)
    }
}

/// Music playing on the Mac (Spotify) — GET/POST :8772/macmusic.
struct MacMusic: Decodable {
    var state: String?
    var volume: Int?
    var track: String?
    var artist: String?
}

extension VibeyAPI {
    func macMusic(_ body: [String: Any]? = nil) async throws -> MacMusic {
        guard let u = URL(string: "http://\(host):8772/macmusic") else {
            throw APIError.unreachable("Bad address: \(host)")
        }
        var req = URLRequest(url: u, timeoutInterval: 10)
        if !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, _) = try await URLSession.shared.data(for: req)
        return try JSONDecoder().decode(MacMusic.self, from: data)
    }
}
