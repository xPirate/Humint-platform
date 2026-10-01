package org.humint.field.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch
import org.humint.field.FieldViewModel
import org.humint.field.data.ReportRow
import org.humint.field.data.Templates
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The queue, and the way in to a new report.
 *
 * The one screen the analyst sees on opening the app, and it answers two
 * questions in that order: what have I not sent yet, and how do I start
 * another. Everything else is a screen away.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun QueueScreen(
    vm: FieldViewModel,
    onOpen: (String) -> Unit,
    onUpload: () -> Unit,
    onSettings: () -> Unit,
) {
    val queue by vm.queue.collectAsStateWithLifecycle()
    val ready by vm.readyCount.collectAsStateWithLifecycle()
    var picking by remember { mutableStateOf(false) }
    val sheet = rememberModalBottomSheetState()
    val scope = rememberCoroutineScope()

    Scaffold(
        topBar = {
            TopAppBar(title = {
                Column {
                    Text("Field reports")
                    Text(
                        if (queue.isEmpty()) "nothing waiting"
                        else "${queue.size} on this phone · $ready ready to send",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            },
            actions = {
                TextButton(onClick = onSettings) { Text("Settings") }
            })
        },
        bottomBar = {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Button(
                    onClick = { picking = true },
                    modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                ) { Text("New report", style = MaterialTheme.typography.titleMedium) }
                if (ready > 0) {
                    Button(
                        onClick = onUpload,
                        modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                        colors = androidx.compose.material3.ButtonDefaults.buttonColors(
                            containerColor = MaterialTheme.colorScheme.secondary,
                            contentColor = MaterialTheme.colorScheme.onPrimary,
                        ),
                    ) { Text("Send $ready — scan the console's code") }
                }
            }
        },
    ) { padding ->
        if (queue.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(padding).padding(32.dp),
                contentAlignment = Alignment.Center) {
                Text(
                    "Nothing on this phone.\n\nReports are written here and stay here, " +
                    "encrypted, until you are back in range of the console.",
                    style = MaterialTheme.typography.bodyLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            LazyColumn(
                Modifier.fillMaxSize().padding(padding),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(
                    start = 12.dp, end = 12.dp, top = 8.dp, bottom = 12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(queue, key = { it.id }) { row -> QueueCard(row) { onOpen(row.id) } }
            }
        }
    }

    if (picking) {
        ModalBottomSheet(onDismissRequest = { picking = false }, sheetState = sheet) {
            Text(
                "What are you reporting?",
                style = MaterialTheme.typography.titleLarge,
                modifier = Modifier.padding(start = 20.dp, end = 20.dp, bottom = 8.dp),
            )
            Templates.all.forEach { template ->
                Column(
                    Modifier
                        .fillMaxWidth()
                        .clickable {
                            scope.launch {
                                val id = vm.newReport(template.key)
                                picking = false
                                onOpen(id)
                            }
                        }
                        .padding(horizontal = 20.dp, vertical = 14.dp)
                ) {
                    Text(template.label, style = MaterialTheme.typography.titleMedium,
                         fontWeight = FontWeight.SemiBold)
                    Text(template.blurb, style = MaterialTheme.typography.bodyMedium,
                         color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            Spacer(Modifier.height(28.dp))
        }
    }
}

@Composable
private fun QueueCard(row: ReportRow, onClick: () -> Unit) {
    val template = Templates.byKey(row.template)
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                StatusChip(row.status)
                Spacer(Modifier.padding(horizontal = 4.dp))
                Text(
                    template?.label ?: row.template,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Spacer(Modifier.height(6.dp))
            Text(row.title, style = MaterialTheme.typography.titleMedium)
            Text(
                SimpleDateFormat("d MMM, HH:mm", Locale.getDefault())
                    .format(Date(row.observedAt ?: row.createdAt)),
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            // A failed send keeps its reason on the card rather than in a
            // toast that has long since gone. The analyst comes back to this
            // screen an hour later and needs to know why it is still here.
            row.lastError?.let {
                Spacer(Modifier.height(6.dp))
                Text(it, style = MaterialTheme.typography.bodyMedium,
                     color = MaterialTheme.colorScheme.error)
            }
        }
    }
}

@Composable
private fun StatusChip(status: String) {
    val (label, colour) = when (status) {
        "ready" -> "READY" to MaterialTheme.colorScheme.primary
        "sent" -> "SENT" to MaterialTheme.colorScheme.onSurfaceVariant
        else -> "DRAFT" to FieldAmber
    }
    Box(
        Modifier
            .background(colour.copy(alpha = 0.16f), RoundedCornerShape(4.dp))
            .padding(horizontal = 7.dp, vertical = 2.dp)
    ) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = colour)
    }
}
