package org.humint.field.ui

import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsFocusedAsState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.OutlinedTextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.input.TextFieldValue
import kotlinx.coroutines.delay

/**
 * A text field that does not fight the person typing into it.
 *
 * ## The bug this exists to kill
 *
 * Every field on the report screen used to be written like this:
 *
 *     OutlinedTextField(
 *         value = report.body ?: "",
 *         onValueChange = { vm.save(report.copy(body = it)) },
 *     )
 *
 * where `report` comes from a Room `Flow`. So each keystroke started a
 * coroutine, wrote to an encrypted database, and waited for the row to come
 * back out of the flow before the field could show it. Type faster than that
 * round trip — which is to say, type at all — and the value arriving from
 * the database is one or two characters behind the keyboard. Compose then
 * replaces the field's contents with that stale string and puts the cursor
 * wherever the new length happens to land.
 *
 * On a handset that reads as: characters vanishing, and the caret jumping
 * into the middle of what you already wrote. Which is exactly what it was
 * reported as.
 *
 * ## The rule
 *
 * While a field has focus, the person holding the phone owns its contents.
 * Nothing else may write to it. The field pushes changes *outward* on a
 * short debounce, and only adopts a value from outside when it is not
 * focused — a report loaded from the queue, say, or a field cleared by a
 * template reset.
 *
 * Holding [TextFieldValue] rather than a `String` is the other half: it
 * carries the selection, so a recomposition for an unrelated reason (the GPS
 * fix improving, an attachment finishing) cannot move the caret either.
 *
 * Nothing about crash-safety is given up. The debounce is well under the
 * time it takes to put a phone in a pocket, every field still commits on
 * losing focus, and [DraftTextField] flushes when it leaves the composition.
 */
@Composable
fun DraftTextField(
    value: String,
    onCommit: (String) -> Unit,
    modifier: Modifier = Modifier,
    label: String? = null,
    supportingText: String? = null,
    suffix: String? = null,
    singleLine: Boolean = false,
    minLines: Int = 1,
    enabled: Boolean = true,
    textStyle: TextStyle? = null,
    keyboardOptions: KeyboardOptions = KeyboardOptions.Default,
    /** Applied to what the person typed before it goes anywhere — digits
     *  only for a number field, and so on. Must be idempotent. */
    transform: (String) -> String = { it },
    debounceMs: Long = 350,
) {
    val interaction = remember { MutableInteractionSource() }
    val focused by interaction.collectIsFocusedAsState()

    var field by remember { mutableStateOf(TextFieldValue(value)) }
    // What we last told the caller. Used to tell "the value changed because
    // of us" from "the value changed because something else wrote to it".
    var pushed by remember { mutableStateOf(value) }

    // Adopt an outside change only when the field is not being typed into.
    // While it is focused this block is deliberately inert — that is the
    // entire fix.
    LaunchedEffect(value, focused) {
        if (!focused && value != field.text && value != pushed) {
            field = TextFieldValue(value, androidx.compose.ui.text.TextRange(value.length))
            pushed = value
        }
    }

    // Commit shortly after typing stops, rather than on every keystroke: the
    // database here is SQLCipher, and a write per character is both the
    // latency that caused the bug and a waste of the battery that has to
    // last the day.
    LaunchedEffect(field.text) {
        val text = field.text
        if (text == pushed) return@LaunchedEffect
        delay(debounceMs)
        pushed = text
        onCommit(text)
    }

    // Leaving the field, or the screen, commits immediately — nothing waits
    // on a timer that a navigation could outrun.
    LaunchedEffect(focused) {
        if (!focused && field.text != pushed) {
            pushed = field.text
            onCommit(field.text)
        }
    }
    androidx.compose.runtime.DisposableEffect(Unit) {
        onDispose { if (field.text != pushed) onCommit(field.text) }
    }

    OutlinedTextField(
        value = field,
        onValueChange = { next ->
            val cleaned = transform(next.text)
            field = if (cleaned == next.text) next else {
                // The transform rejected something (a letter in a number
                // field). Keep the caret where the accepted text ends rather
                // than throwing it to position zero.
                val caret = minOf(next.selection.end, cleaned.length)
                next.copy(text = cleaned,
                          selection = androidx.compose.ui.text.TextRange(caret))
            }
        },
        label = label?.let { { androidx.compose.material3.Text(it) } },
        supportingText = supportingText?.let { { androidx.compose.material3.Text(it) } },
        suffix = suffix?.let { { androidx.compose.material3.Text(it) } },
        singleLine = singleLine,
        minLines = minLines,
        enabled = enabled,
        textStyle = textStyle ?: androidx.compose.material3.LocalTextStyle.current,
        keyboardOptions = keyboardOptions,
        interactionSource = interaction,
        modifier = modifier,
    )
}
