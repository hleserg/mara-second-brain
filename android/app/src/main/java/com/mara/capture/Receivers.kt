package com.mara.capture

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager

/** После перезагрузки расписание надо ставить заново (ТЗ §5.1F), и тут же
 *  — health workflow (ТЗ §8.6, Т4.2): через две минуты, когда система
 *  доставит разрешения и SAF-гранты, а не в первую секунду после загрузки.
 *  То же после обновления APK (`MY_PACKAGE_REPLACED`): иначе новая проверка
 *  здоровья ждала бы перезагрузки или открытого экрана (Codex по #137). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        SyncWorker.schedule(ctx)
        SyncWorker.kick(ctx)
        HealthWorker.schedule(ctx)
        HealthWorker.kick(ctx, delaySec = 120)
    }
}

/**
 * Разговор кончился — через минуту смотрим папку. Иначе запись ждала бы
 * очередной четвертьчасовой сверки.
 *
 * Минута, а не сразу: рекордер дописывает файл уже после отбоя, и признак
 * готовности всё равно потребует тишины (FileReady).
 */
class PhoneStateReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
        if (state == TelephonyManager.EXTRA_STATE_IDLE) {
            SyncWorker.kick(ctx, delaySec = 60)
            // §8.4: окно вышло — проверить, оставил ли рекордер файл, не дожидаясь часа
            HealthWorker.kick(ctx, delaySec = Здоровье.ОКНО_МС / 1000 + 60)
        }
    }
}
