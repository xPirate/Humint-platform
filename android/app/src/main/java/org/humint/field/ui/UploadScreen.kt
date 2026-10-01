package org.humint.field.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.humint.field.FieldViewModel
import org.humint.field.net.Uploader

/**
 * Sending, and saying honestly what happened.
 *
 * A field tool that reports "uploaded ✓" when two of five reports failed is
 * worse than one that reports nothing, because the analyst will delete their
 * only copy on the strength of it. So this screen names the number that got
 * through, the number that did not, and leaves the failures in the queue with
 * their reasons on them.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun UploadScreen(vm: FieldViewModel, onDone: () -> Unit) {
    val progress by vm.upload.collectAsStateWithLifecycle()
    val notice by vm.notice.collectAsStateWithLifecycle()

    LaunchedEffect(Unit) { vm.send() }

    Scaffold(topBar = { TopAppBar(title = { Text("Sending") }) }) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Spacer(Modifier.height(24.dp))
            when (val p = progress) {
                is Uploader.Progress.Checking -> {
                    CircularProgressIndicator()
                    Text("Talking to ${p.host}…", style = MaterialTheme.typography.bodyLarge)
                }
                is Uploader.Progress.Sending -> {
                    CircularProgressIndicator()
                    Text("${p.index} of ${p.total}",
                         style = MaterialTheme.typography.titleMedium)
                    Text(p.title, style = MaterialTheme.typography.bodyMedium,
                         color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                is Uploader.Progress.SendingFile -> {
                    CircularProgressIndicator()
                    Text("Sending ${p.filename} (${p.index} of ${p.total})",
                         style = MaterialTheme.typography.bodyMedium)
                }
                is Uploader.Progress.Done -> {
                    Text(
                        when {
                            p.failed == 0 && p.sent > 0 ->
                                "Sent ${p.sent}. Removed from this phone."
                            p.sent == 0 -> "Nothing went through."
                            else -> "Sent ${p.sent}. ${p.failed} still here."
                        },
                        style = MaterialTheme.typography.titleMedium,
                    )
                    if (p.failed > 0) {
                        Text(
                            "What failed is still in the queue with the reason on it. " +
                            "Nothing was thrown away.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Text(
                        "The console's address and token are gone from this phone again.",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                is Uploader.Progress.Failed -> {
                    Text("Could not send", style = MaterialTheme.typography.titleMedium,
                         color = MaterialTheme.colorScheme.error)
                    Text(p.message, style = MaterialTheme.typography.bodyLarge)
                    Text(
                        "Everything is still on this phone. Scan again when you can.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                null -> CircularProgressIndicator()
            }

            notice?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium, color = FieldAmber)
            }

            Spacer(Modifier.height(8.dp))
            val finished = progress is Uploader.Progress.Done ||
                           progress is Uploader.Progress.Failed
            if (finished) {
                Button(
                    onClick = { vm.dismissUpload(); vm.clearNotice(); onDone() },
                    modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                ) { Text("Back to the queue") }
            }
        }
    }
}
