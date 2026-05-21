struct CountriesRepositoryImpl: CountriesRepository {
    private let client: CountriesAPIClient

    init(client: CountriesAPIClient) {
        self.client = client
    }

    func fetchAll() async throws -> [Country] {
        try await client.fetchAll().map(\.asDomain)
    }
}

private extension CountryDTO {
    var asDomain: Country {
        Country(
            cca2: cca2,
            name: name.common,
            capital: capital?.first,
            region: region,
            population: population,
            flagEmoji: cca2.toFlagEmoji()
        )
    }
}

private extension String {
    func toFlagEmoji() -> String {
        uppercased().unicodeScalars.compactMap {
            Unicode.Scalar($0.value - 65 + 0x1F1E6)
        }.map(String.init).joined()
    }
}
