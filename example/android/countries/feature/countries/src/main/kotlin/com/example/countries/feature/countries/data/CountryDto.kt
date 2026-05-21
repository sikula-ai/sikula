package com.example.countries.feature.countries.data

import com.example.countries.feature.countries.domain.model.Country
import com.squareup.moshi.Json
import com.squareup.moshi.JsonClass

@JsonClass(generateAdapter = false)
internal data class CountryDto(
    @Json(name = "name") val name: NameDto,
    @Json(name = "cca2") val cca2: String,
    @Json(name = "capital") val capital: List<String>?,
    @Json(name = "region") val region: String,
    @Json(name = "population") val population: Long,
)

@JsonClass(generateAdapter = false)
internal data class NameDto(
    @Json(name = "common") val common: String,
)

internal fun CountryDto.toDomain() = Country(
    code = cca2,
    name = name.common,
    capital = capital?.firstOrNull(),
    population = population,
    region = region,
    flagEmoji = cca2.toFlagEmoji(),
)

// Unicode regional indicator symbol for letter A (U+1F1E6)
private const val REGIONAL_INDICATOR_A = 0x1F1E6

private fun String.toFlagEmoji(): String =
    uppercase().map { char ->
        String(Character.toChars(char.code - 'A'.code + REGIONAL_INDICATOR_A))
    }.joinToString("")
