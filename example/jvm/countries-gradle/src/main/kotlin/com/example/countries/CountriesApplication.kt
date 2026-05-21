package com.example.countries

import org.springframework.boot.autoconfigure.SpringBootApplication
import org.springframework.boot.runApplication

@SpringBootApplication
class CountriesApplication

fun main(args: Array<String>) {
    runApplication<CountriesApplication>(*args)
}
