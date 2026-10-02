package org.humint.field.media

import android.os.Handler
import android.os.Looper
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.atomic.AtomicInteger
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

    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                // The enrollment payload is JSON and runs to a few hundred
                // characters, which makes for a dense code. TRY_HARDER is
                // worth the CPU and is the difference between scanning first
                // time and waving the phone about.
                DecodeHintType.TRY_HARDER to true,
            )
        )
    }

    @Volatile private var done = false

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
            frames.incrementAndGet()
            val text = decode(image)
            if (text != null) {
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

    private fun decode(image: ImageProxy): String? {
        val plane = image.planes[0]
        val buffer = plane.buffer
        val bytes = ByteArray(buffer.remaining())
        buffer.get(bytes)

        var w = image.width
        var h = image.height
        var packed = QrFrames.pack(bytes, plane.rowStride, plane.pixelStride, w, h)

        // The sensor is mounted sideways in every phone, so a portrait
        // viewfinder hands us a frame that is rotated. ZXing finds QR
        // patterns at any orientation in principle, but a dense code at the
        // edge of focus is a different matter, so try the upright frame
        // first and then the raw one.
        val rotation = image.imageInfo.rotationDegrees
        val candidates = ArrayList<Triple<ByteArray, Int, Int>>(2)
        if (rotation != 0) {
            val r = QrFrames.rotate(packed, w, h, rotation)
            candidates.add(Triple(r.data, r.width, r.height))
        }
        candidates.add(Triple(packed, w, h))

        for ((data, cw, ch) in candidates) {
            tryDecode(data, cw, ch)?.let { return it }
        }
        return null
    }

    private fun tryDecode(data: ByteArray, w: Int, h: Int): String? {
        // Whole frame first. A centred square second: a code that fills only
        // the middle of a wide frame binarises better once the empty edges
        // are out of the histogram, which is the case the analyst hits when
        // they hold the phone back far enough to get focus.
        val whole = PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false)
        read(whole)?.let { return it }

        // A square frame has no edges to trim, so the crop would just be the
        // whole frame decoded a second time.
        if (w == h) return null
        val side = minOf(w, h)
        val left = (w - side) / 2
        val top = (h - side) / 2
        return read(PlanarYUVLuminanceSource(data, w, h, left, top, side, side, false))
    }

    private fun read(source: PlanarYUVLuminanceSource): String? = try {
        reader.decodeWithState(BinaryBitmap(HybridBinarizer(source)))?.text
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
