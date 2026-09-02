package com.mara.capture

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.telephony.TelephonyManager

/** После перезагрузки расписание надо ставить заново (ТЗ §5.1F). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        SyncWorker.schedule(ctx)
        SyncWorker.kick(ctx)
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
        if (state == TelephonyManager.EXTRA_STATE_IDLE) SyncWorker.kick(ctx, delaySec = 60)
    }
}
