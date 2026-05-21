package com.example.countries.feature.countries.data

import com.example.countries.feature.countries.domain.CountriesRepository
import com.example.countries.feature.countries.domain.model.Country

internal class CountriesRepositoryImpl(
    private val api: CountriesApi,
) : CountriesRepository {

    override suspend fun fetchCountries(): Result<List<Country>> =
        runCatching { api.fetchCountries().map { it.toDomain() } }
}
