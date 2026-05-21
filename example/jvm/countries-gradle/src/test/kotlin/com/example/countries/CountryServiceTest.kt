package com.example.countries

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.mockito.kotlin.mock
import org.mockito.kotlin.whenever

class CountryServiceTest {

    private val repository: CountryRepository = mock()
    private val service = CountryService(repository)

    private val germany = Country("DEU", "Germany", "Berlin", "Europe", 83200000, 357114.0)
    private val france = Country("FRA", "France", "Paris", "Europe", 67000000, 543965.0)
    private val brazil = Country("BRA", "Brazil", "Brasília", "Americas", 215353593, 8515767.0)
    private val all = listOf(germany, france, brazil)

    @Test
    fun `listAll returns all countries when no region filter`() {
        whenever(repository.findAll()).thenReturn(all)
        val result = service.listAll()
        assertEquals(3, result.size)
    }

    @Test
    fun `listAll filters by region`() {
        whenever(repository.findByRegion("Europe")).thenReturn(listOf(germany, france))
        val result = service.listAll("Europe")
        assertEquals(2, result.size)
        assertTrue(result.all { it.region == "Europe" })
    }

    @Test
    fun `listAll returns empty list when region has no matches`() {
        whenever(repository.findByRegion("Antarctica")).thenReturn(emptyList())
        val result = service.listAll("Antarctica")
        assertTrue(result.isEmpty())
    }

    @Test
    fun `findByCode returns country when found`() {
        whenever(repository.findByCode("DEU")).thenReturn(germany)
        assertEquals(germany, service.findByCode("DEU"))
    }

    @Test
    fun `findByCode returns null when not found`() {
        whenever(repository.findByCode("XXX")).thenReturn(null)
        assertNull(service.findByCode("XXX"))
    }
}
