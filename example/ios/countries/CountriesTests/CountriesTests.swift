import XCTest
@testable import Countries

final class FetchCountriesUseCaseTests: XCTestCase {
    func testExecuteReturnsCountriesFromRepository() async throws {
        let expected = [Country.fixture()]
        let repo = MockCountriesRepository(result: .success(expected))
        let useCase = FetchCountriesUseCase(repository: repo)

        let result = try await useCase.execute()

        XCTAssertEqual(result.count, 1)
        XCTAssertEqual(result[0].cca2, "DE")
        XCTAssertEqual(result[0].name, "Germany")
    }

    func testExecutePropagatesRepositoryError() async {
        let repo = MockCountriesRepository(result: .failure(URLError(.badURL)))
        let useCase = FetchCountriesUseCase(repository: repo)

        do {
            _ = try await useCase.execute()
            XCTFail("Expected error to be thrown")
        } catch {
            XCTAssertTrue(error is URLError)
        }
    }
}

final class CountryModelTests: XCTestCase {
    func testCapitalFallbackWhenNil() {
        let country = Country.fixture(capital: nil)
        XCTAssertNil(country.capital)
    }

    func testFlagEmojiComputedFromCca2() {
        let country = Country.fixture()
        XCTAssertEqual(country.flagEmoji, "🇩🇪")
    }
}

final class CountriesRepositoryImplTests: XCTestCase {
    func testFetchAllReturnsMappedRemoteCountries() async throws {
        let repository = CountriesRepositoryImpl(fetchDTOs: {
            [
                CountryDTO(
                    name: CountryDTO.Name(common: "Germany"),
                    cca2: "DE",
                    capital: ["Berlin"],
                    region: "Europe",
                    population: 83_240_525
                )
            ]
        })

        let result = try await repository.fetchAll()

        XCTAssertEqual(result.map(\.name), ["Germany"])
        XCTAssertEqual(result.first?.flagEmoji, "🇩🇪")
    }

    func testFetchAllFallsBackToLocalCountriesWhenRemoteFails() async throws {
        let repository = CountriesRepositoryImpl(fetchDTOs: {
            throw URLError(.cannotDecodeContentData)
        })

        let result = try await repository.fetchAll()

        XCTAssertEqual(result.map(\.name), FallbackCountryDTOs.countries.map(\.name.common))
    }
}

final class FallbackCountryDTOsTests: XCTestCase {
    func testFallbackCountriesUseRemoteDTOShape() {
        XCTAssertEqual(FallbackCountryDTOs.countries.first?.name.common, "Argentina")
        XCTAssertEqual(FallbackCountryDTOs.countries.first?.cca2, "AR")
        XCTAssertEqual(FallbackCountryDTOs.countries.first?.capital, ["Buenos Aires"])
    }

    func testFallbackCountryLookupIsCaseInsensitive() {
        XCTAssertEqual(FallbackCountryDTOs.country(cca2: "de")?.name.common, "Germany")
    }
}

@MainActor
final class CountriesViewModelTests: XCTestCase {
    func testInitialStateHasIsLoadingTrue() {
        let repo = MockCountriesRepository(result: .success([]))
        let viewModel = CountriesViewModel(useCase: FetchCountriesUseCase(repository: repo))

        XCTAssertTrue(viewModel.isLoading)
        XCTAssertTrue(viewModel.countries.isEmpty)
        XCTAssertNil(viewModel.error)
    }

    func testLoadSortsCountriesAlphabetically() async throws {
        let unsorted = [
            Country.fixture(cca2: "DE", name: "Germany"),
            Country.fixture(cca2: "AT", name: "Austria"),
            Country.fixture(cca2: "FR", name: "France"),
        ]
        let repo = MockCountriesRepository(result: .success(unsorted))
        let viewModel = CountriesViewModel(useCase: FetchCountriesUseCase(repository: repo))

        await viewModel.load()

        XCTAssertEqual(viewModel.countries.map(\.name), ["Austria", "France", "Germany"])
    }

    func testLoadSetsErrorOnFailure() async {
        let repo = MockCountriesRepository(result: .failure(URLError(.badURL)))
        let viewModel = CountriesViewModel(useCase: FetchCountriesUseCase(repository: repo))

        await viewModel.load()

        XCTAssertNotNil(viewModel.error)
        XCTAssertTrue(viewModel.countries.isEmpty)
    }
}

// MARK: - Helpers

private struct MockCountriesRepository: CountriesRepository {
    let result: Result<[Country], Error>
    func fetchAll() async throws -> [Country] { try result.get() }
}

private extension Country {
    static func fixture(
        cca2: String = "DE",
        name: String = "Germany",
        capital: String? = "Berlin"
    ) -> Country {
        Country(
            cca2: cca2,
            name: name,
            capital: capital,
            region: "Europe",
            population: 83_240_525,
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
