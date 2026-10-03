package org.humint.field.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * What the scanner thinks it is looking at, as the viewfinder shows it.
 *
 * Before this the only sign of life was a line of text under the preview,
 * and a scanner that is working and a scanner that has quietly stopped
 * looked identical. Every state here is one the person holding the phone
 * can act on differently.
 */
enum class ScanPhase {
    /** No frame has reached the analyzer yet. */
    Starting,
    /** Frames are being read; no code in them. Grey brackets, moving line. */
    Searching,
    /** A QR is in view but this frame could not read it. Amber. */
    Located,
    /** Read. Green, and the phone buzzes. */
    Read,
    /** Read, but it was not a usable enrollment code. Red, briefly. */
    Rejected,
}

private val Idle = Color(0xFFBDBDBD)
private val Seen = Color(0xFFFFB020)
private val Good = Color(0xFF39FF88)
private val Bad = Color(0xFFFF4D3D)

/**
 * Corner brackets over the camera preview, with a scrim outside them.
 *
 * The square is the region the analyzer tries first (the centred square of
 * each frame), so "fit the code inside the brackets" is literally true and
 * not just a picture.
 */
@Composable
fun BoxScope.ScanViewfinder(phase: ScanPhase, frames: Int, onTap: () -> Unit) {
    val colour by animateColorAsState(
        targetValue = when (phase) {
            ScanPhase.Starting, ScanPhase.Searching -> Idle
            ScanPhase.Located -> Seen
            ScanPhase.Read -> Good
            ScanPhase.Rejected -> Bad
        },
        animationSpec = tween(180),
        label = "bracket",
    )

    // The sweep is the "it is trying" signal. It runs off the frame clock,
    // not the analyzer, so it shows the screen is alive; the frame counter
    // in the label is what shows the analyzer is.
    val sweep by rememberInfiniteTransition(label = "sweep").animateFloat(
        initialValue = 0f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(1600, easing = LinearEasing),
                                           RepeatMode.Reverse),
        label = "sweep",
    )
    val sweeping = phase == ScanPhase.Searching || phase == ScanPhase.Located

    Canvas(
        Modifier
            .fillMaxSize()
            // Tapping asks the camera to focus on the middle again — the fix
            // for a lens that has hunted off to the background.
            .pointerInput(Unit) { detectTapGestures { onTap() } }
            .semantics { contentDescription = phaseLabel(phase, frames) },
    ) {
        val side = size.minDimension * 0.72f
        val left = (size.width - side) / 2f
        val top = (size.height - side) / 2f

        scrimAround(left, top, side, Color.Black.copy(alpha = 0.45f))

        if (phase == ScanPhase.Read || phase == ScanPhase.Rejected) {
            drawRect(colour.copy(alpha = 0.18f), Offset(left, top), Size(side, side))
        }

        if (sweeping) {
            val y = top + side * sweep
            val band = side * 0.08f
            drawRect(
                Brush.verticalGradient(
                    listOf(Color.Transparent, colour.copy(alpha = 0.55f), Color.Transparent),
                    startY = y - band, endY = y + band,
                ),
                Offset(left + 6.dp.toPx(), y - band),
                Size(side - 12.dp.toPx(), band * 2),
            )
        }

        brackets(left, top, side, colour,
                 stroke = (if (phase == ScanPhase.Read) 6 else 4).dp.toPx(),
                 arm = side * 0.18f)
    }

    Text(
        phaseLabel(phase, frames),
        color = Color.White,
        fontSize = 14.sp,
        fontWeight = FontWeight.Medium,
        modifier = Modifier
            .align(Alignment.BottomCenter)
            .padding(bottom = 14.dp)
            .background(Color.Black.copy(alpha = 0.6f), RoundedCornerShape(50))
            .padding(horizontal = 14.dp, vertical = 6.dp),
    )
}

private fun phaseLabel(phase: ScanPhase, frames: Int): String = when (phase) {
    ScanPhase.Starting -> "Starting camera…"
    ScanPhase.Searching -> "Scanning · $frames frames"
    ScanPhase.Located -> "Code found — hold steady"
    ScanPhase.Read -> "Read"
    ScanPhase.Rejected -> "Not an enrollment code"
}

private fun DrawScope.scrimAround(left: Float, top: Float, side: Float, c: Color) {
    drawRect(c, Offset.Zero, Size(size.width, top))
    drawRect(c, Offset(0f, top + side), Size(size.width, size.height - top - side))
    drawRect(c, Offset(0f, top), Size(left, side))
    drawRect(c, Offset(left + side, top), Size(size.width - left - side, side))
}

private fun DrawScope.brackets(left: Float, top: Float, side: Float, c: Color,
                               stroke: Float, arm: Float) {
    val r = left + side
    val b = top + side
    fun l(x1: Float, y1: Float, x2: Float, y2: Float) =
        drawLine(c, Offset(x1, y1), Offset(x2, y2), stroke, StrokeCap.Round)
    l(left, top, left + arm, top);  l(left, top, left, top + arm)
    l(r, top, r - arm, top);        l(r, top, r, top + arm)
    l(left, b, left + arm, b);      l(left, b, left, b - arm)
    l(r, b, r - arm, b);            l(r, b, r, b - arm)
}
