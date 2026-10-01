package org.humint.field.ui

import androidx.biometric.BiometricManager
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.humint.field.data.Settings
import org.humint.field.data.ThemeChoice
import org.humint.field.data.Vault
import java.util.concurrent.Executor

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val activity = context as? FragmentActivity
    val theme by Settings.theme.collectAsStateWithLifecycle()
    var changingPin by remember { mutableStateOf(false) }
    var bioOn by remember { mutableStateOf(Vault.biometricEnabled(context)) }
    var notice by remember { mutableStateOf<String?>(null) }

    val bioAvailable = remember {
        BiometricManager.from(context)
            .canAuthenticate(BiometricManager.Authenticators.BIOMETRIC_STRONG) ==
            BiometricManager.BIOMETRIC_SUCCESS
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = { TextButton(onClick = onBack) { Text("Back") } },
            )
        },
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp),
        ) {
            Section("Appearance")
            ThemeChoice.entries.forEach { choice ->
                Row(
                    Modifier.fillMaxWidth().heightIn(min = TapTarget)
                        .clickable { Settings.setTheme(context, choice) },
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    RadioButton(selected = theme == choice,
                                onClick = { Settings.setTheme(context, choice) })
                    Column(Modifier.padding(start = 6.dp)) {
                        Text(choice.label, style = MaterialTheme.typography.bodyLarge)
                        Text(choice.blurb, style = MaterialTheme.typography.labelMedium,
                             color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }

            Spacer(Modifier.height(18.dp))
            HorizontalDivider()
            Section("Getting in")

            Row(
                Modifier.fillMaxWidth().heightIn(min = TapTarget),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text("Fingerprint shortcut", style = MaterialTheme.typography.bodyLarge)
                    Text(
                        if (!bioAvailable)
                            "No fingerprint is set up on this phone."
                        else
                            "Opens the same lock the PIN does. The PIN still works, and " +
                            "is asked for again if a new fingerprint is ever enrolled.",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Switch(
                    checked = bioOn,
                    enabled = bioAvailable && activity != null,
                    onCheckedChange = { want ->
                        if (!want) {
                            Vault.disableBiometric(context)
                            bioOn = false
                        } else if (activity != null) {
                            enrollBiometric(activity) { ok ->
                                bioOn = ok
                                if (!ok) notice = "That did not take. The PIN still works."
                            }
                        }
                    },
                )
            }

            Spacer(Modifier.height(10.dp))
            Button(
                onClick = { changingPin = true },
                modifier = Modifier.fillMaxWidth().heightIn(min = TapTarget),
            ) { Text("Change the PIN") }

            Spacer(Modifier.height(10.dp))
            Text(
                "The PIN is what the reports on this phone are encrypted with — it is " +
                "not a screen lock over them. Changing it re-locks the same reports " +
                "with the new one; nothing is re-encrypted or lost.",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            notice?.let {
                Spacer(Modifier.height(12.dp))
                Text(it, color = FieldAmber, style = MaterialTheme.typography.bodyMedium)
            }
            Spacer(Modifier.height(30.dp))
        }
    }

    if (changingPin) {
        ChangePinDialog(
            onDismiss = { changingPin = false },
            onDone = { changingPin = false; notice = "PIN changed." },
        )
    }
}

@Composable
private fun Section(title: String) {
    Spacer(Modifier.height(16.dp))
    Text(title, style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.height(4.dp))
}

@Composable
private fun ChangePinDialog(onDismiss: () -> Unit, onDone: () -> Unit) {
    val context = LocalContext.current
    var old by remember { mutableStateOf("") }
    var new1 by remember { mutableStateOf("") }
    var new2 by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Change the PIN") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Pin(old, { old = it; error = null }, "Current PIN")
                Pin(new1, { new1 = it; error = null }, "New PIN")
                Pin(new2, { new2 = it; error = null }, "New PIN again")
                error?.let {
                    Text(it, color = MaterialTheme.colorScheme.error,
                         style = MaterialTheme.typography.bodyMedium)
                }
            }
        },
        confirmButton = {
            TextButton(onClick = {
                error = when {
                    new1.length < Vault.MIN_PIN_LENGTH ->
                        "At least ${Vault.MIN_PIN_LENGTH} digits."
                    new1 != new2 -> "The new ones do not match."
                    new1.toSet().size == 1 -> "All the same digit is not a PIN."
                    !Vault.changePin(context, old.toCharArray(), new1.toCharArray()) ->
                        "The current PIN is wrong."
                    else -> null
                }
                if (error == null) onDone()
            }) { Text("Change it") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun Pin(value: String, onChange: (String) -> Unit, label: String) {
    OutlinedTextField(
        value = value,
        onValueChange = { t -> onChange(t.filter { it.isDigit() }.take(Vault.MAX_PIN_LENGTH)) },
        label = { Text(label) },
        singleLine = true,
        visualTransformation = PasswordVisualTransformation(),
        keyboardOptions = KeyboardOptions(
            keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Next),
        modifier = Modifier.fillMaxWidth(),
    )
}

/**
 * Turning the shortcut on wraps the *already unlocked* data key under a
 * keystore key that needs a fingerprint. So it can only be done from inside
 * an unlocked app, which is the right constraint: you cannot add a way in
 * without already being in.
 */
private fun enrollBiometric(activity: FragmentActivity, onResult: (Boolean) -> Unit) {
    val context = activity.applicationContext
    val cipher = Vault.biometricEnrollCipher()
    if (cipher == null) { onResult(false); return }
    val executor = Executor { it.run() }
    BiometricPrompt(activity, executor, object : BiometricPrompt.AuthenticationCallback() {
        override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
            val c = result.cryptoObject?.cipher
            onResult(c != null && Vault.finishBiometricEnroll(context, c))
        }
        override fun onAuthenticationError(code: Int, message: CharSequence) = onResult(false)
    }).authenticate(
        BiometricPrompt.PromptInfo.Builder()
            .setTitle("Turn on the fingerprint shortcut")
            .setSubtitle("Confirm it is you")
            .setNegativeButtonText("Cancel")
            .build(),
        BiometricPrompt.CryptoObject(cipher),
    )
}
