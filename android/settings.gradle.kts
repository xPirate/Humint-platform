// The companion app is its own Gradle build, deliberately not wired into
// anything else in this repository: the server half is Python in Docker and
// has no business sharing a build system with an Android app.
//
// The one thing crossing the boundary is api/field_templates.json, which
// app/build.gradle.kts copies into the app's assets. See the note there.

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "HUMINT Field"
include(":app")
