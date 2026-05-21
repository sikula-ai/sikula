package com.example.countries.feature.countries.domain

import com.example.countries.feature.countries.domain.model.Country

internal interface CountriesRepository {
    suspend fun fetchCountries(): Result<List<Country>>
}
