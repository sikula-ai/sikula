import Foundation

struct CountriesAPIClient {
    private static let fields = "name,cca2,capital,region,population"
    private static let url = URL(string: "https://restcountries.com/v3.1/all?fields=\(fields)")!

    func fetchAll() async throws -> [CountryDTO] {
        let (data, _) = try await URLSession.shared.data(from: Self.url)
        return try JSONDecoder().decode([CountryDTO].self, from: data)
    }
}
