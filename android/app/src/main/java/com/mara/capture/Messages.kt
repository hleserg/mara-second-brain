package com.mara.capture

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
    /** Хеш и SQLite — не на главном потоке: после воскрешения слушатель
     *  перечитывает всю шторку разом, а Huawei на ANR скор. */
    override fun onListenerConnected() {
        val висят = runCatching { activeNotifications?.toList() }.getOrNull() ?: return
        Thread { висят.forEach { runCatching { принять(it) } } }.start()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        Thread { runCatching { принять(sbn) } }.start()
    }

    private fun принять(sbn: StatusBarNotification) {
        val pkg = sbn.packageName
        if (pkg !in NotificationParse.WHATSAPP && pkg !in NotificationParse.SMS_APPS) return
        // Keystore отказал — теряем только учёт, сообщения всё равно в очередь
        val s = runCatching { Settings(this) }.getOrNull()
        // режимы SMS взаимоисключающие, и решает не разрешение, а то, отдал ли
        // провайдер: иначе одно SMS легло бы под двумя ключами — или ни под одним;
        // режим неизвестен — SMS не трогаем, дубли хуже
        if (pkg in NotificationParse.SMS_APPS && s?.smsDirect != false) return
        val msgs = NotificationParse.messages(seen(sbn))
        if (msgs.isEmpty()) return
        msgs.firstOrNull { it.source == "whatsapp" }?.let { m -> s?.lastChatTitle = m.chat }
        val q = Queue(this)
        val now = System.currentTimeMillis()
        val zone = ZoneId.systemDefault()
        // WhatsApp на каждое новое перепостит последние несколько — дедуп по ключу
        val новых = msgs.count { q.put(it, MessageJson.build(it, zone), now) }
        if (новых == 0) return
        if (s == null) return
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
