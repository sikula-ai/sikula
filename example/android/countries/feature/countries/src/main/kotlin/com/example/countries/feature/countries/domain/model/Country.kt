package com.example.countries.feature.countries.domain.model

data class Country(
    val code: String,
    val name: String,
    val capital: String?,
    val population: Long,
    val region: String,
    val flagEmoji: String,
)
