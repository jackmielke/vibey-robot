import Foundation

// MARK: - Models (only the fields the app reads; everything optional so a
// server-side rename degrades one label, not the whole screen)

struct Turn: Decodable, Hashable {
    let who: String
    let text: String
    let ts: Double
}

/// One line of Vibey's stream of consciousness (GET :8772/events).
/// `detail` is free-form JSON, kept as pretty text for the expanded view.
struct VibeEvent: Identifiable, Hashable {
    let id: Int
    let kind: String      // thinking | action | telegram | senses | system | chat
    let text: String
    let ts: Double        // ms
    let icon: String
    let detail: String?

    init?(_ d: [String: Any]) {
        guard let id = d["id"] as? Int, let text = d["text"] as? String else { return nil }
        self.id = id
        self.text = text
        kind = d["kind"] as? String ?? "system"
        ts = (d["ts"] as? Double) ?? Double(d["ts"] as? Int ?? 0)
        icon = d["icon"] as? String ?? "•"
        var extra = d["detail"] as? [String: Any] ?? [:]
        if let s = d["source"] as? String, !s.isEmpty { extra["source"] = s }
        if !extra.isEmpty, let data = try? JSONSerialization.data(withJSONObject: extra, options: [.prettyPrinted, .sortedKeys]) {
            detail = String(data: data, encoding: .utf8)
        } else { detail = nil }
    }
}

struct VibeyState: Decodable {
    var camera: Bool?
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
    var stage: Int?
    var scribe: Scribe?
    var switches: [String: Bool]?
    var frontdesk: FrontDesk?
    var transcript: [Turn]?
}

struct FrontDesk: Decodable {
    var on: Bool?
    var guests: Int?
    var checked_in: Int?
}

struct Scribe: Decodable {
    var on: Bool?
    var minutes: Double?
}

struct Memory: Decodable, Identifiable, Hashable {
    let id: String
    let text: String
    let day: String?
    let time: String?
    let date: String?
    let mtime: Double?
}

struct Dials: Decodable {
    var privacy: Bool?
    var camera: Bool?
    var awake: Bool?
    var listening: Bool?
    var face_tracking: Bool?
    var muted: Bool?
    var incognito: Bool?
    var think_aloud: Bool?
    var volume: Int?
    var start_volume: Int?
    var voice_brain: String?
    var mic_source: String?
    var speaker_source: String?
    var voice: Bool?
}

struct Cost: Decodable {
    var today: Double?
    var today_turns: Int?
    var week: Double?
    var hour: Double?
    var month: Double?
    var by_source_today: [String: Double]?
    var unpriced_today: Int?
    var budget: Budget?
    var billed: Billed?
    var blocked: Bool?
    var blocked_why: String?

    struct Budget: Decodable {
        var daily_cap: Double?
        var monthly_cap: Double?
        var fraction: Double?
        var level: String?
        var guard_off: Bool?
    }
    struct Billed: Decodable {
        var available: Bool?
        var today: Double?
        var month: Double?
        var hint: String?
        var error: String?
    }

    static let labels = ["live_voice": "Live voice", "live_backend": "Live brain (gpt-5.5)",
                         "realtime_voice": "Realtime voice", "text_brain": "Texts",
                         "guest_brain": "Guests", "vision": "Vision", "other": "Other"]
}

struct SoundFX: Decodable, Identifiable, Hashable {
    let name: String
    let label: String
    let group: String?
    var id: String { name }
}

struct DJStatus: Decodable, Equatable {
    var track: String?
    var bpm: Double?
    var target_bpm: Double?
    var playing: Bool?
    var position: Double?
    var duration: Double?
    var rate: Double?
    var error: String?
}

struct Friend: Decodable, Identifiable, Hashable {
    let id: String
    var name: String?
    var times_seen: Int?
    var snapshot: String?

    enum CodingKeys: String, CodingKey { case id, name, times_seen, snapshot }
    init(from d: Decoder) throws {
        let c = try d.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        snapshot = try? c.decodeIfPresent(String.self, forKey: .snapshot)
        if let n = try? c.decodeIfPresent(Int.self, forKey: .times_seen) { times_seen = n }
        else if let s = try? c.decodeIfPresent(String.self, forKey: .times_seen) { times_seen = Int(s) }
    }
}

struct Capture: Decodable, Identifiable, Hashable {
    let name: String
    let size: Int?
    var id: String { name }
}

private struct MemoriesResponse: Decodable { let memories: [Memory] }
private struct EmotesResponse: Decodable { let emotes: [String] }
private struct SFXResponse: Decodable { let sfx: [SoundFX] }
private struct FriendsResponse: Decodable { let friends: [Friend] }
private struct TracksResponse: Decodable { let tracks: [String]? }
private struct AskResponse: Decodable { let reply: String?; let error: String? }
private struct TextTurnResponse: Decodable { let delivered: String?; let reply: String?; let spoke: Bool? }
private struct VolumeResponse: Decodable { let volume: Double? }
private struct DriveResponse: Decodable { let ok: Bool?; let result: String?; let error: String? }

enum APIError: LocalizedError {
    case unauthorized, eyesClosed, server(String), unreachable(String)
    var errorDescription: String? {
        switch self {
        case .unauthorized: return "Token rejected (401). Check it in Settings."
        case .eyesClosed: return "Eyes closed (privacy mode)."
        case .server(let m): return m
        case .unreachable(let m): return m
        }
    }

    /// Say what actually went wrong. "Can't reach" for every failure hid a
    /// 15s timeout behind a Wi-Fi question for a whole evening.
    static func transport(_ error: Error, url: URL, timeout: TimeInterval) -> Error {
        let where_ = "\(url.host ?? "?"):\(url.port.map(String.init) ?? "80")\(url.path)"
        guard let u = error as? URLError else { return APIError.unreachable("\(where_): \(error.localizedDescription)") }
        switch u.code {
        case .cancelled:
            return CancellationError()
        case .timedOut:
            return APIError.unreachable("Timed out after \(Int(timeout))s waiting on \(where_).")
        case .cannotConnectToHost:
            return APIError.unreachable("Connection refused at \(where_). Is that service running?")
        case .cannotFindHost, .dnsLookupFailed:
            return APIError.unreachable("Can't find \(url.host ?? "the Mac"). Check the address in Settings.")
        case .notConnectedToInternet:
            return APIError.unreachable("No network. Same Wi-Fi as the Mac?")
        case .networkConnectionLost:
            return APIError.unreachable("Connection dropped mid-request (\(where_)).")
        default:
            return APIError.unreachable("\(where_): \(u.localizedDescription) (\(u.code.rawValue))")
        }
    }
}

// MARK: - Client

struct VibeyAPI {
    var host: String
    var token: String

    private var chat: String { "http://\(host):8772" }
    private var viewer: String { "http://\(host):8770" }
    private var camera: String { "http://\(host):8771" }

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
            throw APIError.transport(error, url: u, timeout: timeout)
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { throw APIError.unauthorized }
        let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        if code == 403, obj?["privacy"] as? Bool == true { throw APIError.eyesClosed }
        if code >= 400 {
            let msg = obj?["error"] as? String
            throw APIError.server(msg.map { "\($0) (HTTP \(code))" } ?? "HTTP \(code) from \(u.path)")
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
    func events(since: Int) async throws -> (events: [VibeEvent], last: Int) {
        let data = try await request("\(chat)/events?since=\(since)", timeout: 4)
        let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
        let list = (obj["events"] as? [[String: Any]] ?? []).compactMap(VibeEvent.init)
        return (list, obj["last"] as? Int ?? since)
    }
    func wake() async throws { try await post("\(chat)/wake", timeout: 40) }
    func sleep() async throws { try await post("\(chat)/sleep", timeout: 40) }
    func setOff(_ off: Bool) async throws { try await post("\(chat)/off", ["off": off], timeout: 30) }
    func setPrivacy(_ on: Bool) async throws { try await post("\(chat)/privacy", ["on": on]) }
    func ask(_ text: String) async throws -> String {
        // Awake: the message joins the live voice conversation, same as a
        // Telegram text. Vibey answers by text and decides itself whether to
        // also say something in the room (🔊 when it did).
        if let d = try? await request("\(chat)/textturn", method: "POST",
                                      json: ["text": text, "who": "Jack"], timeout: 35),
           let t = try? JSONDecoder().decode(TextTurnResponse.self, from: d),
           t.delivered == "voice", let reply = t.reply, !reply.isEmpty {
            return (t.spoke == true ? "🔊 " : "") + reply
        }
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

    // Controls (:8772)
    func dials() async throws -> Dials {
        try JSONDecoder().decode(Dials.self, from: try await request("\(chat)/dials"))
    }
    @discardableResult
    func setDials(_ body: [String: Any]) async throws -> Dials {
        try JSONDecoder().decode(Dials.self, from: try await request("\(chat)/dials", method: "POST",
                                                                     json: body, timeout: 20))
    }
    func setBrain(_ b: String) async throws { try await post("\(chat)/brain", ["brain": b]) }
    func setStage(_ n: Int) async throws { try await post("\(chat)/stage", ["stage": n], timeout: 40) }
    func setScribe(_ on: Bool) async throws { try await post("\(chat)/scribe", ["on": on]) }
    func setSwitch(_ name: String, _ on: Bool) async throws {
        try await post("\(chat)/switch", ["name": name, "on": on])
    }
    func setIncognito(_ on: Bool) async throws { try await post("\(chat)/incognito", ["on": on]) }
    /// Door mode: scans Luma ticket QRs. The Mac refuses (409) while privacy is on.
    func setFrontDesk(_ on: Bool) async throws {
        try await post("\(chat)/frontdesk/\(on ? "on" : "off")", [:], timeout: 20)
    }
    func cost() async throws -> Cost {
        try JSONDecoder().decode(Cost.self, from: try await request("\(chat)/cost"))
    }

    // Play (:8772)
    func emotes() async throws -> [String] {
        try JSONDecoder().decode(EmotesResponse.self, from: try await request("\(chat)/emotes")).emotes
    }
    func sounds() async throws -> [SoundFX] {
        try JSONDecoder().decode(SFXResponse.self, from: try await request("\(chat)/sfx")).sfx
    }
    func emote(_ name: String) async throws { try await post("\(chat)/emote", ["name": name]) }
    func sfx(_ name: String) async throws { try await post("\(chat)/sfx", ["name": name]) }
    func say(_ text: String) async throws { try await post("\(chat)/say", ["text": text]) }
    func djStatus() async throws -> DJStatus {
        try JSONDecoder().decode(DJStatus.self, from: try await request("\(chat)/dj/status", timeout: 4))
    }
    func djTracks() async throws -> [String] {
        try JSONDecoder().decode(TracksResponse.self, from: try await request("\(chat)/dj/tracks")).tracks ?? []
    }
    @discardableResult
    func dj(_ action: String, _ body: [String: Any] = [:]) async throws -> DJStatus {
        try JSONDecoder().decode(DJStatus.self, from: try await request("\(chat)/dj/\(action)", method: "POST",
                                                                        json: body, timeout: 30))
    }

    // Friends, captures, camera
    func friends() async throws -> [Friend] {
        try JSONDecoder().decode(FriendsResponse.self, from: try await request("\(chat)/friends", timeout: 15)).friends
    }
    func nameFriend(_ id: String, _ name: String) async throws {
        try await post("\(chat)/friends/name", ["face_id": id, "name": name])
    }
    func captures() async throws -> [Capture] {
        try JSONDecoder().decode([Capture].self, from: try await request("\(viewer)/captures"))
    }
    func captureFile(_ name: String) async throws -> URL {
        let q = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? name
        let data = try await request("\(viewer)/captures/\(q)", timeout: 60)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        try data.write(to: url, options: .atomic)
        return url
    }
    /// One camera frame. Throws .eyesClosed when the Mac refuses (privacy).
    func frame() async throws -> Data {
        try await request("\(camera)/frame.jpg", timeout: 5)
    }

    /// Live camera as one long MJPEG response (640x360 copy made for phones),
    /// instead of a request per frame. Read greedily and only the NEWEST frame
    /// is kept (bufferingNewest(1)): a frame that arrives while the UI is
    /// busy replaces the one waiting, so the picture never plays back the past.
    /// Throws .eyesClosed if privacy is on; the Mac also just ends the stream
    /// the moment privacy switches on.
    func frameStream() -> AsyncThrowingStream<Data, Error> {
        AsyncThrowingStream(bufferingPolicy: .bufferingNewest(1)) { cont in
            guard let u = URL(string: "\(camera)/stream?size=small") else {
                cont.finish(throwing: APIError.unreachable("Bad address: \(host)"))
                return
            }
            var req = URLRequest(url: u, timeoutInterval: 8)
            if !token.isEmpty { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
            // A delegate session, not URLSession.bytes: on iOS 26 the async byte
            // sequence faults on a never-ending multipart response ("received
            // data before consuming") and cancels it ~100ms in, so the camera
            // sat on "Opening eyes…" forever. Chunks also beat one await per byte.
            let reader = MJPEGReader(url: u, cont: cont)
            let session = URLSession(configuration: .default, delegate: reader, delegateQueue: nil)
            let task = session.dataTask(with: req)
            task.resume()
            cont.onTermination = { _ in task.cancel(); session.invalidateAndCancel() }
        }
    }
}


/// Splits a multipart/x-mixed-replace JPEG stream into frames as chunks arrive.
final class MJPEGReader: NSObject, URLSessionDataDelegate {
    private let url: URL
    private let cont: AsyncThrowingStream<Data, Error>.Continuation
    private var buf = Data()
    private var want = -1

    init(url: URL, cont: AsyncThrowingStream<Data, Error>.Continuation) {
        self.url = url
        self.cont = cont
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 401 { cont.finish(throwing: APIError.unauthorized); completionHandler(.cancel); return }
        if code == 403 { cont.finish(throwing: APIError.eyesClosed); completionHandler(.cancel); return }
        if code >= 400 {
            cont.finish(throwing: APIError.server("HTTP \(code) from /stream"))
            completionHandler(.cancel); return
        }
        // URLSession understands multipart/x-mixed-replace itself: every part
        // arrives as a fresh response followed by just its body, headers
        // stripped. A new response means the previous part is complete.
        flushJPEG()
        completionHandler(.allow)
    }

    /// A bare JPEG body (FFD8 … FFD9) sitting in the buffer: hand it over.
    private func flushJPEG() {
        if buf.count > 4, buf[buf.startIndex] == 0xFF, buf[buf.startIndex + 1] == 0xD8 {
            cont.yield(buf)
        }
        buf.removeAll(keepingCapacity: true)
        want = -1
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        buf.append(data)
        // Split by URLSession: the body alone. Done once it ends in FFD9.
        if want < 0, buf.count > 4, buf[buf.startIndex] == 0xFF, buf[buf.startIndex + 1] == 0xD8 {
            if buf[buf.endIndex - 2] == 0xFF, buf[buf.endIndex - 1] == 0xD9 { flushJPEG() }
            return
        }
        let sep = Data([13, 10, 13, 10])
        while true {
            if want < 0 {
                guard let r = buf.range(of: sep) else {
                    if buf.count > 4096 { buf.removeFirst(buf.count - 4096) }
                    return
                }
                let head = String(decoding: buf[buf.startIndex..<r.lowerBound], as: UTF8.self).lowercased()
                buf.removeSubrange(buf.startIndex..<r.upperBound)
                if let cl = head.range(of: "content-length:") {
                    let n = head[cl.upperBound...].prefix { $0 != "\r" && $0 != "\n" }
                    want = Int(n.trimmingCharacters(in: .whitespaces)) ?? -1
                }
            } else {
                guard buf.count >= want else { return }
                let end = buf.startIndex + want
                cont.yield(Data(buf[buf.startIndex..<end]))
                buf.removeSubrange(buf.startIndex..<end)
                want = -1
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error, (error as NSError).code != NSURLErrorCancelled {
            cont.finish(throwing: APIError.transport(error, url: url, timeout: 8))
        } else {
            cont.finish()
        }
    }
}

// MARK: - Friend profile (face service :8773, same bearer token)

/// One captured photo of a friend: a face-recognition sample. Deleting or
/// moving it changes what Vibey matches against, not just the picture.
struct FacePhoto: Decodable, Identifiable, Hashable {
    let id: String
    let snapshot: String?
    let created_at: String?
}

struct FriendProfile: Decodable {
    let id: String
    var name: String?
    var times_seen: Int?
    var first_seen: String?
    var last_seen: String?
    var snapshot: String?
    var samples: [FacePhoto]?
}

private struct ReassignResponse: Decodable { let face_id: String?; let name: String?; let merged: Bool? }

extension VibeyAPI {
    private var faces: String { "http://\(host):8773" }

    func friendProfile(_ id: String) async throws -> FriendProfile {
        let q = id.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? id
        return try JSONDecoder().decode(FriendProfile.self,
                                        from: try await request("\(faces)/person?face_id=\(q)", timeout: 15))
    }
    /// The Mac refuses to delete someone's last photo (they would become unrecognizable).
    func deletePhoto(_ sampleID: String) async throws {
        try await post("\(faces)/deletesample", ["sample_id": sampleID])
    }
    /// "This is someone else": to an existing person (faceID) or a name
    /// (existing person by that name, else a new one). Returns who it went to.
    @discardableResult
    func reassignPhoto(_ sampleID: String, toFace faceID: String? = nil,
                       name: String? = nil) async throws -> (faceID: String?, name: String?) {
        var body: [String: Any] = ["sample_id": sampleID]
        if let faceID { body["face_id"] = faceID }
        if let name { body["name"] = name }
        let data = try await request("\(faces)/reassignsample", method: "POST", json: body, timeout: 20)
        let r = try JSONDecoder().decode(ReassignResponse.self, from: data)
        return (r.face_id, r.name)
    }
}
