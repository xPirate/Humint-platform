package org.humint.field

import android.app.Application
import org.humint.field.data.Templates

class FieldApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // The registry is read once, from assets, before any screen exists.
        // Nothing in the app works without it and it cannot be fetched, so
        // failing here — loudly, at launch — is better than a form that
        // renders with no fields in it.
        Templates.load(this)
    }
}
