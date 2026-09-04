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

// ── сообщения: WhatsApp из уведомлений, SMS из провайдера (спека 8–9) ────────

object Sha {
    fun hex(s: String): String = java.security.MessageDigest.getInstance("SHA-256")
        .digest(s.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
}

/** Сообщение, каким его видят слушатель уведомлений и провайдер SMS. */
data class Message(
    val source: String,          // whatsapp | sms
    val id: String,              // source_id — ключ, общий с импортёром экспорта
    val chat: String,            // название беседы; для личной — собеседник
    val sender: String,          // пусто — своё
    val text: String,
    val atMs: Long,
    val group: Boolean = false,
    val outgoing: Boolean = false,
    val via: String = "notification",   // notification | provider
    val pkg: String? = null,
    val number: String? = null,
    val keyHash: String? = null,        // sha256 ключа уведомления, не сам ключ
    val threadId: Long? = null,
    val read: Boolean? = null,
)

object MessageId {
    /** Только ASCII-пробелы: `\s` у JVM и `str.split()` у Python видят NBSP по-разному. */
    private val ПРОБЕЛЫ = Regex("[ \\t\\n\\r]+")
    private const val КРАЯ = " \t\n\r"

    fun normalize(text: String): String =
        text.trim { it in КРАЯ }.replace(ПРОБЕЛЫ, " ")

    /**
     * Общий с `scripts/whatsapp_import.py` ключ: одно и то же сообщение из
     * уведомления и из экспорта чата должно лечь в один `source_id`, иначе
     * contextd не отсеет дубль. Минута — целым числом эпохи, чтобы зона и
     * формат не могли разойтись. Пин — tests/fixtures/whatsapp-message-id.json.
     */
    fun of(pkg: String, chat: String, sender: String, text: String, atMs: Long): String =
        Sha.hex("$pkg|$chat|$sender|${normalize(text)}|${Math.floorDiv(atMs, 60_000L)}")

    /** Не `_id` провайдера: dedupe_key на сервере без устройства, и `_id=5`
     *  нового телефона столкнулся бы с `_id=5` старого. */
    fun sms(address: String, dateMs: Long, type: Int, body: String): String =
        Sha.hex("sms|$address|$dateMs|$type|$body")
}

object NotificationParse {
    val WHATSAPP = setOf("com.whatsapp", "com.whatsapp.w4b")
    val SMS_APPS = setOf("com.huawei.message", "com.android.mms",
        "com.google.android.apps.messaging", "com.samsung.android.messaging")

    /** Одна строка MessagingStyle. sender null — своё: ответ из шторки. */
    data class Line(val sender: String?, val text: String?, val atMs: Long)

    /** Уведомление, сведённое к значениям ТЗ §5.1C. Bundle сюда не попадает. */
    data class Seen(
        val pkg: String, val postMs: Long, val key: String,
        val summary: Boolean, val ongoing: Boolean,
        val title: String?, val text: String?, val conversationTitle: String?,
        val group: Boolean, val lines: List<Line>,
    )

    /**
     * Пусто — уведомление не про сообщение: сводка группы, постоянное, WhatsApp
     * без `EXTRA_MESSAGES` («проверяю сообщения», «N новых» при выключенных
     * превью — по локалям их регэкспом не ловим). SMS-приложению без
     * MessagingStyle разрешён простой «заголовок — кто, текст — что».
     */
    fun messages(n: Seen): List<Message> {
        if (n.summary || n.ongoing) return emptyList()
        val source = when (n.pkg) {
            in WHATSAPP -> "whatsapp"
            in SMS_APPS -> "sms"
            else -> return emptyList()
        }
        val chat = (n.conversationTitle ?: n.title ?: "").trim()
        if (chat.isEmpty()) return emptyList()
        val lines = when {
            n.lines.isNotEmpty() -> n.lines
            source == "sms" && !n.text.isNullOrBlank() -> listOf(Line(n.title, n.text, n.postMs))
            else -> emptyList()
        }
        val keyHash = Sha.hex(n.key)
        return lines.filter { !it.text.isNullOrBlank() }.map { l ->
            val own = l.sender == null
            val sender = if (own) "" else l.sender!!.trim()
            Message(source, MessageId.of(n.pkg, chat, sender, l.text!!, l.atMs), chat, sender,
                l.text.trim(), l.atMs, group = n.group, outgoing = own,
                via = "notification", pkg = n.pkg, keyHash = keyHash)
        }
    }
}

object MessageJson {
    /** Тело для `POST /v1/ingest/message` — тот же путь, что у Telegram. */
    fun build(m: Message, zone: ZoneId): JSONObject {
        val p = JSONObject().put("text", m.text).put("outgoing", m.outgoing).put("via", m.via)
        m.pkg?.let { p.put("package", it) }
        if (m.source == "sms") {
            m.number?.let { p.put("number", it) }
            if (m.chat.isNotEmpty() && m.chat != m.number) p.put("contact_name", m.chat)
            p.put("direction", if (m.outgoing) "outgoing" else "incoming")
            m.threadId?.let { p.put("thread_id", it) }
            m.read?.let { p.put("read", it) }
        } else {
            p.put("chat_title", m.chat).put("chat_type", if (m.group) "group" else "private")
                .put("sender_name", m.sender)
        }
        m.keyHash?.let { p.put("notification_key_hash", it) }
        return JSONObject().put("source", m.source).put("source_id", m.id)
            .put("occurred_at", Iso.at(m.atMs, zone)).put("classification", "personal")
            .put("payload", p)
    }
}

object MessageFlow {
    /** 200 — доставлено (дубль тоже); токен — сдаёмся; сеть и 5xx — повторим;
     *  прочие 4xx — сервер отверг тело, повтор не поможет. */
    fun next(reply: ServerReply): JobState = when {
        reply.code == 200 -> JobState.DONE
        reply.code == 0 || reply.code >= 500 -> JobState.NEW
        else -> JobState.FAILED
    }
}

/**
 * Проверка адреса сервера перед сохранением.
 *
 * Открытый HTTP допустим только внутрь своей сети: домашняя локалка, VPN
 * роутера, петля. Наружу — только TLS: снаружи в открытом виде поедет
 * bearer-токен, а за ним записи разговоров. Манифест разрешает cleartext
 * глобально (иначе отвалился бы запасной путь по локалке), так что запрет
 * держится здесь, в одном месте и с внятным текстом ошибки.
 */
object Адрес {

    private val частные = Regex(
        """^(127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|""" +
        """172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+|""" +
        """localhost|[^.]+\.local)$""",
        RegexOption.IGNORE_CASE)

    /** Хост из своей сети: ему открытый HTTP прощаем. */
    fun свой(host: String): Boolean = частные.matches(host)

    /** null — адрес годится; иначе строка для показа владельцу. */
    fun беда(url: String): String? {
        val s = url.trim()
        if (s.isEmpty()) return "адрес пустой"
        val u = try {
            java.net.URI(s)
        } catch (e: Exception) {
            return "адрес не разбирается"
        }
        val scheme = u.scheme?.lowercase() ?: return "нет http:// или https://"
        val host = u.host ?: return "в адресе нет имени сервера"
        if (s.endsWith("/")) return "лишний / на конце"
        return when (scheme) {
            "https" -> null
            "http" -> if (свой(host)) null
                      else "снаружи только https: по http туда уедет токен открытым текстом"
            else -> "нужен http:// или https://"
        }
    }
}
