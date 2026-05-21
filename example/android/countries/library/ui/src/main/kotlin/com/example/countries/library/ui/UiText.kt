package com.example.countries.library.ui

import androidx.annotation.StringRes
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource

sealed class UiText {
    data class Res(@StringRes val id: Int, val args: List<Any> = emptyList()) : UiText()
    data class Dynamic(val value: String) : UiText()

    @Composable
    @Suppress("SpreadOperator")
    fun asString(): String = when (this) {
        is Res -> stringResource(id, *args.toTypedArray())
        is Dynamic -> value
    }
}
