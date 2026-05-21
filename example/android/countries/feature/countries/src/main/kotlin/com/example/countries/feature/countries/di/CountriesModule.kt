package com.example.countries.feature.countries.di

import com.example.countries.feature.countries.data.CountriesApi
import com.example.countries.feature.countries.data.CountriesRepositoryImpl
import com.example.countries.feature.countries.domain.CountriesRepository
import com.example.countries.feature.countries.domain.FetchCountriesUseCase
import com.example.countries.feature.countries.presentation.CountriesViewModel
import com.example.countries.library.network.createApi
import org.koin.core.module.dsl.factoryOf
import org.koin.core.module.dsl.viewModelOf
import org.koin.dsl.bind
import org.koin.dsl.module
import retrofit2.Retrofit

val countriesModule = module {
    single { get<Retrofit>().createApi<CountriesApi>() }
    single { CountriesRepositoryImpl(get()) } bind CountriesRepository::class
    factoryOf(::FetchCountriesUseCase)
    viewModelOf(::CountriesViewModel)
}
