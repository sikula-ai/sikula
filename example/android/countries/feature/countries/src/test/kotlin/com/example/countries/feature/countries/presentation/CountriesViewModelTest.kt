package com.example.countries.feature.countries.presentation

import com.example.countries.feature.countries.R
import com.example.countries.feature.countries.domain.FetchCountriesUseCase
import com.example.countries.feature.countries.model.countryFixture
import com.example.countries.library.testing.AbstractTest
import com.example.countries.library.ui.UiText
import io.kotest.matchers.shouldBe
import io.mockk.coEvery
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Test

internal class CountriesViewModelTest : AbstractTest() {

    private val fetchCountries = mockk<FetchCountriesUseCase>()

    @Test
    fun `loads countries sorted alphabetically on init`() = runTest {
        val countries = listOf(
            countryFixture(name = "Germany", code = "DE"),
            countryFixture(name = "Austria", code = "AT"),
            countryFixture(name = "France", code = "FR"),
        )
        coEvery { fetchCountries() } returns Result.success(countries)

        val viewModel = CountriesViewModel(fetchCountries)

        viewModel.states.value.countries.map { it.name } shouldBe listOf("Austria", "France", "Germany")
        viewModel.states.value.isLoading shouldBe false
        viewModel.states.value.error shouldBe null
    }

    @Test
    fun `shows error state on failure`() = runTest {
        coEvery { fetchCountries() } returns Result.failure(RuntimeException("timeout"))

        val viewModel = CountriesViewModel(fetchCountries)

        viewModel.states.value.isLoading shouldBe false
        viewModel.states.value.error shouldBe UiText.Res(R.string.countries_error)
        viewModel.states.value.countries shouldBe emptyList()
    }

    @Test
    fun `retries loading on onRetry`() = runTest {
        val countries = listOf(countryFixture())
        coEvery { fetchCountries() } returnsMany listOf(
            Result.failure(RuntimeException("error")),
            Result.success(countries),
        )

        val viewModel = CountriesViewModel(fetchCountries)
        viewModel.states.value.error shouldBe UiText.Res(R.string.countries_error)

        viewModel.onRetry()

        viewModel.states.value.countries shouldBe countries
        viewModel.states.value.error shouldBe null
    }

    @Test
    fun `handles country with null capital`() = runTest {
        val country = countryFixture(capital = null)
        coEvery { fetchCountries() } returns Result.success(listOf(country))

        val viewModel = CountriesViewModel(fetchCountries)

        viewModel.states.value.countries.first().capital shouldBe null
    }
}
