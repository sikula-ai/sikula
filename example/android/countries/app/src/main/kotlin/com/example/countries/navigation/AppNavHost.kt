package com.example.countries.navigation

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.rememberNavController
import com.example.countries.feature.countries.navigation.CountriesRoutes
import com.example.countries.feature.countries.navigation.countriesGraph

@Composable
fun AppNavHost(modifier: Modifier = Modifier) {
    val navController = rememberNavController()
    NavHost(
        navController = navController,
        startDestination = CountriesRoutes.LIST,
        modifier = modifier,
    ) {
        countriesGraph(navController = navController)
    }
}
