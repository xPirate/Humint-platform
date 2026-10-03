package org.humint.field.ui

import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import android.os.SystemClock
import android.util.Size
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.SurfaceOrientedMeteringPointFactory
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.humint.field.FieldViewModel
import org.humint.field.media.QrAnalyzer
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * The moment the phone learns where the console is.
 *
 * This screen exists because the address and the token are not on the
 * handset. The analyst comes back into range, an admin (or their own laptop)
 * shows the enrollment QR, they scan it, and the app holds those details in
 * memory just long enough to empty the queue.
 *
 * There is a typed fallback underneath, because a phone with a cracked
 * camera or a QR on a screen that will not focus should not mean a queue
 * that cannot be delivered.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ScanScreen(vm: FieldViewModel, onScanned: () -> Unit, onCancel: () -> Unit) {
    val context = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val ready by vm.readyCount.collectAsStateWithLifecycle()
    val notice by vm.notice.collectAsStateWithLifecycle()
    var granted by remember { mutableStateOf(false) }
    var typing by remember { mutableStateOf(false) }
    var handled by remember { mutableStateOf(false) }

    val permission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted = it }

    LaunchedEffect(Unit) {
        granted = ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
        if (!granted) permission.launch(Manifest.permission.CAMERA)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Scan to send") },
                navigationIcon = { TextButton(onClick = onCancel) { Text("Back") } },
            )
        },
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
        ) {
            Text(
                "$ready report${if (ready == 1) "" else "s"} ready. Scan the code the console " +
                "shows under Admin settings → Field devices.",
                style = MaterialTheme.typography.bodyLarge,
                modifier = Modifier.padding(vertical = 10.dp),
            )

            if (granted && !typing) {
                val executor = remember { Executors.newSingleThreadExecutor() }
                var looked by remember { mutableStateOf(0) }
                var fault by remember { mutableStateOf<String?>(null) }
                var phase by remember { mutableStateOf(ScanPhase.Starting) }
                var rejectedUntil by remember { mutableStateOf(0L) }
                var camera by remember { mutableStateOf<Camera?>(null) }
                val haptics = LocalHapticFeedback.current
                val scope = rememberCoroutineScope()
                val analysis = remember {
                    ImageAnalysis.Builder()
                        // Drop frames rather than queue them: the analyst is
                        // moving the phone, and a backlog of stale frames is
                        // a scanner that feels broken.
                        .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                        // CameraX analyses at 640x480 unless told otherwise,
                        // which leaves a phone-held enrollment code only a
                        // couple of pixels per module — readable on a good
                        // camera, not on a rugged handset's. 1280x960 is 4:3
                        // like the viewfinder, so the brackets line up with
                        // what is analysed.
                        .setResolutionSelector(
                            ResolutionSelector.Builder()
                                .setAspectRatioStrategy(
                                    AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY)
                                .setResolutionStrategy(ResolutionStrategy(
                                    Size(1280, 960),
                                    ResolutionStrategy.FALLBACK_RULE_CLOSEST_LOWER_THEN_HIGHER))
                                .build())
                        .build()
                }
                // QrAnalyzer guarantees onFound arrives on the main thread,
                // so this can navigate and tear the camera down safely.
                val analyzer = remember {
                    QrAnalyzer(onFound = { payload ->
                        if (handled) return@QrAnalyzer
                        handled = true
                        phase = ScanPhase.Read
                        vm.onScanned(payload) { ok ->
                            if (ok) {
                                // Leave the brackets green long enough to be
                                // seen, so "it worked" is something the
                                // analyst watched happen rather than infers
                                // from the screen changing.
                                haptics.performHapticFeedback(HapticFeedbackType.LongPress)
                                scope.launch { delay(450); onScanned() }
                            } else {
                                // A code, but not one that leads anywhere.
                                // Show it in red for a moment, then look
                                // again — the loop below resumes the
                                // analyzer once the moment has passed.
                                rejectedUntil = SystemClock.elapsedRealtime() + 1500
                                phase = ScanPhase.Rejected
                                handled = false
                            }
                        }
                    })
                }
                fun focusCentre() {
                    val c = camera ?: return
                    val point = SurfaceOrientedMeteringPointFactory(1f, 1f)
                        .createPoint(0.5f, 0.5f)
                    runCatching {
                        c.cameraControl.startFocusAndMetering(
                            FocusMeteringAction.Builder(
                                point, FocusMeteringAction.FLAG_AF or FocusMeteringAction.FLAG_AE)
                                .setAutoCancelDuration(3, TimeUnit.SECONDS)
                                .build())
                    }
                }
                LaunchedEffect(analysis, analyzer) {
                    analysis.setAnalyzer(executor, analyzer)
                    // Read the analyzer's counters on a timer rather than
                    // being called back per frame — thirty wake-ups a second
                    // is a cost the battery does not need. Fast enough that
                    // the brackets turn amber while the code is still there.
                    var lastFocus = 0L
                    while (true) {
                        delay(120)
                        val now = SystemClock.elapsedRealtime()
                        looked = analyzer.frames.get()
                        fault = analyzer.lastError.get()
                        if (phase == ScanPhase.Read) continue
                        if (phase == ScanPhase.Rejected) {
                            if (now < rejectedUntil) continue
                            analyzer.resume()
                        }
                        val seen = now - analyzer.lastLocatedAt.get() < 700
                        phase = when {
                            looked == 0 -> ScanPhase.Starting
                            seen -> ScanPhase.Located
                            else -> ScanPhase.Searching
                        }
                        // Nudge the focus back to the middle every few
                        // seconds while nothing is in view. Continuous
                        // autofocus will happily settle on the room behind
                        // a laptop screen and stay there.
                        if (phase == ScanPhase.Searching && now - lastFocus > 4000) {
                            focusCentre(); lastFocus = now
                        }
                    }
                }
                CameraPreview(
                    bind = { provider, preview ->
                        camera = provider.bindToLifecycle(
                            owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
                        focusCentre()
                    },
                    overlay = {
                        ScanViewfinder(phase = phase, frames = looked, onTap = { focusCentre() })
                    },
                )
                Spacer(Modifier.height(8.dp))
                Text(
                    when {
                        fault != null -> "The scanner is failing on this phone: $fault"
                        phase == ScanPhase.Starting -> "Starting the camera…"
                        phase == ScanPhase.Located ->
                            "There is a code in view but it is not readable yet. Hold " +
                            "still, and move closer until it fills the brackets."
                        phase == ScanPhase.Read -> "Read. Checking it…"
                        phase == ScanPhase.Rejected -> "That code was read, but it is not " +
                            "an enrollment code this app can use. Looking again."
                        looked < 25 -> "Fit the code inside the brackets."
                        else -> "Still looking — try more light, or move back a little " +
                                "so the whole code is inside the brackets. Tap to refocus."
                    },
                    style = MaterialTheme.typography.bodyMedium,
                    color = when {
                        fault != null || phase == ScanPhase.Rejected ->
                            MaterialTheme.colorScheme.error
                        phase == ScanPhase.Located || looked >= 25 -> FieldAmber
                        else -> MaterialTheme.colorScheme.onSurfaceVariant
                    },
                )
                Spacer(Modifier.height(6.dp))
                Text(
                    "Nothing about the console is written to this phone. When the upload is " +
                    "finished, or the app goes to the background, it is forgotten again.",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(10.dp))
                OutlinedButton(
                    onClick = { typing = true },
                    modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                ) { Text("Type it instead") }
            } else if (!granted && !typing) {
                Text("The camera is needed to scan the code.",
                     style = MaterialTheme.typography.bodyLarge)
                Spacer(Modifier.height(12.dp))
                OutlinedButton(
                    onClick = { typing = true },
                    modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                ) { Text("Type the address and token instead") }
            } else {
                TypedEntry(onSubmit = { url, token ->
                    val payload = JSONObject()
                        .put("v", 1).put("url", url).put("token", token).toString()
                    vm.onScanned(payload) { ok -> if (ok) onScanned() }
                }, onBack = { typing = false })
            }

            notice?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, color = MaterialTheme.colorScheme.error,
                     style = MaterialTheme.typography.bodyMedium)
            }
        }
    }
}

@Composable
private fun TypedEntry(onSubmit: (String, String) -> Unit, onBack: () -> Unit) {
    var url by remember { mutableStateOf("http://") }
    var token by remember { mutableStateOf("") }
    Column {
        OutlinedTextField(
            value = url, onValueChange = { url = it },
            label = { Text("Console address") },
            supportingText = { Text("the one the phone can reach, not localhost") },
            singleLine = true, modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(10.dp))
        OutlinedTextField(
            value = token, onValueChange = { token = it },
            label = { Text("Token") },
            singleLine = false, minLines = 2,
            textStyle = MonoStyle,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(14.dp))
        Button(
            onClick = { onSubmit(url.trim(), token.trim()) },
            enabled = token.isNotBlank() && url.length > 8,
            modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
        ) { Text("Connect and send") }
        Spacer(Modifier.height(8.dp))
        TextButton(onClick = onBack) { Text("Back to the scanner") }
    }
}
