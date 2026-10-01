package org.humint.field.ui

import android.Manifest
import android.annotation.SuppressLint
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.video.FileOutputOptions
import androidx.camera.video.Quality
import androidx.camera.video.QualitySelector
import androidx.camera.video.Recorder
import androidx.camera.video.Recording
import androidx.camera.video.VideoCapture
import androidx.camera.video.VideoRecordEvent
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.humint.field.data.mediaDir
import org.humint.field.media.Capture
import java.io.File

/**
 * Taking a photo, a clip or a recording, without leaving the report.
 *
 * A bottom sheet rather than a separate screen: the analyst is mid-report
 * and every navigation is a chance to lose the thread of what they were
 * writing. The sheet also means the camera is torn down the moment it is
 * dismissed, which matters on a phone whose battery is the day's budget.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CaptureSheet(
    kind: String,
    onDone: (Capture.Captured?) -> Unit,
    onCancel: () -> Unit,
) {
    val context = LocalContext.current
    var granted by remember { mutableStateOf(false) }
    val permission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result -> granted = result.values.all { it } }

    LaunchedEffect(kind) {
        val needed = when (kind) {
            "audio" -> arrayOf(Manifest.permission.RECORD_AUDIO)
            "video" -> arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
            else -> arrayOf(Manifest.permission.CAMERA)
        }
        val already = needed.all {
            ContextCompat.checkSelfPermission(context, it) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        }
        if (already) granted = true else permission.launch(needed)
    }

    ModalBottomSheet(onDismissRequest = onCancel) {
        Column(Modifier.padding(horizontal = 16.dp).padding(bottom = 28.dp)) {
            if (!granted) {
                Text(
                    when (kind) {
                        "audio" -> "This needs the microphone."
                        else -> "This needs the camera."
                    },
                    style = MaterialTheme.typography.bodyLarge,
                )
                Text(
                    "Nothing recorded leaves this phone until you upload it.",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 6.dp),
                )
            } else when (kind) {
                "audio" -> AudioPane(onDone)
                "video" -> VideoPane(onDone)
                else -> PhotoPane(onDone)
            }
        }
    }
}

@Composable
private fun PhotoPane(onDone: (Capture.Captured?) -> Unit) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val scope = rememberCoroutineScope()
    val capture = remember { ImageCapture.Builder().build() }
    var busy by remember { mutableStateOf(false) }

    CameraPreview(bind = { provider, preview ->
        provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, capture)
    })

    Button(
        onClick = {
            if (busy) return@Button
            busy = true
            scope.launch {
                val result = runCatching { Capture.takePhoto(context, capture) }
                onDone(result.getOrNull())
            }
        },
        enabled = !busy,
        modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget).padding(top = 12.dp),
    ) { Text(if (busy) "Saving…" else "Take the photo") }
}

// withAudioEnabled() needs RECORD_AUDIO, which this sheet requested and
// waited for before rendering this pane at all; lint cannot see that.
@SuppressLint("MissingPermission")
@Composable
private fun VideoPane(onDone: (Capture.Captured?) -> Unit) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    // SD rather than the sensor's best. A minute of 1080p off a Pixel is
    // tens of megabytes and this has to go up a field uplink; HD is offered
    // as the fallback only because some devices do not expose SD.
    val recorder = remember {
        Recorder.Builder()
            .setQualitySelector(QualitySelector.from(
                Quality.SD, androidx.camera.video.FallbackStrategy.higherQualityOrLowerThan(Quality.HD)))
            .build()
    }
    val videoCapture = remember { VideoCapture.withOutput(recorder) }
    var recording by remember { mutableStateOf<Recording?>(null) }
    var startedAt by remember { mutableStateOf(0L) }
    var elapsed by remember { mutableStateOf(0) }
    var plainFile by remember { mutableStateOf<File?>(null) }

    CameraPreview(bind = { provider, preview ->
        provider.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, videoCapture)
    })

    LaunchedEffect(recording) {
        while (recording != null) {
            elapsed = ((System.currentTimeMillis() - startedAt) / 1000).toInt()
            // Two minutes is a hard stop. Not a limitation — a discipline:
            // nobody watches a ten-minute clip in a review queue, and it
            // would not upload from a field connection anyway.
            if (elapsed >= 120) recording?.stop()
            delay(250)
        }
    }

    Text(
        if (recording != null) "Recording  ${elapsed}s  (stops at 120s)"
        else "Up to two minutes, standard definition — it has to upload.",
        style = MaterialTheme.typography.labelMedium,
        color = if (recording != null) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 8.dp),
    )

    Button(
        onClick = {
            if (recording != null) {
                recording?.stop()
                return@Button
            }
            val target = File(mediaDir(context), "vid-${System.nanoTime()}.mp4")
            plainFile = target
            startedAt = System.currentTimeMillis()
            recording = videoCapture.output
                .prepareRecording(context, FileOutputOptions.Builder(target).build())
                .withAudioEnabled()
                .start(ContextCompat.getMainExecutor(context)) { event ->
                    if (event is VideoRecordEvent.Finalize) {
                        val duration = System.currentTimeMillis() - startedAt
                        recording = null
                        // CameraX had to write plaintext to get a seekable
                        // container; sealRecording encrypts it and shreds
                        // the original. See the note in Capture.kt.
                        onDone(Capture.sealRecording(context, target, duration))
                    }
                }
        },
        modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget).padding(top = 10.dp),
    ) { Text(if (recording != null) "Stop" else "Start recording") }
}

@Composable
private fun AudioPane(onDone: (Capture.Captured?) -> Unit) {
    val context = LocalContext.current
    val recorder = remember { Capture.AudioRecording(context) }
    var running by remember { mutableStateOf(false) }
    var elapsed by remember { mutableStateOf(0) }

    LaunchedEffect(running) {
        while (running) {
            elapsed = (recorder.elapsedMs() / 1000).toInt()
            delay(250)
        }
    }
    DisposableEffect(Unit) { onDispose { if (running) recorder.cancel() } }

    Text(
        if (running) "Recording  ${elapsed}s" else "Talk. It is faster than typing in the dark.",
        style = MaterialTheme.typography.titleMedium,
        color = if (running) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.onSurface,
        modifier = Modifier.padding(vertical = 16.dp),
    )

    Button(
        onClick = {
            if (running) {
                running = false
                onDone(recorder.stop())
            } else {
                runCatching { recorder.start() }
                    .onSuccess { running = true }
                    .onFailure { onDone(null) }
            }
        },
        modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
    ) { Text(if (running) "Stop and attach" else "Start recording") }

    if (running) {
        OutlinedButton(
            onClick = { running = false; recorder.cancel(); onDone(null) },
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        ) { Text("Throw it away") }
    }
}

/** The viewfinder. Bound to the composition's lifecycle, so it is released
 *  the moment the sheet closes rather than when the GC gets round to it. */
@Composable
fun CameraPreview(bind: (ProcessCameraProvider, Preview) -> Unit) {
    val context = LocalContext.current
    Box(
        Modifier
            .fillMaxWidth()
            .aspectRatio(3f / 4f)
    ) {
        AndroidView(
            factory = { ctx ->
                val view = PreviewView(ctx).apply {
                    scaleType = PreviewView.ScaleType.FILL_CENTER
                }
                val future = ProcessCameraProvider.getInstance(ctx)
                future.addListener({
                    val provider = future.get()
                    val preview = Preview.Builder().build()
                        .also { it.setSurfaceProvider(view.surfaceProvider) }
                    runCatching {
                        provider.unbindAll()
                        bind(provider, preview)
                    }
                }, ContextCompat.getMainExecutor(ctx))
                view
            },
            modifier = Modifier.fillMaxWidth(),
        )
    }
    DisposableEffect(Unit) {
        onDispose {
            runCatching {
                ProcessCameraProvider.getInstance(context).get().unbindAll()
            }
        }
    }
}
