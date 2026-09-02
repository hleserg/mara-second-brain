package com.mara.capture

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.provider.CallLog
import android.provider.MediaStore
import androidx.documentfile.provider.DocumentFile
import java.io.InputStream
import java.security.MessageDigest

/**
 * Всё, что спрашивает у телефона. Тонкий слой: решения принимает Core.kt, тут
 * только чтение того, что система готова отдать.
 */
object Device {

    /** Слова в пути, по которым узнаём запись разговора. Регистр не важен. */
    private val ПРИМЕТЫ = listOf("call", "record", "voice", "запис")

    fun granted(ctx: Context, perm: String): Boolean =
        ctx.checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED

    /**
     * Способ 1 — медиатека. Если прошивка публикует запись сюда, мы узнаём о
     * ней сразу и без обхода папок.
     *
     * Путь не зашит: берём всё аудио и оставляем то, в чьём пути есть примета
     * (ТЗ §22 — не hardcode'ить recording path Huawei).
     */
    fun mediaStore(ctx: Context, sinceMs: Long = 0): List<Recording> {
        val out = mutableListOf<Recording>()
        val cols = arrayOf(
            MediaStore.Audio.Media._ID, MediaStore.Audio.Media.DISPLAY_NAME,
            MediaStore.Audio.Media.SIZE, MediaStore.Audio.Media.DATE_MODIFIED,
            MediaStore.Audio.Media.RELATIVE_PATH, MediaStore.Audio.Media.OWNER_PACKAGE_NAME,
        )
        val c: Cursor = ctx.contentResolver.query(
            MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, cols, null, null,
            MediaStore.Audio.Media.DATE_MODIFIED + " desc"
        ) ?: return out
        c.use {
            while (it.moveToNext()) {
                val путь = (it.getString(4) ?: "") + (it.getString(1) ?: "")
                if (ПРИМЕТЫ.none { p -> путь.contains(p, ignoreCase = true) }) continue
                val mtime = it.getLong(3) * 1000
                if (mtime < sinceMs) continue
                out += Recording(
                    Uri.withAppendedPath(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
                        it.getLong(0).toString()).toString(),
                    it.getString(1) ?: "?", it.getLong(2), mtime, it.getString(5),
                )
            }
        }
        return out
    }

    /**
     * Способ 2 — папка, выбранная владельцем один раз через SAF. За SAF-деревом
     * FileObserver следить не умеет, поэтому здесь только обход.
     */
    fun folder(ctx: Context, uri: String): List<Recording> {
        if (uri.isEmpty()) return emptyList()
        val dir = DocumentFile.fromTreeUri(ctx, Uri.parse(uri)) ?: return emptyList()
        return dir.listFiles().filter { it.isFile && (it.length() > 0) }
            .map { Recording(it.uri.toString(), it.name ?: "?", it.length(), it.lastModified()) }
    }

    /** Всё, что видно обоими способами. Один и тот же файл через MediaStore и
     *  SAF даёт разные uri, но одинаковый sha256 — сервер отсеет дубль сам. */
    fun scan(ctx: Context, s: Settings, sinceMs: Long = 0): List<Recording> =
        (mediaStore(ctx, sinceMs) + folder(ctx, s.folderUri)).distinctBy { it.id }

    fun open(ctx: Context, rec: Recording): InputStream? =
        ctx.contentResolver.openInputStream(Uri.parse(rec.id))

    fun sha256(ctx: Context, rec: Recording): String? {
        val md = MessageDigest.getInstance("SHA-256")
        val buf = ByteArray(64 * 1024)
        (open(ctx, rec) ?: return null).use { s ->
            while (true) {
                val n = s.read(buf)
                if (n <= 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    fun ext(rec: Recording): String =
        rec.name.substringAfterLast('.', "").lowercase().ifEmpty { "bin" }

    /** Журнал звонков за последние сутки: сопоставлять дальше уже незачем. */
    fun callLog(ctx: Context, sinceMs: Long): List<CallLogEntry> {
        if (!granted(ctx, Manifest.permission.READ_CALL_LOG)) return emptyList()
        val out = mutableListOf<CallLogEntry>()
        val cols = arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME,
            CallLog.Calls.TYPE, CallLog.Calls.DATE, CallLog.Calls.DURATION)
        ctx.contentResolver.query(
            CallLog.Calls.CONTENT_URI, cols,
            CallLog.Calls.DATE + ">=?", arrayOf(sinceMs.toString()),
            CallLog.Calls.DATE + " desc"
        )?.use {
            while (it.moveToNext()) out += CallLogEntry(
                it.getString(0), it.getString(1),
                when (it.getInt(2)) {
                    CallLog.Calls.INCOMING_TYPE -> "incoming"
                    CallLog.Calls.OUTGOING_TYPE -> "outgoing"
                    else -> "missed"
                },
                it.getLong(3), it.getLong(4),
            )
        }
        return out
    }

    /** Кодек, каналы, частота — строка для мастера (ТЗ §6). */
    fun audioInfo(ctx: Context, rec: Recording): String {
        val r = MediaMetadataRetriever()
        return try {
            r.setDataSource(ctx, Uri.parse(rec.id))
            fun m(k: Int) = r.extractMetadata(k) ?: "?"
            "мим=%s, каналов=%s, Гц=%s, мс=%s".format(
                m(MediaMetadataRetriever.METADATA_KEY_MIMETYPE),
                m(MediaMetadataRetriever.METADATA_KEY_NUM_TRACKS),
                m(MediaMetadataRetriever.METADATA_KEY_SAMPLERATE),
                m(MediaMetadataRetriever.METADATA_KEY_DURATION),
            )
        } catch (e: Exception) {
            "не прочиталось: ${e.javaClass.simpleName}"
        } finally {
            runCatching { r.release() }
        }
    }

    /** Кандидаты в производители записи. Список перебираем, показываем найденное. */
    private val КАНДИДАТЫ = listOf(
        "com.huawei.soundrecorder", "com.android.soundrecorder", "com.huawei.contacts",
        "com.android.dialer", "com.google.android.dialer",
        "com.nll.acr", "com.skvalex.callrecorder", "com.appstar.callrecorder",
    )

    fun producers(ctx: Context): List<String> = КАНДИДАТЫ.mapNotNull { p ->
        runCatching {
            @Suppress("DEPRECATION")
            val i = ctx.packageManager.getPackageInfo(p, 0)
            "$p ${i.versionName}"
        }.getOrNull()
    }
}
