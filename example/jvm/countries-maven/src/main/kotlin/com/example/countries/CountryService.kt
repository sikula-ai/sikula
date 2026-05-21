package com.example.countries

import org.springframework.stereotype.Service

@Service
class CountryService(private val repository: CountryRepository) {

    fun listAll(region: String? = null): List<Country> =
        if (region != null) repository.findByRegion(region) else repository.findAll()

    fun findByCode(code: String): Country? = repository.findByCode(code)
}
