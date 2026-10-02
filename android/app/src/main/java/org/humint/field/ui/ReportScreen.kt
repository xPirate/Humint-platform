package org.humint.field.ui

import android.Manifest
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
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
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch
import org.humint.field.FieldViewModel
import org.humint.field.data.AttachmentRow
import org.humint.field.data.ReportRow
import org.humint.field.data.TemplateField
import org.humint.field.data.Templates
import org.humint.field.toJson
import org.humint.field.toMap

/**
 * One report, on one screen.
 *
 * The form is generated from the template rather than written per type:
 * seven hand-built forms would be seven places for the field names to drift
 * away from the console's, which is precisely the bug the shared registry
 * exists to prevent.
 *
 * Everything is saved as it is typed. There is no Save button for the body
 * of the form and there should not be — the phone is going in a pocket
 * between fields, Android will kill the process, and a report lost that way
 * is a report that does not get re-written.
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun ReportScreen(vm: FieldViewModel, reportId: String, onDone: () -> Unit) {
    val context = LocalContext.current
    val row by vm.reportFlow(reportId).collectAsStateWithLifecycle(initialValue = null)
    val attachments by vm.attachmentsFlow(reportId)
        .collectAsStateWithLifecycle(initialValue = emptyList())
    val fix by vm.position.fix.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    var confirmDiscard by remember { mutableStateOf(false) }
    var capturing by remember { mutableStateOf<String?>(null) }

    // Position runs only while this screen is on it. See Position.kt: there
    // is no background location here on purpose.
    val locationPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { vm.position.start() }
    DisposableEffect(Unit) {
        if (vm.position.hasPermission()) vm.position.start()
        else locationPermission.launch(arrayOf(
            Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION))
        onDispose { vm.position.stop() }
    }

    val report = row ?: return
    val template = Templates.byKey(report.template) ?: return
    val values = remember(report.fields) { report.fields.toMap() }

    // Each field writes only its own key, onto whatever the row looks like
    // when the write happens — see FieldViewModel.edit. Writing a whole
    // captured row here would let a slow field revert a fast one.
    fun put(key: String, value: Any?) {
        vm.edit(report.id) { row -> row.copy(fields = (row.fields.toMap() + (key to value)).toJson()) }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(template.label) },
                navigationIcon = {
                    TextButton(onClick = onDone) { Text("Queue") }
                },
                actions = {
                    TextButton(onClick = { confirmDiscard = true }) {
                        Text("Discard", color = MaterialTheme.colorScheme.error)
                    }
                },
            )
        },
        snackbarHost = { SnackbarHost(snackbar) },
        bottomBar = {
            Column(Modifier.padding(16.dp)) {
                Button(
                    onClick = {
                        vm.markReady(report) { why ->
                            scope.launch { snackbar.showSnackbar(why) }
                        }
                        if (template.whatIsMissing(values) == null) onDone()
                    },
                    modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                ) {
                    Text(
                        if (report.status == "ready") "Ready to send ✓" else "Mark ready to send",
                        style = MaterialTheme.typography.titleMedium,
                    )
                }
                Text(
                    "Nothing leaves this phone until you upload, back in range.",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 6.dp),
                )
            }
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Spacer(Modifier.height(2.dp))

            CriticalityRow(report.criticality) { level -> vm.edit(report.id) { it.copy(criticality = level) } }

            template.fields.forEach { field ->
                FormField(field, values[field.key]) { put(field.key, it) }
            }

            // Every template takes free text on top of its own fields. The
            // form can never cover everything, and the alternative is the
            // analyst leaving it out.
            DraftTextField(
                value = report.body.orEmpty(),
                onCommit = { text -> vm.edit(report.id) { it.copy(body = text.ifBlank { null }) } },
                label = "Anything else",
                modifier = Modifier.fillMaxWidth(),
                minLines = 2,
            )

            PositionRow(fix, report) { note -> vm.edit(report.id) { it.copy(locationNote = note) } }
            LaunchedEffect(fix) {
                // Stamp the fix onto the report as it improves, so a report
                // written over ten minutes carries the best position the
                // phone had rather than the first one it managed.
                fix?.let { f ->
                    vm.edit(report.id) { row ->
                        if (row.accuracyM == null || f.accuracyM <= row.accuracyM)
                            row.copy(lat = f.lat, lng = f.lng, accuracyM = f.accuracyM)
                        else row
                    }
                }
            }

            CaptureRow(template.capture, attachments,
                       onCapture = { capturing = it },
                       onRemove = { vm.removeAttachment(it) })

            Spacer(Modifier.height(8.dp))
        }
    }

    capturing?.let { kind ->
        val finish: (org.humint.field.media.Capture.Captured?) -> Unit = { captured ->
            captured?.let { vm.attach(reportId, it, kind) }
            capturing = null
        }
        // A photo takes the whole screen; audio and video stay in a sheet.
        // The sheet was pushing the shutter off the bottom of the display,
        // and a camera wants a viewfinder anyway. Audio has nothing to look
        // at and video's control is one button, so both are fine where they
        // are.
        if (kind == "image") {
            PhotoCaptureScreen(onDone = finish, onCancel = { capturing = null })
        } else {
            CaptureSheet(kind = kind, onDone = finish, onCancel = { capturing = null })
        }
    }

    if (confirmDiscard) {
        AlertDialog(
            onDismissRequest = { confirmDiscard = false },
            title = { Text("Discard this report?") },
            text = {
                Text("It is deleted from this phone, along with anything attached to it. " +
                     "It has not been sent, so there is no other copy.")
            },
            confirmButton = {
                TextButton(onClick = { vm.discard(report); confirmDiscard = false; onDone() }) {
                    Text("Discard", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { confirmDiscard = false }) { Text("Keep it") }
            },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun FormField(field: TemplateField, value: Any?, onChange: (Any?) -> Unit) {
    val focus = remember { FocusRequester() }
    LaunchedEffect(Unit) { if (field.autofocus) runCatching { focus.requestFocus() } }

    when (field.type) {
        "select" -> {
            var open by remember { mutableStateOf(false) }
            ExposedDropdownMenuBox(expanded = open, onExpandedChange = { open = it }) {
                OutlinedTextField(
                    value = value?.toString().orEmpty(),
                    onValueChange = {},
                    readOnly = true,
                    label = { Text(field.label) },
                    trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded = open) },
                    modifier = Modifier.fillMaxWidth()
                        .menuAnchor(MenuAnchorType.PrimaryNotEditable),
                )
                ExposedDropdownMenu(expanded = open, onDismissRequest = { open = false }) {
                    field.options.forEach { option ->
                        DropdownMenuItem(
                            text = { Text(option, style = MaterialTheme.typography.bodyLarge) },
                            onClick = { onChange(option); open = false },
                        )
                    }
                }
            }
        }

        "bool" -> Row(
            Modifier.fillMaxWidth().heightIn(min = TapTarget),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Checkbox(checked = value == true, onCheckedChange = { onChange(it) })
            Column(Modifier.padding(start = 4.dp)) {
                Text(field.label, style = MaterialTheme.typography.bodyLarge)
                field.hint?.let {
                    Text(it, style = MaterialTheme.typography.labelMedium,
                         color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        }

        // One line per item while it is being typed. Splitting on every
        // keystroke is what used to eat the newline the moment you pressed
        // it; now the raw text is the field's own and only becomes a list
        // when the typing stops.
        "list" -> DraftTextField(
            value = (value as? List<*>)?.joinToString("\n").orEmpty(),
            onCommit = { text ->
                onChange(text.split("\n").map { it.trim() }.filter { it.isNotEmpty() })
            },
            label = field.label,
            supportingText = field.hint,
            modifier = Modifier.fillMaxWidth().focusRequester(focus),
            minLines = 2,
        )

        "textarea" -> DraftTextField(
            value = value?.toString().orEmpty(),
            onCommit = onChange,
            label = field.label,
            supportingText = field.hint,
            modifier = Modifier.fillMaxWidth().focusRequester(focus),
            minLines = 3,
            keyboardOptions = KeyboardOptions(
                capitalization = KeyboardCapitalization.Sentences,
                imeAction = ImeAction.Default,
            ),
        )

        else -> DraftTextField(
            value = value?.toString().orEmpty(),
            onCommit = onChange,
            transform = { text ->
                if (field.type == "number") text.filter { it.isDigit() } else text
            },
            label = field.label,
            supportingText = field.hint,
            suffix = field.suffix,
            singleLine = true,
            modifier = Modifier.fillMaxWidth().focusRequester(focus),
            keyboardOptions = KeyboardOptions(
                keyboardType = when (field.keyboard) {
                    "decimal" -> KeyboardType.Decimal
                    "number" -> KeyboardType.Number
                    else -> KeyboardType.Text
                },
                // A plate is read off the back of a car and typed as it is
                // written; sentence case would quietly turn "ABC 1234" into
                // "Abc 1234" on some keyboards.
                capitalization = if (field.capitalize) KeyboardCapitalization.Characters
                                 else KeyboardCapitalization.Words,
                imeAction = ImeAction.Next,
            ),
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CriticalityRow(current: String?, onPick: (String) -> Unit) {
    FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        Templates.criticality.forEach { level ->
            FilterChip(
                selected = current == level,
                onClick = { onPick(level) },
                label = { Text(level) },
            )
        }
    }
}

@Composable
private fun PositionRow(
    fix: org.humint.field.data.Position.Fix?,
    report: ReportRow,
    onNote: (String?) -> Unit,
) {
    Column {
        Text(
            when {
                fix == null && report.lat == null ->
                    "No position yet — GPS is still looking, or it is switched off."
                fix != null -> "%.5f, %.5f  ±%d m%s".format(
                    fix.lat, fix.lng, fix.accuracyM.toInt(),
                    if (fix.stale) "  (last fix, a while ago)" else "")
                else -> "%.5f, %.5f".format(report.lat, report.lng)
            },
            style = MonoStyle.merge(MaterialTheme.typography.bodyMedium),
            color = if (fix?.stale == true) FieldAmber
                    else MaterialTheme.colorScheme.onSurfaceVariant,
        )
        DraftTextField(
            value = report.locationNote.orEmpty(),
            onCommit = { onNote(it.ifBlank { null }) },
            label = "Where you were, in words",
            supportingText = "\"kerbside opposite the gate\" — the coordinates cannot say that",
            singleLine = true,
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun CaptureRow(
    capture: List<String>,
    attachments: List<AttachmentRow>,
    onCapture: (String) -> Unit,
    onRemove: (AttachmentRow) -> Unit,
) {
    var full by remember { mutableStateOf<AttachmentRow?>(null) }
    full?.let { FullImage(it, onClose = { full = null }) }

    Column {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (capture.contains("photo")) {
                OutlinedButton(onClick = { onCapture("image") },
                               modifier = Modifier.heightIn(min = TapTarget)) { Text("Photo") }
            }
            if (capture.contains("video")) {
                OutlinedButton(onClick = { onCapture("video") },
                               modifier = Modifier.heightIn(min = TapTarget)) { Text("Video") }
            }
            if (capture.contains("audio")) {
                OutlinedButton(onClick = { onCapture("audio") },
                               modifier = Modifier.heightIn(min = TapTarget)) { Text("Record") }
            }
        }
        if (attachments.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            attachments.forEach { a ->
                Row(Modifier.fillMaxWidth().heightIn(min = 56.dp),
                    verticalAlignment = Alignment.CenterVertically) {
                    // A photo you cannot see is a photo you cannot check.
                    // Before this, the first chance to find out you had
                    // photographed your own thumb was after the upload, back
                    // in range, hours later and nowhere near the subject.
                    if (a.kind == "image") {
                        AttachmentThumb(a, onOpen = { full = a })
                        Spacer(Modifier.width(10.dp))
                    }
                    Column(Modifier.weight(1f)) {
                        Text(
                            a.filename,
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        Text(
                            buildString {
                                a.durationMs?.let { append("${it / 1000}s · ") }
                                append("${a.sizeBytes / 1024} KB")
                                if (a.kind == "image") append(" · tap to check it")
                            },
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    TextButton(onClick = { onRemove(a) }) {
                        Text("Remove", color = MaterialTheme.colorScheme.error)
                    }
                }
            }
        }
    }
}
