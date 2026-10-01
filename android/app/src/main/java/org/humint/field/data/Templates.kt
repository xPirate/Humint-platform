package org.humint.field.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * The app's half of the contract in api/field_templates.json.
 *
 * The file is not hand-copied into assets — the Gradle build copies the one
 * the console reads (see app/build.gradle.kts). If the two ever disagree it
 * is because the handset has an older APK than the console has code, which
 * is exactly what [version] is for: the app sends it with every report, the
 * console compares, and the queue card says so in words an analyst can act
 * on. Nothing is refused over it at either end.
 */
class Template(
    val key: String,
    val label: String,
    val blurb: String,
    val titlePattern: String,
    val capture: List<String>,
    val requireAny: List<String>,
    val fields: List<TemplateField>,
) {
    fun captures(kind: String) = capture.contains(kind)

    /** Whether the form has enough in it to be worth sending. */
    fun isComplete(values: Map<String, Any?>): Boolean {
        val required = fields.filter { it.required }
        if (required.any { values.blank(it.key) }) return false
        if (requireAny.isNotEmpty() && requireAny.all { values.blank(it) }) return false
        return true
    }

    /** What is still missing, phrased for a person rather than a validator. */
    fun whatIsMissing(values: Map<String, Any?>): String? {
        val missing = fields.filter { it.required && values.blank(it.key) }
        if (missing.isNotEmpty()) return missing.joinToString(", ") { it.label.lowercase() }
        if (requireAny.isNotEmpty() && requireAny.all { values.blank(it) }) {
            val names = requireAny.mapNotNull { k -> fields.find { it.key == k }?.label?.lowercase() }
            return "at least one of " + names.joinToString(", ")
        }
        return null
    }

    private fun Map<String, Any?>.blank(key: String): Boolean {
        val v = this[key] ?: return true
        return when (v) {
            is String -> v.isBlank()
            is List<*> -> v.isEmpty()
            is Boolean -> false
            else -> false
        }
    }

    /**
     * The report's title, from the same pattern the console uses as its
     * fallback. Kept deliberately identical in behaviour: a placeholder with
     * nothing behind it takes its preceding separator with it, so
     * "{color} {make} {model} · {plate}" with only a plate is "ABC 1234" and
     * not "· ABC 1234".
     */
    fun composeTitle(values: Map<String, Any?>): String {
        val out = StringBuilder()
        val buf = StringBuilder()
        // Whether the placeholder most recently seen produced anything. It
        // decides what happens to the literal text after it: "{bearing_deg}°"
        // keeps its degree sign when there is a bearing and loses it when
        // there is not. The console's _fill does exactly the same, and
        // TitleParityTest fails if the two ever stop agreeing.
        var lastEmitted = false
        var i = 0
        while (i < titlePattern.length) {
            val c = titlePattern[i]
            if (c == '{') {
                val end = titlePattern.indexOf('}', i)
                if (end == -1) { buf.append(c); i++; continue }
                val value = display(values[titlePattern.substring(i + 1, end)])
                if (value.isNotBlank()) {
                    out.append(buf).append(value)
                    buf.setLength(0)
                    lastEmitted = true
                } else {
                    buf.setLength(0)
                    lastEmitted = false
                }
                i = end + 1
            } else {
                buf.append(c); i++
            }
        }
        if (lastEmitted) out.append(buf)
        val text = out.toString().trim(' ', '\t', '\n', '·', '-', '—', '–', ',', ':', ';', '|', '/')
            .split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ")
        if (text.isNotBlank()) return text.take(200)
        // Same fallback the console uses, in the same order: the first
        // non-blank string in the bag, then a generic title. A report with
        // no usable title still has to be sendable.
        values.values.filterIsInstance<String>().firstOrNull { it.isNotBlank() }
            ?.let { return it.trim().take(200) }
        return "Field report"
    }

    private fun display(v: Any?): String = when (v) {
        null -> ""
        is Boolean -> if (v) "yes" else ""
        is List<*> -> v.joinToString(", ")
        else -> v.toString()
    }
}

class TemplateField(
    val key: String,
    val label: String,
    val type: String,
    val hint: String?,
    val suffix: String?,
    val required: Boolean,
    val autofocus: Boolean,
    val capitalize: Boolean,
    val keyboard: String?,
    val options: List<String>,
    val default: Any?,
    val min: Int?,
    val max: Int?,
)

object Templates {
    lateinit var all: List<Template>
        private set
    var version: Int = 0
        private set
    lateinit var criticality: List<String>
        private set

    fun load(context: Context) {
        if (::all.isInitialized) return
        parse(context.assets.open("field_templates.json").bufferedReader().use { it.readText() })
    }

    /** Split out from [load] so the parser can be exercised by a plain JVM
     *  test. The title logic in particular has to behave identically to the
     *  console's Python, and TitleParityTest is what keeps it honest. */
    fun parse(text: String) {
        val root = JSONObject(text)
        version = root.getInt("version")
        criticality = root.getJSONArray("criticality").strings()
        all = root.getJSONArray("templates").objects().map { t ->
            Template(
                key = t.getString("key"),
                label = t.getString("label"),
                blurb = t.optString("blurb"),
                titlePattern = t.getString("title"),
                capture = t.optJSONArray("capture").orEmpty().strings(),
                requireAny = t.optJSONArray("require_any").orEmpty().strings(),
                fields = t.getJSONArray("fields").objects().map { f ->
                    TemplateField(
                        key = f.getString("key"),
                        label = f.getString("label"),
                        type = f.getString("type"),
                        hint = f.optString("hint").ifBlank { null },
                        suffix = f.optString("suffix").ifBlank { null },
                        required = f.optBoolean("required", false),
                        autofocus = f.optBoolean("autofocus", false),
                        capitalize = f.optBoolean("capitalize", false),
                        keyboard = f.optString("keyboard").ifBlank { null },
                        options = f.optJSONArray("options").orEmpty().strings(),
                        default = if (f.has("default")) f.get("default") else null,
                        min = if (f.has("min")) f.getInt("min") else null,
                        max = if (f.has("max")) f.getInt("max") else null,
                    )
                }
            )
        }
    }

    fun byKey(key: String): Template? = all.find { it.key == key }

    private fun JSONArray?.orEmpty(): JSONArray = this ?: JSONArray()
    private fun JSONArray.strings(): List<String> = (0 until length()).map { getString(it) }
    private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }
}
