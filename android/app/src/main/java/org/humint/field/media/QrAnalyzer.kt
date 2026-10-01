package org.humint.field.media

import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.NotFoundException
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer

/**
 * Reading the console's enrollment QR, without Google Play Services.
 *
 * ML Kit's barcode scanner is the usual answer and the wrong one here: the
 * unbundled variant needs GMS to fetch its model, and the bundled variant
 * still drags a Google dependency into an app that is meant to run on a
 * de-Googled, off-network handset. ZXing is a few hundred kilobytes of pure
 * Java, decodes a QR from a luminance plane, and has no opinions about which
 * services the phone has.
 *
 * The analyser runs on CameraX's KEEP_ONLY_LATEST strategy, so a slow decode
 * drops frames rather than queueing them — which is what you want when the
 * analyst is holding a phone up to a screen and moving it.
 */
class QrAnalyzer(private val onFound: (String) -> Unit) : ImageAnalysis.Analyzer {

    private val reader = MultiFormatReader().apply {
        setHints(mapOf(
            DecodeHintType.POSSIBLE_FORMATS to listOf(com.google.zxing.BarcodeFormat.QR_CODE),
            // The enrollment payload is JSON and can run to a few hundred
            // characters, which makes for a dense code. TRY_HARDER is worth
            // the CPU on a modern handset and is the difference between
            // scanning first time and waving the phone about.
            DecodeHintType.TRY_HARDER to true,
        ))
    }

    @Volatile private var done = false

    override fun analyze(image: ImageProxy) {
        if (done) { image.close(); return }
        try {
            // Y plane only: a QR is black and white, and chroma is nothing
            // but work. CameraX gives YUV_420_888, whose first plane is
            // exactly the luminance ZXing wants.
            val plane = image.planes[0]
            val buffer = plane.buffer
            val bytes = ByteArray(buffer.remaining()).also { buffer.get(it) }
            val source = PlanarYUVLuminanceSource(
                bytes, plane.rowStride, image.height,
                0, 0, image.width, image.height, false
            )
            val result = reader.decodeWithState(BinaryBitmap(HybridBinarizer(source)))
            result?.text?.let {
                done = true
                onFound(it)
            }
        } catch (_: NotFoundException) {
            // Overwhelmingly the common case: this frame had no code in it.
        } catch (_: Throwable) {
            // A malformed frame is not worth crashing a scanner over.
        } finally {
            reader.reset()
            image.close()
        }
    }
}
