package com.example.countries.feature.countries.system

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.example.countries.feature.countries.R
import com.example.countries.feature.countries.domain.model.Country
import com.example.countries.feature.countries.presentation.CountriesViewModel
import org.koin.androidx.compose.koinViewModel

@Composable
fun CountriesScreen() {
    val viewModel: CountriesViewModel = koinViewModel()
    val state by viewModel.states.collectAsState()
    CountriesScreenImpl(
        state = state,
        onRetry = viewModel::onRetry,
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun CountriesScreenImpl(
    state: CountriesViewModel.State,
    onRetry: () -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(title = { Text(stringResource(R.string.countries_title)) })
        },
    ) { padding ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            when {
                state.isLoading -> CircularProgressIndicator(
                    modifier = Modifier.align(Alignment.Center),
                )

                state.error != null -> ErrorContent(
                    message = state.error.asString(),
                    onRetry = onRetry,
                    modifier = Modifier.align(Alignment.Center),
                )

                else -> CountriesList(countries = state.countries)
            }
        }
    }
}

@Composable
private fun CountriesList(countries: List<Country>) {
    LazyColumn {
        items(countries, key = { it.code }) { country ->
            ListItem(
                headlineContent = { Text(country.name) },
                supportingContent = {
                    Text("${country.region} · ${country.capital ?: "—"}")
                },
                leadingContent = {
                    Text(
                        text = country.flagEmoji,
                        style = MaterialTheme.typography.headlineMedium,
                    )
                },
            )
            HorizontalDivider()
        }
    }
}

@Composable
private fun ErrorContent(
    message: String,
    onRetry: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier.padding(24.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodyLarge,
            textAlign = TextAlign.Center,
        )
        TextButton(onClick = onRetry) {
            Text(stringResource(R.string.countries_retry))
        }
    }
}
