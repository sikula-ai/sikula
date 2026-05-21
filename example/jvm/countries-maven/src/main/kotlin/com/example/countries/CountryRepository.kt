package com.example.countries

import com.fasterxml.jackson.module.kotlin.jacksonObjectMapper
import com.fasterxml.jackson.module.kotlin.readValue
import org.springframework.stereotype.Repository

@Repository
class CountryRepository {

    private val countries: List<Country> by lazy { load() }

    fun findAll(): List<Country> = countries

    fun findByCode(code: String): Country? =
        countries.firstOrNull { it.code.equals(code, ignoreCase = true) }

    fun findByRegion(region: String): List<Country> =
        countries.filter { it.region.equals(region, ignoreCase = true) }

    private fun load(): List<Country> {
        val stream = javaClass.getResourceAsStream("/data/countries.json")
            ?: error("data/countries.json not found in classpath")
        return jacksonObjectMapper().readValue(stream)
    }
}
