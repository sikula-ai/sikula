package com.example.countries

import android.app.Application
import com.example.countries.di.appModule
import org.koin.android.ext.koin.androidContext
import org.koin.core.context.startKoin

class SikulaExampleApp : Application() {

    override fun onCreate() {
        super.onCreate()
        startKoin {
            androidContext(this@SikulaExampleApp)
            modules(appModule)
        }
    }
}
