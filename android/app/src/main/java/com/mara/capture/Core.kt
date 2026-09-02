package com.mara.capture

import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/**
 * Решения приложения — здесь, и только чистыми функциями. Всё, что трогает
 * Android (SAF, MediaStore, SQLite, сеть), живёт снаружи тонкими оболочками.
 *
 * Так это можно проверить на JVM без телефона, эмулятора и Robolectric —
 * а телефона у нас пока нет.
 */

/** Файл записи, каким его видит скан: без содержимого, только приметы. */
data class Recording(
    val id: String,          // uri или путь — ключ локальной очереди
    val name: String,
    val sizeBytes: Long,
    val modifiedMs: Long,
    val producer: String? = null,   // пакет, записавший файл; медиатека знает, SAF — нет
)

/** Строка журнала звонков. Адресную книгу целиком не трогаем (ТЗ §5.1B). */
data class CallLogEntry(
    val number: String?,
    val name: String?,
    val direction: String,   // incoming | outgoing | missed
    val startMs: Long,
    val durationS: Long,
) {
    val endMs: Long get() = startMs + durationS * 1000
}

object Iso {
    private val FMT: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ssXXX")

    /** Секунды без долей: сервер хранит строку как есть, лишняя точность врёт. */
    fun at(ms: Long, zone: ZoneId): String =
        ZonedDateTime.ofInstant(Instant.ofEpochMilli(ms), zone).format(FMT)
}

object FileReady {
    /** Столько файл должен не меняться, прежде чем мы поверим, что он дописан. */
    const val QUIET_MS = 20_000L

    /**
     * Рекордер дописывает файл уже после отбоя. Если забрать его на середине,
     * sha256 сойдётся с тем, что мы посчитали, и ошибка будет тихой: на сервер
     * уедет половина разговора, а признаков сбоя не будет ни одного.
     */
    fun ready(before: Recording?, now: Recording, elapsedMs: Long): Boolean =
        before != null &&
            now.sizeBytes > 0 &&
            before.sizeBytes == now.sizeBytes &&
            before.modifiedMs == now.modifiedMs &&
            elapsedMs >= QUIET_MS
}

object CallLogMatcher {
    /** Дальше этого по времени связь между файлом и звонком уже выдумана. */
    const val WINDOW_MS = 5 * 60 * 1000L

    /**
     * Разные рекордеры ставят mtime по-разному: кто в начале записи, кто в
     * конце. Поэтому меряем расстояние до отрезка разговора, а не до точки:
     * попал внутрь — расстояние ноль, иначе до ближайшего края.
     */
    fun distance(e: CallLogEntry, ms: Long): Long = when {
        ms < e.startMs -> e.startMs - ms
        ms > e.endMs -> ms - e.endMs
        else -> 0L
    }

    fun nearest(entries: List<CallLogEntry>, ms: Long): CallLogEntry? =
        entries.filter { distance(it, ms) <= WINDOW_MS }.minByOrNull { distance(it, ms) }
}

object EventJson {
    /**
     * Тело для `POST /v1/ingest/event`. Приложение не изобретает полей:
     * контракт закреплён в tests/fixtures/phone-call-event.json, и с ним
     * сверяются обе стороны — Kotlin и питоновский тест contextd.
     *
     * `source_id` — это sha256 содержимого, а не имя файла: переименование не
     * должно порождать второй звонок (ТЗ §7). `device_id` не шлём, сервер
     * берёт его из токена.
     */
    fun build(
        rec: Recording,
        call: CallLogEntry?,
        sha256: String,
        ext: String,
        producer: String?,
        zone: ZoneId,
    ): JSONObject {
        val payload = JSONObject()
        if (call != null) {
            call.name?.let { payload.put("contact_name", it) }
            call.number?.let { payload.put("number", it) }
            payload.put("direction", call.direction)
            // Только при этом значении call_project.person_card заводит карточку
            // человека: имя, услышанное в разговоре, в реестр сущностей не пускают.
            payload.put("contact_source", "call-log")
            payload.put("duration_s", call.durationS)
        }
        producer?.let { payload.put("producer", it) }

        val blob = JSONObject()
            .put("sha256", sha256)
            .put("bytes", rec.sizeBytes)
            .put("ext", ext)

        val ev = JSONObject()
            .put("kind", "call")
            .put("source", "phone")
            .put("source_id", sha256)
            .put("occurred_at", Iso.at(call?.startMs ?: rec.modifiedMs, zone))
        if (call != null) ev.put("ended_at", Iso.at(call.endMs, zone))
        return ev.put("payload", payload).put("blob", blob)
    }
}

enum class JobState { NEW, HASHED, POSTED, DONE, FAILED }

/** Ответ сервера в том виде, в каком он влияет на решение. */
data class ServerReply(val code: Int, val eventId: String? = null, val needBlob: Boolean = false)

object JobFlow {
    /**
     * Куда переводить работу. Возврат того же состояния означает «повторить
     * позже»: сеть и пятисотки попытку не сжигают, этим занимается WorkManager.
     */
    fun next(state: JobState, reply: ServerReply): JobState = when {
        reply.code == 401 || reply.code == 403 -> JobState.FAILED   // токен, повтор не поможет
        reply.code == 0 || reply.code >= 500 -> state               // сеть или сервер, повторим
        state == JobState.HASHED && reply.code == 200 ->
            // дубль без запроса блоба значит, что аудио на сервере уже лежит
            if (reply.needBlob) JobState.POSTED else JobState.DONE
        state == JobState.POSTED && reply.code == 200 -> JobState.DONE
        // 409 — сервер посчитал другой хеш: файл дописали, пока мы его читали.
        // Считаем заново с самого начала, иначе будем слать половину разговора.
        state == JobState.POSTED && reply.code == 409 -> JobState.NEW
        else -> JobState.FAILED
    }
}
