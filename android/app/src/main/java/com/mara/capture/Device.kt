package com.mara.capture

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.Cursor
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.Build
import android.provider.CallLog
import android.provider.ContactsContract
import android.provider.DocumentsContract
import android.provider.Telephony
import android.provider.MediaStore
import androidx.documentfile.provider.DocumentFile
import java.io.FileNotFoundException
import java.io.InputStream
import java.security.MessageDigest

/**
 * Всё, что спрашивает у телефона. Тонкий слой: решения принимает Core.kt, тут
 * только чтение того, что система готова отдать.
 */
object Device {

    fun granted(ctx: Context, perm: String): Boolean =
        ctx.checkSelfPermission(perm) == PackageManager.PERMISSION_GRANTED

    /** Разрешение на медиатеку: до 33 её открывало общее чтение хранилища,
     *  с 33 — отдельное на аудио. */
    val АУДИО: String = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO
        else Manifest.permission.READ_EXTERNAL_STORAGE

    /**
     * Способ 1 — медиатека (`Медиатека`). Если прошивка публикует запись
     * сюда, мы узнаём о ней сразу и без обхода папок.
     *
     * Путь не зашит: берём всё аудио и оставляем то, в чьём пути есть примета
     * (ТЗ §22 — не hardcode'ить recording path Huawei) или чей владелец —
     * рекордер звонков из профилей (`Рекордеры.вМедиатеке`: у ACR Phone в
     * пути приметы нет).
     *
     * null — провайдер не отдал курсор: это не «записей нет», здоровье
     * (Т4.2) различает; сверке и мастеру хватает пустого списка.
     */
    fun mediaStoreOrNull(ctx: Context, sinceMs: Long = 0): List<Recording>? {
        val out = mutableListOf<Recording>()
        val cols = arrayOf(
            MediaStore.Audio.Media._ID, MediaStore.Audio.Media.DISPLAY_NAME,
            MediaStore.Audio.Media.SIZE, MediaStore.Audio.Media.DATE_MODIFIED,
            MediaStore.Audio.Media.RELATIVE_PATH, MediaStore.Audio.Media.OWNER_PACKAGE_NAME,
        )
        val c: Cursor = ctx.contentResolver.query(
            MediaStore.Audio.Media.EXTERNAL_CONTENT_URI, cols, null, null,
            MediaStore.Audio.Media.DATE_MODIFIED + " desc"
        ) ?: return null
        c.use {
            while (it.moveToNext()) {
                val путь = (it.getString(4) ?: "") + (it.getString(1) ?: "")
                if (!Рекордеры.вМедиатеке(путь, it.getString(5))) continue
                val mtime = it.getLong(3) * 1000
                if (mtime < sinceMs) continue
                out += Recording(
                    Uri.withAppendedPath(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
                        it.getLong(0).toString()).toString(),
                    it.getString(1) ?: "?", it.getLong(2), mtime, it.getString(5),
                    it.getString(4),   // RELATIVE_PATH: у ACR тут каталог с номером (Т4.3)
                )
            }
        }
        return out
    }

    /**
     * Способ 2 — папка, выбранная владельцем один раз через SAF (`Папка`). За SAF-деревом
     * FileObserver следить не умеет, поэтому здесь только обход.
     *
     * Обход именно в глубину: ACR кладёт записи не в выбранную папку, а в
     * `[гггг]/[ММ]/[дд]/[номер телефона]/` внутри неё, и плоский список
     * находил там ровно ноль. Потолки, порядок и отбор — в `Дерево.записи`;
     * здесь только переходники к `DocumentFile`.
     */
    fun folder(ctx: Context, uri: String): List<Recording> {
        if (uri.isEmpty()) return emptyList()
        val dir = DocumentFile.fromTreeUri(ctx, Uri.parse(uri)) ?: return emptyList()
        return Дерево.записи(dir, { it.isDirectory }, { it.listFiles().toList() },
            { it.name ?: "" }, { it.length() })
            // имя и размер берём из отбора, он их уже спросил: у SAF это два
            // сэкономленных запроса к провайдеру на каждую запись
            .map { (док, имя, байт) ->
                Recording(док.uri.toString(), имя, байт, док.lastModified())
            }
    }

    /** Провайдеры этого телефона (ТЗ §7.4, ADR-0007 п.1): медиатека всегда,
     *  папка — если владелец её выбрал. */
    fun провайдеры(ctx: Context, s: Settings): List<RecorderProvider> =
        listOfNotNull(Медиатека(ctx), s.folderUri.ifEmpty { null }?.let { Папка(ctx, it) })

    /** Провайдер, через который видна запись, — по её uri: у медиатеки свой
     *  (`content://media/`), всё прочее пришло из папки. Открывают оба
     *  одинаково, по uri самой записи, так что сменённая потом папка запись
     *  не теряет. */
    fun провайдерЗаписи(ctx: Context, s: Settings, rec: Recording): RecorderProvider =
        if (rec.id.startsWith(Recording.МЕДИАТЕКА)) Медиатека(ctx) else Папка(ctx, s.folderUri)

    /**
     * Всё, что видно обоими провайдерами. Один и тот же файл через MediaStore
     * и SAF даёт разные uri, но одинаковый sha256 — сервер отсеет дубль сам.
     *
     * Медиатека — с `sinceMs`, папка — целиком: так было с первой версии, и
     * архив, показанный владельцем через SAF, уезжает весь. Решения о
     * бэкфилле никто не принимал, и менять его молча вместе с выносом
     * провайдеров (Т4.1) нельзя — отсюда асимметрия в одном месте.
     */
    fun scan(ctx: Context, s: Settings, sinceMs: Long = 0): List<Recording> =
        ((Медиатека(ctx).listCandidates(sinceMs) ?: emptyList()) + изПапки(ctx, s)).distinctBy { it.id }

    /** То же, но `null`, если медиатека не отдала курсор — для здоровья (Т4.2). */
    fun scanOrNull(ctx: Context, s: Settings, sinceMs: Long = 0): List<Recording>? =
        Медиатека(ctx).listCandidates(sinceMs)?.let { (it + изПапки(ctx, s)).distinctBy { r -> r.id } }

    private fun изПапки(ctx: Context, s: Settings): List<Recording> =
        if (s.folderUri.isEmpty()) emptyList() else Папка(ctx, s.folderUri).listCandidates(Long.MIN_VALUE)

    /** null — файла больше нет. `openInputStream` на исчезнувшей строке
     *  MediaStore не возвращает null, а бросает: без этого перехвата
     *  объявленный тут `InputStream?` был обещанием, которого никто не
     *  выполнял, а обработка null у обоих зовущих — мёртвым кодом.
     *
     *  Ловим ровно `FileNotFoundException` и ничего шире. Оба зовущих на null
     *  отвечают `FAILED`, а `FAILED` в этой очереди почти терминален:
     *  `Store.pending()` его не отдаёт, и поднимает работу только
     *  `retryFailed()` при пересохранении токена. Отозванное на минуту
     *  `READ_MEDIA_AUDIO` или отвалившийся SAF-грант дают `SecurityException`
     *  при целом файле — похоронить из-за него разговор значило бы потерять
     *  его молча. Пусть летит выше, там повторят. */
    fun open(ctx: Context, rec: Recording): InputStream? = try {
        ctx.contentResolver.openInputStream(Uri.parse(rec.id))
    } catch (e: FileNotFoundException) {
        null
    }

    /** Хеш содержимого — через провайдер, которым запись видна
     *  (`openRecording`): так и считает, и шлёт один и тот же путь. */
    fun sha256(провайдер: RecorderProvider, rec: Recording): String? {
        val md = MessageDigest.getInstance("SHA-256")
        val buf = ByteArray(64 * 1024)
        (провайдер.openRecording(rec) ?: return null).use { s ->
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

    /** Журнал звонков с `sinceMs`; не прочитался — пустой список: сверке и
     *  мастеру этого достаточно. Здоровью (Т4.2) — нет, ему `callLogOrNull`. */
    fun callLog(ctx: Context, sinceMs: Long): List<CallLogEntry> =
        callLogOrNull(ctx, sinceMs) ?: emptyList()

    /** null — журнал не прочитался: нет разрешения или провайдер не отдал
     *  курсор. Это не «звонков не было», и здоровье различает (Codex по #137). */
    fun callLogOrNull(ctx: Context, sinceMs: Long): List<CallLogEntry>? {
        if (!granted(ctx, Manifest.permission.READ_CALL_LOG)) return null
        val out = mutableListOf<CallLogEntry>()
        val cols = arrayOf(CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME,
            CallLog.Calls.TYPE, CallLog.Calls.DATE, CallLog.Calls.DURATION)
        val c = ctx.contentResolver.query(
            CallLog.Calls.CONTENT_URI, cols,
            CallLog.Calls.DATE + ">=?", arrayOf(sinceMs.toString()),
            CallLog.Calls.DATE + " desc"
        ) ?: return null
        c.use {
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

    /** Имя по одному номеру — ровно столько, сколько нужно событию (ТЗ §5.1B). */
    fun contactName(ctx: Context, number: String): String? {
        if (number.isEmpty() || !granted(ctx, Manifest.permission.READ_CONTACTS)) return null
        val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(number))
        return runCatching {
            ctx.contentResolver.query(uri, arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME),
                null, null, null)?.use { if (it.moveToFirst()) it.getString(0) else null }
        }.getOrNull()
    }

    /**
     * SMS напрямую из провайдера (ТЗ §14). Курсор — `_id`, ниже него не смотрим;
     * первый заход ограничен `sinceMs`, чтобы не тащить всю историю аппарата.
     * SMS-приложением по умолчанию не становимся (§5.1D). Нет разрешения или
     * прошивка отказала — null, и слушатель уведомлений берёт SMS на себя.
     */
    fun sms(ctx: Context, sinceId: Long, sinceMs: Long, limit: Int = 500): List<Pair<Long, Message>>? {
        if (!granted(ctx, Manifest.permission.READ_SMS)) return null
        val cols = arrayOf(Telephony.Sms._ID, Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS,
            Telephony.Sms.DATE, Telephony.Sms.TYPE, Telephony.Sms.BODY, Telephony.Sms.READ)
        val c = runCatching {
            ctx.contentResolver.query(Telephony.Sms.CONTENT_URI, cols,
                "${Telephony.Sms._ID}>? and ${Telephony.Sms.DATE}>=? and ${Telephony.Sms.TYPE} in (?,?)",
                arrayOf(sinceId.toString(), sinceMs.toString(),
                    Telephony.Sms.MESSAGE_TYPE_INBOX.toString(), Telephony.Sms.MESSAGE_TYPE_SENT.toString()),
                "${Telephony.Sms._ID} asc limit $limit")
        }.getOrNull() ?: return null
        val имена = HashMap<String, String?>()
        val out = mutableListOf<Pair<Long, Message>>()
        c.use {
            while (it.moveToNext()) {
                val address = it.getString(2) ?: ""
                val date = it.getLong(3); val type = it.getInt(4); val body = it.getString(5) ?: ""
                if (body.isBlank()) continue
                val своё = type == Telephony.Sms.MESSAGE_TYPE_SENT
                val имя = имена.getOrPut(address) { contactName(ctx, address) }
                out += it.getLong(0) to Message("sms", MessageId.sms(address, date, type, body),
                    chat = имя ?: address, sender = if (своё) "" else (имя ?: address),
                    text = body, atMs = date, outgoing = своё, via = "provider",
                    number = address, threadId = it.getLong(1), read = it.getInt(6) != 0)
            }
        }
        return out
    }

    /** Известные рекордеры, найденные на телефоне (§8.3: установлен,
     *  включён, версия). Android 11+ показывает только пакеты из `<queries>`
     *  манифеста — там тот же список, `Рекордеры.ПАКЕТЫ` (сверяет
     *  `RecorderTest`). */
    fun рекордеры(ctx: Context): List<Установлен> = Рекордеры.ИЗВЕСТНЫЕ.mapNotNull { р ->
        р.пакеты.firstNotNullOfOrNull { p ->
            runCatching {
                @Suppress("DEPRECATION")
                val i = ctx.packageManager.getPackageInfo(p, 0)
                Установлен(р, i.versionName, i.applicationInfo?.enabled != false)
            }.getOrNull()
        }
    }

    /** Прочие пакеты из `Рекордеры.ПРОЧИЕ`, что стоят, — только для мастера. */
    fun прочие(ctx: Context): List<String> = Рекордеры.ПРОЧИЕ.mapNotNull { p ->
        runCatching {
            @Suppress("DEPRECATION")
            val i = ctx.packageManager.getPackageInfo(p, 0)
            "$p ${i.versionName}"
        }.getOrNull()
    }
}

/**
 * Медиатека как провайдер §7.4 (ADR-0007 п.1). Знает пакет-владельца файла
 * (`OWNER_PACKAGE_NAME`), поэтому рекордер за ней опознаётся
 * (`Рекордеры.чей`). Ничего не удаляет и не переименовывает.
 */
class Медиатека(private val ctx: Context) : RecorderProvider {
    override val providerId = "mediastore"
    override val providerVersion = 1

    override fun capabilities() = Манифест.поКандидатам(providerId, providerVersion,
        "mediastore", null, true, listCandidates(Long.MIN_VALUE) ?: emptyList(),
        Рекордеры.вЗдоровье(Device.рекордеры(ctx)))

    /** Проба чтения, а не только разрешения (ADR-0007 п.6): курсор
     *  медиатеки отдан. */
    override fun health(): Проба {
        if (!Device.granted(ctx, Device.АУДИО))
            return Проба(false, "нет разрешения " + Device.АУДИО.substringAfterLast('.'))
        val отдан = runCatching {
            ctx.contentResolver.query(MediaStore.Audio.Media.EXTERNAL_CONTENT_URI,
                arrayOf(MediaStore.Audio.Media._ID), null, null, null)?.use { true } ?: false
        }.getOrDefault(false)
        return if (отдан) Проба(true, "медиатека отдаёт аудио")
               else Проба(false, "медиатека не отдала курсор")
    }

    override fun listCandidates(sinceMs: Long): List<Recording>? = Device.mediaStoreOrNull(ctx, sinceMs)
    override fun openRecording(candidate: Recording): InputStream? = Device.open(ctx, candidate)
    override fun extractMetadata(candidate: Recording): String = Device.audioInfo(ctx, candidate)
    override fun acknowledgeIngested(candidate: Recording, receipt: String) {}

    override fun diagnostics(): List<String> {
        val все = listCandidates(Long.MIN_VALUE) ?: return listOf("$providerId: курсор не отдан")
        // пакеты — имена приложений, не людей: в отчёт можно
        val поПакетам = все.groupingBy { it.producer ?: "?" }.eachCount()
            .entries.sortedByDescending { it.value }.joinToString(", ") { "${it.key} ${it.value}" }
        return listOf("$providerId: записей ${все.size}" + (if (все.isEmpty()) "" else ", по пакетам: $поПакетам"))
    }
}

/**
 * Папка, выбранная владельцем через SAF, как провайдер §7.4 (ADR-0007 п.1).
 * Владельца файла не сообщает — рекордер за ней не опознаётся, и манифест
 * говорит `unknown`, а не догадку по пути. Грант только на чтение: ничего
 * не удаляет.
 */
class Папка(private val ctx: Context, private val uri: String) : RecorderProvider {
    override val providerId = "saf_folder"
    override val providerVersion = 1

    override fun capabilities() = Манифест(providerId, providerVersion, "folder",
        runCatching { DocumentsContract.getTreeDocumentId(Uri.parse(uri)) }.getOrNull(),
        false, emptyList())

    override fun health(): Проба =
        if (runCatching { DocumentFile.fromTreeUri(ctx, Uri.parse(uri))?.canRead() == true }
                .getOrDefault(false)) Проба(true, "папка читается")
        else Проба(false, "выбранная папка не читается — выбери её заново")

    /** Обход SAF всегда целый (фильтра у провайдера нет), курсор режет уже
     *  найденное. */
    override fun listCandidates(sinceMs: Long): List<Recording> =
        Device.folder(ctx, uri).filter { it.modifiedMs >= sinceMs }

    override fun openRecording(candidate: Recording): InputStream? = Device.open(ctx, candidate)
    override fun extractMetadata(candidate: Recording): String = Device.audioInfo(ctx, candidate)
    override fun acknowledgeIngested(candidate: Recording, receipt: String) {}
    override fun diagnostics(): List<String> =
        listOf("$providerId: записей ${listCandidates(Long.MIN_VALUE).size}")
}
