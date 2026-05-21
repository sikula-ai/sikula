package com.example.countries

import org.junit.jupiter.api.Test
import org.mockito.kotlin.whenever
import org.springframework.beans.factory.annotation.Autowired
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest
import org.springframework.boot.test.mock.mockito.MockBean
import org.springframework.test.web.servlet.MockMvc
import org.springframework.test.web.servlet.get

@WebMvcTest(CountryController::class)
class CountryControllerTest {

    @Autowired
    private lateinit var mvc: MockMvc

    @MockBean
    private lateinit var service: CountryService

    private val germany = Country("DEU", "Germany", "Berlin", "Europe", 83200000, 357114.0)
    private val france = Country("FRA", "France", "Paris", "Europe", 67000000, 543965.0)

    @Test
    fun `GET countries returns list of all countries`() {
        whenever(service.listAll(null)).thenReturn(listOf(germany, france))
        mvc.get("/countries").andExpect {
            status { isOk() }
            jsonPath("$.length()") { value(2) }
            jsonPath("$[0].code") { value("DEU") }
        }
    }

    @Test
    fun `GET countries with region filter returns filtered list`() {
        whenever(service.listAll("Europe")).thenReturn(listOf(germany, france))
        mvc.get("/countries?region=Europe").andExpect {
            status { isOk() }
            jsonPath("$.length()") { value(2) }
        }
    }

    @Test
    fun `GET countries by code returns country`() {
        whenever(service.findByCode("DEU")).thenReturn(germany)
        mvc.get("/countries/DEU").andExpect {
            status { isOk() }
            jsonPath("$.name") { value("Germany") }
            jsonPath("$.capital") { value("Berlin") }
        }
    }

    @Test
    fun `GET countries by unknown code returns 404`() {
        whenever(service.findByCode("XXX")).thenReturn(null)
        mvc.get("/countries/XXX").andExpect {
            status { isNotFound() }
        }
    }
}
