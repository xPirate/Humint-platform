package org.humint.field.media

import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.ChecksumException
import com.google.zxing.DecodeHintType
import com.google.zxing.FormatException
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.ResultPointCallback
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong
import java.util.concurrent.atomic.AtomicReference

/**
 * Reading the console's enrollment QR, without Google Play Services.
 *
 * ML Kit's barcode scanner is the usual answer and the wrong one here: the
 * unbundled variant needs GMS to fetch its model, and the bundled variant
 * still drags a Google dependency into an app meant to run on a de-Googled,
 * off-network handset. ZXing is a few hundred kilobytes of pure Java and has
 * no opinions about which services the phone has.
 *
 * ## What went wrong the first time
 *
 * The original handed `plane.rowStride` to ZXing as the data width and let
 * it index a buffer that is only `rowStride * (height - 1) + width` bytes
 * long — CameraX stops the buffer after the last real pixel, not after the
 * last row's padding. Every frame therefore read past the end on its final
 * row, threw, and was swallowed by a bare `catch (Throwable)`. The camera
 * opened, the preview ran, and nothing was ever decoded: a silent, total
 * failure with no log line, on an app that deliberately logs nothing.
 *
 * So this version repacks the Y plane into a tight `width * height` buffer
 * of its own, honouring both strides and never reading past the end of what
 * it was given. [QrFrames.pack] is a pure function with no Android types in
 * it, which is why it can be unit-tested on the JVM — see QrDecodeTest.
 */
class QrAnalyzer(
    /**
     * Called with the decoded payload, **on the main thread**.
     *
     * That guarantee is this class's job, not the caller's. CameraX runs an
     * analyzer on a background executor, and the first version handed the
     * payload straight to the UI from there. The decode worked; acting on it
     * did not — navigating away tears down the camera binding, and CameraX's
     * lifecycle bookkeeping refuses to run off the main thread, so the
     * successful scan died with "setCurrentState must be called on the main
     * thread" and the scanner stopped. Posting here makes that unavailable
     * as a mistake.
     */
    private val onFound: (String) -> Unit,
) : ImageAnalysis.Analyzer {

    private val main = Handler(Looper.getMainLooper())

    /**
     * Finder patterns — the three big squares in a QR's corners — that ZXing
     * located in the frame being analysed. Reset at the start of every frame
     * and only touched from the analyzer thread.
     */
    private var finderPoints = 0

    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                // The enrollment payload is JSON and runs to a few hundred
                // characters, which makes for a dense code. TRY_HARDER is
                // worth the CPU and is the difference between scanning first
                // time and waving the phone about.
                DecodeHintType.TRY_HARDER to true,
                // Told about every finder pattern as it is found, whether or
                // not the decode goes on to succeed. This is what lets the
                // viewfinder say "there is a code, I just cannot read it
                // yet" — the difference between "hold steady" and "point it
                // at the code", which from the outside look identical.
                DecodeHintType.NEED_RESULT_POINT_CALLBACK to
                    ResultPointCallback { finderPoints++ },
            )
        )
    }

    @Volatile private var done = false

    /** True once a code has been read and handed over, until [resume]. */
    val paused: Boolean get() = done

    /**
     * Start looking again after a code was read but turned out to be no use
     * — not an enrollment code, or one with no address in it.
     *
     * Without this the analyzer stayed finished after its first read, so a
     * wrong code left a live viewfinder that had silently stopped looking:
     * the preview moved, the right code could be held in front of it, and
     * nothing would ever happen.
     */
    fun resume() {
        lastLocatedAt.set(0)
        done = false
    }

    /**
     * When a code was last located in a frame — [SystemClock.elapsedRealtime],
     * or 0 for never. Located means the corner patterns were found, or the
     * decode got as far as the error-correction step and failed there: either
     * way there is a QR in view that this frame could not quite read.
     */
    val lastLocatedAt = AtomicLong(0)

    /** Frames looked at since the scanner opened. */
    val frames = AtomicInteger(0)

    /** The last unexpected failure, if there has been one. Not logged —
     *  nothing in this app writes to logcat — but shown in the UI after
     *  enough consecutive misses, because "it is broken" and "hold it
     *  steadier" need different answers from the person holding the phone. */
    val lastError = AtomicReference<String?>(null)

    override fun analyze(image: ImageProxy) {
        if (done) { image.close(); return }
        try {
            val n = frames.incrementAndGet()
            finderPoints = 0
            val text = decode(image, n)
            // Three corner patterns is a QR in view. Fewer can be noise —
            // a window frame or a keyboard can produce one or two.
            if (finderPoints >= 3) lastLocatedAt.set(SystemClock.elapsedRealtime())
            if (text != null) {
                lastLocatedAt.set(SystemClock.elapsedRealtime())
                // Set before posting: the next frame arrives long before the
                // main thread gets round to the handler, and scanning the
                // same code twice would start two uploads.
                done = true
                main.post {
                    runCatching { onFound(text) }.onFailure { note(it) }
                }
            }
        } catch (t: Throwable) {
            // A malformed frame is not worth killing the scanner over, but
            // it is worth remembering: a scanner that fails every frame for
            // the same reason should be able to say so.
            note(t)
        } finally {
            image.close()
        }
    }

    private fun note(t: Throwable) {
        lastError.set(t::class.java.simpleName + (t.message?.let { ": $it" } ?: ""))
    }

    private fun decode(image: ImageProxy, n: Int): String? {
        val plane = image.planes[0]
        val buffer = plane.buffer
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)

        val w = image.width
        val h = image.height
        val packed = QrFrames.pack(bytes, plane.rowStride, plane.pixelStride, w, h)

        // The centred square first: it is exactly what the brackets on the
        // viewfinder enclose, it is the same whichever way the sensor is
        // mounted, and with the background trimmed off a code that fills it
        // binarises better than it does in the whole frame. Then the whole
        // frame, for a code held off-centre.
        tryDecode(packed, w, h)?.let { return it }

        // The sensor is mounted sideways in every phone, so a portrait
        // viewfinder hands us a rotated frame. ZXing finds a QR at any
        // orientation in principle; in practice a dense code at the edge of
        // focus sometimes reads only upright. Rotating a full frame is the
        // costly step, so it is tried on every third frame rather than all
        // of them — at the analysis resolution the scanner now asks for,
        // doing it every time would halve the frame rate it can keep up.
        val rotation = image.imageInfo.rotationDegrees
        if (rotation != 0 && n % 3 == 0) {
            val r = QrFrames.rotate(packed, w, h, rotation)
            read(PlanarYUVLuminanceSource(r.data, r.width, r.height,
                                          0, 0, r.width, r.height, false))?.let { return it }
        }
        return null
    }

    private fun tryDecode(data: ByteArray, w: Int, h: Int): String? {
        if (w != h) {
            val side = minOf(w, h)
            val left = (w - side) / 2
            val top = (h - side) / 2
            read(PlanarYUVLuminanceSource(data, w, h, left, top, side, side, false))
                ?.let { return it }
        }
        // A square frame has no edges to trim, so it is only decoded once.
        return read(PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false))
    }

    private fun read(source: PlanarYUVLuminanceSource): String? = try {
        reader.decodeWithState(BinaryBitmap(HybridBinarizer(source)))?.text
    } catch (_: ChecksumException) {
        // Found, sampled, and failed error correction: a code is in view but
        // blurred, glared or too small. Worth saying so.
        lastLocatedAt.set(SystemClock.elapsedRealtime())
        null
    } catch (_: FormatException) {
        lastLocatedAt.set(SystemClock.elapsedRealtime())
        null
    } catch (_: Throwable) {
        // NotFoundException is the overwhelmingly common case — this frame
        // simply had no code in it — and the binarizer can throw on a frame
        // that is all one colour. Neither is an error worth recording.
        null
    } finally {
        reader.reset()
    }
}

/**
 * The frame arithmetic, with no Android types in it so it can be tested.
 *
 * Kept deliberately separate from [QrAnalyzer]: the bug that made the
 * scanner useless lived entirely in these few lines, and the only way to
 * know it is fixed without a handset in hand is to be able to call them
 * from a unit test.
 */
object QrFrames {

    class Frame(val data: ByteArray, val width: Int, val height: Int)

    /**
     * Copy a camera Y plane into a tight `width * height` luminance buffer.
     *
     * `rowStride` is how many bytes one row occupies including padding, and
     * `pixelStride` how many bytes one pixel occupies — both are whatever
     * the device felt like, and neither is safe to assume. The source buffer
     * routinely stops before `rowStride * height`, so every read is bounded:
     * a short or truncated buffer yields black pixels rather than an
     * exception, because a scanner that throws is a scanner that silently
     * stops scanning.
     */
    fun pack(src: ByteArray, rowStride: Int, pixelStride: Int, width: Int, height: Int): ByteArray {
        val out = ByteArray(width * height)
        if (width <= 0 || height <= 0) return out
        for (row in 0 until height) {
            val from = row * rowStride
            val to = row * width
            if (pixelStride == 1) {
                val n = (minOf(from + width, src.size) - from).coerceAtLeast(0)
                if (n > 0) System.arraycopy(src, from, out, to, n)
            } else {
                for (col in 0 until width) {
                    val i = from + col * pixelStride
                    if (i < src.size) out[to + col] = src[i]
                }
            }
        }
        return out
    }

    /** Rotate a tight luminance buffer clockwise by 0, 90, 180 or 270. */
    fun rotate(src: ByteArray, width: Int, height: Int, degrees: Int): Frame {
        return when (((degrees % 360) + 360) % 360) {
            90 -> {
                val out = ByteArray(width * height)
                for (y in 0 until height) for (x in 0 until width) {
                    out[x * height + (height - 1 - y)] = src[y * width + x]
                }
                Frame(out, height, width)
            }
            180 -> {
                val out = ByteArray(width * height)
                val last = width * height - 1
                for (i in 0 until width * height) out[last - i] = src[i]
                Frame(out, width, height)
            }
            270 -> {
                val out = ByteArray(width * height)
                for (y in 0 until height) for (x in 0 until width) {
                    out[(width - 1 - x) * height + y] = src[y * width + x]
                }
                Frame(out, height, width)
            }
            else -> Frame(src, width, height)
        }
    }
}
