import Observation

@Observable
final class CountriesViewModel {
    private(set) var countries: [Country] = []
    private(set) var isLoading = true
    private(set) var error: String?

    private let useCase: FetchCountriesUseCase

    init(useCase: FetchCountriesUseCase) {
        self.useCase = useCase
    }

    @MainActor
    func load() async {
        isLoading = true
        error = nil
        do {
            countries = try await useCase.execute().sorted { $0.name < $1.name }
        } catch {
            self.error = String(localized: "countries_error")
        }
        isLoading = false
    }
}
