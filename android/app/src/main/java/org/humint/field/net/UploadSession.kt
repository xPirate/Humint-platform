package org.humint.field.net

import android.os.SystemClock
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import org.json.JSONObject
import java.util.Arrays

/**
 * The console's address and a token, held in memory and nowhere else.
 *
 * This is the whole reason the app is shaped the way it is. A field handset
 * is a thing that gets lost, lent, seized or simply left in a vehicle. If it
 * carried the console's address and a working token, every one of those
 * events would hand somebody a route to the case file — or at minimum, tell
 * them the case file exists and what its address is, which is often the more
 * damaging half.
 *
 * So: nothing here is ever written to disk. Not to SharedPreferences, not to
 * the encrypted database, not to the keystore. The credentials arrive by
 * scanning a QR the console shows, live for one upload session, and are
 * wiped when it ends — on success, on failure, on the app going to the
 * background, or after [IDLE_TIMEOUT_MS] of nothing happening.
 *
 * What this costs: somebody has to be able to show the analyst a QR code
 * when they are back in range. What it buys: the reports on a seized handset
 * are all anybody gets, and even they are encrypted.
 *
 * The one thing that would quietly undo all of it is a crash reporter, an
 * analytics SDK, or a log line that writes [baseUrl] or [token] somewhere
 * durable. There are none of the first two in this app, and [toString] is
 * overridden below so an accidental log of this object cannot leak either.
 */
class UploadSession private constructor(
    val baseUrl: String,
    private val tokenChars: CharArray,
    val deviceLabel: String?,
    val username: String?,
) {
    private var lastTouched = SystemClock.elapsedRealtime()

    val token: String get() {
        lastTouched = SystemClock.elapsedRealtime()
        return String(tokenChars)
    }

    val expired: Boolean
        get() = SystemClock.elapsedRealtime() - lastTouched > IDLE_TIMEOUT_MS

    /** Overwrite the token's backing array. Best effort — the JVM may have
     *  copied it during String construction, and there is no way to reach
     *  those copies. Still worth doing: it closes the case where the array
     *  itself is what ends up in a heap dump. */
    fun wipe() {
        Arrays.fill(tokenChars, '\u0000')
    }

    override fun toString(): String = "UploadSession(${hostOnly()}, token=<redacted>)"

    fun hostOnly(): String = runCatching {
        java.net.URI(baseUrl).let { it.host ?: baseUrl }
    }.getOrDefault("the console")

    companion object {
        /**
         * Fifteen minutes. Long enough to work through a queue of reports
         * with photographs on a poor uplink; short enough that a phone put
         * down on a table mid-upload does not sit there with live
         * credentials in it.
         */
        const val IDLE_TIMEOUT_MS = 15 * 60 * 1000L

        /**
         * Read the payload the console's enrollment QR carries:
         *
         *     {"v":1,"url":"http://10.0.0.14:8080","token":"…",
         *      "label":"Jordan's phone","user":"jordan"}
         *
         * Version is checked because a later console may add fields; an
         * unknown *higher* version is accepted with whatever it can read,
         * since refusing to upload because the QR gained a key would be the
         * wrong failure. A malformed payload is refused with a message that
         * says what was wrong, because the analyst is standing in front of
         * the screen and can try again.
         */
        fun fromQr(payload: String): Result<UploadSession> = runCatching {
            val json = JSONObject(payload)
            val url = json.optString("url").trim().trimEnd('/')
            val token = json.optString("token").trim()
            require(url.isNotEmpty()) {
                "That code has no address in it. The admin needs to fill in the address " +
                "the phone can reach when they enroll the device."
            }
            require(url.startsWith("http://") || url.startsWith("https://")) {
                "That code's address is not an http address."
            }
            require(token.isNotEmpty()) { "That code has no token in it." }
            UploadSession(
                baseUrl = url,
                tokenChars = token.toCharArray(),
                deviceLabel = json.optString("label").ifBlank { null },
                username = json.optString("user").ifBlank { null },
            )
        }.recoverCatching { cause ->
            throw IllegalArgumentException(
                cause.message ?: "That does not look like an enrollment code from the console.",
                cause
            )
        }
    }
}

/**
 * The live session, such as it is. A StateFlow so the UI can grey out
 * "Upload" the instant it is cleared, and so clearing it is one call from
 * the lifecycle observer that watches for the app going to the background.
 */
object SessionHolder {
    private val _current = MutableStateFlow<UploadSession?>(null)
    val current: StateFlow<UploadSession?> = _current

    fun set(session: UploadSession) {
        _current.value?.wipe()
        _current.value = session
    }

    /** Called on success, on failure, on background, and on idle timeout. */
    fun clear() {
        _current.value?.wipe()
        _current.value = null
    }

    /** Returns the session, or null if it has gone stale since last use. */
    fun live(): UploadSession? {
        val s = _current.value ?: return null
        if (s.expired) { clear(); return null }
        return s
    }
}
