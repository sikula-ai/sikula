package com.example.countries.di

import com.example.countries.feature.countries.di.countriesModule
import com.example.countries.library.network.networkModule
import org.koin.dsl.module

val appModule = module {
    includes(
        networkModule,
        countriesModule,
    )
}
