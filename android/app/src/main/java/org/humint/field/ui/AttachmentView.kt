package org.humint.field.ui

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.humint.field.data.AttachmentRow
import org.humint.field.media.Capture

/**
 * Looking at a photo that has not been sent yet.
 *
 * Attachments are encrypted the moment they are taken, so there is no file
 * any gallery can open and nothing on this phone can show them but this app.
 * That is the right trade for a handset that might be taken, but it had one
 * consequence nobody thought through: until the report reached the console,
 * hours later and miles away, there was no way to find out whether the shot
 * was any good. A blurred plate, a thumb over the lens, the wrong vehicle —
 * all of it discovered far too late to walk back and take another.
 *
 * So the bytes are decrypted into memory, decoded, and drawn. They are never
 * written back out in the clear.
 */
@Composable
fun AttachmentThumb(row: AttachmentRow, onOpen: () -> Unit, size: Int = 56) {
    val context = LocalContext.current
    var bitmap by remember(row.id) { mutableStateOf<ImageBitmap?>(null) }

    LaunchedEffect(row.id) {
        bitmap = withContext(Dispatchers.IO) {
            runCatching {
                val plain = Capture.open(context, row.path) ?: return@runCatching null
                // Downsample on the way in. A 12-megapixel JPEG decoded at
                // full size to fill a 56dp square is how a phone with four
                // photos attached runs out of memory.
                val small = Capture.jpegThumbnail(plain, maxEdge = size * 4) ?: plain
                BitmapFactory.decodeByteArray(small, 0, small.size)?.asImageBitmap()
            }.getOrNull()
        }
    }

    Box(
        Modifier
            .size(size.dp)
            .clip(RoundedCornerShape(6.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable(onClick = onOpen),
        contentAlignment = Alignment.Center,
    ) {
        bitmap?.let {
            Image(it, contentDescription = "Photo attached to this report",
                  contentScale = ContentScale.Crop, modifier = Modifier.fillMaxSize())
        } ?: Text("…", style = MaterialTheme.typography.labelMedium,
                  color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

/** The same photo, as large as the screen allows, so it can actually be
 *  judged before the analyst walks away from whatever it is of. */
@Composable
fun FullImage(row: AttachmentRow, onClose: () -> Unit) {
    val context = LocalContext.current
    var bitmap by remember(row.id) { mutableStateOf<ImageBitmap?>(null) }
    var failed by remember(row.id) { mutableStateOf(false) }

    LaunchedEffect(row.id) {
        val decoded = withContext(Dispatchers.IO) {
            runCatching {
                val plain = Capture.open(context, row.path) ?: return@runCatching null
                val opts = BitmapFactory.Options().apply { inSampleSize = 2 }
                BitmapFactory.decodeByteArray(plain, 0, plain.size, opts)?.asImageBitmap()
            }.getOrNull()
        }
        bitmap = decoded
        failed = decoded == null
    }

    Dialog(
        onDismissRequest = onClose,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Box(Modifier.fillMaxSize().background(Color.Black)) {
            bitmap?.let {
                Image(it, contentDescription = row.filename,
                      contentScale = ContentScale.Fit,
                      modifier = Modifier.fillMaxSize().padding(bottom = 72.dp))
            }
            if (failed) {
                Text(
                    "This photo cannot be opened on this phone.",
                    color = Color.White,
                    style = MaterialTheme.typography.bodyLarge,
                    modifier = Modifier.align(Alignment.Center).padding(32.dp),
                )
            }
            Column(
                Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                    .fillMaxWidth().padding(16.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    "${row.filename} · ${row.sizeBytes / 1024} KB · not sent yet",
                    color = Color.White.copy(alpha = 0.8f),
                    style = MaterialTheme.typography.labelMedium,
                )
                Spacer(Modifier.height(6.dp))
                TextButton(onClick = onClose, modifier = Modifier.heightIn(min = TapTarget)) {
                    Text("Close", color = Color.White)
                }
            }
            TextButton(
                onClick = onClose,
                modifier = Modifier.align(Alignment.TopStart).statusBarsPadding()
                    .heightIn(min = TapTarget),
            ) { Text("Back", color = Color.White) }
        }
    }
}
