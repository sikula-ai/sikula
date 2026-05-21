package com.example.countries

import org.springframework.http.ResponseEntity
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.PathVariable
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RequestParam
import org.springframework.web.bind.annotation.RestController

@RestController
@RequestMapping("/countries")
class CountryController(private val service: CountryService) {

    @GetMapping
    fun list(@RequestParam region: String? = null): List<Country> =
        service.listAll(region)

    @GetMapping("/{code}")
    fun getByCode(@PathVariable code: String): ResponseEntity<Country> {
        val country = service.findByCode(code) ?: return ResponseEntity.notFound().build()
        return ResponseEntity.ok(country)
    }
}
