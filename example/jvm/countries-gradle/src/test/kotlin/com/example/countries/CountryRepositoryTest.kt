package com.example.countries

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

class CountryRepositoryTest {

    private val repository = CountryRepository()

    @Test
    fun `findAll returns non-empty list`() {
        assertTrue(repository.findAll().isNotEmpty())
    }

    @Test
    fun `findAll returns same list on repeated calls`() {
        assertEquals(repository.findAll().size, repository.findAll().size)
    }

    @Test
    fun `findByCode returns country for known code`() {
        val country = repository.findByCode("DEU")
        assertNotNull(country)
        assertEquals("DEU", country?.code)
    }

    @Test
    fun `findByCode is case insensitive`() {
        assertNotNull(repository.findByCode("deu"))
    }

    @Test
    fun `findByCode returns null for unknown code`() {
        assertNull(repository.findByCode("XXX"))
    }

    @Test
    fun `findByRegion returns only countries in that region`() {
        val europe = repository.findByRegion("Europe")
        assertTrue(europe.isNotEmpty())
        assertTrue(europe.all { it.region.equals("Europe", ignoreCase = true) })
    }

    @Test
    fun `findByRegion is case insensitive`() {
        val lower = repository.findByRegion("europe")
        val upper = repository.findByRegion("Europe")
        assertEquals(upper.size, lower.size)
    }

    @Test
    fun `findByRegion returns empty list for unknown region`() {
        assertTrue(repository.findByRegion("Atlantis").isEmpty())
    }
}
