struct FetchCountriesUseCase {
    private let repository: CountriesRepository

    init(repository: CountriesRepository) {
        self.repository = repository
    }

    func execute() async throws -> [Country] {
        try await repository.fetchAll()
    }
}
