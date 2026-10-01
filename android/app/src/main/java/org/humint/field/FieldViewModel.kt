package org.humint.field

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import org.humint.field.data.AttachmentRow
import org.humint.field.data.Crypto
import org.humint.field.data.FieldDao
import org.humint.field.data.FieldDatabase
import org.humint.field.data.Position
import org.humint.field.data.ReportRow
import org.humint.field.data.Templates
import org.humint.field.data.Vault
import org.humint.field.media.Capture
import org.humint.field.net.SessionHolder
import org.humint.field.net.UploadSession
import org.humint.field.net.Uploader
import org.json.JSONObject
import java.io.File

class FieldViewModel(app: Application) : AndroidViewModel(app) {

    /*
     * The database is fetched per call, never cached.
     *
     * Locking the vault closes the database — that is the point of it — so a
     * handle held in a field here would be a closed one the next time the
     * analyst unlocked, and every read would throw. Fetching it each time
     * costs a map lookup and removes a whole class of bug.
     */
    private fun dao(): FieldDao = FieldDatabase.get(getApplication()).dao()

    private val uploader = Uploader(app) { dao() }

    val position = Position(app)

    /** Re-attached when the vault opens, emptied while it is shut. Without
     *  the flatMapLatest these would stay subscribed to a closed database. */
    @OptIn(ExperimentalCoroutinesApi::class)
    val queue: StateFlow<List<ReportRow>> = Vault.state
        .flatMapLatest { st ->
            if (st is Vault.State.Open) dao().queue() else flowOf(emptyList())
        }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())

    @OptIn(ExperimentalCoroutinesApi::class)
    val readyCount: StateFlow<Int> = Vault.state
        .flatMapLatest { st ->
            if (st is Vault.State.Open) dao().readyCount() else flowOf(0)
        }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    val session: StateFlow<UploadSession?> = SessionHolder.current

    private val _upload = MutableStateFlow<Uploader.Progress?>(null)
    val upload: StateFlow<Uploader.Progress?> = _upload

    private val _notice = MutableStateFlow<String?>(null)
    val notice: StateFlow<String?> = _notice

    fun clearNotice() { _notice.value = null }

    // ---------------------------------------------------------------- drafts

    /** Start a report and return its id, so the caller can navigate to it.
     *  Written to the database immediately: a report that exists only in
     *  memory is one Android is entitled to throw away while the phone is in
     *  a pocket, and it would. */
    suspend fun newReport(templateKey: String): String {
        val template = Templates.byKey(templateKey) ?: error("No template $templateKey")
        val defaults = JSONObject()
        template.fields.forEach { f -> f.default?.let { defaults.put(f.key, it) } }
        val row = ReportRow(
            template = templateKey,
            title = template.label,
            fields = defaults.toString(),
            criticality = "Routine",
            body = null,
            observedAt = System.currentTimeMillis(),
            lat = null, lng = null, accuracyM = null, locationNote = null,
        )
        dao().insert(row)
        return row.id
    }

    fun reportFlow(id: String) = dao().reportFlow(id)
    fun attachmentsFlow(id: String) = dao().attachmentsFlow(id)

    fun save(row: ReportRow) = viewModelScope.launch {
        val template = Templates.byKey(row.template)
        val values = row.fields.toMap()
        dao().update(row.copy(
            // The title always follows the fields. An analyst who renames a
            // vehicle's plate should not be left with a queue entry still
            // showing the old one.
            title = template?.composeTitle(values) ?: row.title,
            updatedAt = System.currentTimeMillis(),
        ))
    }

    /** Mark a report finished and ready to go out with the next upload. */
    fun markReady(row: ReportRow, onRefused: (String) -> Unit) = viewModelScope.launch {
        val values = row.fields.toMap()
        // A report whose template this build has never heard of can still be
        // sent — it came from somewhere, and refusing to send it would be the
        // app doing what the console is careful never to do.
        val template = Templates.byKey(row.template)
        if (template != null) {
            val missing = template.whatIsMissing(values)
            if (missing != null) { onRefused("Still needs $missing."); return@launch }
        }
        dao().update(row.copy(status = "ready", lastError = null,
                            title = template?.composeTitle(values) ?: row.title,
                            updatedAt = System.currentTimeMillis()))
    }

    fun reopen(row: ReportRow) = viewModelScope.launch {
        dao().update(row.copy(status = "draft", updatedAt = System.currentTimeMillis()))
    }

    /** Delete a report and shred everything attached to it. */
    fun discard(row: ReportRow) = viewModelScope.launch { erase(row) }

    private suspend fun erase(row: ReportRow) {
        // Files first: once the rows are gone there is nothing left saying
        // which encrypted blobs in the sandbox belonged to this report.
        dao().attachments(row.id).forEach { Crypto.shred(File(it.path)) }
        dao().deleteAttachmentsFor(row.id)
        dao().deleteReport(row.id)
    }

    // ------------------------------------------------------------- captures

    fun attach(reportId: String, captured: Capture.Captured, kind: String) =
        viewModelScope.launch {
            dao().insert(AttachmentRow(
                reportId = reportId,
                kind = kind,
                filename = captured.filename,
                mimeType = captured.mimeType,
                path = captured.file.absolutePath,
                sizeBytes = captured.sizeBytes,
                durationMs = captured.durationMs,
            ))
        }

    fun removeAttachment(row: AttachmentRow) = viewModelScope.launch {
        Crypto.shred(File(row.path))
        dao().deleteAttachment(row.id)
    }

    // -------------------------------------------------------------- sending

    fun onScanned(payload: String, onResult: (Boolean) -> Unit) {
        UploadSession.fromQr(payload)
            .onSuccess { SessionHolder.set(it); onResult(true) }
            .onFailure { _notice.value = it.message; onResult(false) }
    }

    /**
     * Send the queue, then forget the credentials.
     *
     * The `finally` is the important line in this method. Whatever happens —
     * success, one report refused, the network dropping halfway, an
     * exception nobody predicted — the address and token do not survive the
     * call. Leaving them live "just in case the analyst wants to retry" is
     * precisely the convenience this app is built to refuse.
     */
    fun send() = viewModelScope.launch {
        val session = SessionHolder.live()
        if (session == null) {
            _notice.value = "Scan the console's code first — nothing about it is kept on here."
            return@launch
        }
        _upload.value = Uploader.Progress.Checking(session.hostOnly())
        try {
            val hello = uploader.hello(session).getOrElse {
                _upload.value = Uploader.Progress.Failed(
                    it.message ?: "Could not reach the console.")
                return@launch
            }
            if (hello.consoleIsNewer()) {
                _notice.value = "The console has newer report forms than this app. " +
                    "What you send still arrives in full; ask for an updated app when convenient."
            }
            val outcome = uploader.sendAll(session) { _upload.value = it }
            _upload.value = outcome
            if (outcome is Uploader.Progress.Done && outcome.sent > 0) purgeSent()
        } finally {
            SessionHolder.clear()
        }
    }

    /**
     * Everything the console has is deleted from the phone, media and all.
     *
     * This is the half of "wiped on upload" that actually does the work, and
     * it runs on the strength of the console's own 201 — not on a guess. A
     * report the console never acknowledged keeps its place in the queue.
     */
    private suspend fun purgeSent() {
        // Only rows the console acknowledged with a 201 are marked 'sent'.
        // Anything still 'ready' failed and keeps its place in the queue
        // with the reason attached, which is the whole point of not simply
        // clearing everything after an upload run.
        dao().sentReports().forEach { erase(it) }
    }

    fun dismissUpload() { _upload.value = null }
}

/** The stored field bag, as a plain map. org.json rather than a serialization
 *  library: the object is flat, and this is the only place it is read. */
fun String.toMap(): Map<String, Any?> {
    val json = runCatching { JSONObject(this) }.getOrElse { return emptyMap() }
    return json.keys().asSequence().associateWith { key ->
        when (val v = json.get(key)) {
            is org.json.JSONArray -> (0 until v.length()).map { v.getString(it) }
            JSONObject.NULL -> null
            else -> v
        }
    }
}

fun Map<String, Any?>.toJson(): String {
    val json = JSONObject()
    forEach { (k, v) ->
        when (v) {
            null -> Unit
            is List<*> -> json.put(k, org.json.JSONArray(v))
            else -> json.put(k, v)
        }
    }
    return json.toString()
}
