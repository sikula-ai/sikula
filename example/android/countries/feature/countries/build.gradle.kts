plugins {
    alias(libs.plugins.android.library)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.example.countries.feature.countries"
    compileSdk = 35

    defaultConfig {
        minSdk = 26
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }

    testOptions {
        unitTests.all { it.useJUnitPlatform() }
    }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.bundles.compose)
    implementation(libs.compose.material.icons.core)
    implementation(libs.compose.navigation)
    implementation(libs.koin.compose)
    implementation(libs.coroutines.core)

    implementation(project(":library:network"))
    implementation(project(":library:presentation"))
    implementation(project(":library:ui"))

    testImplementation(project(":library:testing"))
    testImplementation(libs.bundles.test.unit)
}
