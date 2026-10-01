import SwiftUI

@main
struct VibeyApp: App {
    @StateObject private var store = Store()
    @Environment(\.scenePhase) private var phase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(store)
                .preferredColorScheme(.dark)
                .tint(Palette.duck)
        }
        .onChange(of: phase, initial: true) { _, p in
            if p == .active { store.startPolling() } else { store.stopPolling() }
        }
    }
}

struct RootView: View {
    @EnvironmentObject var store: Store
    @State private var tab = Self.initialTab

    // Screenshot helper: `simctl launch ... -tab chat` opens on that tab.
    private static var initialTab: Int {
        let args = ProcessInfo.processInfo.arguments
        guard let i = args.firstIndex(of: "-tab"), i + 1 < args.count else { return 0 }
        return ["home": 0, "chat": 1, "memories": 2, "drive": 3, "settings": 4][args[i + 1]] ?? 0
    }

    var body: some View {
        ZStack(alignment: .top) {
            TabView(selection: $tab) {
                HomeView().tag(0)
                    .tabItem { Label("Home", systemImage: "sparkles") }
                ChatView().tag(1)
                    .tabItem { Label("Chat", systemImage: "bubble.left.and.bubble.right.fill") }
                MemoriesView().tag(2)
                    .tabItem { Label("Memories", systemImage: "brain.head.profile") }
                DriveView().tag(3)
                    .tabItem { Label("Drive", systemImage: "dpad.fill") }
                SettingsView().tag(4)
                    .tabItem { Label("Settings", systemImage: "gearshape.fill") }
            }
            .onChange(of: tab) { _, _ in Haptics.soft() }

            if let toast = store.toast {
                Text(toast)
                    .font(.system(.subheadline, design: .rounded).weight(.semibold))
                    .padding(.horizontal, 18).padding(.vertical, 12)
                    .background(Capsule().fill(.ultraThinMaterial))
                    .overlay(Capsule().stroke(.white.opacity(0.15)))
                    .foregroundStyle(.white)
                    .padding(.top, 8)
                    .padding(.horizontal, 24)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
    }
}

/// Starfield behind every screen.
struct SpaceScreen<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ZStack {
            Starfield()
            content
        }
    }
}
