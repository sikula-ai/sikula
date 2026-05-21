plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "com.example.countries"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.example.countries"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
        }
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
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.bundles.compose)
    implementation(libs.compose.navigation)
    implementation(libs.koin.android)
    implementation(libs.koin.compose)
    implementation(libs.coroutines.android)

    implementation(project(":feature:countries"))
    implementation(project(":library:network"))
    implementation(project(":library:ui"))

    debugImplementation(libs.compose.ui.tooling)
}
