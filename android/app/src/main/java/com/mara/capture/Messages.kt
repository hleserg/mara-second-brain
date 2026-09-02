package com.mara.capture

import android.Manifest
import android.app.Notification
import android.os.Parcelable
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import java.time.ZoneId

/**
 * Слушатель уведомлений — единственный честный источник WhatsApp без root
 * (ТЗ §13) и запасной путь для SMS (§14). Из уведомления берётся только
 * перечисленное в §5.1C: ни картинок, ни иконок, ни Person-URI. Решения —
 * в NotificationParse, здесь только распаковка extras в значения.
 */
class MessageListener : NotificationListenerService() {

    /** Huawei убивает слушатель. После воскрешения перечитываем то, что ещё
     *  висит в шторке — снятые за время смерти уведомления потеряны, и это
     *  одна из дыр, которые спека признаёт. */
    override fun onListenerConnected() {
        runCatching { activeNotifications?.forEach { принять(it) } }
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        runCatching { принять(sbn) }
    }

    private fun принять(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        if (pkg !in NotificationParse.WHATSAPP && pkg !in NotificationParse.SMS_APPS) return
        // режимы SMS взаимоисключающие: есть READ_SMS — читаем провайдер,
        // иначе одно и то же SMS легло бы под двумя ключами
        if (pkg in NotificationParse.SMS_APPS && Device.granted(this, Manifest.permission.READ_SMS)) return
        val msgs = NotificationParse.messages(seen(sbn))
        if (msgs.isEmpty()) return
        val q = Queue(this)
        val now = System.currentTimeMillis()
        val zone = ZoneId.systemDefault()
        // WhatsApp на каждое новое перепостит последние несколько — дедуп по ключу
        val новых = msgs.count { q.put(it, MessageJson.build(it, zone), now) }
        if (новых == 0) return
        val s = runCatching { Settings(this) }.getOrNull() ?: return
        msgs.filter { it.source == "sms" }.maxOfOrNull { it.atMs }
            ?.let { s.lastSmsNotificationMs = maxOf(s.lastSmsNotificationMs, it) }
        if (s.paired) SyncWorker.kick(this, delaySec = 30)   // подождать соседей, слать пачкой
    }

    private fun seen(sbn: StatusBarNotification): NotificationParse.Seen {
        val n = sbn.notification
        val e = n.extras
        @Suppress("DEPRECATION")
        return NotificationParse.Seen(
            pkg = sbn.packageName, postMs = sbn.postTime, key = sbn.key,
            summary = n.flags and Notification.FLAG_GROUP_SUMMARY != 0,
            ongoing = n.flags and Notification.FLAG_ONGOING_EVENT != 0,
            title = e.getCharSequence(Notification.EXTRA_TITLE)?.toString(),
            text = e.getCharSequence(Notification.EXTRA_TEXT)?.toString(),
            conversationTitle = e.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)?.toString(),
            group = e.getBoolean(Notification.EXTRA_IS_GROUP_CONVERSATION, false),
            lines = lines(e.getParcelableArray(Notification.EXTRA_MESSAGES)),
        )
    }

    /** Строки MessagingStyle. Без отправителя — своё (ответ из шторки). */
    @Suppress("DEPRECATION")
    private fun lines(arr: Array<Parcelable>?): List<NotificationParse.Line> {
        if (arr == null) return emptyList()
        return Notification.MessagingStyle.Message.getMessagesFromBundleArray(arr).map { m ->
            NotificationParse.Line((m.senderPerson?.name ?: m.sender)?.toString(),
                m.text?.toString(), m.timestamp)
        }
    }
}
