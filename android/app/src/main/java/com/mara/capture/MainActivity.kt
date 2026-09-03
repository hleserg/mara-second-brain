package com.mara.capture

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings as AndroidSettings
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.NotificationManagerCompat
import com.mara.capture.databinding.ActivityMainBinding
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Один экран на три роли из ТЗ §5.1F и §6: спаривание, здоровье, мастер.
 * Отдельных экранов нет намеренно — владелец открывает приложение раз в жизнь
 * и потом только когда что-то сломалось.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var b: ActivityMainBinding
    private lateinit var s: Settings
    private var отчёт = ""

    private val выборПапки = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri: Uri? ->
        if (uri == null) return@registerForActivityResult
        contentResolver.takePersistableUriPermission(
            uri, Intent.FLAG_GRANT_READ_URI_PERMISSION
        )
        s.folderUri = uri.toString()
        здоровье()
    }

    private val разрешения = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { здоровье() }

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        b = ActivityMainBinding.inflate(layoutInflater)
        setContentView(b.root)
        // Keystore на некоторых сборках Huawei падает при первом обращении.
        // Без этого первый отчёт владельца был бы «оно вылетает» и ни строки,
        // которую можно прислать.
        s = try {
            Settings(this)
        } catch (e: Exception) {
            покажи("Keystore не завёлся — пришли эту строку:\n${e.javaClass.name}: ${e.message}")
            return
        }

        b.url.setText(s.baseUrl)
        b.token.setText(s.token)

        b.save.setOnClickListener {
            val беда = Адрес.беда(b.url.text.toString())
            if (беда != null) {
                покажи("Адрес не годится: $беда")
                return@setOnClickListener
            }
            s.baseUrl = b.url.text.toString().trim()
            s.token = b.token.text.toString()
            if (s.paired) {
                // чужой токен клал работы в FAILED; новый токен — новая попытка
                Queue(this).retryFailed()
                SyncWorker.schedule(this)
                SyncWorker.kick(this)
                Toast.makeText(this, "запущено", Toast.LENGTH_SHORT).show()
            }
            здоровье()
        }
        b.perms.setOnClickListener { разрешения.launch(НУЖНЫ) }
        b.folder.setOnClickListener { выборПапки.launch(null) }
        b.selftest.setOnClickListener { проверка() }
        b.wizard.setOnClickListener { мастер() }
        b.battery.setOnClickListener { батарея() }
        b.notif.setOnClickListener {
            runCatching { startActivity(Intent(AndroidSettings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
        }
        b.copy.setOnClickListener {
            (getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager)
                .setPrimaryClip(ClipData.newPlainText("mara", отчёт))
            Toast.makeText(this, "скопировано", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onResume() {
        super.onResume()
        if (::s.isInitialized) здоровье()   // иначе на экране сообщение про Keystore
    }

    // ── здоровье ─────────────────────────────────────────────────────────

    private fun здоровье() = фоном {
        val q = Queue(this)
        val последняя = Device.scan(this, s).maxByOrNull { it.modifiedMs }
        listOf(
            "спарено: " + if (s.paired) "да, " + s.baseUrl else "нет",
            "папка: " + (s.folderUri.ifEmpty { "не выбрана, смотрю медиатеку" }),
            "последняя запись: " + (последняя?.let { "${it.name} · ${когда(it.modifiedMs)}" }
                ?: "не вижу ни одной"),
            "в очереди: ${q.depth()}, отправлено: ${q.count(JobState.DONE)}, " +
                "сдалось: ${q.count(JobState.FAILED)}",
            "последняя отправка: " + когда(s.lastUploadMs),
            // пишется при любой попытке, и неудачной тоже: удачные видно на сервере
            "последняя попытка связи: " + когда(s.lastContactMs),
            "разрешения: " + НУЖНЫ.filter { Device.granted(this, it) }
                .joinToString(", ") { it.substringAfterLast('.') }.ifEmpty { "нет" },
            "батарея не ограничена: " + if (безОграничений()) "да" else "нет, нажми кнопку",
            "уведомления (WhatsApp): " + if (слушаем()) "доступ есть" else "нет доступа, нажми кнопку",
            "SMS: " + when {
                s.smsDirect -> "напрямую, курсор ${s.smsLastId}"
                слушаем() && Device.granted(this, Manifest.permission.READ_SMS) ->
                    "провайдер не отдал — через уведомления"
                слушаем() -> "через уведомления"
                Device.granted(this, Manifest.permission.READ_SMS) ->
                    "разрешение есть, провайдер ещё не опрашивался"
                else -> "нет ни разрешения, ни уведомлений"
            },
            "сообщений в очереди: ${q.countMessages(JobState.NEW)}, отправлено: " +
                "${q.countMessages(JobState.DONE)}, сдалось: ${q.countMessages(JobState.FAILED)}",
        ).joinToString("\n")
    }

    /** Доступ к уведомлениям — не runtime-разрешение, а системный список. */
    private fun слушаем(): Boolean =
        NotificationManagerCompat.getEnabledListenerPackages(this).contains(packageName)

    // ── самопроверка ─────────────────────────────────────────────────────

    private fun проверка() {
        if (!s.paired) return покажи("сначала адрес и токен")
        покажи("проверяю…")
        Thread {
            val api = Api(s.baseUrl, s.token)
            val жив = api.health()
            val почему = api.lastError   // до tokenOk, тот перезапишет
            val токен = api.tokenOk()
            s.lastContactMs = System.currentTimeMillis()
            val текст = listOf(
                "сервер жив: " + if (жив == 200) "да" else "нет (ответ $жив${почему?.let { ", $it" } ?: ""})",
                "токен принят: " + when (токен) {
                    404 -> "да"
                    401 -> "нет, токен не тот"
                    0 -> "не дозвонился"
                    else -> "непонятно (ответ $токен)"
                },
                "если сервер не отвечает — проверь, что телефон в домашней локалке (дома по вайфаю, вне дома через VPN роутера)",
            ).joinToString("\n")
            runOnUiThread { покажи(текст) }
        }.start()
    }

    // ── мастер ───────────────────────────────────────────────────────────

    /**
     * Диагностика по ТЗ §6: что именно видно на этой прошивке. Отчёт владелец
     * копирует кнопкой и присылает текстом — это и есть материал для пункта
     * §24 про ограничения конкретной Huawei.
     */
    private fun мастер() = фоном {
        val медиатека = Device.mediaStore(this)
        val папка = Device.folder(this, s.folderUri)
        val последняя = (медиатека + папка).maxByOrNull { it.modifiedMs }
        val журнал = Device.callLog(this, System.currentTimeMillis() - 7 * 24 * 3600_000L)
        val совпало = последняя?.let { CallLogMatcher.nearest(журнал, it.modifiedMs) }
        listOf(
            "модель: ${Build.MODEL} (${Build.MANUFACTURER})",
            "сборка: ${Build.DISPLAY}, Android ${Build.VERSION.RELEASE}",
            "рекордеры: " + Device.producers(this).joinToString("; ").ifEmpty { "ни одного из известных" },
            "в медиатеке записей: ${медиатека.size}",
            "в выбранной папке: " + if (s.folderUri.isEmpty()) "папка не выбрана" else "${папка.size}",
            "последний файл: " + (последняя?.let { "${it.name}, ${it.sizeBytes} Б, ${когда(it.modifiedMs)}" }
                ?: "нет"),
            "читается: " + (последняя?.let {
                if (Device.open(this, it) != null) "да" else "нет"
            } ?: "-"),
            "звук: " + (последняя?.let { Device.audioInfo(this, it) } ?: "-"),
            "звонков в журнале за неделю: ${журнал.size}",
            "сопоставился с: " + (совпало?.let {
                "${it.name ?: it.number} · ${it.direction} · ${it.durationS} с"
            } ?: "ни с чем"),
            "SMS в провайдере за неделю: " + (Device.sms(this, 0, System.currentTimeMillis() - 7 * 24 * 3600_000L)
                ?.size?.toString() ?: "провайдер не отдал (нет разрешения или прошивка)"),
            "слушатель уведомлений: " + if (слушаем()) "включён" else "выключен",
            // сойдётся ли с именем файла экспорта — вопрос к полю, не к коду
            "последняя беседа WhatsApp по уведомлению: " + s.lastChatTitle.ifEmpty { "ещё не было" },
        ).joinToString("\n")
    }

    /** Скан медиатеки, обход SAF и разбор кодека — не на главном потоке. */
    private fun фоном(сбор: () -> String) {
        Thread {
            val t = runCatching(сбор).getOrElse { "не собралось: ${it.javaClass.simpleName}: ${it.message}" }
            runOnUiThread { покажи(t) }
        }.start()
    }

    // ── мелочи ───────────────────────────────────────────────────────────

    private fun безОграничений(): Boolean =
        (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .isIgnoringBatteryOptimizations(packageName)

    /**
     * Huawei душит фоновые приложения агрессивнее прочих (ТЗ §5.1F). Кнопка
     * ведёт в системный список; добавить приложение в «защищённые» всё равно
     * может только человек руками.
     */
    private fun батарея() {
        runCatching {
            startActivity(Intent(AndroidSettings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }.onFailure {
            runCatching { startActivity(Intent(AndroidSettings.ACTION_SETTINGS)) }
        }
    }

    private fun покажи(t: String) {
        отчёт = t
        b.status.text = t
    }

    private fun когда(ms: Long): String =
        if (ms <= 0) "никогда"
        else SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).format(Date(ms))

    companion object {
        /** До 33 медиатеку открывало общее чтение хранилища, с 33 — отдельное на аудио. */
        val НУЖНЫ: Array<String> = arrayOf(
            Manifest.permission.READ_CALL_LOG,
            Manifest.permission.READ_CONTACTS,
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.READ_SMS,
            if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO
            else Manifest.permission.READ_EXTERNAL_STORAGE,
        )
    }
}
