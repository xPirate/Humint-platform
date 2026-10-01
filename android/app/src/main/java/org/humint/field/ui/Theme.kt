package org.humint.field.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import org.humint.field.data.ThemeChoice

/*
 * Two schemes, and the analyst picks.
 *
 * This is a tool used outside, often at night, and a white screen at 2am is
 * both hard on the eyes and visible from a long way off — so the dark scheme
 * is the one that got the attention. But a phone in direct sun needs the
 * other one, and the system setting does not always know which situation its
 * owner is in. Hence Settings: match the system, or override it.
 *
 * Text is deliberately larger than Material's defaults throughout. The
 * person using this is standing up, possibly in the rain, possibly with
 * gloves on, and reading a 12sp label is not something they should have to
 * do.
 */

private val Green = Color(0xFF39FF88)
private val GreenDim = Color(0xFF1F7A46)
private val Amber = Color(0xFFFFB020)
private val Red = Color(0xFFFF4D3D)

private val Dark = darkColorScheme(
    primary = Green,
    onPrimary = Color(0xFF00210E),
    primaryContainer = GreenDim,
    onPrimaryContainer = Color(0xFFD6FFE2),
    secondary = Color(0xFF7FD4B0),
    background = Color(0xFF05100A),
    onBackground = Color(0xFFD6FFE2),
    surface = Color(0xFF0B1B12),
    onSurface = Color(0xFFD6FFE2),
    surfaceVariant = Color(0xFF14291C),
    onSurfaceVariant = Color(0xFF8FBFA3),
    outline = Color(0xFF2C5740),
    error = Red,
    onError = Color(0xFF2A0000),
)

private val Light = lightColorScheme(
    primary = Color(0xFF13704A),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFB8F0D2),
    onPrimaryContainer = Color(0xFF00210E),
    secondary = Color(0xFF3C6B55),
    background = Color(0xFFF7FBF8),
    onBackground = Color(0xFF111B15),
    surface = Color.White,
    onSurface = Color(0xFF111B15),
    surfaceVariant = Color(0xFFE2EFE7),
    onSurfaceVariant = Color(0xFF41544A),
    outline = Color(0xFF9DB3A6),
    error = Color(0xFFB3261E),
)

val FieldAmber = Amber

private val FieldTypography = Typography().let { base ->
    base.copy(
        headlineSmall = base.headlineSmall.copy(fontSize = 26.sp),
        titleLarge = base.titleLarge.copy(fontSize = 22.sp),
        titleMedium = base.titleMedium.copy(fontSize = 19.sp),
        bodyLarge = base.bodyLarge.copy(fontSize = 18.sp, lineHeight = 26.sp),
        bodyMedium = base.bodyMedium.copy(fontSize = 16.sp, lineHeight = 23.sp),
        labelLarge = base.labelLarge.copy(fontSize = 17.sp),
        labelMedium = base.labelMedium.copy(fontSize = 14.sp),
    )
}

/** Minimum size for anything tappable. Material says 48dp; this is a tool
 *  used with cold or gloved hands, so the buttons that matter are bigger. */
val TapTarget = 56.dp

@Composable
fun FieldTheme(choice: ThemeChoice = ThemeChoice.SYSTEM, content: @Composable () -> Unit) {
    val dark = when (choice) {
        ThemeChoice.SYSTEM -> isSystemInDarkTheme()
        ThemeChoice.DARK -> true
        ThemeChoice.LIGHT -> false
    }
    MaterialTheme(
        colorScheme = if (dark) Dark else Light,
        typography = FieldTypography,
        content = content,
    )
}

val MonoStyle = TextStyle(fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace)
