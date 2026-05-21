package com.example.countries.feature.countries.model

import com.example.countries.feature.countries.domain.model.Country

internal fun countryFixture(
    code: String = "DE",
    name: String = "Germany",
    capital: String? = "Berlin",
    population: Long = 83_149_545,
    region: String = "Europe",
    flagEmoji: String = "🇩🇪",
) = Country(
    code = code,
    name = name,
    capital = capital,
    population = population,
    region = region,
    flagEmoji = flagEmoji,
)
