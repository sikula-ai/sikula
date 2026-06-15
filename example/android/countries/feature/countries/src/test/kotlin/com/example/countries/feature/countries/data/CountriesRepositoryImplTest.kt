package com.example.countries.feature.countries.data

import com.example.countries.feature.countries.model.countryFixture
import com.example.countries.library.testing.AbstractTest
import io.kotest.matchers.shouldBe
import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Test

internal class CountriesRepositoryImplTest : AbstractTest() {

    private val api = mockk<CountriesApi>()
    private val repository = CountriesRepositoryImpl(api)

    @Test
    fun `fetchCountries returns mapped countries on success`() = runTest {
        val dto = CountryDto(
            name = NameDto(common = "Germany"),
            cca2 = "DE",
            capital = listOf("Berlin"),
            region = "Europe",
            population = 83_149_545,
        )
        coEvery { api.fetchCountries() } returns listOf(dto)

        val result = repository.fetchCountries()

        result.getOrThrow() shouldBe listOf(countryFixture())
    }

    @Test
    fun `fetchCountries maps null capital to null`() = runTest {
        val dto = CountryDto(
            name = NameDto(common = "Germany"),
            cca2 = "DE",
            capital = null,
            region = "Europe",
            population = 83_149_545,
        )
        coEvery { api.fetchCountries() } returns listOf(dto)

        val result = repository.fetchCountries()

        result.getOrThrow().first().capital shouldBe null
    }

    @Test
    fun `fetchCountries maps empty capital list to null`() = runTest {
        val dto = CountryDto(
            name = NameDto(common = "Germany"),
            cca2 = "DE",
            capital = emptyList(),
            region = "Europe",
            population = 83_149_545,
        )
        coEvery { api.fetchCountries() } returns listOf(dto)

        val result = repository.fetchCountries()

        result.getOrThrow().first().capital shouldBe null
    }

    @Test
    fun `fetchCountries returns empty list when api returns empty list`() = runTest {
        coEvery { api.fetchCountries() } returns emptyList()

        val result = repository.fetchCountries()

        result.getOrThrow() shouldBe emptyList()
    }

    @Test
    fun `fetchCountries takes first capital when list has multiple entries`() = runTest {
        val dto = CountryDto(
            name = NameDto(common = "Netherlands"),
            cca2 = "NL",
            capital = listOf("Amsterdam", "The Hague"),
            region = "Europe",
            population = 17_590_672,
        )
        coEvery { api.fetchCountries() } returns listOf(dto)

        val result = repository.fetchCountries()

        result.getOrThrow().first().capital shouldBe "Amsterdam"
    }

    @Test
    fun `fetchCountries converts cca2 to flag emoji`() = runTest {
        val dto = CountryDto(
            name = NameDto(common = "Japan"),
            cca2 = "JP",
            capital = listOf("Tokyo"),
            region = "Asia",
            population = 125_681_593,
        )
        coEvery { api.fetchCountries() } returns listOf(dto)

        val result = repository.fetchCountries()

        result.getOrThrow().first().flagEmoji shouldBe "🇯🇵"
    }

    @Test
    fun `fetchCountries falls back to local countries when api throws`() = runTest {
        val error = RuntimeException("timeout")
        coEvery { api.fetchCountries() } throws error

        val result = repository.fetchCountries()

        result.isSuccess shouldBe true
        result.getOrThrow().map { it.name } shouldBe FallbackCountryDtos.countries.map { it.name.common }
    }

    @Test
    fun `fetchCountries preserves cancellation instead of falling back`() = runTest {
        val cancellation = CancellationException("cancelled")
        coEvery { api.fetchCountries() } throws cancellation

        var caught: Throwable? = null
        try {
            repository.fetchCountries()
        } catch (error: CancellationException) {
            caught = error
        }

        caught shouldBe cancellation
    }

    @Test
    fun `fallback country lookup matches cca2 case insensitively`() {
        FallbackCountryDtos.countryByCode("de")?.name?.common shouldBe "Germany"
    }
}
