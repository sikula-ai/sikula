package com.example.countries.feature.countries.domain

import com.example.countries.feature.countries.domain.model.Country

internal class FetchCountriesUseCase(
    private val repository: CountriesRepository,
) {
    suspend operator fun invoke(): Result<List<Country>> =
        repository.fetchCountries()
}
