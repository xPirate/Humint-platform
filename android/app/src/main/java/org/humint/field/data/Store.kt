package org.humint.field.data

import android.content.Context
import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Update
import kotlinx.coroutines.flow.Flow
import net.zetetic.database.sqlcipher.SupportOpenHelperFactory
import java.io.File
import java.util.UUID

/**
 * The queue, on disk, encrypted.
 *
 * A report exists here from the moment the analyst starts typing. Nothing is
 * held only in memory and lost when the phone is put in a pocket and the
 * process is killed — which, on a modern Android, is most times.
 *
 * [clientRef] is generated once, when the draft is created, and never
 * changes. It is what makes a retry idempotent on the console: an upload
 * interrupted after the server committed but before the app heard back
 * resends the same ref and lands as the same report. This is the single most
 * important field in the schema and the reason it is a UUID assigned at
 * draft time rather than at upload time.
 */
@Entity(tableName = "reports")
data class ReportRow(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    @ColumnInfo(name = "client_ref") val clientRef: String = UUID.randomUUID().toString(),
    val template: String,
    val title: String,
    /** The template's fields, as a JSON object. Parsed with org.json. */
    val fields: String,
    val criticality: String?,
    val body: String?,
    @ColumnInfo(name = "observed_at") val observedAt: Long?,
    val lat: Double?,
    val lng: Double?,
    @ColumnInfo(name = "location_accuracy_m") val accuracyM: Float?,
    @ColumnInfo(name = "location_note") val locationNote: String?,
    /** draft | ready | sent */
    val status: String = "draft",
    @ColumnInfo(name = "created_at") val createdAt: Long = System.currentTimeMillis(),
    @ColumnInfo(name = "updated_at") val updatedAt: Long = System.currentTimeMillis(),
    /** What the console said when it last refused or failed. Shown, not hidden. */
    @ColumnInfo(name = "last_error") val lastError: String? = null,
)

@Entity(tableName = "attachments")
data class AttachmentRow(
    @PrimaryKey val id: String = UUID.randomUUID().toString(),
    @ColumnInfo(name = "report_id") val reportId: String,
    @ColumnInfo(name = "client_ref") val clientRef: String = UUID.randomUUID().toString(),
    /** image | audio | video */
    val kind: String,
    val filename: String,
    @ColumnInfo(name = "mime_type") val mimeType: String,
    /** Absolute path in the app's sandbox. The file itself is encrypted. */
    val path: String,
    @ColumnInfo(name = "size_bytes") val sizeBytes: Long,
    @ColumnInfo(name = "duration_ms") val durationMs: Long? = null,
    /** Set once the console has this file, so a resumed upload skips it. */
    val sent: Boolean = false,
)

@Dao
interface FieldDao {
    @Query("SELECT * FROM reports WHERE status != 'sent' ORDER BY created_at DESC")
    fun queue(): Flow<List<ReportRow>>

    @Query("SELECT * FROM reports WHERE status = 'ready' ORDER BY created_at ASC")
    suspend fun readyToSend(): List<ReportRow>

    @Query("SELECT count(*) FROM reports WHERE status = 'ready'")
    fun readyCount(): Flow<Int>

    @Query("SELECT * FROM reports WHERE id = :id")
    suspend fun report(id: String): ReportRow?

    @Query("SELECT * FROM reports WHERE id = :id")
    fun reportFlow(id: String): Flow<ReportRow?>

    @Insert suspend fun insert(row: ReportRow)
    @Update suspend fun update(row: ReportRow)

    @Query("SELECT * FROM reports WHERE status = 'sent'")
    suspend fun sentReports(): List<ReportRow>

    @Query("DELETE FROM reports WHERE id = :id")
    suspend fun deleteReport(id: String)

    // Attachment rows are removed explicitly rather than by a foreign key
    // with ON DELETE CASCADE, because the file on disk has to be shredded
    // first and the database cannot do that. A cascade here would leave
    // encrypted media orphaned in the sandbox with nothing pointing at it.
    @Query("DELETE FROM attachments WHERE report_id = :reportId")
    suspend fun deleteAttachmentsFor(reportId: String)

    @Query("SELECT * FROM attachments WHERE report_id = :reportId ORDER BY rowid")
    fun attachmentsFlow(reportId: String): Flow<List<AttachmentRow>>

    @Query("SELECT * FROM attachments WHERE report_id = :reportId ORDER BY rowid")
    suspend fun attachments(reportId: String): List<AttachmentRow>

    @Insert suspend fun insert(row: AttachmentRow)
    @Update suspend fun update(row: AttachmentRow)

    @Query("DELETE FROM attachments WHERE id = :id")
    suspend fun deleteAttachment(id: String)

    @Query("UPDATE reports SET last_error = :message, updated_at = :now WHERE id = :id")
    suspend fun noteError(id: String, message: String?, now: Long = System.currentTimeMillis())
}

@Database(entities = [ReportRow::class, AttachmentRow::class], version = 1, exportSchema = false)
abstract class FieldDatabase : RoomDatabase() {
    abstract fun dao(): FieldDao

    companion object {
        @Volatile private var instance: FieldDatabase? = null

        /**
         * Opened with the key the Vault is holding, and closed when it locks.
         *
         * Throws rather than opening an unencrypted database if the vault is
         * shut. Every caller is downstream of the lock screen, so this should
         * be unreachable — and if it ever is reached, failing loudly beats
         * quietly creating a second, readable copy of the queue.
         */
        fun get(context: Context): FieldDatabase = instance ?: synchronized(this) {
            instance ?: build(context.applicationContext).also { instance = it }
        }

        /** Called when the vault locks. The next unlock reopens with the key
         *  it gets then, so a stale handle cannot outlive a lock. */
        fun closeAndForget() {
            synchronized(this) {
                runCatching { instance?.close() }
                instance = null
            }
        }

        private fun build(context: Context): FieldDatabase {
            System.loadLibrary("sqlcipher")
            val key = Vault.key() ?: throw Crypto.Locked()
            // SQLCipher's factory zeroes the array it is given, so it gets a
            // copy — the Vault's own key has to survive for the media files.
            val factory = SupportOpenHelperFactory(key.copyOf())
            return Room.databaseBuilder(context, FieldDatabase::class.java, "field.db")
                .openHelperFactory(factory)
                // No fallbackToDestructiveMigration. A migration this app
                // cannot perform must not silently throw away a queue of
                // reports nobody has uploaded yet; crashing is louder and
                // recoverable, wiping is neither.
                .build()
        }
    }
}

/** Where captured media goes. Inside filesDir, so it is in the app sandbox
 *  and is removed with the app — never in shared storage, where the gallery
 *  and every other app would see it. */
fun mediaDir(context: Context): File =
    File(context.filesDir, "media").apply { mkdirs() }
