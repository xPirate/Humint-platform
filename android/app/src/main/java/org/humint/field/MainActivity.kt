package org.humint.field

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.lifecycle.DefaultLifecycleObserver
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.ProcessLifecycleOwner
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import androidx.compose.runtime.getValue
import androidx.compose.runtime.LaunchedEffect
import androidx.fragment.app.FragmentActivity
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import org.humint.field.data.Settings
import org.humint.field.data.Vault
import org.humint.field.ui.LockScreen
import org.humint.field.ui.SettingsScreen
import org.humint.field.net.SessionHolder
import org.humint.field.ui.FieldTheme
import org.humint.field.ui.QueueScreen
import org.humint.field.ui.ReportScreen
import org.humint.field.ui.ScanScreen
import org.humint.field.ui.UploadScreen

// FragmentActivity rather than ComponentActivity: BiometricPrompt needs a
// fragment host, and there is no Compose-only way around that.
class MainActivity : FragmentActivity() {

    private val vm: FieldViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        /*
         * FLAG_SECURE does two things that matter on a handset like this: it
         * keeps the app out of the recents thumbnail, so a half-written
         * report about a person is not sitting in the task switcher for
         * whoever picks the phone up, and it blocks screenshots and screen
         * recording, including by anything else on the device.
         *
         * The cost is that the analyst cannot screenshot their own report.
         * That is the right trade here — the report is going to the console
         * anyway, and a screenshot of it would land in the gallery, outside
         * everything this app encrypts.
         */
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE,
                        WindowManager.LayoutParams.FLAG_SECURE)

        /*
         * The credentials die when the app leaves the foreground.
         *
         * Not when the activity is destroyed — that is too late and too
         * unreliable. ProcessLifecycleOwner fires onStop when the app goes
         * to the background at all: home button, task switcher, screen off,
         * a call coming in. Every one of those is a moment when the phone
         * might leave the analyst's hand, and the console's address should
         * not still be in memory when it does.
         */
        ProcessLifecycleOwner.get().lifecycle.addObserver(object : DefaultLifecycleObserver {
            override fun onStop(owner: LifecycleOwner) {
                SessionHolder.clear()
                // Starts the clock. The vault does not shut the instant the
                // screen turns off — an analyst who glances at a message and
                // comes back should not have to type a PIN — but a phone left
                // in a pocket is locked when it comes out again.
                Vault.onBackgrounded()
            }

            override fun onStart(owner: LifecycleOwner) {
                Vault.onForegrounded(applicationContext)
            }
        })

        Settings.load(this)
        Vault.refreshState(this)

        setContent {
            val theme by Settings.theme.collectAsStateWithLifecycle()
            val vault by Vault.state.collectAsStateWithLifecycle()
            FieldTheme(theme) {
                // Nothing behind the lock is composed at all while it is shut:
                // the queue screen would otherwise try to open the database,
                // which has no key until the PIN is in.
                if (vault !is Vault.State.Open) {
                    LockScreen(vault) { Vault.refreshState(applicationContext) }
                    return@FieldTheme
                }
                val nav = rememberNavController()
                NavHost(navController = nav, startDestination = "queue") {
                    composable("queue") {
                        QueueScreen(
                            vm = vm,
                            onOpen = { id -> nav.navigate("report/$id") },
                            onUpload = { nav.navigate("scan") },
                            onSettings = { nav.navigate("settings") },
                        )
                    }
                    composable("report/{id}") { entry ->
                        ReportScreen(
                            vm = vm,
                            reportId = entry.arguments?.getString("id").orEmpty(),
                            onDone = { nav.popBackStack() },
                        )
                    }
                    composable("scan") {
                        ScanScreen(
                            vm = vm,
                            onScanned = {
                                nav.popBackStack()
                                nav.navigate("upload")
                            },
                            onCancel = { nav.popBackStack() },
                        )
                    }
                    composable("upload") {
                        UploadScreen(vm = vm, onDone = {
                            nav.popBackStack("queue", inclusive = false)
                        })
                    }
                    composable("settings") {
                        SettingsScreen(onBack = { nav.popBackStack() })
                    }
                }
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        SessionHolder.clear()
        if (isFinishing) Vault.lock()
    }
}
