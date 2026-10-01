package org.humint.field.net

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.humint.field.data.AttachmentRow
import org.humint.field.data.Crypto
import org.humint.field.data.FieldDao
import org.humint.field.data.ReportRow
import org.humint.field.data.Templates
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Sending the queue.
 *
 * Three properties matter more than speed here:
 *
 *  1. **A retry must not duplicate.** Every report and every file carries a
 *     client_ref generated when it was created, not when it was sent. The
 *     console keys on it, so an upload cut off after the server committed
 *     and before the app heard back resends the same ref and lands as the
 *     same row. This is why [ReportRow.clientRef] is assigned at draft time.
 *
 *  2. **Text goes before media.** A report's words are a few hundred bytes
 *     and its video is forty megabytes. Sending the text first means a
 *     marginal uplink delivers the thing that matters and keeps trying on
 *     the rest, rather than delivering nothing until the clip is through.
 *
 *  3. **Partial progress is kept.** Each file is marked sent as it lands, so
 *     resuming after a drop-out picks up at the next one instead of pushing
 *     the whole set again.
 *
 * Nothing here writes the address or the token anywhere. The session is
 * passed in, used, and forgotten by the caller.
 */
class Uploader(
    private val context: Context,
    /** Fetched per call rather than held: locking the vault closes the
     *  database, so a cached handle would be a closed one after the next
     *  unlock. */
    private val dao: () -> FieldDao,
) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        // Generous: a 40 MB clip over a field uplink is not a fast write, and
        // timing it out halfway is how you get a queue that never drains.
        .writeTimeout(10, TimeUnit.MINUTES)
        .retryOnConnectionFailure(true)
        .build()

    sealed interface Progress {
        data class Checking(val host: String) : Progress
        data class Sending(val index: Int, val total: Int, val title: String) : Progress
        data class SendingFile(val index: Int, val total: Int, val filename: String) : Progress
        data class Done(val sent: Int, val failed: Int) : Progress
        data class Failed(val message: String) : Progress
    }

    /** Ask the console whether the token still works, and what it will take.
     *  Doing this before sending anything turns "revoked three weeks ago"
     *  into one clear message instead of a failure per report. */
    suspend fun hello(session: UploadSession): Result<Hello> = withContext(Dispatchers.IO) {
        runCatching {
            val request = Request.Builder()
                .url("${session.baseUrl}/api/intake/hello")
                .header("Authorization", "Bearer ${session.token}")
                .get().build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (response.code == 401) throw IOException(
                    "The console did not accept that code. It may have been revoked, " +
                    "or the account it belongs to switched off."
                )
                if (!response.isSuccessful) throw IOException(serverMessage(response.code, text))
                val json = JSONObject(text)
                val templates = json.optJSONObject("templates")
                Hello(
                    deviceLabel = json.optString("device").ifBlank { null },
                    username = json.optString("user").ifBlank { null },
                    maxFileBytes = json.optLong("max_file_bytes", Long.MAX_VALUE),
                    maxSubmissionBytes = json.optLong("max_submission_bytes", Long.MAX_VALUE),
                    maxFiles = json.optInt("max_files", 10),
                    consoleTemplateVersion = templates?.optInt("version") ?: 0,
                )
            }
        }
    }

    data class Hello(
        val deviceLabel: String?,
        val username: String?,
        val maxFileBytes: Long,
        val maxSubmissionBytes: Long,
        val maxFiles: Int,
        val consoleTemplateVersion: Int,
    ) {
        /** The console has forms this app has never heard of. Worth saying
         *  once, not worth blocking on: the console renders what it does not
         *  recognise rather than dropping it. */
        fun consoleIsNewer(): Boolean =
            consoleTemplateVersion > 0 && consoleTemplateVersion > Templates.version
    }

    /**
     * Send everything marked ready. Returns how many made it.
     *
     * One report failing does not stop the rest: a single oversized clip
     * should not hold up four other reports, and the one that failed keeps
     * its place in the queue with the reason attached.
     */
    suspend fun sendAll(
        session: UploadSession,
        onProgress: (Progress) -> Unit,
    ): Progress = withContext(Dispatchers.IO) {
        val queue = dao().readyToSend()
        var sent = 0
        var failed = 0
        queue.forEachIndexed { index, report ->
            onProgress(Progress.Sending(index + 1, queue.size, report.title))
            val result = runCatching { sendOne(session, report, queue.size, index, onProgress) }
            if (result.isSuccess) {
                sent++
            } else {
                failed++
                dao().noteError(report.id, humanise(result.exceptionOrNull()))
            }
        }
        Progress.Done(sent, failed)
    }

    private suspend fun sendOne(
        session: UploadSession,
        report: ReportRow,
        total: Int,
        index: Int,
        onProgress: (Progress) -> Unit,
    ) {
        val body = JSONObject().apply {
            put("title", report.title)
            put("template", report.template)
            put("template_version", Templates.version)
            put("fields", JSONObject(report.fields))
            report.criticality?.let { put("criticality", it) }
            report.body?.let { put("body", it) }
            report.observedAt?.let { put("observed_at", isoUtc(it)) }
            report.lat?.let { put("lat", it) }
            report.lng?.let { put("lng", it) }
            report.accuracyM?.let { put("location_accuracy_m", it.toDouble()) }
            report.locationNote?.let { put("location_note", it) }
            put("client_ref", report.clientRef)
        }

        val request = Request.Builder()
            .url("${session.baseUrl}/api/intake/submissions")
            .header("Authorization", "Bearer ${session.token}")
            .post(body.toString().toRequestBody(JSON))
            .build()

        val remoteId = client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(serverMessage(response.code, text))
            // duplicate:true is the happy path for a retry, not an error —
            // the console is telling us this one already landed.
            JSONObject(text).getInt("id")
        }

        val files = dao().attachments(report.id).filterNot { it.sent }
        files.forEachIndexed { fileIndex, file ->
            onProgress(Progress.SendingFile(fileIndex + 1, files.size, file.filename))
            sendFile(session, remoteId, file)
            dao().update(file.copy(sent = true))
        }

        // Only now is the report finished with. Marking it sent before its
        // files were through would lose the pictures on a drop-out.
        dao().update(report.copy(status = "sent", lastError = null,
                               updatedAt = System.currentTimeMillis()))
    }

    private fun sendFile(session: UploadSession, remoteId: Int, file: AttachmentRow) {
        val onDisk = File(file.path)
        if (!onDisk.exists()) {
            // The row outlived its file. Nothing to send and nothing to fix;
            // skipping beats failing the whole report over it.
            return
        }
        // Decrypted into memory for the length of the upload only. These are
        // phone-camera sized, and streaming a decrypting cipher into OkHttp
        // would mean a custom RequestBody whose failure mode is a corrupt
        // upload that nobody notices.
        val plain = Crypto.openBytes(context, onDisk.readBytes())
        val multipart = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("client_ref", file.clientRef)
            .apply { file.durationMs?.let { addFormDataPart("duration_ms", it.toString()) } }
            .addFormDataPart(
                "file", file.filename,
                plain.toRequestBody(file.mimeType.toMediaType())
            )
            .build()
        val request = Request.Builder()
            .url("${session.baseUrl}/api/intake/submissions/$remoteId/files")
            .header("Authorization", "Bearer ${session.token}")
            .post(multipart)
            .build()
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) throw IOException(serverMessage(response.code, text))
        }
    }

    /** The console's own words where it gave any — it writes better errors
     *  than a status code, and the analyst is the one who has to act on it. */
    private fun serverMessage(code: Int, text: String): String {
        val detail = runCatching { JSONObject(text).optString("detail") }.getOrNull()
        if (!detail.isNullOrBlank()) return detail
        return when (code) {
            401 -> "The console did not accept that code."
            413 -> "The console says that is too large."
            429 -> "The console is asking this device to slow down. Try again shortly."
            in 500..599 -> "The console had an error ($code)."
            else -> "The console refused it ($code)."
        }
    }

    private fun humanise(t: Throwable?): String = when (t) {
        null -> "Something went wrong."
        is java.net.UnknownHostException ->
            "Could not find the console at that address. Is this phone on the right network?"
        is java.net.ConnectException ->
            "Nothing answered at that address."
        is java.net.SocketTimeoutException ->
            "The console stopped responding part way through."
        else -> t.message ?: t.javaClass.simpleName
    }

    private fun isoUtc(millis: Long): String {
        val format = java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", java.util.Locale.US)
        format.timeZone = java.util.TimeZone.getTimeZone("UTC")
        return format.format(java.util.Date(millis))
    }

    private companion object {
        val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
