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

    /** Курсор провайдера SMS: `_id` последнего забранного. 0 — ещё не читали. */
    var smsLastId: Long
        get() = prefs.getLong("sms_last_id", 0)
        set(v) = prefs.edit().putLong("sms_last_id", v).apply()

    /** Последнее SMS, пойманное уведомлением: провайдер при первом заходе
     *  стартует отсюда, чтобы окно перехода между режимами не дало дублей. */
    var lastSmsNotificationMs: Long
        get() = prefs.getLong("sms_notif", 0)
        set(v) = prefs.edit().putLong("sms_notif", v).apply()

    /** Провайдер SMS в последний раз отдал строки. Именно это, а не
     *  разрешение, решает, кто читает SMS: с разрешением, но с молча
     *  отказавшим провайдером, слушатель обязан взять SMS на себя. */
    var smsDirect: Boolean
        get() = prefs.getBoolean("sms_direct", false)
        set(v) = prefs.edit().putBoolean("sms_direct", v).apply()

    /** Последняя беседа WhatsApp, как её назвало уведомление — для мастера:
     *  сойдётся ли с именем файла экспорта, покажет только поле. */
    var lastChatTitle: String
        get() = prefs.getString("last_chat", "") ?: ""
        set(v) = prefs.edit().putString("last_chat", v).apply()

    // ── здоровье (Т4.2) ──────────────────────────────────────────────────

    /** Последний вердикт `Здоровье.оценить` — имя состояния §8.5 и причина. */
    var healthState: String
        get() = prefs.getString("health_state", "") ?: ""
        set(v) = prefs.edit().putString("health_state", v).apply()

    var healthReason: String
        get() = prefs.getString("health_reason", "") ?: ""
        set(v) = prefs.edit().putString("health_reason", v).apply()

    var healthAtMs: Long
        get() = prefs.getLong("health_at", 0)
        set(v) = prefs.edit().putLong("health_at", v).apply()

    /** Открытая тревога «звонок был, записи нет»: `startMs` звонка; 0 — нет.
     *  Ключ дедупа §8.4 п.4: один звонок — одна тревога. */
    var alertCallMs: Long
        get() = prefs.getLong("alert_call", 0)
        set(v) = prefs.edit().putLong("alert_call", v).apply()

    /** История §8.4 п.7: сколько тревог было и когда закрыта последняя. */
    var alertCount: Int
        get() = prefs.getInt("alert_count", 0)
        set(v) = prefs.edit().putInt("alert_count", v).apply()

    var alertRecoveredMs: Long
        get() = prefs.getLong("alert_recovered", 0)
        set(v) = prefs.edit().putLong("alert_recovered", v).apply()

    /** Начало наблюдения за здоровьем: первый прогон `HealthWorker`. Звонки
     *  до него — до установки приложения, за них тревоги нет. */
    var healthSinceMs: Long
        get() = prefs.getLong("health_since", 0)
        set(v) = prefs.edit().putLong("health_since", v).apply()

    val paired: Boolean get() = baseUrl.isNotEmpty() && token.isNotEmpty()
}

/** Сообщение в очереди: тело уже собрано, осталось доставить. */
data class Msg(val id: String, val source: String, val body: String, val state: JobState,
               val attempts: Int, val atMs: Long)

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
    val path: String? = null,
    /** Ключ квитанции идемпотентности (ТЗ §4.4, Т2.9): выдан до первого
     *  `POST /v1/ingest/event`, повтор уходит с ним же. */
    val idemKey: String? = null,
) {
    fun recording() = Recording(id, name, sizeBytes, modifiedMs, producer, path)
}

/**
 * Очередь на SQLite. Room тут — это annotation processor ради трёх запросов.
 *
 * Очередь обязана пережить reboot и force-stop (ТЗ §5.1E), поэтому она на
 * диске, а не в памяти воркера.
 */
class Queue(ctx: Context) : SQLiteOpenHelper(ctx.applicationContext, "queue.db", null, 5) {

    private val JOBS = """create table jobs(
                 id text primary key, name text, size integer, mtime integer,
                 state text, attempts integer default 0, sha256 text, event_id text,
                 seen_size integer default -1, seen_mtime integer default -1,
                 seen_at integer default 0, error text, producer text, updated integer,
                 path text, idem_key text)"""
    // `if not exists`: после отката APK ниже схемы 3 и возврата таблица уже
    // есть, а `onDowngrade` её не трогает (Codex по #136, круг 5)
    private val MESSAGES = """create table if not exists messages(
                 id text primary key, source text, body text, state text,
                 attempts integer default 0, error text, at integer, updated integer)"""

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(JOBS); db.execSQL(MESSAGES)
    }

    /** Миграция добавляющая: снести `jobs` значило бы перехешировать на
     *  телефоне каждую запись после обновления. Сервер отсеял бы дубли, но
     *  первое впечатление от обновления было бы «оно всё шлёт заново». */
    override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) {
        if (old < 2) { db.execSQL("drop table if exists jobs"); db.execSQL(JOBS) }
        if (old < 3) db.execSQL(MESSAGES)
        // путь медиатеки для сопоставления по номеру (Т4.3); у старых работ
        // его нет — они сопоставятся по времени, как и раньше. Колонка уже
        // есть, если таблицу только что пересоздали с версии 1 или если базу
        // открывал откаченный APK (`onDowngrade` колонок не трогает) — второй
        // раз её не добавить (Codex по #136, круги 2–3)
        if (old < 4 && !естьКолонка(db, "jobs", "path")) {
            db.execSQL("alter table jobs add column path text")
        }
        // ключ квитанции (Т2.9); у работ, уехавших до обновления, его нет —
        // ключ выдаст первый же прогон на `HASHED`, а `DONE` и `FAILED` он не
        // нужен
        if (old < 5) {
            if (!естьКолонка(db, "jobs", "idem_key")) {
                db.execSQL("alter table jobs add column idem_key text")
            }
            // Колонка уже была — базу открывал откаченный APK схемы 4. Он её
            // не знает: на доросшем файле обнулил sha256, не тронув ключ, а
            // мог и пересчитать хеш — строка в HASHED с ключом прежнего тела.
            // Под старым ключом сервер отдал бы квитанцию про прежние байты
            // (`need_blob=false` → DONE без заливки). Ключи до POST сжигаем
            // все: свежий ключ стоит одного лишнего дедупа на сервере, старый
            // — потерянной записи (Codex по #139, круги 2 и 4)
            db.execSQL("update jobs set idem_key=null where state in ('NEW','HASHED')")
        }
    }

    /** Откат APK на прежнюю версию: лишняя колонка или таблица старому коду
     *  не мешают — он называет колонки явно, а новые допускают null. Штатный
     *  `onDowngrade` бросает, и очередь не открылась бы вовсе (Codex по
     *  #136, круг 3). Номер версии при этом опускается, и следующий апгрейд
     *  снова пройдёт через `onUpgrade` — потому там проверка колонки. */
    override fun onDowngrade(db: SQLiteDatabase, old: Int, new: Int) {}

    private fun естьКолонка(db: SQLiteDatabase, таблица: String, колонка: String): Boolean =
        db.rawQuery("pragma table_info($таблица)", null).use { c ->
            var есть = false
            while (c.moveToNext()) if (c.getString(1) == колонка) есть = true
            есть
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
                    put("path", rec.path)
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
                // и ключ квитанции с ним: под старым ключом сервер отдал бы
                // квитанцию про прежние байты, и доросший файл либо уехал бы
                // лишний раз (409), либо лёг бы в DONE без события (ревью Т2.9)
                прежние.putNull("idem_key")
            }
            db.update("jobs", прежние, "id=?", arrayOf(rec.id))
        }
    }

    fun pending(): List<Job> {
        val out = mutableListOf<Job>()
        readableDatabase.rawQuery(
            "select id,name,size,mtime,state,attempts,sha256,event_id,seen_size,seen_mtime," +
                "seen_at,error,producer,path,idem_key from jobs where state not in (?,?) order by mtime",
            arrayOf(JobState.DONE.name, JobState.FAILED.name)
        ).use { c ->
            while (c.moveToNext()) out += Job(
                c.getString(0), c.getString(1), c.getLong(2), c.getLong(3),
                JobState.valueOf(c.getString(4)), c.getInt(5), c.getString(6), c.getString(7),
                c.getLong(8), c.getLong(9), c.getLong(10), c.getString(11), c.getString(12),
                c.getString(13), c.getString(14),
            )
        }
        return out
    }

    fun save(job: Job, nowMs: Long) {
        writableDatabase.update("jobs", ContentValues().apply {
            put("state", job.state.name); put("attempts", job.attempts)
            put("sha256", job.sha256); put("event_id", job.eventId)
            // Ключ квитанции `save` не трогает вовсе: снимок бывает старее
            // строки (второй воркер успел выдать ключ, сжечь его в `seen` или
            // уйти дальше), и любая запись по снимку — хоть ключа, хоть null
            // по `job.state == NEW` — вернула бы ключ под новое тело или стёрла
            // бы выданный под принятый запрос. Выдаёт ключ только
            // `выдатьКлюч`, сжигают — `сжечьКлюч` с проверкой состояния строки,
            // `seen`, `retryFailed`, миграция 5 (Codex по #139, круги 3–4)
            put("error", job.error); put("updated", nowMs)
        }, "id=?", arrayOf(job.id))
    }

    /**
     * Ключ квитанции работе — один, даже когда работу разом взяли два воркера
     * (периодический `mara-sync` и разовый `mara-sync-once` друг друга не
     * исключают): предложенный ключ ложится только в пустую колонку, а
     * возвращается то, что в строке лежит после этого — своё или чужое.
     * null — строки нет (Codex по #139).
     */
    fun выдатьКлюч(id: String, ключ: String): String? {
        val db = writableDatabase
        db.compileStatement("update jobs set idem_key=? where id=? and idem_key is null").apply {
            bindString(1, ключ); bindString(2, id)
        }.executeUpdateDelete()
        return db.rawQuery("select idem_key from jobs where id=?", arrayOf(id))
            .use { if (it.moveToFirst()) it.getString(0) else null }
    }

    /**
     * Сжечь ключ при возврате в NEW после 409: тело с новым хешем — другое.
     * Условие по состоянию **строки**, не снимка: запоздалый снимок другого
     * воркера сюда не попадёт, а строку, которую уже увели из `из`, не
     * тронем (Codex по #139, круг 4).
     */
    fun сжечьКлюч(id: String, из: JobState) {
        writableDatabase.compileStatement("update jobs set idem_key=null where id=? and state=?")
            .apply { bindString(1, id); bindString(2, из.name) }.executeUpdateDelete()
    }

    fun count(state: JobState): Int =
        readableDatabase.rawQuery("select count(*) from jobs where state=?", arrayOf(state.name))
            .use { if (it.moveToFirst()) it.getInt(0) else 0 }

    fun depth(): Int =
        readableDatabase.rawQuery(
            "select count(*) from jobs where state not in (?,?)",
            arrayOf(JobState.DONE.name, JobState.FAILED.name)
        ).use { if (it.moveToFirst()) it.getInt(0) else 0 }

    /** Чужой токен вбили — все работы легли в FAILED. Поправили токен — поднимаем. */
    fun retryFailed(): Int {
        val db = writableDatabase
        db.compileStatement("update messages set state='NEW', error=null, attempts=0 where state='FAILED'")
            .executeUpdateDelete()
        return db.compileStatement(
            // ключ квитанции сгорает вместе с хешем: тело посчитается заново
            "update jobs set state='NEW', sha256=null, idem_key=null, error=null, attempts=0 " +
                "where state='FAILED'"
        ).executeUpdateDelete()
    }

    // ── сообщения ────────────────────────────────────────────────────────

    /** true — новое. Повтор того же ключа молча отбрасывается: WhatsApp
     *  перепощивает последние сообщения беседы на каждое новое. */
    fun put(m: Message, body: org.json.JSONObject, nowMs: Long): Boolean =
        writableDatabase.insertWithOnConflict("messages", null, ContentValues().apply {
            put("id", m.id); put("source", m.source); put("body", body.toString())
            put("state", JobState.NEW.name); put("at", m.atMs); put("updated", nowMs)
        }, SQLiteDatabase.CONFLICT_IGNORE) != -1L

    fun pendingMessages(limit: Int = 200): List<Msg> {
        val out = mutableListOf<Msg>()
        readableDatabase.rawQuery(
            "select id,source,body,state,attempts,at from messages where state=? order by at limit $limit",
            arrayOf(JobState.NEW.name)
        ).use { c ->
            while (c.moveToNext()) out += Msg(c.getString(0), c.getString(1), c.getString(2),
                JobState.valueOf(c.getString(3)), c.getInt(4), c.getLong(5))
        }
        return out
    }

    fun saveMessage(m: Msg, error: String?, nowMs: Long) {
        writableDatabase.update("messages", ContentValues().apply {
            put("state", m.state.name); put("attempts", m.attempts); put("error", error)
            put("updated", nowMs)
            // доставлено — текст на телефоне больше не нужен; ключ остаётся для дедупа
            if (m.state == JobState.DONE) putNull("body")
        }, "id=?", arrayOf(m.id))
    }

    fun countMessages(state: JobState): Int =
        readableDatabase.rawQuery("select count(*) from messages where state=?", arrayOf(state.name))
            .use { if (it.moveToFirst()) it.getInt(0) else 0 }
}
