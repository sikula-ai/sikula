import SwiftUI

struct CountryRowView: View {
    let country: Country

    var body: some View {
        HStack(spacing: 12) {
            Text(country.flagEmoji)
                .font(.title2)
            VStack(alignment: .leading, spacing: 2) {
                Text(country.name)
                    .font(.body)
                Text("\(country.region) · \(country.capital ?? "—")")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }
}
