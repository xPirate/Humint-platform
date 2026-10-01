package org.humint.field.data

import android.content.Context
import android.os.Build
import android.os.SystemClock
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyPermanentlyInvalidatedException
import android.security.keystore.KeyProperties
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.File
import java.security.KeyStore
import java.security.SecureRandom
import java.util.Arrays
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.PBEKeySpec
import javax.crypto.spec.SecretKeySpec

/**
 * The PIN, and the key it holds.
 *
 * Everything on this handset that matters — the queued reports and their
 * photographs — is encrypted under one random 32-byte **data key**. This
 * class is the only thing that knows how to get at it.
 *
 * ## How the data key is protected
 *
 * The data key is wrapped twice, in this order:
 *
 *     DK  --wrapped with a key derived from the PIN-->  inner
 *     inner --wrapped with a key that lives in the Android keystore--> stored
 *
 * Unwrapping needs both, which is the point:
 *
 *   - The outer layer is an AES key generated inside the device keystore. On
 *     a Pixel that means the Titan M2 security chip; the key cannot be
 *     exported even from a rooted device. So the stored file copied off the
 *     phone is inert. There is no offline attack.
 *
 *   - The inner layer is PBKDF2-HMAC-SHA256 over the PIN, 310,000 iterations.
 *     So somebody holding the unlocked phone, or able to run code as this
 *     app, still cannot read the queue without the PIN.
 *
 * ## What this is honestly worth
 *
 * A six-digit PIN is a million possibilities. Guessing has to happen **on
 * this phone**, because the hardware key never leaves it, and each guess
 * costs a PBKDF2 derivation — a sizeable fraction of a second. That plus the
 * throttling below puts a patient attacker in the range of days to weeks for
 * six digits, and out of reach entirely past eight. It is not a passphrase
 * and it should not be described as one. What it does defeat is the case the
 * app is actually built for: a handset that is lost, seized, or handed over.
 *
 * ## Forgetting it
 *
 * There is no recovery, by the analyst, by an admin, or by anybody else.
 * That is what "the PIN holds the key" means. Reports already uploaded are
 * on the console and safe; anything still queued is gone. [destroyEverything]
 * exists so a handset with a forgotten PIN can be put back into service, and
 * it says exactly that before it runs.
 */
object Vault {

    private const val KEYSTORE = "AndroidKeyStore"
    private const val HW_KEY_ALIAS = "humint.field.wrap.v2"
    private const val BIO_KEY_ALIAS = "humint.field.bio.v2"
    private const val VAULT_FILE = "vault.bin"
    private const val BIO_FILE = "vault.bio"
    private const val THROTTLE_FILE = "vault.attempts"

    private const val GCM_TAG_BITS = 128
    private const val IV_BYTES = 12
    private const val SALT_BYTES = 16
    private const val DK_BYTES = 32
    private const val PBKDF2_ITERATIONS = 310_000

    const val MIN_PIN_LENGTH = 6
    const val MAX_PIN_LENGTH = 12

    /** How long the app may sit in the background before it locks again. */
    const val LOCK_AFTER_BACKGROUND_MS = 90_000L

    sealed interface State {
        /** No PIN has ever been set. First run. */
        data object NeedsSetup : State
        data class Locked(val waitUntilMs: Long = 0L, val failures: Int = 0) : State
        data object Open : State

        /**
         * The PIN was right, and the database still will not open.
         *
         * In practice this means a store left behind by a build that keyed
         * it differently. It used to crash the app on a background thread
         * with a bare "file is not a database", which tells an analyst
         * standing outside precisely nothing.
         */
        data object Unreadable : State
    }

    private val _state = MutableStateFlow<State>(State.Locked())
    val state: StateFlow<State> = _state

    /** The data key, in memory, only while unlocked. */
    @Volatile private var dataKey: ByteArray? = null
    @Volatile private var backgroundedAt: Long = 0L

    fun isConfigured(context: Context): Boolean =
        File(context.filesDir, VAULT_FILE).exists()

    fun refreshState(context: Context) {
        _state.value = when {
            !isConfigured(context) -> State.NeedsSetup
            dataKey != null -> State.Open
            else -> lockedState(context)
        }
    }

    /** The key, or null if locked. Callers must handle null rather than
     *  assume: the vault can lock underneath them while a screen is open. */
    fun key(): ByteArray? = dataKey

    // ------------------------------------------------------------ setting up

    /**
     * First run: make a data key and seal it under this PIN.
     *
     * Returns false only if a vault already exists — changing a PIN goes
     * through [changePin], which needs the old one.
     */
    fun setUp(context: Context, pin: CharArray): Boolean {
        if (isConfigured(context)) return false
        // There is no vault, so anything already on disk was written under a
        // key that no longer exists — a build from before the PIN, or a
        // vault that was erased. It cannot be read by anybody ever again, so
        // it goes now rather than crashing the first query after unlock.
        discardUnreadableStore(context)
        val dk = ByteArray(DK_BYTES).also { SecureRandom().nextBytes(it) }
        writeVault(context, dk, pin)
        dataKey = dk
        clearThrottle(context)
        _state.value = if (openable(context)) State.Open else State.Unreadable
        return true
    }

    fun unlock(context: Context, pin: CharArray): Boolean {
        val wait = remainingWaitMs(context)
        if (wait > 0) {
            _state.value = State.Locked(System.currentTimeMillis() + wait, failureCount(context))
            return false
        }
        val dk = readVault(context, pin)
        if (dk == null) {
            recordFailure(context)
            _state.value = lockedState(context)
            return false
        }
        dataKey = dk
        clearThrottle(context)
        _state.value = if (openable(context)) State.Open else State.Unreadable
        return true
    }

    fun changePin(context: Context, oldPin: CharArray, newPin: CharArray): Boolean {
        val dk = readVault(context, oldPin) ?: run { recordFailure(context); return false }
        writeVault(context, dk, newPin)
        // A biometric shortcut wraps the same data key, so it survives a PIN
        // change untouched. Nothing to redo.
        dataKey = dk
        clearThrottle(context)
        _state.value = State.Open
        return true
    }

    // ------------------------------------------------------------- locking

    fun lock() {
        // The database handle goes first: it was opened with this key, and
        // leaving it open after locking would mean the queue is still
        // readable by anything already holding a DAO.
        FieldDatabase.closeAndForget()
        dataKey?.let { Arrays.fill(it, 0) }
        dataKey = null
        _state.value = State.Locked()
    }

    /** Called when the app goes to the background. The vault does not lock
     *  immediately: an analyst who glances at a message and comes back should
     *  not have to type a PIN, but a phone in a pocket should be shut. */
    fun onBackgrounded() {
        backgroundedAt = SystemClock.elapsedRealtime()
    }

    fun onForegrounded(context: Context) {
        val away = SystemClock.elapsedRealtime() - backgroundedAt
        if (backgroundedAt > 0L && away >= LOCK_AFTER_BACKGROUND_MS) lock()
        backgroundedAt = 0L
        refreshState(context)
    }

    // ---------------------------------------------------------- the wrapping

    private fun writeVault(context: Context, dk: ByteArray, pin: CharArray) {
        val salt = ByteArray(SALT_BYTES).also { SecureRandom().nextBytes(it) }
        val pinKey = deriveFromPin(pin, salt)
        val inner = seal(pinKey, dk)
        val outer = seal(hardwareKey(), inner)
        // salt || outer. The salt is not secret; it is here so the same PIN
        // on two handsets does not derive the same key.
        File(context.filesDir, VAULT_FILE).outputStream().use {
            it.write(salt); it.write(outer)
        }
        // No attempt to zero the derived key here: SecretKeySpec.getEncoded()
        // hands back a copy, so wiping it wipes nothing. The material lives
        // until the GC takes it, which is a limitation of the platform's
        // crypto API rather than something this code can fix.
    }

    private fun readVault(context: Context, pin: CharArray): ByteArray? {
        val raw = runCatching { File(context.filesDir, VAULT_FILE).readBytes() }.getOrNull()
            ?: return null
        if (raw.size <= SALT_BYTES) return null
        val salt = raw.copyOfRange(0, SALT_BYTES)
        val outer = raw.copyOfRange(SALT_BYTES, raw.size)
        val inner = open(hardwareKey(), outer) ?: return null
        return open(deriveFromPin(pin, salt), inner)
    }

    private fun deriveFromPin(pin: CharArray, salt: ByteArray): SecretKey {
        val spec = PBEKeySpec(pin, salt, PBKDF2_ITERATIONS, DK_BYTES * 8)
        val factory = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
        val bytes = factory.generateSecret(spec).encoded
        spec.clearPassword()
        return SecretKeySpec(bytes, "AES")
    }

    private fun seal(key: SecretKey, plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key) }
        return cipher.iv + cipher.doFinal(plain)
    }

    /** Null rather than an exception on a bad key: a wrong PIN is an ordinary
     *  event here, not an error condition, and it arrives as a GCM tag
     *  mismatch which is indistinguishable from corruption. */
    private fun open(key: SecretKey, sealed: ByteArray): ByteArray? = runCatching {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(GCM_TAG_BITS, sealed, 0, IV_BYTES))
        }
        cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
    }.getOrNull()

    private fun hardwareKey(): SecretKey = keystoreKey(HW_KEY_ALIAS, requireAuth = false)

    private fun keystoreKey(alias: String, requireAuth: Boolean): SecretKey {
        val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (store.getEntry(alias, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        val spec = KeyGenParameterSpec.Builder(
            alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
        if (requireAuth) {
            spec.setUserAuthenticationRequired(true)
            // A newly enrolled fingerprint invalidates this key, which drops
            // the analyst back to the PIN. That is the behaviour we want: a
            // print added to a seized handset must not open the queue.
            spec.setInvalidatedByBiometricEnrollment(true)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                spec.setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)
            } else {
                // API 29. -1 means "every use needs a fresh authentication",
                // which is the same intent as a zero-second validity window.
                @Suppress("DEPRECATION")
                spec.setUserAuthenticationValidityDurationSeconds(-1)
            }
        }
        generator.init(spec.build())
        return generator.generateKey()
    }

    // ------------------------------------------------------ biometric shortcut

    fun biometricEnabled(context: Context): Boolean = File(context.filesDir, BIO_FILE).exists()

    /** Cipher to hand BiometricPrompt for enabling the shortcut. Must be
     *  called while unlocked — the data key is what gets wrapped. */
    fun biometricEnrollCipher(): Cipher? = runCatching {
        Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.ENCRYPT_MODE, keystoreKey(BIO_KEY_ALIAS, requireAuth = true))
        }
    }.getOrNull()

    /** Called after the prompt authorises the cipher. */
    fun finishBiometricEnroll(context: Context, cipher: Cipher): Boolean {
        val dk = dataKey ?: return false
        return runCatching {
            val sealed = cipher.iv + cipher.doFinal(dk)
            File(context.filesDir, BIO_FILE).writeBytes(sealed)
            true
        }.getOrDefault(false)
    }

    fun disableBiometric(context: Context) {
        File(context.filesDir, BIO_FILE).let { if (it.exists()) Crypto.shred(it) }
        runCatching {
            KeyStore.getInstance(KEYSTORE).apply { load(null) }.deleteEntry(BIO_KEY_ALIAS)
        }
    }

    /** Cipher to hand BiometricPrompt for unlocking, or null if the shortcut
     *  is off or the key was invalidated by a fingerprint being enrolled. */
    fun biometricUnlockCipher(context: Context): Cipher? {
        val sealed = runCatching { File(context.filesDir, BIO_FILE).readBytes() }.getOrNull()
            ?: return null
        if (sealed.size <= IV_BYTES) return null
        return try {
            Cipher.getInstance("AES/GCM/NoPadding").apply {
                init(Cipher.DECRYPT_MODE, keystoreKey(BIO_KEY_ALIAS, requireAuth = true),
                     GCMParameterSpec(GCM_TAG_BITS, sealed, 0, IV_BYTES))
            }
        } catch (_: KeyPermanentlyInvalidatedException) {
            // Somebody enrolled a new fingerprint. Fall back to the PIN and
            // clear the shortcut rather than leaving a key that cannot work.
            disableBiometric(context)
            null
        } catch (_: Throwable) {
            null
        }
    }

    fun finishBiometricUnlock(context: Context, cipher: Cipher): Boolean {
        val sealed = runCatching { File(context.filesDir, BIO_FILE).readBytes() }.getOrNull()
            ?: return false
        val dk = runCatching {
            cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
        }.getOrNull() ?: return false
        dataKey = dk
        clearThrottle(context)
        _state.value = if (openable(context)) State.Open else State.Unreadable
        return true
    }

    /**
     * Open the database once, here, while we can still say something useful
     * about it.
     *
     * Room opens lazily on the first query, which happens on a background
     * coroutine inside a Flow — so a key that does not fit the file surfaced
     * as an uncatchable crash. Doing it eagerly at unlock turns that into a
     * screen with words on it.
     */
    private fun openable(context: Context): Boolean = runCatching {
        FieldDatabase.get(context).openHelper.writableDatabase.isOpen
    }.getOrElse {
        FieldDatabase.closeAndForget()
        false
    }

    /** Shred a store this app can no longer read. Only ever called when
     *  there is no vault, so nothing reachable is being destroyed. */
    private fun discardUnreadableStore(context: Context) {
        FieldDatabase.closeAndForget()
        listOf("field.db", "field.db-wal", "field.db-shm").forEach {
            Crypto.shred(context.getDatabasePath(it))
        }
        mediaDir(context).listFiles()?.forEach { Crypto.shred(it) }
        // The key file the pre-PIN build kept. Nothing reads it any more.
        Crypto.shred(File(context.filesDir, "store.key"))
    }

    // --------------------------------------------------------- the throttle

    /**
     * Increasing delays, and nothing is ever destroyed.
     *
     * The counter is an ordinary file. Somebody with root could delete it to
     * reset the delay — which is why the delay is not what protects the data;
     * PBKDF2 and the hardware key are. This makes casual guessing pointless
     * and buys time, and it will never cost an analyst their own reports
     * because they fumbled the PIN in the rain.
     */
    private val DELAYS_MS = longArrayOf(0, 0, 0, 5_000, 15_000, 60_000, 300_000, 900_000, 1_800_000)

    private fun failureCount(context: Context): Int =
        runCatching {
            File(context.filesDir, THROTTLE_FILE).readText().split(":")[0].toInt()
        }.getOrDefault(0)

    private fun lockedUntil(context: Context): Long =
        runCatching {
            File(context.filesDir, THROTTLE_FILE).readText().split(":")[1].toLong()
        }.getOrDefault(0L)

    fun remainingWaitMs(context: Context): Long =
        (lockedUntil(context) - System.currentTimeMillis()).coerceAtLeast(0L)

    private fun recordFailure(context: Context) {
        val n = (failureCount(context) + 1).coerceAtMost(9_999)
        val delay = DELAYS_MS[minOf(n, DELAYS_MS.size - 1)]
        File(context.filesDir, THROTTLE_FILE)
            .writeText("$n:${System.currentTimeMillis() + delay}")
    }

    private fun clearThrottle(context: Context) {
        runCatching { File(context.filesDir, THROTTLE_FILE).delete() }
    }

    private fun lockedState(context: Context) =
        State.Locked(System.currentTimeMillis() + remainingWaitMs(context), failureCount(context))

    // ------------------------------------------------------- the way back

    /**
     * Forgotten PIN: destroy the vault and everything it protected, so the
     * handset can be used again.
     *
     * This is deliberately the only way out, and deliberately total. Callers
     * must have told the analyst, in words, that the queued reports are gone.
     */
    fun destroyEverything(context: Context) {
        lock()
        FieldDatabase.closeAndForget()
        listOf(VAULT_FILE, BIO_FILE, THROTTLE_FILE, "store.key").forEach {
            Crypto.shred(File(context.filesDir, it))
        }
        mediaDir(context).listFiles()?.forEach { Crypto.shred(it) }
        listOf("field.db", "field.db-wal", "field.db-shm").forEach {
            Crypto.shred(context.getDatabasePath(it))
        }
        runCatching {
            val store = KeyStore.getInstance(KEYSTORE).apply { load(null) }
            store.deleteEntry(HW_KEY_ALIAS)
            store.deleteEntry(BIO_KEY_ALIAS)
        }
        _state.value = State.NeedsSetup
    }
}
