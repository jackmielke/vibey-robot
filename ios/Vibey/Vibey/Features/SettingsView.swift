import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var store: Store
    @State private var host = ""
    @State private var token = ""
    @State private var showToken = false
    @State private var results: [(String, Bool, String)] = []
    @State private var testing = false

    var body: some View {
        SpaceScreen {
            ScrollView {
                VStack(spacing: 16) {
                    ScreenTitle(title: "Settings", subtitle: "Where Vibey's Mac lives")

                    VStack(alignment: .leading, spacing: 12) {
                        Text("Mac address").font(.system(.headline, design: .rounded))
                        field("10.0.0.108", text: $host, secure: false)
                            .keyboardType(.URL)
                        HStack(spacing: 8) {
                            quick("This Wi-Fi IP", Secrets.host)
                            quick("Bonjour", "Jacks-MacBook-Pro-6.local")
                        }
                    }
                    .shell()

                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text("App token").font(.system(.headline, design: .rounded))
                            Spacer()
                            Button { showToken.toggle() } label: {
                                Image(systemName: showToken ? "eye.slash" : "eye")
                            }
                            .foregroundStyle(Palette.inkDim)
                        }
                        field("VIBEY_APP_TOKEN", text: $token, secure: !showToken)
                        Text("Stored in the Keychain. It's VIBEY_APP_TOKEN in the robot's .env.")
                            .font(.system(.footnote, design: .rounded))
                            .foregroundStyle(Palette.inkDim)
                    }
                    .shell()

                    Button {
                        store.save(host: host, token: token)
                        test()
                    } label: {
                        Label(testing ? "Testing…" : "Save & test connection", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .buttonStyle(PillButtonStyle())
                    .disabled(testing)

                    if !results.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(results, id: \.0) { r in
                                HStack(alignment: .top, spacing: 10) {
                                    Image(systemName: r.1 ? "checkmark.circle.fill" : "xmark.octagon.fill")
                                        .foregroundStyle(r.1 ? Color.green : Palette.bad)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(r.0).font(.system(.subheadline, design: .rounded).weight(.semibold))
                                        Text(r.2).font(.system(.footnote, design: .rounded))
                                            .foregroundStyle(Palette.inkDim)
                                    }
                                }
                            }
                        }
                        .shell()
                    }

                    Text("Works on the same Wi-Fi as the Mac. Away from home, put both on Tailscale and use the Mac's Tailscale name here.")
                        .font(.system(.footnote, design: .rounded))
                        .foregroundStyle(.white.opacity(0.45))
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.horizontal, 18)
                .padding(.bottom, 24)
            }
            .scrollIndicators(.hidden)
            .scrollDismissesKeyboard(.interactively)
        }
        .onAppear { host = store.host; token = store.token }
    }

    private func field(_ placeholder: String, text: Binding<String>, secure: Bool) -> some View {
        Group {
            if secure { SecureField(placeholder, text: text) } else { TextField(placeholder, text: text) }
        }
        .textInputAutocapitalization(.never)
        .autocorrectionDisabled()
        .font(.system(.body, design: .monospaced))
        .padding(.horizontal, 14).padding(.vertical, 12)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Palette.ink.opacity(0.06)))
        .foregroundStyle(Palette.ink)
    }

    private func quick(_ label: String, _ value: String) -> some View {
        Button { Haptics.soft(); host = value } label: {
            Text(label)
                .font(.system(.footnote, design: .rounded).weight(.semibold))
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(Capsule().fill(host == value ? Palette.duck : Palette.ink.opacity(0.07)))
                .foregroundStyle(Palette.ink)
        }
        .buttonStyle(.plain)
    }

    private func test() {
        testing = true
        results = []
        let api = VibeyAPI(host: host.trimmingCharacters(in: .whitespaces),
                           token: token.trimmingCharacters(in: .whitespacesAndNewlines))
        Task {
            var out: [(String, Bool, String)] = []
            do {
                let s = try await api.state()
                let st = (s.off ?? false) ? "off" : (s.asleep ?? true) ? "asleep" : "awake"
                out.append(("Voice service :8772", true, "Vibey is \(st)"))
            } catch { out.append(("Voice service :8772", false, error.localizedDescription)) }
            do {
                let m = try await api.memories()
                out.append(("Dashboard :8770", true, "\(m.count) memories"))
            } catch { out.append(("Dashboard :8770", false, error.localizedDescription)) }
            results = out
            out.allSatisfy(\.1) ? Haptics.ok() : Haptics.fail()
            testing = false
        }
    }
}
