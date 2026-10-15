import SwiftUI
import UIKit

/// What Vibey sees: a live MJPEG stream from the camera bridge (:8771).
/// With privacy on it never asks for a frame, and the Mac refuses anyway.
struct CameraCard: View {
    @EnvironmentObject var store: Store
    @State private var full = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Eyes").font(.system(.headline, design: .rounded))
                Spacer()
                Text(!store.cameraOn ? "off" : store.privacy ? "closed" : "live")
                    .font(.system(.caption, design: .rounded).weight(.heavy))
                    .textCase(.uppercase)
                    .foregroundStyle(store.privacy || !store.cameraOn ? Palette.inkDim : Palette.ink)
            }
            .padding(.horizontal, 6)
            CameraFeed(active: !full)
                .onTapGesture {
                    guard !store.privacy, store.cameraOn else { return }
                    Haptics.soft(); full = true
                }
        }
        .shell(padding: 10)
        .fullScreenCover(isPresented: $full) {
            ZStack(alignment: .topTrailing) {
                Color.black.ignoresSafeArea()
                CameraFeed(active: true, corner: 0)
                    .frame(maxHeight: .infinity)
                Button { Haptics.soft(); full = false } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 16, weight: .bold))
                        .frame(width: 44, height: 44)
                        .background(Circle().fill(.ultraThinMaterial))
                        .foregroundStyle(.white)
                }
                .padding(18)
            }
            .environmentObject(store)
            .onChange(of: store.privacy) { _, p in if p { full = false } }
        }
    }
}

struct CameraFeed: View {
    @EnvironmentObject var store: Store
    @Environment(\.scenePhase) private var phase
    var active: Bool
    var corner: CGFloat = 20

    @State private var image: UIImage?
    @State private var problem: String?
    @State private var refused = false
    @State private var fps: Double = 0

    private var closed: Bool { store.privacy || refused || !store.cameraOn }
    private var key: String { "\(store.privacy)-\(store.cameraOn)-\(active)-\(phase == .active)-\(store.host)" }

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: corner, style: .continuous)
                .fill(LinearGradient(colors: [Palette.ink, Color(red: 0.05, green: 0.06, blue: 0.12)],
                                     startPoint: .top, endPoint: .bottom))
            if !store.cameraOn {
                CameraOff()
            } else if closed {
                EyesClosed()
            } else if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .clipShape(RoundedRectangle(cornerRadius: corner, style: .continuous))
                    .overlay(alignment: .topLeading) { liveBadge.padding(10) }
            } else {
                VStack(spacing: 8) {
                    if problem == nil { ProgressView().tint(.white) }
                    else { Image(systemName: "video.slash.fill").font(.title2).foregroundStyle(.white.opacity(0.6)) }
                    Text(problem ?? "Opening eyes…")
                        .font(.system(.footnote, design: .rounded))
                        .foregroundStyle(.white.opacity(0.6))
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 20)
                }
            }
        }
        .aspectRatio(16 / 9, contentMode: .fit)
        .animation(.easeInOut(duration: 0.25), value: closed)
        .task(id: key) { await loop() }
    }

    private var liveBadge: some View {
        HStack(spacing: 5) {
            Circle().fill(Palette.live).frame(width: 7, height: 7).shadow(color: Palette.live, radius: 4)
            Text(fps > 0 ? "LIVE · \(Int(fps.rounded())) fps" : "LIVE")
                .font(.system(size: 11, weight: .heavy, design: .rounded))
        }
        .padding(.horizontal, 9).padding(.vertical, 5)
        .background(Capsule().fill(.black.opacity(0.45)))
        .foregroundStyle(.white)
    }

    private func loop() async {
        refused = false
        guard active, phase == .active, !store.privacy, store.cameraOn else {
            image = nil
            return
        }
        var stamps: [Date] = []
        while !Task.isCancelled {
            if store.privacy || !store.cameraOn { image = nil; return }   // never fetch with eyes closed
            do {
                // One long MJPEG stream, newest frame only. Replaces polling
                // /frame.jpg, which paid a full request per frame and topped
                // out around 6 fps of 1280px frames.
                for try await data in store.api.frameStream() {
                    if store.privacy { image = nil; return }
                    guard let img = UIImage(data: data) else { continue }
                    image = img
                    problem = nil
                    stamps.append(Date())
                    stamps.removeAll { $0.timeIntervalSinceNow < -2 }
                    fps = Double(stamps.count) / 2
                }
                // The Mac ended the stream (privacy on, or the bridge restarted).
                try? await Task.sleep(for: .milliseconds(500))
            } catch APIError.eyesClosed {
                image = nil
                refused = true
                return          // the next privacy change restarts the loop
            } catch where error.isCancellation {
                return
            } catch {
                image = nil
                problem = error.localizedDescription
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }
}

/// Two shut eyes and a word. Shown instead of the camera in privacy mode.
/// The camera switch is off: no video session exists at all.
struct CameraOff: View {
    @EnvironmentObject var store: Store

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "video.slash.fill")
                .font(.system(size: 30, weight: .bold))
                .foregroundStyle(.white.opacity(0.7))
            VStack(spacing: 3) {
                Text("Camera off")
                    .font(.system(.headline, design: .rounded))
                    .foregroundStyle(.white)
                Text("No video on the robot or the Mac.")
                    .font(.system(.caption, design: .rounded))
                    .foregroundStyle(.white.opacity(0.55))
            }
            Button {
                Haptics.tap()
                store.run("Camera on") { _ = try await $0.setDials(["camera": true]) }
            } label: {
                Text("Turn on")
                    .font(.system(.subheadline, design: .rounded).weight(.bold))
                    .padding(.horizontal, 16).padding(.vertical, 8)
                    .background(Capsule().fill(.white.opacity(0.15)))
                    .foregroundStyle(.white)
            }
            .buttonStyle(.plain)
        }
    }
}

struct EyesClosed: View {
    var body: some View {
        VStack(spacing: 14) {
            HStack(spacing: 34) {
                lid
                lid
            }
            VStack(spacing: 3) {
                Text("Eyes closed")
                    .font(.system(.headline, design: .rounded))
                    .foregroundStyle(.white)
                Text("Privacy is on. No frames leave the Mac.")
                    .font(.system(.caption, design: .rounded))
                    .foregroundStyle(.white.opacity(0.55))
            }
        }
    }

    private var lid: some View {
        Path { p in
            p.move(to: CGPoint(x: 0, y: 0))
            p.addQuadCurve(to: CGPoint(x: 46, y: 0), control: CGPoint(x: 23, y: 22))
        }
        .stroke(Palette.duck, style: StrokeStyle(lineWidth: 6, lineCap: .round))
        .frame(width: 46, height: 14)
        .shadow(color: Palette.duck.opacity(0.6), radius: 8)
    }
}
