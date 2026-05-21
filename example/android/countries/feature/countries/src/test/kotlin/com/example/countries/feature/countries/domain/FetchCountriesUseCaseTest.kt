package com.example.countries.feature.countries.domain

import com.example.countries.feature.countries.model.countryFixture
import com.example.countries.library.testing.AbstractTest
import io.kotest.matchers.shouldBe
import io.mockk.coEvery
import io.mockk.coVerify
import io.mockk.mockk
import kotlinx.coroutines.test.runTest
import org.junit.jupiter.api.Test

internal class FetchCountriesUseCaseTest : AbstractTest() {

    private val repository = mockk<CountriesRepository>()
    private val useCase = FetchCountriesUseCase(repository)

    @Test
    fun `returns countries from repository on success`() = runTest {
        val expected = listOf(countryFixture(name = "Germany"), countryFixture(name = "France", code = "FR"))
        coEvery { repository.fetchCountries() } returns Result.success(expected)

        val result = useCase()

        result.getOrThrow() shouldBe expected
        coVerify(exactly = 1) { repository.fetchCountries() }
    }

    @Test
    fun `propagates failure from repository`() = runTest {
        val error = RuntimeException("network error")
        coEvery { repository.fetchCountries() } returns Result.failure(error)

        val result = useCase()

        result.isFailure shouldBe true
        result.exceptionOrNull() shouldBe error
    }
}
