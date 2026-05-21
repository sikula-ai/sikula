protocol CountriesRepository {
    func fetchAll() async throws -> [Country]
}
