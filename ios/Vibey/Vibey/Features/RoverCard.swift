import SwiftUI

/// Car controls right under the camera on Home, so you can watch where the
/// rover goes while you drive it. Same gas and steering as Drive > Car, and
/// the same rule: let go and it stops.
struct RoverCard: View {
    @EnvironmentObject var store: Store
    @Environment(\.scenePhase) private var phase
    @StateObject private var driver = RoverDriver()
    @AppStorage("vibey.wspeed") private var speed = "medium"
    let info: RoverInfo?

    private var off: Bool { info?.online == false }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Drive").font(.system(.headline, design: .rounded))
                Spacer()
                Text(driver.note ?? info?.battery.map { "🔋 " + $0 } ?? (off ? "rover not answering" : "connected"))
                    .font(.system(.caption, design: .rounded).monospacedDigit())
                    .foregroundStyle(driver.note == nil && !off ? Palette.inkDim : Palette.bad)
                    .lineLimit(1)
            }
            HStack(alignment: .center, spacing: 12) {
                CarTrack(vertical: true, label: "GAS", long: 190, driver: driver, disabled: off)
                Spacer(minLength: 0)
                CarTrack(vertical: false, label: "STEER", long: 190, driver: driver, disabled: off)
            }
            HStack(spacing: 10) {
                Picker("Speed", selection: $speed) {
                    Text("Slow").tag("slow"); Text("Med").tag("medium"); Text("Fast").tag("fast")
                }
                .pickerStyle(.segmented)
                Button { driver.halt() } label: {
                    Label("STOP", systemImage: "stop.fill")
                        .font(.system(.subheadline, design: .rounded).weight(.black))
                        .padding(.horizontal, 14).padding(.vertical, 8)
                        .background(Capsule().fill(Palette.bad))
                        .foregroundStyle(.white)
                }
                .buttonStyle(PressScale())
                .accessibilityLabel("Stop")
            }
        }
        .shell()
        .onAppear { driver.api = store.api; driver.speed = speed }
        .onChange(of: speed) { _, s in driver.speed = s }
        .onChange(of: phase) { _, p in if p != .active { driver.letGo() } }
        .onDisappear { driver.letGo() }
    }
}
