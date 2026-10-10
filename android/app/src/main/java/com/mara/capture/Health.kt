package com.mara.capture

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.StatFs
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import androidx.documentfile.provider.DocumentFile
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import java.util.concurrent.TimeUnit

/**
 * Health workflow (Т4.2, ТЗ §8.4–8.6): после `BOOT_COMPLETED` и раз в час —
 * разрешение или папку отбирают и без перезагрузки, и единственная проверка
 * на загрузке к вечеру уже врёт. На WorkManager, как велит §7, а не на
 * своём таймере.
 *
 * Сам воркер ничего не решает: приметы собирает `собрать`, вердикт выносит
 * `Здоровье.оценить`, и только он проверяется на JVM. Здесь — Android:
 * разрешения, SAF, журнал, уведомление.
 */
class HealthWorker(ctx: Context, p: WorkerParameters) : Worker(ctx, p) {

    override fun doWork(): Result {
        val s = Settings(applicationContext)
        val о = проверить(applicationContext, s)
        // §8.4 п.3: пока запись за звонком не появилась — в окне или уже с
        // открытой тревогой — повтор с отступом, а не до следующего часа:
        // файл, дописанный чуть позже окна, закрывает тревогу за минуты.
        // `ПОВТОРОВ` — чтобы не ждать вечно (Codex по #137, круг 5)
        val ждём = о.состояние == Состояние.recovering || s.alertCallMs != 0L
        return if (ждём && runAttemptCount < ПОВТОРОВ) Result.retry() else Result.success()
    }

    companion object {
        const val ПЕРИОД = "health-periodic"
        const val РАЗОВЫЙ = "health-once"
        const val ПОСЛЕ_ОТБОЯ = "health-after-call"
        const val ПЕРИОД_МИН = 60L
        const val ПОВТОРОВ = 3
        const val КАНАЛ = "health"
        /** Два уведомления, не одно: тревога по звонку живёт до появления
         *  записи, «сломано» — до починки; одно перезаписывало бы другое. */
        const val ТРЕВОГА = 1
        const val СЛОМАНО = 2
        private const val НЕДЕЛЯ_МС = 7 * 24 * 3600_000L

        /** Сбор примет и переход состояния — под одним замком: снимок,
         *  собранный до чужого перехода, иначе переоткрыл бы закрытую тревогу
         *  (Codex по #137, круг 3). */
        private val ЗАМОК = Any()

        /** Без этих двух захват не работает; остальные из `MainActivity.НУЖНЫ`
         *  — про контакты и SMS, их отсутствие здоровье захвата не ломает. */
        val ДЛЯ_ЗАХВАТА: List<String> = listOf(
            Manifest.permission.READ_CALL_LOG,
            if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO
            else Manifest.permission.READ_EXTERNAL_STORAGE,
        )

        /** Всё, что видно телефону, без единого решения. Что не прочиталось
         *  — честный `null`, пустой список или флаг, а не «всё хорошо».
         *  Единственная запись — начало наблюдения при первом прогоне: за
         *  звонки до установки приложения отвечать нечем (Codex по #137). */
        fun собрать(ctx: Context, s: Settings): Приметы {
            val сейчас = System.currentTimeMillis()
            val неделя = сейчас - НЕДЕЛЯ_МС
            if (s.healthSinceMs == 0L) s.healthSinceMs = сейчас
            // «не прочитался» — не «звонков не было» и не «записей нет»: без
            // разрешения или с молчащим провайдером журнал — null, упавший
            // скан — null (Codex, круги 3–4)
            val журнал = runCatching { Device.callLogOrNull(ctx, неделя) }.getOrNull()
            val скан = runCatching { Device.scanOrNull(ctx, s, неделя) }.getOrNull()
            return Приметы(
                сейчас = сейчас,
                загрузка = сейчас - SystemClock.elapsedRealtime(),
                разрешенийНет = ДЛЯ_ЗАХВАТА.filterNot { Device.granted(ctx, it) }
                    .map { it.substringAfterLast('.') },
                папкаЧитается = s.folderUri.ifEmpty { null }?.let { uri ->
                    runCatching { DocumentFile.fromTreeUri(ctx, Uri.parse(uri))?.canRead() == true }
                        .getOrDefault(false)
                },
                рекордерЕсть = Device.producers(ctx).isNotEmpty(),
                звонки = журнал ?: emptyList(),
                записи = скан ?: emptyList(),
                // обе периодические: сверка и сама проверка здоровья — потеря
                // любой из них значит, что следующего часа не будет
                расписаниеЖиво = runCatching {
                    val wm = WorkManager.getInstance(ctx)
                    listOf(SyncWorker.ПЕРИОД, ПЕРИОД).all { имя ->
                        wm.getWorkInfosForUniqueWork(имя).get().any { !it.state.isFinished }
                    }
                }.getOrNull(),
                свободноБайт = runCatching { StatFs(ctx.filesDir.path).availableBytes }.getOrNull(),
                уведомленияРазрешены = Build.VERSION.SDK_INT < 33 ||
                    Device.granted(ctx, Manifest.permission.POST_NOTIFICATIONS),
                наблюдениеС = s.healthSinceMs,
                журналЧитается = журнал != null,
                записиЧитаются = скан != null,
                отбойСлышен = Device.granted(ctx, Manifest.permission.READ_PHONE_STATE),
            )
        }

        /**
         * Один прогон: собрать, оценить, запомнить, поднять или снять тревогу.
         * Зовётся и воркером, и экраном здоровья — состояние на экране и в
         * уведомлении одно и то же, а не два разных мнения. Сбор и переход
         * под одним замком: экран и воркер в одну секунду подняли бы одну
         * тревогу дважды (ревью), а снимок, собранный до чужого перехода,
         * переоткрыл бы закрытую (Codex, круг 3).
         */
        fun проверить(ctx: Context, s: Settings): Оценка = synchronized(ЗАМОК) {
            val п = собрать(ctx, s)
            val о = Здоровье.оценить(п)
            for (е in Здоровье.события(о, s.alertCallMs, п)) when (е) {
                Здоровье.Событие.ВОССТАНОВЛЕНО -> {
                    s.alertCallMs = 0L
                    s.alertRecoveredMs = п.сейчас
                    снять(ctx, ТРЕВОГА)
                }
                // звонок ушёл из недельного журнала: записи так и нет, но
                // проверять больше нечего — закрыть, не называя восстановлением
                Здоровье.Событие.ИСТЕКЛА -> {
                    s.alertCallMs = 0L
                    снять(ctx, ТРЕВОГА)
                }
                Здоровье.Событие.ТРЕВОГА -> {
                    s.alertCallMs = о.тревога ?: 0L
                    s.alertCount = s.alertCount + 1
                    уведомить(ctx, ТРЕВОГА, "Звонок был, записи нет", о.причина)
                }
            }
            // Открытая тревога выставляется каждым прогоном, не только в момент
            // подъёма: перезагрузка чистит шторку, а без POST_NOTIFICATIONS
            // первый notify молча пропал — durable warning обязан вернуться
            // (Codex по #137, круг 2). Тот же id и setOnlyAlertOnce — без
            // повторного сигнала; счётчик растёт только на ТРЕВОГА.
            if (s.alertCallMs != 0L) уведомить(ctx, ТРЕВОГА, "Звонок был, записи нет",
                if (о.тревога == s.alertCallMs) о.причина else "записи за звонком так и нет")
            // сломанное разрешение или папка — своё уведомление, пока сломано
            if (о.состояние == Состояние.unhealthy && о.тревога == null)
                уведомить(ctx, СЛОМАНО, "Захват сломан", о.причина)
            else снять(ctx, СЛОМАНО)
            s.healthState = о.состояние.name
            s.healthReason = о.причина
            s.healthAtMs = п.сейчас
            о
        }

        /** Проверка раз в час; сети не требует — смотрит только на телефон. */
        fun schedule(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                ПЕРИОД, ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<HealthWorker>(ПЕРИОД_МИН, TimeUnit.MINUTES).build()
            )
        }

        /** Разовая проверка после загрузки или обновления: одна на имя, повтор заменяет. */
        fun kick(ctx: Context, delaySec: Long = 0) {
            val b = OneTimeWorkRequestBuilder<HealthWorker>()
            if (delaySec > 0) b.setInitialDelay(delaySec, TimeUnit.SECONDS)
            WorkManager.getInstance(ctx).enqueueUniqueWork(РАЗОВЫЙ, ExistingWorkPolicy.REPLACE, b.build())
        }

        /**
         * Проверка на выход окна после отбоя — своя на каждый звонок, не
         * уникальная: второй звонок через девять минут иначе отменял бы срок
         * первого и сдвигал его ещё на окно, а третий — ещё (Codex по #137,
         * круг 4). Прогон смотрит все звонки, так что лишняя проверка
         * безвредна, а пропущенная — нарушение §8.4.
         */
        fun послеОтбоя(ctx: Context) {
            WorkManager.getInstance(ctx).enqueue(
                OneTimeWorkRequestBuilder<HealthWorker>()
                    .setInitialDelay(Здоровье.ОКНО_МС / 1000 + 60, TimeUnit.SECONDS)
                    .addTag(ПОСЛЕ_ОТБОЯ)
                    .build()
            )
        }

        /**
         * Durable warning на телефоне (§8.4 п.5). Текст — причина из оценки,
         * без номера и имени: их там нет по построению. Без
         * `POST_NOTIFICATIONS` на 33+ система молча не покажет — разрешение
         * спрашивает кнопка на экране.
         */
        private fun уведомить(ctx: Context, id: Int, заголовок: String, текст: String) {
            val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(
                NotificationChannel(КАНАЛ, "Здоровье захвата", NotificationManager.IMPORTANCE_HIGH))
            val открыть = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE)
            val n = NotificationCompat.Builder(ctx, КАНАЛ)
                .setSmallIcon(android.R.drawable.stat_notify_error)
                .setContentTitle(заголовок)
                .setContentText(текст)
                .setContentIntent(открыть)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .build()
            runCatching { nm.notify(id, n) }
        }

        private fun снять(ctx: Context, id: Int) {
            (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(id)
        }
    }
}
