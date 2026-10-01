package org.humint.field.data

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * The handful of preferences that are not secret.
 *
 * Plain SharedPreferences, deliberately. The theme has to be readable
 * *before* the vault is unlocked — the lock screen has to be drawn in the
 * right colours — so it cannot live behind the PIN. Nothing here says
 * anything about a case: which theme somebody likes, and whether they turned
 * on the fingerprint shortcut. Everything that matters is in the encrypted
 * store.
 */
enum class ThemeChoice(val label: String, val blurb: String) {
    SYSTEM("Match the system", "Follow the phone's own light or dark setting"),
    DARK("Always dark", "Easier on the eyes at night, and less to see from a distance"),
    LIGHT("Always light", "Readable in direct sun");

    companion object {
        fun from(name: String?): ThemeChoice =
            entries.firstOrNull { it.name == name } ?: SYSTEM
    }
}

object Settings {
    private const val FILE = "field.settings"
    private const val KEY_THEME = "theme"

    private val _theme = MutableStateFlow(ThemeChoice.SYSTEM)
    val theme: StateFlow<ThemeChoice> = _theme

    fun load(context: Context) {
        val prefs = context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
        _theme.value = ThemeChoice.from(prefs.getString(KEY_THEME, null))
    }

    fun setTheme(context: Context, choice: ThemeChoice) {
        context.getSharedPreferences(FILE, Context.MODE_PRIVATE)
            .edit().putString(KEY_THEME, choice.name).apply()
        _theme.value = choice
    }
}
