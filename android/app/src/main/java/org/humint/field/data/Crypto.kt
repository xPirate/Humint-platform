package org.humint.field.data

import android.content.Context
import java.io.File
import javax.crypto.Cipher
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Sealing the captured media, and an honest note about shredding.
 *
 * The key itself belongs to [Vault], which holds it only while the app is
 * unlocked. This file used to generate and store a key of its own, wrapped
 * by the device keystore; it no longer does, because the PIN has to be part
 * of that wrapping. See Vault's docstring for how the data key is protected
 * and what that is actually worth.
 *
 * Everything here throws if the vault is locked. That is deliberate: a
 * photograph silently written in the clear because the key happened to be
 * gone would be the worst failure this app could have, and much worse than
 * a crash.
 */
object Crypto {

    private const val GCM_TAG_BITS = 128
    private const val IV_BYTES = 12

    class Locked : IllegalStateException(
        "The vault is locked; there is no key to encrypt or decrypt with.")

    /** Encrypt a captured file's bytes, returning iv || ciphertext. */
    fun sealBytes(context: Context, plain: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.ENCRYPT_MODE, mediaKey())
        }
        return cipher.iv + cipher.doFinal(plain)
    }

    fun openBytes(context: Context, sealed: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
            init(Cipher.DECRYPT_MODE, mediaKey(),
                 GCMParameterSpec(GCM_TAG_BITS, sealed, 0, IV_BYTES))
        }
        return cipher.doFinal(sealed, IV_BYTES, sealed.size - IV_BYTES)
    }

    private fun mediaKey(): SecretKey =
        SecretKeySpec(Vault.key() ?: throw Locked(), "AES")

    /**
     * Overwrite a file's bytes before unlinking it.
     *
     * On flash this is not a guarantee — the controller may well write the
     * zeroes to a fresh block and leave the original untouched until it is
     * garbage collected. It is done anyway because it costs nothing and
     * closes the easy case, and it is written down here so nobody mistakes
     * it for one. The real protection is that the file was encrypted to
     * begin with, under a key that needs both this handset and the PIN.
     */
    fun shred(file: File) {
        runCatching {
            if (file.exists() && file.length() > 0) {
                val zeros = ByteArray(minOf(file.length(), 1L shl 20).toInt())
                file.outputStream().use { out ->
                    var left = file.length()
                    while (left > 0) {
                        val n = minOf(left, zeros.size.toLong()).toInt()
                        out.write(zeros, 0, n)
                        left -= n
                    }
                    out.flush()
                }
            }
        }
        runCatching { file.delete() }
    }
}
