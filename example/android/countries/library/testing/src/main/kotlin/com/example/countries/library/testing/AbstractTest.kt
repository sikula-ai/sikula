package com.example.countries.library.testing

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.UnconfinedTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach

@OptIn(ExperimentalCoroutinesApi::class)
abstract class AbstractTest {

    private val testDispatcher = UnconfinedTestDispatcher()

    @BeforeEach
    fun setUpDispatcher() {
        Dispatchers.setMain(testDispatcher)
    }

    @AfterEach
    fun tearDownDispatcher() {
        Dispatchers.resetMain()
    }
}
