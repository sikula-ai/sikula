package com.example.countries.library.presentation

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

abstract class AbstractViewModel<S : AbstractViewModel.State>(initialState: S) : ViewModel() {

    interface State

    private val _states = MutableStateFlow(initialState)
    val states: StateFlow<S> = _states.asStateFlow()

    protected fun updateState(reducer: S.() -> S) {
        _states.update { it.reducer() }
    }

    protected fun launch(block: suspend CoroutineScope.() -> Unit): Job =
        viewModelScope.launch { block() }
}
