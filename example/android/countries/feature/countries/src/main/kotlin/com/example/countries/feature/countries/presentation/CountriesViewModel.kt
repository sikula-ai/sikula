package com.example.countries.feature.countries.presentation

import com.example.countries.feature.countries.R
import com.example.countries.feature.countries.domain.FetchCountriesUseCase
import com.example.countries.feature.countries.domain.model.Country
import com.example.countries.library.presentation.AbstractViewModel
import com.example.countries.library.ui.UiText

internal class CountriesViewModel(
    private val fetchCountries: FetchCountriesUseCase,
) : AbstractViewModel<CountriesViewModel.State>(State()) {

    data class State(
        val isLoading: Boolean = false,
        val countries: List<Country> = emptyList(),
        val error: UiText? = null,
    ) : AbstractViewModel.State

    init {
        loadCountries()
    }

    fun onRetry() {
        loadCountries()
    }

    private fun loadCountries() {
        launch {
            updateState { copy(isLoading = true, error = null) }
            fetchCountries()
                .onSuccess { countries ->
                    updateState {
                        copy(
                            isLoading = false,
                            countries = countries.sortedBy { it.name },
                        )
                    }
                }
                .onFailure {
                    updateState {
                        copy(
                            isLoading = false,
                            error = UiText.Res(R.string.countries_error),
                        )
                    }
                }
        }
    }
}
