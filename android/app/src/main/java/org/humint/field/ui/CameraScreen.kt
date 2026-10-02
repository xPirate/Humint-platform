package org.humint.field.ui

import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.launch
import org.humint.field.media.Capture

/**
 * Taking a photo, full screen.
 *
 * This replaces a bottom sheet. The sheet seemed right — the analyst is
 * mid-report and a sheet keeps the form underneath — but it sized itself to
 * its content, and on a handset that put the shutter button below the
 * bottom of the screen. You had to know to drag the sheet upward before you
 * could take a picture, which is not something to discover while standing in
 * the rain looking at a van.
 *
 * A camera is also a thing people have firm expectations about: a big
 * viewfinder, a shutter at the bottom under your thumb, a way out that is
 * not a gesture, and a torch. None of that fits in a sheet, so it is a
 * screen.
 */
@Composable
fun PhotoCaptureScreen(
    onDone: (Capture.Captured?) -> Unit,
    onCancel: () -> Unit,
) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val scope = rememberCoroutineScope()

    var granted by remember { mutableStateOf(
        ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
    ) }
    val permission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted = it }
    LaunchedEffect(Unit) { if (!granted) permission.launch(Manifest.permission.CAMERA) }

    val capture = remember { ImageCapture.Builder().build() }
    var camera by remember { mutableStateOf<Camera?>(null) }
    var torch by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }

    Box(Modifier.fillMaxSize().background(Color.Black)) {

        if (granted) {
            AndroidView(
                factory = { ctx ->
                    val view = PreviewView(ctx).apply {
                        scaleType = PreviewView.ScaleType.FILL_CENTER
                    }
                    val future = ProcessCameraProvider.getInstance(ctx)
                    future.addListener({
                        runCatching {
                            val provider = future.get()
                            val preview = Preview.Builder().build()
                                .also { it.setSurfaceProvider(view.surfaceProvider) }
                            provider.unbindAll()
                            camera = provider.bindToLifecycle(
                                owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, capture)
                        }
                    }, ContextCompat.getMainExecutor(ctx))
                    view
                },
                modifier = Modifier.fillMaxSize(),
            )
        } else {
            Column(
                Modifier.fillMaxSize().padding(32.dp),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text("The camera is needed to take a photo.",
                     color = Color.White, style = MaterialTheme.typography.bodyLarge)
                Spacer(Modifier.height(10.dp))
                Text("Nothing taken here leaves the phone until you upload it.",
                     color = Color.White.copy(alpha = 0.7f),
                     style = MaterialTheme.typography.labelMedium)
            }
        }

        // --- top bar: out, and the torch ---------------------------------
        Row(
            Modifier.fillMaxWidth().statusBarsPadding().padding(horizontal = 8.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = onCancel, modifier = Modifier.heightIn(min = TapTarget)) {
                Text("Back", color = Color.White, fontWeight = FontWeight.Medium)
            }
            Spacer(Modifier.weight(1f))
            val hasTorch = camera?.cameraInfo?.hasFlashUnit() == true
            if (hasTorch) {
                TextButton(
                    onClick = {
                        torch = !torch
                        runCatching { camera?.cameraControl?.enableTorch(torch) }
                    },
                    modifier = Modifier.heightIn(min = TapTarget),
                ) {
                    Text(if (torch) "Light on" else "Light off",
                         color = if (torch) FieldAmber else Color.White,
                         fontWeight = FontWeight.Medium)
                }
            }
        }

        // --- the shutter, where a thumb already is ------------------------
        Column(
            Modifier.align(Alignment.BottomCenter).navigationBarsPadding().padding(bottom = 26.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                if (busy) "Saving…" else "Tap to take the photo",
                color = Color.White.copy(alpha = 0.85f),
                style = MaterialTheme.typography.labelMedium,
            )
            Spacer(Modifier.height(10.dp))
            Box(
                Modifier
                    .size(82.dp)
                    .clip(CircleShape)
                    .border(3.dp, Color.White.copy(alpha = 0.9f), CircleShape),
                contentAlignment = Alignment.Center,
            ) {
                Button(
                    onClick = {
                        if (busy || !granted) return@Button
                        busy = true
                        scope.launch {
                            val shot = runCatching { Capture.takePhoto(context, capture) }
                            runCatching { camera?.cameraControl?.enableTorch(false) }
                            onDone(shot.getOrNull())
                        }
                    },
                    enabled = granted && !busy,
                    shape = CircleShape,
                    colors = ButtonDefaults.buttonColors(containerColor = Color.White),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(0.dp),
                    modifier = Modifier.size(68.dp),
                ) {
                    if (busy) {
                        CircularProgressIndicator(
                            strokeWidth = 2.dp,
                            color = Color.Black,
                            modifier = Modifier.size(22.dp),
                        )
                    }
                }
            }
        }
    }

    DisposableEffect(Unit) {
        onDispose {
            runCatching {
                camera?.cameraControl?.enableTorch(false)
                ProcessCameraProvider.getInstance(context).get().unbindAll()
            }
        }
    }
}
