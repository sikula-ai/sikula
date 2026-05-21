package com.example.countries.feature.countries.data

import retrofit2.http.GET

internal interface CountriesApi {
    @GET("all?fields=name,cca2,capital,region,population")
    suspend fun fetchCountries(): List<CountryDto>
}
