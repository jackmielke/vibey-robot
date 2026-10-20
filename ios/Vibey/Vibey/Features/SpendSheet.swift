import SwiftUI
import Charts

/// Tap the "$ today" pill: what Vibey has cost, when, on which model, and how
/// much talking that bought.
struct SpendSheet: View {
    @EnvironmentObject var store: Store
    @Environment(\.dismiss) private var dismiss
    @State private var detail: SpendDetail?
    @State private var problem: String?
    @State private var range = "today"

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let d = detail {
                        header(d)
                        Picker("", selection: $range) {
                            Text("Today").tag("today")
                            Text("7 days").tag("week")
                        }
                        .pickerStyle(.segmented)
                        chart(d)
                        models(d)
                        activity(d)
                    } else if let problem {
                        Text(problem).foregroundStyle(Palette.bad)
                    } else {
                        ProgressView().frame(maxWidth: .infinity).padding(.top, 60)
                    }
                }
                .padding(18)
            }
            .navigationTitle("Spend")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .refreshable { await load() }
            .task { await load() }
        }
    }

    private func load() async {
        do { detail = try await store.api.spendDetail(); problem = nil }
        catch { problem = error.localizedDescription }
    }

    private func money(_ v: Double) -> String { String(format: v < 1 ? "$%.3f" : "$%.2f", v) }

    private func header(_ d: SpendDetail) -> some View {
        let cap = d.budget?.daily_cap ?? 3
        let over = d.today >= cap
        return VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(String(format: "$%.2f", d.today))
                    .font(.system(size: 44, weight: .bold, design: .rounded).monospacedDigit())
                    .foregroundStyle(over ? Palette.bad : .primary)
                Text("today").foregroundStyle(.secondary)
                Spacer()
                Text(String(format: "of $%.0f", cap)).foregroundStyle(.secondary)
            }
            ProgressView(value: min(d.today / max(cap, 0.01), 1))
                .tint(over ? Palette.bad : Palette.live)
            HStack(spacing: 10) {
                stat(String(format: "%.0f min", d.voice_minutes_today), "talked (GPT-Live)")
                stat("\(d.voice_replies_today)", "voice replies")
                stat(money(d.days.reduce(0) { $0 + $1.usd }), "last 7 days")
            }
        }
    }

    private func stat(_ big: String, _ small: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(big).font(.system(.headline, design: .rounded).monospacedDigit())
            Text(small).font(.caption2).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color.secondary.opacity(0.1)))
    }

    @ViewBuilder
    private func chart(_ d: SpendDetail) -> some View {
        if range == "today" {
            Chart(d.hours_today) { h in
                BarMark(x: .value("Hour", h.hour), y: .value("$", h.usd))
                    .foregroundStyle(Palette.live)
            }
            .chartXScale(domain: 0...23)
            .chartXAxis { AxisMarks(values: [0, 6, 12, 18, 23]) { v in
                AxisValueLabel { Text(Self.hourLabel(v.as(Int.self) ?? 0)) }
            } }
            .frame(height: 140)
        } else {
            Chart(d.days) { day in
                BarMark(x: .value("Day", day.day), y: .value("$", day.usd))
                    .foregroundStyle(Palette.live)
            }
            .frame(height: 140)
        }
    }

    static func hourLabel(_ h: Int) -> String {
        h == 0 ? "12a" : h < 12 ? "\(h)a" : h == 12 ? "12p" : "\(h - 12)p"
    }

    private func models(_ d: SpendDetail) -> some View {
        let today = range == "today"
        let rows = d.models.filter { (today ? $0.calls_today : $0.calls_week) > 0 }
        return VStack(alignment: .leading, spacing: 8) {
            Text("By model").font(.headline)
            ForEach(rows) { m in
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(m.source.capitalized).font(.subheadline.weight(.semibold))
                        Text(m.model).font(.caption2.monospaced()).foregroundStyle(.secondary)
                    }
                    Spacer()
                    VStack(alignment: .trailing, spacing: 1) {
                        Text(money(today ? m.usd_today : m.usd_week))
                            .font(.subheadline.monospacedDigit())
                        let mins = today ? m.minutes_today : m.minutes_week
                        let calls = today ? m.calls_today : m.calls_week
                        Text(mins > 0 ? String(format: "%.1f min · %d calls", mins, calls) : "\(calls) calls")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                Divider()
            }
            if rows.isEmpty { Text("Nothing yet.").foregroundStyle(.secondary) }
        }
    }

    private func activity(_ d: SpendDetail) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Recent activity").font(.headline).padding(.top, 6)
            ForEach(d.recent) { c in
                HStack {
                    Text(Self.time(c.at)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        .frame(width: 70, alignment: .leading)
                    Text(c.source).font(.caption)
                    Spacer()
                    Text(money(c.usd)).font(.caption.monospacedDigit())
                }
            }
        }
    }

    static func time(_ ts: Double) -> String {
        let f = DateFormatter()
        let d = Date(timeIntervalSince1970: ts)
        f.dateFormat = Calendar.current.isDateInToday(d) ? "h:mm a" : "EEE h:mm"
        return f.string(from: d)
    }
}
