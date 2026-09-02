package com.mara.capture

import android.content.ContentValues
import android.content.Context
import android.content.SharedPreferences
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKeys

/**
 * Адрес сервера и токен устройства. Keystore, а не открытый SharedPreferences
 * (ТЗ §5.1E): на телефоне, который теряют, это разница между «нашли аппарат» и
 * «получили доступ ко всем разговорам».
 *
 * Значений по умолчанию нет намеренно. Ни адреса, ни токена нет ни в коде, ни в
 * ресурсах, ни в репозитории — их приносит спаривание.
 */
class Settings(ctx: Context) {
    private val prefs: SharedPreferences = EncryptedSharedPreferences.create(
        "mara-capture",
        MasterKeys.getOrCreate(MasterKeys.AES256_GCM_SPEC),
        ctx.applicationContext,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )

    var baseUrl: String
        get() = prefs.getString("base_url", "") ?: ""
        set(v) = prefs.edit().putString("base_url", v.trim().trimEnd('/')).apply()

    var token: String
        get() = prefs.getString("token", "") ?: ""
        set(v) = prefs.edit().putString("token", v.trim()).apply()

    /** Папка, выбранная владельцем через SAF, если автопоиск не справился. */
    var folderUri: String
        get() = prefs.getString("folder", "") ?: ""
        set(v) = prefs.edit().putString("folder", v).apply()

    var lastContactMs: Long
        get() = prefs.getLong("last_contact", 0)
        set(v) = prefs.edit().putLong("last_contact", v).apply()

    var lastUploadMs: Long
        get() = prefs.getLong("last_upload", 0)
        set(v) = prefs.edit().putLong("last_upload", v).apply()

    val paired: Boolean get() = baseUrl.isNotEmpty() && token.isNotEmpty()
}

/** Одна запись очереди: файл плюс всё, что о нём известно на этот момент. */
data class Job(
    val id: String,
    val name: String,
    val sizeBytes: Long,
    val modifiedMs: Long,
    val state: JobState,
    val attempts: Int = 0,
    val sha256: String? = null,
    val eventId: String? = null,
    val seenSize: Long = -1,
    val seenMtime: Long = -1,
    val seenAtMs: Long = 0,
    val error: String? = null,
    val producer: String? = null,
) {
    fun recording() = Recording(id, name, sizeBytes, modifiedMs, producer)
}

/**
 * Очередь на SQLite. Room тут — это annotation processor ради трёх запросов.
 *
 * Очередь обязана пережить reboot и force-stop (ТЗ §5.1E), поэтому она на
 * диске, а не в памяти воркера.
 */
class Queue(ctx: Context) : SQLiteOpenHelper(ctx.applicationContext, "queue.db", null, 2) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """create table jobs(
                 id text primary key, name text, size integer, mtime integer,
                 state text, attempts integer default 0, sha256 text, event_id text,
                 seen_size integer default -1, seen_mtime integer default -1,
                 seen_at integer default 0, error text, producer text, updated integer)"""
        )
    }

    /** Очередь — кэш: сервер дедуплицирует по хешу, так что пересобрать её дёшево. */
    override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) {
        db.execSQL("drop table if exists jobs"); onCreate(db)
    }

    /**
     * Файл увиден сканом. Новый — заводим работу; знакомый — обновляем приметы,
     * по которым потом решится, дописан ли он.
     *
     * Уже уехавшую работу не трогаем: иначе повторный скан гонял бы по кругу
     * один и тот же разговор.
     */
    fun seen(rec: Recording, nowMs: Long) {
        val db = writableDatabase
        val cur = db.rawQuery("select state, size, mtime, seen_size, seen_mtime from jobs where id=?",
            arrayOf(rec.id))
        cur.use {
            if (!it.moveToFirst()) {
                db.insert("jobs", null, ContentValues().apply {
                    put("id", rec.id); put("name", rec.name)
                    put("size", rec.sizeBytes); put("mtime", rec.modifiedMs)
                    put("state", JobState.NEW.name)
                    put("seen_size", rec.sizeBytes); put("seen_mtime", rec.modifiedMs)
                    put("seen_at", nowMs); put("producer", rec.producer); put("updated", nowMs)
                })
                return
            }
            // POSTED тоже: если файл дорос после того, как событие ушло, надо
            // упасть в NEW до загрузки — иначе fixed-length поток оборвётся на
            // клиенте и будет выглядеть как «сети нет» до скончания веков
            if (it.getString(0) !in setOf(JobState.NEW.name, JobState.HASHED.name,
                    JobState.POSTED.name)) return
            val прежние = ContentValues().apply {
                put("size", rec.sizeBytes); put("mtime", rec.modifiedMs); put("updated", nowMs)
            }
            // отсчёт тишины перезапускаем только когда файл действительно изменился
            if (it.getLong(3) != rec.sizeBytes || it.getLong(4) != rec.modifiedMs) {
                прежние.put("seen_size", rec.sizeBytes)
                прежние.put("seen_mtime", rec.modifiedMs)
                прежние.put("seen_at", nowMs)
                прежние.put("state", JobState.NEW.name)   // изменился — хеш недействителен
                прежние.putNull("sha256")
            }
            db.update("jobs", прежние, "id=?", arrayOf(rec.id))
        }
    }

    fun pending(): List<Job> {
        val out = mutableListOf<Job>()
        readableDatabase.rawQuery(
            "select id,name,size,mtime,state,attempts,sha256,event_id,seen_size,seen_mtime," +
                "seen_at,error,producer from jobs where state not in (?,?) order by mtime",
            arrayOf(JobState.DONE.name, JobState.FAILED.name)
        ).use { c ->
            while (c.moveToNext()) out += Job(
                c.getString(0), c.getString(1), c.getLong(2), c.getLong(3),
                JobState.valueOf(c.getString(4)), c.getInt(5), c.getString(6), c.getString(7),
                c.getLong(8), c.getLong(9), c.getLong(10), c.getString(11), c.getString(12),
            )
        }
        return out
    }

    fun save(job: Job, nowMs: Long) {
        writableDatabase.update("jobs", ContentValues().apply {
            put("state", job.state.name); put("attempts", job.attempts)
            put("sha256", job.sha256); put("event_id", job.eventId)
            put("error", job.error); put("updated", nowMs)
        }, "id=?", arrayOf(job.id))
    }

    fun count(state: JobState): Int =
        readableDatabase.rawQuery("select count(*) from jobs where state=?", arrayOf(state.name))
            .use { if (it.moveToFirst()) it.getInt(0) else 0 }

    fun depth(): Int =
        readableDatabase.rawQuery(
            "select count(*) from jobs where state not in (?,?)",
            arrayOf(JobState.DONE.name, JobState.FAILED.name)
        ).use { if (it.moveToFirst()) it.getInt(0) else 0 }

    fun latest(): Job? = pending().lastOrNull()

    /** Чужой токен вбили — все работы легли в FAILED. Поправили токен — поднимаем. */
    fun retryFailed(): Int = writableDatabase.compileStatement(
        "update jobs set state='NEW', sha256=null, error=null, attempts=0 where state='FAILED'"
    ).executeUpdateDelete()
}
