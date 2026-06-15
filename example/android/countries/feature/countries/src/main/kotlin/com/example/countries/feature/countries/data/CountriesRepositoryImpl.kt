package com.example.countries.feature.countries.data

import com.example.countries.feature.countries.domain.CountriesRepository
import com.example.countries.feature.countries.domain.model.Country
import kotlinx.coroutines.CancellationException

internal class CountriesRepositoryImpl(
    private val api: CountriesApi,
) : CountriesRepository {

    override suspend fun fetchCountries(): Result<List<Country>> =
        runCatching {
            runCatching { api.fetchCountries() }
                .rethrowCancellation()
                .getOrElse { FallbackCountryDtos.countries }
                .map { it.toDomain() }
        }.rethrowCancellation()
}

private fun <T> Result<T>.rethrowCancellation(): Result<T> =
    onFailure { error ->
        if (error is CancellationException) {
            throw error
        }
    }
