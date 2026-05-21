import SwiftUI

@main
struct CountriesApp: App {
    var body: some Scene {
        WindowGroup {
            CountriesListView(
                viewModel: CountriesViewModel(
                    useCase: FetchCountriesUseCase(
                        repository: CountriesRepositoryImpl(
                            client: CountriesAPIClient()
                        )
                    )
                )
            )
        }
    }
}
