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
        val о = проверить(applicationContext, Settings(applicationContext))
        // §8.4 п.3: после звонка запись ждём с повтором и отступом, а не до
        // следующего часа; `ПОВТОРОВ` — чтобы не ждать вечно
        return if (о.состояние == Состояние.recovering && runAttemptCount < ПОВТОРОВ) Result.retry()
               else Result.success()
    }

    companion object {
        const val ПЕРИОД = "health-periodic"
        const val РАЗОВЫЙ = "health-once"
        const val ПЕРИОД_МИН = 60L
        const val ПОВТОРОВ = 3
        const val КАНАЛ = "health"
        const val УВЕДОМЛЕНИЕ = 1
        private const val НЕДЕЛЯ_МС = 7 * 24 * 3600_000L

        /** Без этих двух захват не работает; остальные из `MainActivity.НУЖНЫ`
         *  — про контакты и SMS, их отсутствие здоровье захвата не ломает. */
        val ДЛЯ_ЗАХВАТА: List<String> = listOf(
            Manifest.permission.READ_CALL_LOG,
            if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO
            else Manifest.permission.READ_EXTERNAL_STORAGE,
        )

        /** Всё, что видно телефону, без единого решения. Что не прочиталось
         *  — честный `null` или пустой список, а не «всё хорошо». */
        fun собрать(ctx: Context, s: Settings): Приметы {
            val сейчас = System.currentTimeMillis()
            val неделя = сейчас - НЕДЕЛЯ_МС
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
                // без разрешения журнал бросает — это уже учтено строкой выше
                звонки = runCatching { Device.callLog(ctx, неделя) }.getOrDefault(emptyList()),
                записи = runCatching { Device.scan(ctx, s, неделя) }.getOrDefault(emptyList())
                    .map { it.modifiedMs },
                расписаниеЖиво = runCatching {
                    WorkManager.getInstance(ctx).getWorkInfosForUniqueWork(SyncWorker.ПЕРИОД).get()
                        .any { !it.state.isFinished }
                }.getOrNull(),
                свободноБайт = runCatching { StatFs(ctx.filesDir.path).availableBytes }.getOrNull(),
            )
        }

        /**
         * Один прогон: оценить, запомнить, поднять или снять тревогу. Зовётся
         * и воркером, и экраном здоровья — состояние на экране и в
         * уведомлении одно и то же, а не два разных мнения.
         */
        fun проверить(ctx: Context, s: Settings): Оценка {
            val п = собрать(ctx, s)
            val о = Здоровье.оценить(п)
            when (Здоровье.событие(о, s.alertCallMs, п.записи)) {
                Здоровье.Событие.ТРЕВОГА -> {
                    s.alertCallMs = о.тревога ?: 0L
                    s.alertCount = s.alertCount + 1
                    уведомить(ctx, "Звонок был, записи нет", о.причина)
                }
                Здоровье.Событие.ВОССТАНОВЛЕНО -> {
                    s.alertCallMs = 0L
                    s.alertRecoveredMs = п.сейчас
                }
                null -> {}
            }
            // сломанное разрешение или папка — тоже тревога, одна на причину
            if (о.состояние == Состояние.unhealthy && о.тревога == null && о.причина != s.healthReason)
                уведомить(ctx, "Захват сломан", о.причина)
            if (о.состояние != Состояние.unhealthy && s.alertCallMs == 0L) снять(ctx)
            s.healthState = о.состояние.name
            s.healthReason = о.причина
            s.healthAtMs = п.сейчас
            return о
        }

        /** Проверка раз в час; сети не требует — смотрит только на телефон. */
        fun schedule(ctx: Context) {
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                ПЕРИОД, ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<HealthWorker>(ПЕРИОД_МИН, TimeUnit.MINUTES).build()
            )
        }

        /** Разовая проверка: после загрузки и после отбоя, когда окно вышло. */
        fun kick(ctx: Context, delaySec: Long = 0) {
            val b = OneTimeWorkRequestBuilder<HealthWorker>()
            if (delaySec > 0) b.setInitialDelay(delaySec, TimeUnit.SECONDS)
            WorkManager.getInstance(ctx).enqueueUniqueWork(РАЗОВЫЙ, ExistingWorkPolicy.REPLACE, b.build())
        }

        /**
         * Durable warning на телефоне (§8.4 п.5). Текст — причина из оценки,
         * без номера и имени: их там нет по построению. Без
         * `POST_NOTIFICATIONS` на 33+ система молча не покажет — разрешение
         * спрашивает кнопка на экране.
         */
        private fun уведомить(ctx: Context, заголовок: String, текст: String) {
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
                .build()
            runCatching { nm.notify(УВЕДОМЛЕНИЕ, n) }
        }

        private fun снять(ctx: Context) {
            (ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager).cancel(УВЕДОМЛЕНИЕ)
        }
    }
}
