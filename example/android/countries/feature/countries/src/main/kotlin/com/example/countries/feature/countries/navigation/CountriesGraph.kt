package com.example.countries.feature.countries.navigation

import androidx.navigation.NavController
import androidx.navigation.NavGraphBuilder
import androidx.navigation.compose.composable
import com.example.countries.feature.countries.system.CountriesScreen

@Suppress("UnusedParameter")
fun NavGraphBuilder.countriesGraph(navController: NavController) {
    composable(CountriesRoutes.LIST) {
        CountriesScreen()
    }
}
