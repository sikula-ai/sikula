import SwiftUI

struct CountriesListView: View {
    @State var viewModel: CountriesViewModel

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.isLoading {
                    ProgressView()
                } else if let error = viewModel.error {
                    VStack(spacing: 16) {
                        Text(error)
                            .multilineTextAlignment(.center)
                        Button(String(localized: "countries_retry")) {
                            Task { await viewModel.load() }
                        }
                    }
                    .padding()
                } else {
                    List(viewModel.countries) { country in
                        CountryRowView(country: country)
                    }
                }
            }
            .navigationTitle(String(localized: "countries_title"))
        }
        .task { await viewModel.load() }
    }
}
