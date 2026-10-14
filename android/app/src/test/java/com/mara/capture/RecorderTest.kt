package com.mara.capture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/** Рекордер, прошедший бы полевую матрицу (R01, R05, R06) — такого сегодня
 *  нет; нужен, чтобы `healthy` в тестах здоровья был достижим. */
internal val НАДЁЖНЫЙ = Рекордер(
    id = "test_reliable", имя = "надёжный", пакеты = listOf("com.example.recorder"),
    статус = "healthy", риск = "—", готов = true,
)

/**
 * Т4.1, ТЗ §7.4, ADR-0007: провайдеры по способу доступа, рекордеры —
 * профилями из таблицы §7.1, манифест не обещает непроверенного, а
 * здоровье с ACR Phone + APH честно `degraded` (§8.5: reboot-unsafe —
 * известный риск).
 */
class RecorderTest {

    private val начало = 1_760_000_000_000L
    private val сутки = 24 * 3600_000L
    private val звонок = CallLogEntry("+79990000000", "Анна Петрова", "incoming", начало, 120)

    private fun файл(producer: String?, ms: Long = звонок.endMs) =
        Recording("uri://$ms/$producer", "call.m4a", 1, ms, producer)

    private fun приметы(
        записи: List<Recording>,
        рекордеры: List<Рекордер>,
        звонки: List<CallLogEntry> = listOf(звонок),
        журналЧитается: Boolean = true,
        сейчас: Long = начало + 3600_000,
        загрузка: Long = начало - сутки,
        наблюдениеС: Long = 0L,
        разрешенийНет: List<String> = emptyList(),
    ) = Приметы(
        сейчас = сейчас, загрузка = загрузка, разрешенийНет = разрешенийНет,
        папкаЧитается = null, рекордеры = рекордеры, звонки = звонки, записи = записи,
        расписаниеЖиво = true, свободноБайт = null, журналЧитается = журналЧитается,
        наблюдениеС = наблюдениеС,
    )

    // ── профили §7.1–7.2 ─────────────────────────────────────────────────

    @Test
    fun `ACR Phone + APH — degraded и reboot-unsafe, ничего непроверенного не обещает`() {
        val р = Рекордеры.ACR_PHONE
        assertEquals("degraded", р.статус)
        assertEquals(true, р.перезагрузкаЛомает)
        assertEquals(true, р.нуженПомощник)
        // ADR-0007 п.2: R01, R05, R06 не пройдены
        assertFalse(р.готов)
        assertTrue(р.режимыПроверены.isEmpty())
        assertEquals("unknown", р.протоколОтчёта)
        assertEquals("Documents/ACR/ACRPhone", р.папка)
        assertEquals("500", р.настройки["delay_ms"])
        assertEquals("off", р.настройки["auto_speakerphone"])
        assertTrue("ни один известный рекордер не готов", Рекордеры.ИЗВЕСТНЫЕ.none { it.готов })
    }

    @Test
    fun `системный рекордер Huawei звонков не пишет и рекордером звонков не считается`() {
        val найдены = listOf(Установлен(Рекордеры.HUAWEI, "1", true),
            Установлен(Рекордеры.ACR_PHONE, "2", false))
        assertTrue("стоящий Huawei и выключенный ACR здоровью не рекордеры",
            Рекордеры.вЗдоровье(найдены).isEmpty())
        assertNull(Рекордеры.чей(файл("com.huawei.soundrecorder"), emptyList()))
        assertEquals(Рекордеры.ACR_PHONE, Рекордеры.чей(файл("com.nll.cb"), emptyList()))
        assertNull(Рекордеры.чей(файл(null), listOf(Рекордеры.ACR_PHONE)))
        assertNull(Рекордеры.чей(файл("com.example.other"), listOf(Рекордеры.ACR_PHONE)))
        assertFalse("штатная звонилка — не профиль", Рекордеры.ИЗВЕСТНЫЕ.any { "com.huawei.contacts" in it.пакеты })
    }

    @Test
    fun `экран здоровья называет факты §7 словами`() {
        assertTrue(Рекордеры.словами(emptyList()).contains("§7.1"))
        val acr = Рекордеры.словами(listOf(Установлен(Рекордеры.ACR_PHONE, "1.2", true)))
        assertTrue(acr, acr.startsWith("ACR Phone + APH 1.2"))
        assertTrue(acr, acr.contains("degraded: reboot-unsafe"))
        assertTrue(acr, acr.contains("помощник: состояние не читается — unknown"))
        assertTrue(Рекордеры.словами(listOf(Установлен(Рекордеры.CUBE_ACR, null, false))).contains("выключен"))
    }

    @Test
    fun `сверка руками — зафиксированная конфигурация §7_2`() {
        val с = Рекордеры.сверитьРуками(Рекордеры.ACR_PHONE)!!
        assertTrue(с, с.contains("delay_ms=500"))
        assertTrue(с, с.contains("bitrate_kbps=192"))
        assertTrue(с, с.contains("папка=Documents/ACR/ACRPhone"))
        assertNull("нечего сверять", Рекордеры.сверитьРуками(Рекордеры.CUBE_ACR))
    }

    @Test
    fun `каждый пакет рекордеров объявлен в queries манифеста`() {
        // Android 11+ без `<queries>` пакет не покажет, и рекордер сойдёт за
        // неустановленный — так ACR Phone и был невидим до Т4.1
        var d: File? = File("").absoluteFile
        val путь = "android/app/src/main/AndroidManifest.xml"
        while (d != null && !File(d, путь).exists()) d = d.parentFile
        val манифест = File(requireNotNull(d) { "не нашёл $путь" }, путь).readText()
        val запросы = манифест.substringAfter("<queries>").substringBefore("</queries>")
        for (пакет in Рекордеры.ПАКЕТЫ)
            assertTrue("$пакет не в <queries>", запросы.contains("\"$пакет\""))
    }

    // ── манифест §7.4 ────────────────────────────────────────────────────

    @Test
    fun `манифест медиатеки с опознанным ACR — его значения, production_ready false`() {
        val м = Манифест.поКандидатам("mediastore", 1, "mediastore", null, true,
            listOf(файл("com.nll.cb"), файл("com.nll.cb"), файл(null)), emptyList())
        assertEquals(listOf(Рекордеры.ACR_PHONE), м.рекордеры)
        val j = м.json()
        for (ключ in listOf("provider_id", "provider_version", "status", "capture_modes_tested",
                "storage_mode", "storage_path", "requires_helper", "requires_post_reboot_recovery",
                "remote_report_protocol", "production_ready"))
            assertTrue("нет ключа §7.4 $ключ", j.has(ключ))
        assertEquals("mediastore", j.getString("provider_id"))
        assertEquals("degraded", j.getString("status"))
        assertEquals(0, j.getJSONArray("capture_modes_tested").length())
        assertEquals(true, j.get("requires_helper"))
        assertEquals(true, j.get("requires_post_reboot_recovery"))
        assertEquals("unknown", j.getString("remote_report_protocol"))
        assertEquals(false, j.get("production_ready"))
        assertEquals("unknown", j.getString("storage_path"))
        assertEquals("500", j.getJSONObject("config_expected").getString("delay_ms"))
    }

    @Test
    fun `манифест папки SAF — владелец не виден, всё неизвестное названо unknown`() {
        val j = Манифест.поКандидатам("saf_folder", 1, "folder", "primary:Documents/ACR/ACRPhone",
            false, listOf(файл(null)), listOf(Рекордеры.ACR_PHONE)).json()
        assertEquals(false, j.get("owner_known"))
        assertEquals("unknown", j.getString("status"))
        assertEquals("unknown", j.get("requires_helper"))
        assertEquals(false, j.get("production_ready"))
        assertFalse(j.has("config_expected"))
        assertEquals("primary:Documents/ACR/ACRPhone", j.getString("storage_path"))
    }

    @Test
    fun `два опознанных — конкретики нет, готов только если готовы все`() {
        val два = Манифест("mediastore", 1, "mediastore", null, true,
            listOf(Рекордеры.ACR_PHONE, НАДЁЖНЫЙ)).json()
        assertEquals("unknown", два.getString("status"))
        assertEquals(false, два.get("production_ready"))
        val готов = Манифест("mediastore", 1, "mediastore", null, true, listOf(НАДЁЖНЫЙ)).json()
        assertEquals(true, готов.get("production_ready"))
        // JSON — для отчёта мастера; разбирается обратно без потерь
        assertEquals("healthy", JSONObject(готов.toString()).getString("status"))
    }

    // ── здоровье с рекордером (§8.5, §8.6) ───────────────────────────────

    @Test
    fun `контрольный звонок записал ACR — degraded с reboot-unsafe, а не healthy`() {
        val о = Здоровье.оценить(приметы(listOf(файл("com.nll.cb")), listOf(Рекордеры.ACR_PHONE)))
        assertEquals(Состояние.degraded, о.состояние)
        assertTrue(о.причина, о.причина.contains("ACR Phone + APH — reboot-unsafe"))
        assertNull(о.тревога)
    }

    @Test
    fun `надёжный по файлу — healthy, даже если рядом стоит ACR`() {
        val о = Здоровье.оценить(приметы(listOf(файл(НАДЁЖНЫЙ.пакеты[0])),
            listOf(Рекордеры.ACR_PHONE, НАДЁЖНЫЙ)))
        assertEquals(Состояние.healthy, о.состояние)
        // а если тот же звонок писали оба — риск ACR не прячется
        val оба = Здоровье.оценить(приметы(listOf(файл(НАДЁЖНЫЙ.пакеты[0]), файл("com.nll.cb")),
            listOf(Рекордеры.ACR_PHONE, НАДЁЖНЫЙ)))
        assertEquals(Состояние.degraded, оба.состояние)
    }

    @Test
    fun `чей файл не видно — надёжность не доказана`() {
        // SAF владельца не сообщает: стоящий ACR назван подозреваемым
        val saf = Здоровье.оценить(приметы(listOf(файл(null)), listOf(Рекордеры.ACR_PHONE)))
        assertEquals(Состояние.degraded, saf.состояние)
        assertTrue(saf.причина, saf.причина.contains("рекордер по файлу не опознан; стоит ACR Phone + APH"))
        // и при одном надёжном установленном — пакет не доказательство (ADR-0007 п.6)
        val пакет = Здоровье.оценить(приметы(listOf(файл(null)), listOf(НАДЁЖНЫЙ)))
        assertEquals(Состояние.degraded, пакет.состояние)
        assertTrue(пакет.причина, пакет.причина.contains("не опознан — надёжность не доказана"))
        // файл диктофона Huawei — не запись звонка: звонок без записи, тревога
        val huawei = Здоровье.оценить(приметы(listOf(файл("com.huawei.soundrecorder")), listOf(НАДЁЖНЫЙ)))
        assertEquals(Состояние.unhealthy, huawei.состояние)
        assertEquals(звонок.startMs, huawei.тревога)
    }

    @Test
    fun `после перезагрузки с ACR — unknown с предупреждением и стойкое напоминание`() {
        val без = приметы(emptyList(), listOf(Рекордеры.ACR_PHONE), звонки = emptyList())
        val о = Здоровье.оценить(без)
        assertEquals(Состояние.unknown, о.состояние)
        assertTrue(о.причина, о.причина.contains("ACR Phone + APH после перезагрузки может не писать"))
        assertTrue(Здоровье.контрольныйЗвонок(о, без)!!.contains("ACR Phone + APH"))
        // звонок после перезагрузки был — дальше говорит его исход
        val сЗвонком = приметы(listOf(файл("com.nll.cb")), listOf(Рекордеры.ACR_PHONE))
        assertNull(Здоровье.контрольныйЗвонок(Здоровье.оценить(сЗвонком), сЗвонком))
        // журнал не прочитался — снять напоминание было бы нечем
        val безЖурнала = без.copy(журналЧитается = false)
        assertNull(Здоровье.контрольныйЗвонок(Здоровье.оценить(безЖурнала), безЖурнала))
        // рекордер, заведомо переживающий перезагрузку, — напоминать не о чем
        val надёжный = без.copy(рекордеры = listOf(НАДЁЖНЫЙ.copy(перезагрузкаЛомает = false)))
        assertNull(Здоровье.контрольныйЗвонок(Здоровье.оценить(надёжный), надёжный))
        assertFalse(Здоровье.оценить(надёжный).причина.contains("может не писать"))
    }

    @Test
    fun `без видимой перезагрузки напоминания нет и причина не врёт`() {
        // телефон работает месяц, звонков за неделю нет — «после перезагрузки» было бы неправдой
        val давно = приметы(emptyList(), listOf(Рекордеры.ACR_PHONE), звонки = emptyList(),
            загрузка = начало - 30 * сутки)
        val о = Здоровье.оценить(давно)
        assertEquals(Состояние.unknown, о.состояние)
        assertFalse(о.причина, о.причина.contains("после перезагрузки"))
        assertTrue(о.причина, о.причина.contains("контрольный звонок"))
        assertNull(Здоровье.контрольныйЗвонок(о, давно))
        // приложение поставили после загрузки — это не перезагрузка под наблюдением
        val поставили = давно.copy(загрузка = начало - сутки, наблюдениеС = начало - 3600_000)
        val оП = Здоровье.оценить(поставили)
        assertTrue(оП.причина, оП.причина.startsWith("с начала наблюдения"))
        assertNull(Здоровье.контрольныйЗвонок(оП, поставили))
        // захват сломан — звонок ничего не проверит, говорит «Захват сломан»
        val сломан = приметы(emptyList(), listOf(Рекордеры.ACR_PHONE), звонки = emptyList(),
            разрешенийНет = listOf("READ_CALL_LOG"))
        assertNull(Здоровье.контрольныйЗвонок(Здоровье.оценить(сломан), сломан))
    }

    @Test
    fun `Cube ACR — непроверенное названо unknown, после перезагрузки под подозрением`() {
        val j = Манифест.поКандидатам("mediastore", 1, "mediastore", null, true,
            listOf(файл("com.catalinagroup.callrecorder")), emptyList()).json()
        assertEquals("degraded", j.getString("status"))
        assertEquals("unknown", j.get("requires_helper"))
        assertEquals("unknown", j.get("requires_post_reboot_recovery"))
        assertEquals(false, j.get("production_ready"))
        val без = приметы(emptyList(), listOf(Рекордеры.CUBE_ACR), звонки = emptyList())
        assertTrue(Здоровье.контрольныйЗвонок(Здоровье.оценить(без), без)!!.contains("Cube ACR"))
        assertFalse("помощник не обещан", Рекордеры.словами(
            listOf(Установлен(Рекордеры.CUBE_ACR, null, true))).contains("помощник"))
    }

    // ── сценарии ревью Т4.1: защита от ложного зелёного ─────────────────

    @Test
    fun `файл готового рекордера не за контрольным звонком — не доказательство`() {
        // за звонком — файл без владельца (SAF), а файл готового рекордера —
        // до перезагрузки: опознаётся только писавший за этим звонком
        val о = Здоровье.оценить(приметы(listOf(файл(null),
            файл(НАДЁЖНЫЙ.пакеты[0], начало - 2 * сутки)), listOf(НАДЁЖНЫЙ)))
        assertEquals(о.причина, Состояние.degraded, о.состояние)
    }

    @Test
    fun `решает последний контрольный звонок, а не первый`() {
        val второй = звонок.copy(startMs = звонок.endMs + 3600_000)
        val о = Здоровье.оценить(приметы(listOf(файл(НАДЁЖНЫЙ.пакеты[0]), файл("com.nll.cb", второй.endMs)),
            listOf(НАДЁЖНЫЙ, Рекордеры.ACR_PHONE), звонки = listOf(звонок, второй),
            сейчас = второй.endMs + 3600_000))
        assertEquals(о.причина, Состояние.degraded, о.состояние)
        assertTrue(о.причина, о.причина.contains("ACR Phone + APH"))
    }

    @Test
    fun `поздняя копия с номером за контрольным звонком — тоже писавший`() {
        // запись с номером звонка в пути дописана через полчаса после отбоя
        // (за окном): за этим звонком она всё равно, и рекордер опознаётся
        val поздняя = Recording("content://x/doc/primary%3AACR%2F%2B79990000000%2Fx.m4a", "x.m4a", 1,
            звонок.endMs + 30 * 60_000, НАДЁЖНЫЙ.пакеты[0])
        val о = Здоровье.оценить(приметы(listOf(поздняя), listOf(НАДЁЖНЫЙ),
            сейчас = звонок.endMs + 3600_000))
        assertEquals(о.причина, Состояние.healthy, о.состояние)
    }

    @Test
    fun `без рекордеров — своя причина, а не «не опознан»`() {
        val о = Здоровье.оценить(приметы(listOf(файл(НАДЁЖНЫЙ.пакеты[0])), emptyList()))
        assertEquals(Состояние.degraded, о.состояние)
        assertTrue(о.причина, о.причина.contains("не установлен или выключен"))
    }

    @Test
    fun `заметка диктофона звонок не покрывает`() {
        // ACR после перезагрузки не записал, а владелец в окне надиктовал
        // заметку: прежде она закрывала пропажу по времени, и тревоги не было
        val п = приметы(listOf(файл("com.huawei.soundrecorder")), listOf(Рекордеры.ACR_PHONE),
            сейчас = звонок.endMs + Здоровье.ОКНО_МС + 1)
        assertEquals(listOf(звонок), Здоровье.безЗаписи(п))
        assertEquals(звонок.startMs, Здоровье.оценить(п).тревога)
        // файл без владельца (SAF) — по-прежнему по времени
        assertTrue(Здоровье.безЗаписи(п.copy(записи = listOf(файл(null)))).isEmpty())
    }

    // ── медиатека и отчёт ────────────────────────────────────────────────

    @Test
    fun `медиатека берёт файлы ACR по владельцу, хоть в пути приметы нет`() {
        assertTrue(Рекордеры.вМедиатеке("Documents/ACR/ACRPhone/2026/10/14/+79990000000/x.m4a", "com.nll.cb"))
        assertFalse("чужой плеер без приметы — не кандидат",
            Рекордеры.вМедиатеке("Music/album/track.m4a", "com.example.player"))
        assertFalse("диктофон Huawei звонков не пишет",
            Рекордеры.вМедиатеке("Sounds/memo.m4a", "com.huawei.soundrecorder"))
        assertTrue("примета в пути — как раньше", Рекордеры.вМедиатеке("Sounds/CallRecord/x.m4a", null))
    }

    @Test
    fun `путь папки в отчёте — формой`() {
        assertEquals("primary:Documents/ACR/ACRPhone",
            Затирание.папка("primary:Documents/ACR/ACRPhone"))
        assertEquals("primary:Documents/ACR/ACRPhone/<…>/<…>/<…>",
            Затирание.папка("primary:Documents/ACR/ACRPhone/2026/+79990000000/Анна"))
        assertEquals("unknown", Затирание.папка(null))
        assertEquals("unknown", Затирание.папка("unknown"))
    }
}
