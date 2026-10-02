package org.humint.field

import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeWriter
import org.humint.field.media.QrFrames
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The scanner that opened the camera and never found anything.
 *
 * The first version passed `plane.rowStride` to ZXing as the data width,
 * against a buffer CameraX ends after the last real pixel rather than after
 * the last row's padding. Every frame read past the end on its final row and
 * threw; a bare `catch (Throwable)` swallowed it. The result was a scanner
 * that failed totally and silently, which is the worst way for something to
 * fail in an app that writes no log lines by design.
 *
 * These tests build the awkward frame shapes a real device produces and
 * assert a code comes back out. They run on the JVM — no emulator, no
 * handset — because the whole point is to know this works before anybody
 * drives somewhere with it.
 */
class QrDecodeTest {

    /** What an enrollment QR actually carries: a few hundred characters of
     *  JSON, which is a dense code and the hardest case. */
    private val payload = """{"v":1,"url":"http://10.0.0.14:8080",""" +
        """"token":"${"k7QmX2vP".repeat(8)}","label":"Field phone 2","user":"jneal"}"""

    /** Render a QR to a tight 8-bit luminance buffer. */
    private fun renderQr(text: String, scale: Int = 4): Triple<ByteArray, Int, Int> {
        val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, 0, 0)
        val w = matrix.width * scale
        val h = matrix.height * scale
        val out = ByteArray(w * h)
        for (y in 0 until h) for (x in 0 until w) {
            out[y * w + x] = if (matrix.get(x / scale, y / scale)) 0 else 255.toByte()
        }
        return Triple(out, w, h)
    }

    /**
     * Turn a tight buffer into the thing CameraX hands an analyzer: rows
     * padded out to `rowStride`, pixels optionally interleaved, and the
     * buffer stopping after the last real pixel — which is exactly the
     * detail the original code got wrong.
     */
    private fun asCameraPlane(
        tight: ByteArray, w: Int, h: Int, rowStride: Int, pixelStride: Int,
    ): ByteArray {
        val full = ByteArray(rowStride * h)
        for (y in 0 until h) for (x in 0 until w) {
            full[y * rowStride + x * pixelStride] = tight[y * w + x]
        }
        val used = (h - 1) * rowStride + (w - 1) * pixelStride + 1
        return full.copyOf(used)
    }

    private fun decode(data: ByteArray, w: Int, h: Int): String? = try {
        val reader = MultiFormatReader().apply {
            setHints(mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                DecodeHintType.TRY_HARDER to true,
            ))
        }
        reader.decodeWithState(
            BinaryBitmap(HybridBinarizer(
                PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false)))
        )?.text
    } catch (_: Throwable) {
        null
    }

    @Test
    fun `a padded camera frame decodes`() {
        val (tight, w, h) = renderQr(payload)
        val plane = asCameraPlane(tight, w, h, rowStride = w + 19, pixelStride = 1)
        val packed = QrFrames.pack(plane, w + 19, 1, w, h)
        assertEquals("the enrollment payload survives the repack", payload, decode(packed, w, h))
    }

    @Test
    fun `the old assumption really was short`() {
        val (tight, w, h) = renderQr(payload)
        val rowStride = w + 19
        val plane = asCameraPlane(tight, w, h, rowStride, 1)
        assertTrue(
            "a real Y plane is shorter than rowStride*height — this is the bug",
            plane.size < rowStride * h
        )
    }

    @Test
    fun `interleaved pixels decode`() {
        val (tight, w, h) = renderQr(payload)
        val rowStride = w * 2 + 13
        val plane = asCameraPlane(tight, w, h, rowStride, pixelStride = 2)
        val packed = QrFrames.pack(plane, rowStride, 2, w, h)
        assertEquals(payload, decode(packed, w, h))
    }

    @Test
    fun `an unpadded frame decodes`() {
        val (tight, w, h) = renderQr(payload)
        val packed = QrFrames.pack(tight, w, 1, w, h)
        assertEquals(payload, decode(packed, w, h))
    }

    @Test
    fun `every rotation the camera can report decodes`() {
        val (tight, w, h) = renderQr(payload)
        for (deg in intArrayOf(0, 90, 180, 270)) {
            val f = QrFrames.rotate(tight, w, h, deg)
            assertEquals("rotated $deg degrees", payload, decode(f.data, f.width, f.height))
        }
    }

    @Test
    fun `rotation preserves every pixel`() {
        val (tight, w, h) = renderQr("round trip", scale = 2)
        for (deg in intArrayOf(90, 180, 270)) {
            val once = QrFrames.rotate(tight, w, h, deg)
            val back = QrFrames.rotate(once.data, once.width, once.height, 360 - deg)
            assertEquals(w, back.width)
            assertEquals(h, back.height)
            assertTrue("$deg degrees there and back is lossless", tight.contentEquals(back.data))
        }
    }

    @Test
    fun `a truncated buffer yields a frame rather than an exception`() {
        val (tight, w, h) = renderQr(payload)
        val plane = asCameraPlane(tight, w, h, w + 19, 1)
        val packed = QrFrames.pack(plane.copyOf(plane.size / 3), w + 19, 1, w, h)
        assertEquals(
            "a cut-off frame still produces width*height bytes and no throw",
            w * h, packed.size
        )
    }

    @Test
    fun `a code off-centre in a wide frame still decodes`() {
        val (tight, w, h) = renderQr(payload)
        // A 16:9 frame with the code sitting in the middle, which is what a
        // phone held up to a laptop screen actually produces.
        val fw = w + 420
        val fh = h + 120
        val canvas = ByteArray(fw * fh) { 255.toByte() }
        val ox = (fw - w) / 2
        val oy = (fh - h) / 2
        for (y in 0 until h) for (x in 0 until w) {
            canvas[(oy + y) * fw + (ox + x)] = tight[y * w + x]
        }
        assertEquals(payload, decode(canvas, fw, fh))
    }
}
