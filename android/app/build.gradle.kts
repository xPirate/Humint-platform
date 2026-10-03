import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    // Kotlin 2.0 moved the Compose compiler out of composeOptions and into a
    // plugin of its own. Setting kotlinCompilerExtensionVersion instead is
    // the classic way to get "this version of the Compose Compiler requires
    // Kotlin 1.x" on a 2.x build.
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
}

/*
 * The registry is copied, not duplicated.
 *
 * api/field_templates.json is the contract between this app and the console.
 * A second hand-maintained copy in the app's assets would drift, and the
 * failure would be silent: the app would send a field key the console stopped
 * rendering under a label, and nobody would notice until somebody read a
 * report carefully. So the build copies the one file, and fails loudly if it
 * has gone missing.
 */
val registrySource = rootProject.file("../api/field_templates.json")

val copyTemplateRegistry by tasks.registering(Copy::class) {
    if (!registrySource.exists()) {
        throw GradleException(
            "api/field_templates.json is missing. This app is built from inside the " +
            "humint-platform repository so it can copy the template registry the " +
            "console uses; building it anywhere else needs that file put back."
        )
    }
    from(registrySource)
    into(layout.buildDirectory.dir("generated/registry"))
}

android {
    namespace = "org.humint.field"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.humint.field"
        // Android 10. The fleet is Pixel 7s, but scoped storage and the
        // permission model settled here and there is no reason to exclude an
        // older handset somebody already owns.
        minSdk = 29
        targetSdk = 35
        versionCode = 3
        versionName = "1.2"

        // SQLCipher ships a native library per ABI and they are 4-6 MB each,
        // which was a fifth of the debug APK for three architectures nothing
        // in this fleet runs. arm64-v8a is every handset made in the last
        // several years, including the Pixel 7s; x86_64 is kept so the app
        // still installs on an emulator.
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
        // No instrumentation runner: everything worth testing in this app is
        // a plain unit test (see src/test), and an emulator in the loop for a
        // field tool nobody will run in CI is a cost with no return.
    }

    signingConfigs {
        /*
         * A release build is signed with a keystore you make once and keep.
         * Its details go in android/keystore.properties, which is NOT in the
         * repository — see the README. Without that file the release variant
         * is simply unsigned, so `assembleDebug` keeps working on a fresh
         * clone with nothing to set up.
         */
        create("release") {
            val props = rootProject.file("keystore.properties")
            if (props.exists()) {
                val p = Properties().apply { FileInputStream(props).use { load(it) } }
                storeFile = rootProject.file(p.getProperty("storeFile"))
                storePassword = p.getProperty("storePassword")
                keyAlias = p.getProperty("keyAlias")
                keyPassword = p.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"),
                          "proguard-rules.pro")
            val props = rootProject.file("keystore.properties")
            if (props.exists()) signingConfig = signingConfigs.getByName("release")
        }
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
    }

    sourceSets {
        getByName("main") {
            assets.srcDir(layout.buildDirectory.dir("generated/registry"))
        }
    }

    buildFeatures { compose = true }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging {
        resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    }
}

tasks.named("preBuild") { dependsOn(copyTemplateRegistry) }

/*
 * Nothing here is a Google Play Services dependency, and that is deliberate
 * rather than incidental. The handsets are off-network and may be de-Googled;
 * an app that needs GMS to scan a QR code or read a GPS fix is an app that
 * does not start on one.
 *
 *   QR          ZXing core, driven from a CameraX analyser. Not ML Kit.
 *   Position    android.location.LocationManager, AOSP. Not FusedLocation.
 *   Camera      CameraX, which is AndroidX and has no GMS dependency.
 *   Storage     Room over SQLCipher, keyed out of the Android keystore.
 */
dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.10.01")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
    // ProcessLifecycleOwner: how the app notices it has gone to the
    // background, which is when the upload credentials are wiped.
    implementation("androidx.lifecycle:lifecycle-process:2.8.7")
    implementation("androidx.navigation:navigation-compose:2.8.4")

    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    ksp("androidx.room:room-compiler:2.6.1")
    // SQLCipher: the whole local database is encrypted at rest, including the
    // drafts and the paths to their media. See Crypto.kt for where the key
    // lives and why.
    implementation("net.zetetic:sqlcipher-android:4.6.1")
    implementation("androidx.sqlite:sqlite-ktx:2.4.0")
    implementation("androidx.security:security-crypto:1.1.0-alpha06")
    // BiometricPrompt, for the fingerprint shortcut onto the same key the PIN
    // unwraps. Brings androidx.fragment with it, which is why MainActivity is
    // a FragmentActivity.
    implementation("androidx.biometric:biometric:1.1.0")

    implementation("androidx.camera:camera-core:1.4.0")
    implementation("androidx.camera:camera-camera2:1.4.0")
    implementation("androidx.camera:camera-lifecycle:1.4.0")
    implementation("androidx.camera:camera-view:1.4.0")
    implementation("androidx.camera:camera-video:1.4.0")

    implementation("com.google.zxing:core:3.5.3")
    implementation("com.google.android.material:material:1.12.0")

    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // JSON is org.json, which ships with Android. The registry and the two
    // request bodies are small and flat; adding a serialization library and
    // its compiler plugin to parse them would be more build to go wrong than
    // it saves.

    debugImplementation("androidx.compose.ui:ui-tooling")

    testImplementation("junit:junit:4.13.2")
    // Android's org.json is a stub in unit tests (every method throws), so
    // the real implementation is needed to parse the registry off the JVM.
    testImplementation("org.json:json:20240303")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
}
