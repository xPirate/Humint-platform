package org.humint.field.media

import android.content.Context
import android.media.MediaRecorder
import android.os.Build
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import org.humint.field.data.Crypto
import org.humint.field.data.mediaDir
import java.io.ByteArrayOutputStream
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

/**
 * Capturing a photo, a clip or a recording — and getting it encrypted before
 * it touches the disk.
 *
 * This is the awkward part of the whole app, and the reason it is awkward is
 * worth writing down. Every convenient Android capture API wants to write to
 * a File or a Uri itself: ImageCapture.OutputFileOptions, Recorder.FileOutput,
 * MediaRecorder.setOutputFile. Every one of those puts plaintext on disk,
 * and then "encrypted at rest" means encrypting a copy and deleting an
 * original that the flash controller may keep for weeks.
 *
 * So stills go through the in-memory path — ImageCapture.OnImageCapturedCallback
 * hands back a buffer, which is sealed and written once. Audio and video
 * cannot: MediaRecorder needs a real descriptor to seek in while it writes
 * the container. Those are written to a file in the app sandbox and sealed
 * when recording stops, with the plaintext shredded. That is a genuinely
 * weaker guarantee for clips than for stills, it is called out here rather
 * than glossed over, and it is why [Crypto.shred] exists and says what it
 * can and cannot promise.
 */
object Capture {

    fun stamp(): String =
        SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())

    data class Captured(
        val file: File,
        val filename: String,
        val mimeType: String,
        val sizeBytes: Long,
        val durationMs: Long? = null,
    )

    /**
     * A still, sealed on the way to disk — the JPEG never exists as
     * plaintext in a file at any point.
     */
    suspend fun takePhoto(context: Context, capture: ImageCapture): Captured =
        suspendCoroutine { cont ->
            capture.takePicture(
                androidx.core.content.ContextCompat.getMainExecutor(context),
                object : ImageCapture.OnImageCapturedCallback() {
                    override fun onCaptureSuccess(image: ImageProxy) {
                        try {
                            val bytes = image.use { it.toJpegBytes() }
                            val name = "photo-${stamp()}.jpg"
                            val target = File(mediaDir(context), "${System.nanoTime()}.bin")
                            target.writeBytes(Crypto.sealBytes(context, bytes))
                            cont.resume(
                                Captured(target, name, "image/jpeg", bytes.size.toLong())
                            )
                        } catch (t: Throwable) {
                            cont.resumeWithException(t)
                        }
                    }

                    override fun onError(exception: ImageCaptureException) {
                        cont.resumeWithException(exception)
                    }
                }
            )
        }

    private fun ImageProxy.toJpegBytes(): ByteArray {
        // ImageCapture in JPEG mode gives a single plane already holding a
        // complete JPEG; no YUV conversion is needed and attempting one here
        // would corrupt it.
        val buffer = planes[0].buffer
        return ByteArray(buffer.remaining()).also { buffer.get(it) }
    }

    /**
     * Voice notes. AAC in an MPEG-4 container, mono, 32 kbit/s — a minute is
     * about 240 KB, which matters when the uplink is one bar and the analyst
     * has four reports waiting.
     */
    class AudioRecording(private val context: Context) {
        private var recorder: MediaRecorder? = null
        private var plainFile: File? = null
        private var startedAt = 0L

        fun start() {
            val target = File(mediaDir(context), "rec-${System.nanoTime()}.m4a")
            plainFile = target
            recorder = newRecorder(context).apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioChannels(1)
                setAudioSamplingRate(44_100)
                setAudioEncodingBitRate(32_000)
                setOutputFile(target.absolutePath)
                prepare()
                start()
            }
            startedAt = System.currentTimeMillis()
        }

        /** Stop, seal the result, and shred the plaintext the encoder had to
         *  write. Returns null if nothing usable was recorded. */
        fun stop(): Captured? {
            val recorder = recorder ?: return null
            val plain = plainFile ?: return null
            val elapsed = System.currentTimeMillis() - startedAt
            runCatching { recorder.stop() }
            runCatching { recorder.release() }
            this.recorder = null
            if (!plain.exists() || plain.length() == 0L) {
                Crypto.shred(plain)
                return null
            }
            val bytes = plain.readBytes()
            val sealed = File(mediaDir(context), "${System.nanoTime()}.bin")
            sealed.writeBytes(Crypto.sealBytes(context, bytes))
            Crypto.shred(plain)
            return Captured(
                file = sealed,
                filename = "audio-${stamp()}.m4a",
                mimeType = "audio/mp4",
                sizeBytes = bytes.size.toLong(),
                durationMs = elapsed,
            )
        }

        fun cancel() {
            runCatching { recorder?.stop() }
            runCatching { recorder?.release() }
            recorder = null
            plainFile?.let { Crypto.shred(it) }
            plainFile = null
        }

        fun elapsedMs(): Long = if (recorder == null) 0 else System.currentTimeMillis() - startedAt
    }

    private fun newRecorder(context: Context): MediaRecorder =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) MediaRecorder(context)
        else @Suppress("DEPRECATION") MediaRecorder()

    /** Seal a clip CameraX has finished writing, and shred the plaintext. */
    fun sealRecording(context: Context, plain: File, durationMs: Long): Captured? {
        if (!plain.exists() || plain.length() == 0L) {
            Crypto.shred(plain)
            return null
        }
        val bytes = plain.readBytes()
        val sealed = File(mediaDir(context), "${System.nanoTime()}.bin")
        sealed.writeBytes(Crypto.sealBytes(context, bytes))
        Crypto.shred(plain)
        return Captured(
            file = sealed,
            filename = "video-${stamp()}.mp4",
            mimeType = "video/mp4",
            sizeBytes = bytes.size.toLong(),
            durationMs = durationMs,
        )
    }

    /** Decrypt for display. Small enough to hold: these are thumbnails and
     *  single frames, not the whole queue at once. */
    fun open(context: Context, path: String): ByteArray? =
        runCatching { Crypto.openBytes(context, File(path).readBytes()) }.getOrNull()

    fun jpegThumbnail(bytes: ByteArray, maxEdge: Int = 256): ByteArray? = runCatching {
        val full = android.graphics.BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            ?: return null
        val scale = maxEdge.toFloat() / maxOf(full.width, full.height)
        val small = if (scale >= 1f) full else android.graphics.Bitmap.createScaledBitmap(
            full, (full.width * scale).toInt().coerceAtLeast(1),
            (full.height * scale).toInt().coerceAtLeast(1), true
        )
        ByteArrayOutputStream().use { out ->
            small.compress(android.graphics.Bitmap.CompressFormat.JPEG, 80, out)
            out.toByteArray()
        }
    }.getOrNull()
}
