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

internal object FallbackCountryDtos {
    val countries = listOf(
        country("AR", "Argentina", "Buenos Aires", "Americas", 45_376_763),
        country("AU", "Australia", "Canberra", "Oceania", 25_687_041),
        country("AT", "Austria", "Vienna", "Europe", 8_917_205),
        country("BE", "Belgium", "Brussels", "Europe", 11_555_997),
        country("BR", "Brazil", "Brasilia", "Americas", 212_559_409),
        country("CA", "Canada", "Ottawa", "Americas", 38_005_238),
        country("CL", "Chile", "Santiago", "Americas", 19_116_209),
        country("CN", "China", "Beijing", "Asia", 1_402_112_000),
        country("CO", "Colombia", "Bogota", "Americas", 50_882_884),
        country("CZ", "Czechia", "Prague", "Europe", 10_698_896),
        country("DK", "Denmark", "Copenhagen", "Europe", 5_831_404),
        country("EG", "Egypt", "Cairo", "Africa", 102_334_403),
        country("ET", "Ethiopia", "Addis Ababa", "Africa", 114_963_583),
        country("FI", "Finland", "Helsinki", "Europe", 5_530_719),
        country("FR", "France", "Paris", "Europe", 67_391_582),
        country("DE", "Germany", "Berlin", "Europe", 83_240_525),
        country("GR", "Greece", "Athens", "Europe", 10_715_549),
        country("IN", "India", "New Delhi", "Asia", 1_380_004_385),
        country("ID", "Indonesia", "Jakarta", "Asia", 273_523_615),
        country("IE", "Ireland", "Dublin", "Europe", 4_994_724),
        country("IT", "Italy", "Rome", "Europe", 59_554_023),
        country("JP", "Japan", "Tokyo", "Asia", 125_836_021),
        country("KE", "Kenya", "Nairobi", "Africa", 53_771_300),
        country("MX", "Mexico", "Mexico City", "Americas", 128_932_753),
        country("MA", "Morocco", "Rabat", "Africa", 36_910_558),
        country("NL", "Netherlands", "Amsterdam", "Europe", 17_441_139),
        country("NZ", "New Zealand", "Wellington", "Oceania", 5_084_300),
        country("NG", "Nigeria", "Abuja", "Africa", 206_139_589),
        country("NO", "Norway", "Oslo", "Europe", 5_379_475),
        country("PE", "Peru", "Lima", "Americas", 32_971_846),
        country("PL", "Poland", "Warsaw", "Europe", 37_950_802),
        country("PT", "Portugal", "Lisbon", "Europe", 10_305_564),
        country("SG", "Singapore", "Singapore", "Asia", 5_685_807),
        country("ZA", "South Africa", "Pretoria", "Africa", 59_308_690),
        country("KR", "South Korea", "Seoul", "Asia", 51_780_579),
        country("ES", "Spain", "Madrid", "Europe", 47_351_567),
        country("SE", "Sweden", "Stockholm", "Europe", 10_353_442),
        country("CH", "Switzerland", "Bern", "Europe", 8_654_622),
        country("TH", "Thailand", "Bangkok", "Asia", 69_799_978),
        country("TR", "Turkey", "Ankara", "Asia", 84_339_067),
        country("UA", "Ukraine", "Kyiv", "Europe", 44_134_693),
        country("GB", "United Kingdom", "London", "Europe", 67_215_293),
        country("US", "United States", "Washington, D.C.", "Americas", 329_484_123),
        country("VN", "Vietnam", "Hanoi", "Asia", 97_338_583),
    )

    fun countryByCode(code: String): CountryDto? =
        countries.firstOrNull { it.cca2.equals(code, ignoreCase = true) }

    private fun country(
        cca2: String,
        name: String,
        capital: String,
        region: String,
        population: Long,
    ) = CountryDto(
        name = NameDto(common = name),
        cca2 = cca2,
        capital = listOf(capital),
        region = region,
        population = population,
    )
}

// Unicode regional indicator symbol for letter A (U+1F1E6)
private const val REGIONAL_INDICATOR_A = 0x1F1E6

private fun String.toFlagEmoji(): String =
    uppercase().map { char ->
        String(Character.toChars(char.code - 'A'.code + REGIONAL_INDICATOR_A))
    }.joinToString("")
