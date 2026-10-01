package org.humint.field.ui

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import androidx.compose.runtime.rememberCoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.humint.field.data.Vault
import java.util.concurrent.Executor

/**
 * The way in.
 *
 * Three states, one screen: no PIN set yet, locked, or throttled after too
 * many wrong tries. The wording matters more than usual here — somebody
 * getting this wrong is often cold, in the dark, and about to give up on the
 * app entirely.
 */
@Composable
fun LockScreen(state: Vault.State, onUnlocked: () -> Unit) {
    when (state) {
        is Vault.State.NeedsSetup -> SetPinPane(onUnlocked)
        is Vault.State.Locked -> UnlockPane(state, onUnlocked)
        Vault.State.Unreadable -> UnreadablePane()
        Vault.State.Open -> Unit
    }
}

@Composable
private fun SetPinPane(onDone: () -> Unit) {
    val context = LocalContext.current
    var first by remember { mutableStateOf("") }
    var second by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    Pane("Set a PIN") {
        Text(
            "This PIN encrypts the reports on this phone. It is not a screen " +
            "lock — without it the reports cannot be read at all, by anyone, " +
            "including whoever set this up.",
            style = MaterialTheme.typography.bodyLarge,
        )
        Spacer(Modifier.height(6.dp))
        Text(
            "There is no way to recover it. Reports you have already uploaded " +
            "are safe on the console; anything still waiting here would be lost.",
            style = MaterialTheme.typography.bodyMedium,
            color = FieldAmber,
        )
        Spacer(Modifier.height(18.dp))
        PinField(first, { first = it; error = null }, "PIN", autoFocus = true)
        Spacer(Modifier.height(10.dp))
        PinField(second, { second = it; error = null }, "Again")
        error?.let {
            Spacer(Modifier.height(10.dp))
            Text(it, color = MaterialTheme.colorScheme.error,
                 style = MaterialTheme.typography.bodyMedium)
        }
        Spacer(Modifier.height(18.dp))
        Button(
            onClick = {
                error = when {
                    first.length < Vault.MIN_PIN_LENGTH ->
                        "At least ${Vault.MIN_PIN_LENGTH} digits. Longer is meaningfully better here."
                    first != second -> "Those do not match."
                    first.toSet().size == 1 -> "All the same digit is not a PIN."
                    else -> null
                }
                if (error == null) {
                    busy = true
                    scope.launch {
                        withContext(Dispatchers.Default) {
                            Vault.setUp(context, first.toCharArray())
                        }
                        busy = false
                        onDone()
                    }
                }
            },
            enabled = !busy,
            modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
        ) { Text(if (busy) "Working…" else "Set it") }
    }
}

@Composable
private fun UnlockPane(state: Vault.State.Locked, onUnlocked: () -> Unit) {
    val context = LocalContext.current
    val activity = context as? FragmentActivity
    var pin by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var waitLeft by remember { mutableStateOf(0L) }
    var confirmWipe by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    LaunchedEffect(state) {
        while (true) {
            waitLeft = Vault.remainingWaitMs(context)
            if (waitLeft <= 0) break
            delay(500)
        }
    }

    // The fingerprint prompt comes up on its own, because an analyst who
    // turned it on wants one gesture, not a tap then a gesture.
    LaunchedEffect(Unit) {
        if (activity != null && Vault.biometricEnabled(context) &&
            Vault.remainingWaitMs(context) <= 0) {
            promptBiometric(activity) { ok -> if (ok) onUnlocked() }
        }
    }

    Pane("Locked") {
        if (waitLeft > 0) {
            Text(
                "Too many wrong tries. Try again in ${formatWait(waitLeft)}.",
                style = MaterialTheme.typography.titleMedium,
                color = FieldAmber,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                "Nothing has been deleted and nothing will be. The wait just gets " +
                "longer each time.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            PinField(pin, { pin = it; error = null }, "PIN", autoFocus = true)
            error?.let {
                Spacer(Modifier.height(10.dp))
                Text(it, color = MaterialTheme.colorScheme.error,
                     style = MaterialTheme.typography.bodyMedium)
            }
            Spacer(Modifier.height(16.dp))
            Button(
                onClick = {
                    busy = true
                    scope.launch {
                        // Deriving the key is deliberately slow. Doing it on
                        // the main thread froze the button for a second and
                        // made the app look broken.
                        val ok = withContext(Dispatchers.Default) {
                            Vault.unlock(context, pin.toCharArray())
                        }
                        pin = ""
                        busy = false
                        if (ok) onUnlocked()
                        else {
                            error = "That is not the PIN."
                            waitLeft = Vault.remainingWaitMs(context)
                        }
                    }
                },
                enabled = !busy && pin.length >= Vault.MIN_PIN_LENGTH,
                modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
            ) { Text(if (busy) "Checking…" else "Unlock") }

            if (activity != null && Vault.biometricEnabled(context)) {
                Spacer(Modifier.height(8.dp))
                OutlinedButton(
                    onClick = { promptBiometric(activity) { ok -> if (ok) onUnlocked() } },
                    modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
                ) { Text("Use fingerprint") }
            }
        }

        Spacer(Modifier.height(26.dp))
        TextButton(onClick = { confirmWipe = true }) {
            Text("Forgotten the PIN?", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }

    if (confirmWipe) {
        AlertDialog(
            onDismissRequest = { confirmWipe = false },
            title = { Text("There is no way back in") },
            text = {
                Text(
                    "The PIN is what the reports are encrypted with, so a forgotten " +
                    "PIN cannot be reset — not by you, not by an admin, not by anyone.\n\n" +
                    "The only thing left to do is erase this phone's queue and start " +
                    "again. Anything already uploaded is safe on the console. Anything " +
                    "still waiting here will be gone."
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    Vault.destroyEverything(context)
                    confirmWipe = false
                }) { Text("Erase and start again", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { confirmWipe = false }) { Text("Keep trying") }
            },
        )
    }
}

/**
 * The store on this phone cannot be read with this key.
 *
 * Realistically: an APK that keyed the database differently was installed
 * here before. There is no way to recover those reports — the key they were
 * written under is gone — so the only honest offer is to clear it out.
 */
@Composable
private fun UnreadablePane() {
    val context = LocalContext.current
    var confirm by remember { mutableStateOf(false) }
    Pane("This phone's queue cannot be opened") {
        Text(
            "The PIN was accepted, but the stored reports were written with a " +
            "different key — almost always because an earlier version of the app " +
            "was installed here.",
            style = MaterialTheme.typography.bodyLarge,
        )
        Spacer(Modifier.height(10.dp))
        Text(
            "They cannot be recovered; the key they were encrypted with no longer " +
            "exists. Clearing them out is the only way forward. Anything that had " +
            "already been uploaded is safe on the console.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(20.dp))
        Button(
            onClick = { confirm = true },
            modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
        ) { Text("Clear and start again") }
    }
    if (confirm) {
        AlertDialog(
            onDismissRequest = { confirm = false },
            title = { Text("Clear this phone's queue?") },
            text = { Text("Unsent reports on this handset will be gone for good.") },
            confirmButton = {
                TextButton(onClick = { Vault.destroyEverything(context); confirm = false }) {
                    Text("Clear it", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = { TextButton(onClick = { confirm = false }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun Pane(title: String, content: @Composable () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(28.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.Start,
    ) {
        Text(title, style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(14.dp))
        content()
    }
}

@Composable
private fun PinField(
    value: String,
    onChange: (String) -> Unit,
    label: String,
    autoFocus: Boolean = false,
) {
    val focus = remember { FocusRequester() }
    LaunchedEffect(Unit) { if (autoFocus) runCatching { focus.requestFocus() } }
    OutlinedTextField(
        value = value,
        onValueChange = { text ->
            onChange(text.filter { it.isDigit() }.take(Vault.MAX_PIN_LENGTH))
        },
        label = { Text(label) },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(
            keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Done),
        textStyle = MonoStyle.merge(MaterialTheme.typography.titleLarge),
        modifier = Modifier.fillMaxWidth().focusRequester(focus),
    )
}

private fun formatWait(ms: Long): String {
    val s = (ms / 1000).toInt() + 1
    return when {
        s < 60 -> "$s seconds"
        s < 3600 -> "${s / 60} minutes"
        else -> "${s / 3600} hours"
    }
}

/**
 * The fingerprint shortcut.
 *
 * It unwraps the same data key the PIN does, through a keystore key that
 * requires authentication — so this is a second door to the same room, not a
 * bypass. A newly enrolled fingerprint invalidates that key and drops the
 * analyst back to the PIN, which is the behaviour you want on a handset
 * somebody else has had their hands on.
 */
fun promptBiometric(activity: FragmentActivity, onResult: (Boolean) -> Unit) {
    val context = activity.applicationContext
    val cipher = Vault.biometricUnlockCipher(context)
    if (cipher == null) { onResult(false); return }
    if (BiometricManager.from(context)
            .canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG)
        != BiometricManager.BIOMETRIC_SUCCESS
    ) { onResult(false); return }

    val executor = Executor { it.run() }
    val prompt = BiometricPrompt(activity, executor,
        object : BiometricPrompt.AuthenticationCallback() {
            override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                val c = result.cryptoObject?.cipher
                onResult(c != null && Vault.finishBiometricUnlock(context, c))
            }
            override fun onAuthenticationError(code: Int, message: CharSequence) {
                onResult(false)
            }
        })
    prompt.authenticate(
        BiometricPrompt.PromptInfo.Builder()
            .setTitle("Unlock the field queue")
            .setSubtitle("Or use the PIN")
            .setNegativeButtonText("Use PIN")
            .build(),
        BiometricPrompt.CryptoObject(cipher),
    )
}
