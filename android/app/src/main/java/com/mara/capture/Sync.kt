package com.mara.capture

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.WorkManager
import java.time.ZoneId
import java.util.concurrent.TimeUnit

/**
 * Скан и доставка. Один воркер на оба дела: скан без доставки бессмыслен, а
 * доставка без скана нечего доставлять.
 *
 * WorkManager, а не свой сервис: он переживает reboot и force-stop, чего от
 * Huawei и требуется (ТЗ §5.1F).
 */
class SyncWorker(ctx: Context, p: WorkerParameters) : Worker(ctx, p) {

    override fun doWork(): Result {
        val ctx = applicationContext
        val s = Settings(ctx)
        if (!s.paired) return Result.success()      // не спарено — работать не с чем

        val q = Queue(ctx)
        val now = System.currentTimeMillis()
        // за неделю: дольше держать в очереди старьё смысла нет, а после
        // недели без сети догонит сверка по журналу
        Device.scan(ctx, s, now - 7 * 24 * 3600_000L).forEach { q.seen(it, now) }

        val api = Api(s.baseUrl, s.token)
        val журнал = Device.callLog(ctx, now - 7 * 24 * 3600_000L)
        var работали = false
        for (job in q.pending()) {
            if (!готов(q, job, now)) continue
            работали = true
            if (!шаг(ctx, q, api, job, журнал, now)) break   // сеть легла — не долбим
        }
        s.lastContactMs = now
        // Ничего не сделали — WorkManager не должен считать это неудачей: при
        // Result.retry() он бы двигал экспоненту на пустом месте.
        return if (работали) Result.success() else Result.success()
    }

    /** Файл дописан? Решает Core, здесь только приметы из очереди. */
    private fun готов(q: Queue, job: Job, now: Long): Boolean {
        if (job.state != JobState.NEW) return true      // уже посчитан, ждать нечего
        val было = Recording(job.id, job.name, job.seenSize, job.seenMtime)
        return FileReady.ready(было, job.recording(), now - job.seenAtMs)
    }

    /** Один шаг работы. false — сеть, дальше в этом прогоне идти незачем. */
    private fun шаг(ctx: Context, q: Queue, api: Api, job: Job,
                    журнал: List<CallLogEntry>, now: Long): Boolean {
        when (job.state) {
            JobState.NEW -> {
                val sha = Device.sha256(ctx, job.recording())
                    ?: return true.also { q.save(job.copy(state = JobState.FAILED,
                        error = "файл не читается"), now) }
                q.save(job.copy(state = JobState.HASHED, sha256 = sha), now)
                return true
            }
            JobState.HASHED -> {
                val звонок = CallLogMatcher.nearest(журнал, job.modifiedMs)
                val body = EventJson.build(job.recording(), звонок, job.sha256!!,
                    Device.ext(job.recording()), Device.producers(ctx).firstOrNull(),
                    ZoneId.systemDefault())
                val r = api.postEvent(body)
                q.save(job.copy(state = JobFlow.next(job.state, r), eventId = r.eventId,
                    attempts = job.attempts + 1, error = ошибка(r)), now)
                return r.code != 0
            }
            JobState.POSTED -> {
                val r = api.putAudio(job.eventId!!, job.sizeBytes) {
                    Device.open(ctx, job.recording())!!
                }
                val дальше = JobFlow.next(job.state, r)
                // 409 значит, что файл дописали, пока мы его читали: считаем
                // заново, иначе на сервер уедет половина разговора
                q.save(job.copy(state = дальше, attempts = job.attempts + 1,
                    sha256 = if (дальше == JobState.NEW) null else job.sha256,
                    error = ошибка(r)), now)
                if (дальше == JobState.DONE) Settings(ctx).lastUploadMs = now
                return r.code != 0
            }
            else -> return true
        }
    }

    private fun ошибка(r: ServerReply): String? = when {
        r.code == 0 -> "сети нет"
        r.code == 200 -> null
        else -> "ответ ${r.code}"
    }

    companion object {
        const val ПЕРИОД = "mara-sync"
        const val РАЗОВЫЙ = "mara-sync-once"

        /** Сверка раз в 15 минут — минимум, который разрешает WorkManager. */
        fun schedule(ctx: Context) {
            val сеть = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork(
                ПЕРИОД, ExistingPeriodicWorkPolicy.UPDATE,
                PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                    .setConstraints(сеть)
                    .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
                    .build()
            )
        }

        /**
         * Разовый прогон. После отбоя ставится с задержкой: рекордер дописывает
         * файл уже после того, как звонок закончился.
         */
        fun kick(ctx: Context, delaySec: Long = 0) {
            val b = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED).build())
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
            if (delaySec > 0) b.setInitialDelay(delaySec, TimeUnit.SECONDS)
            WorkManager.getInstance(ctx)
                .enqueueUniqueWork(РАЗОВЫЙ, ExistingWorkPolicy.REPLACE, b.build())
        }
    }
}
