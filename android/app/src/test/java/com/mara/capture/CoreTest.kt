package com.mara.capture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.time.ZoneId

/** Решения приложения проверяются на JVM: телефона у нас нет. */
class CoreTest {

    private val мск = ZoneId.of("Europe/Moscow")
    private val начало = 1_788_347_100_000L   // 2026-09-02T14:05:00+03:00
    private val звонок = CallLogEntry("+79990000000", "Анна Петрова", "incoming", начало, 1091)
    private val файл = Recording("uri://1", "call.m4a", 4_210_688, звонок.endMs)
    private val ША = "9f2c4a1e0b6d8837f5a1c9e2b4d70a3c6e8f1b2d4a6c8e0f2a4c6e8b0d2f4a6c"

    // ── обход выбранной папки ─────────────────────────────────────────────

    /** Узел выдуманного дерева: обход не знает ни SAF, ни файловой системы. */
    private data class Узел(
        val имя: String,
        val папка: Boolean = false,
        val дети: List<Узел> = emptyList(),
        val размер: Long = 1,
    )

    private fun каталог(имя: String, vararg дети: Узел) = Узел(имя, true, дети.toList())

    /** Дерево ACR: `[гггг]/[ММ]/[дд]/[номер телефона]/` и записи на дне. */
    private val дерево = каталог(
        "корень",
        каталог("2026",
            каталог("09",
                каталог("06", каталог("+79990000000", Узел("вчерашний.m4a"))),
                каталог("07",
                    каталог("+79990000000", Узел("первый.m4a"), Узел("второй.m4a")),
                    каталог("+79990000001", Узел("третий.m4a")),
                ),
            ),
        ),
    )

    private fun обход(корень: Узел, глубина: Int = 6, каталогов: Int = 5000) =
        Дерево.файлы(корень, { it.папка }, { it.дети }, { it.имя }, глубина, каталогов)
            .map { it.имя }

    @Test
    fun `записи ACR лежат на пятом уровне, и обход их находит`() {
        // Пять — это `2026/09/07/+79990000000/первый.m4a` от выбранной папки:
        // четыре каталога и сам файл. Потолок глубины меряется так же.
        assertEquals(
            listOf("вчерашний.m4a", "первый.m4a", "второй.m4a", "третий.m4a").sorted(),
            обход(дерево).sorted(),
        )
    }

    @Test
    fun `файлы в самом корне обход не теряет`() {
        // Плоская раскладка — то, что работало до обхода: рекордер кладёт
        // записи прямо в выбранную папку. Сломать её ценой ACR нельзя.
        val плоско = каталог("корень", Узел("a.m4a"), Узел("b.m4a"))
        assertEquals(listOf("a.m4a", "b.m4a"), обход(плоско))
        val вперемешку = каталог("корень", Узел("свой.m4a"), дерево.дети[0])
        assertTrue("свой.m4a" in обход(вперемешку))
        assertTrue("третий.m4a" in обход(вперемешку))
    }

    @Test
    fun `ниже потолка глубины обход не спускается`() {
        assertEquals(emptyList<String>(), обход(дерево, глубина = 4))
        assertEquals(4, обход(дерево, глубина = 5).size)
    }

    @Test
    fun `потолок каталогов останавливает обход, а не только выдачу`() {
        val широко = каталог("корень",
            *(1..3).map { д ->
                каталог("д$д", *(1..10).map { Узел("$д-$it.m4a") }.toTypedArray())
            }.toTypedArray())
        var спрошено = 0
        val взято = Дерево.файлы(широко, { it.папка },
            { спрошено++; it.дети }, { it.имя }, каталогов = 2)
        // Раскрыто два каталога: корень и один из трёх. Раскрытие каталога у
        // SAF — запрос к провайдеру, поэтому важно, что за оставшимися мы уже
        // не полезли, а не только что не сложили их в ответ.
        assertEquals(10, взято.size)
        assertEquals("детей спрашивали только у корня и одного каталога", 2, спрошено)
    }

    @Test
    fun `дефолты годятся для ACR, а не только явные потолки`() {
        // Прод зовёт `Дерево.файлы` без именованных потолков (`Device.folder`),
        // и до этого теста дефолты были мёртвой зоной: мутанты `глубина = 4` и
        // нулевой потолок каталогов переживали весь набор.
        assertEquals(
            listOf("вчерашний.m4a", "первый.m4a", "второй.m4a", "третий.m4a").sorted(),
            Дерево.файлы(дерево, { it.папка }, { it.дети }, { it.имя })
                .map { it.имя }.sorted(),
        )
    }

    @Test
    fun `при тесном потолке записи находятся, а не съедаются каталогами`() {
        // Месяц ACR: тридцать дней, в каждом каталог номера и запись. Потолок
        // тесный нарочно — так видно, на что он тратится.
        //
        // Вглубь: корень, `2026`, `09`, один день, один номер — пять
        // раскрытий, и запись найдена. Вширь: корень, `2026`, `09`, потом дни
        // подряд — потолок кончится на днях, и записей не будет ни одной.
        // Ровно это и происходило на архиве ACR за пару лет, только там
        // потолок был штатный, а каталогов хватало своих.
        val месяц = каталог("корень",
            каталог("2026", каталог("09", *(1..30).map { день ->
                каталог("$день", каталог("+7999000$день", Узел("$день.m4a")))
            }.toTypedArray())))
        assertTrue("потолок ушёл в каталоги, записей ноль",
            обход(месяц, каталогов = 10).isNotEmpty())
    }

    @Test
    fun `файлы не съедают потолок каталогов`() {
        // Шесть тысяч записей в корне и один вложенный каталог за ними. Если
        // файлы считать наравне с каталогами, потолок кончится на файлах, и
        // за вложенный каталог обход уже не полезет — а форма дерева тут ни
        // при чём: неизвестна она, а не число файлов в одной папке.
        val смесь = каталог("корень",
            *((1..6000).map { Узел("$it.m4a") } +
                каталог("вложенный", Узел("глубокий.m4a"))).toTypedArray())
        assertTrue("глубокий.m4a" in обход(смесь))
    }

    @Test
    fun `что отрежет потолок, решаем мы, а не порядок листинга`() {
        // Порядок `listFiles` у SAF не оговорён ничем: `ExternalStorageProvider`
        // отдаёт порядок `readdir`. Если обход берёт детей как дали, то при
        // тесном потолке на большом архиве найдётся то, что провайдер вернул
        // первым, — а это может быть 2019 год, и сегодняшний звонок не
        // найдётся никогда. Каталоги ACR названы датами, поэтому спускаемся
        // от старших имён к младшим: потолок тратится на свежее.
        val дни = (1..30).map { день ->
            каталог("%02d".format(день), каталог("+79990000000", Узел("$день.m4a")))
        }
        fun месяц(порядок: List<Узел>) =
            каталог("корень", каталог("2026", каталог("09", *порядок.toTypedArray())))
        assertEquals("порядок листинга решил, что найдётся",
            обход(месяц(дни), каталогов = 10), обход(месяц(дни.reversed()), каталогов = 10))
        assertEquals("потолок ушёл на старые дни",
            listOf("30.m4a", "29.m4a", "28.m4a"), обход(месяц(дни), каталогов = 10))
    }

    @Test
    fun `плоская папка крупнее потолка отдаётся целиком`() {
        // Штатный рекордер пишет плоско, и до обхода такая папка отдавалась
        // вся. Потолок, считавший файлы, молча резал бы хвост, а очередь
        // каждые пятнадцать минут видела бы одно и то же начало.
        val много = каталог("корень",
            *(1..7000).map { Узел("$it.m4a") }.toTypedArray())
        assertEquals(7000,
            Дерево.файлы(много, { it.папка }, { it.дети }, { it.имя }).size)
    }

    @Test
    fun `имя каталога спрашиваем по разу, а не на каждом сравнении`() {
        // У SAF `name` — такой же запрос к провайдеру, как `listFiles`.
        // Сортировка с селектором (`sortBy { имя(it) }`) зовёт его на каждом
        // сравнении: на два десятка подпапок это впятеро больше запросов,
        // чем нужно, и вся экономия потолка уходит обратно.
        val много = каталог("корень",
            *(1..20).map { каталог("%02d".format(it)) }.toTypedArray())
        var спрошено = 0
        Дерево.файлы(много, { it.папка }, { it.дети }, { спрошено++; it.имя })
        assertEquals("имя каталога спросили лишний раз", 20, спрошено)
    }

    private fun записи(корень: Узел) =
        Дерево.записи(корень, { it.папка }, { it.дети }, { it.имя }, { it.размер })
            .map { it.second }

    @Test
    fun `сборка записи не спрашивает то, что отбор уже узнал`() {
        // `Device.folder` строит `Recording` из того, что вернул отбор. Пока
        // отбор отдавал одни узлы, имя и размер каждой записи спрашивались у
        // провайдера заново — два лишних запроса на файл, а на плоской папке
        // файлы это всё, что там есть.
        val плоско = каталог("корень",
            *(1..10).map { Узел("$it.m4a", размер = 100L + it) }.toTypedArray())
        var спрошено = 0
        val найдено = Дерево.записи(плоско, { it.папка }, { it.дети },
            { спрошено++; it.имя }, { спрошено++; it.размер })
        val записи = найдено.map { (_, имя, байт) -> имя to байт }
        assertEquals("отбор потерял имя или размер",
            listOf("1.m4a" to 101L), записи.take(1))
        assertEquals("на сборку записи ушёл лишний запрос", 20, спрошено)
    }

    @Test
    fun `пустой файл не платит за запрос имени`() {
        val плоско = каталог("корень", Узел("недописанный.m4a", размер = 0))
        var имён = 0
        Дерево.записи(плоско, { it.папка }, { it.дети },
            { имён++; it.имя }, { it.размер })
        assertEquals("имя спросили у файла, который и так отсеян", 0, имён)
    }

    @Test
    fun `буквенный сосед съедает потолок раньше дерева дат`() {
        // Не поведение, которое хочется, а поведение, которое есть, — и
        // которое поэтому названо в §12 тела PR. Потолок каталогов один на
        // всё поддерево, а подпапки раскрываются от старших имён к младшим:
        // буква стоит выше цифры, значит каталог `Сканы` снимается со стопки
        // раньше `2026` и тратит бюджет первым.
        val даты = каталог("2026", каталог("09", каталог("07",
            каталог("+79990000000", Узел("звонок.m4a")))))
        fun рядом(подпапок: Int) = каталог("Documents",
            каталог("Сканы", *(1..подпапок).map { каталог("скан$it") }.toTypedArray()),
            даты)
        assertEquals("потолок достался соседу целиком",
            emptyList<String>(), записи(рядом(5000)))
        assertEquals("сосед поменьше дереву дат не мешает",
            listOf("звонок.m4a"), записи(рядом(10)))
    }

    @Test
    fun `на сервер уходит звук, а не всё поддерево целиком`() {
        // Мастер говорит «выбери папку с записями», и выберут не ту: обход
        // разворачивает выбранную папку в поддерево, так что без отбора
        // домашний сервер получил бы сканы паспорта заодно со звонками.
        val документы = каталог("Documents",
            Узел("паспорт.pdf"), Узел("фото.jpg"), Узел("заметка"),
            каталог("ACR", каталог("2026", каталог("09",
                каталог("07", каталог("+79990000000", Узел("звонок.m4a")))))))
        assertEquals(listOf("звонок.m4a"), записи(документы))
    }

    @Test
    fun `регистр расширения ничего не решает`() {
        // Заглавное расширение пишет не одна программа, а спотыкаться об
        // это отбор не должен: имя листа — всё, что у SAF видно.
        assertEquals(listOf("CALL.AMR"), записи(каталог("корень", Узел("CALL.AMR"))))
    }

    @Test
    fun `пустой файл за запись не считается`() {
        // Рекордер создаёт файл до того, как в нём что-то есть.
        assertEquals(emptyList<String>(),
            записи(каталог("корень", Узел("недописан.m4a", размер = 0))))
    }

    // ── контракт с сервером ───────────────────────────────────────────────

    @Test
    fun `событие совпадает с общим фиксом контракта`() {
        val ждём = JSONObject(fixture().readText())
        val есть = EventJson.build(файл, звонок, ША, "m4a", "com.huawei.soundrecorder", мск)
        assertEquals(canon(ждём), canon(есть))
    }

    @Test
    fun `ключом события служит хеш содержимого, а не имя файла`() {
        val переименован = файл.copy(name = "совсем другое имя.m4a")
        val a = EventJson.build(файл, звонок, ША, "m4a", null, мск).getString("source_id")
        val b = EventJson.build(переименован, звонок, ША, "m4a", null, мск).getString("source_id")
        assertEquals("переименование не должно порождать второй звонок", a, b)
    }

    @Test
    fun `без журнала звонков событие всё равно уходит`() {
        val ev = EventJson.build(файл, null, ША, "m4a", null, мск)
        assertEquals("call", ev.getString("kind"))
        assertFalse("времени конца взять неоткуда", ev.has("ended_at"))
        assertFalse("человека не выдумываем", ev.getJSONObject("payload").has("contact_name"))
    }

    @Test
    fun `contact_source проставлен, иначе карточка человека не заведётся`() {
        val p = EventJson.build(файл, звонок, ША, "m4a", null, мск).getJSONObject("payload")
        assertEquals("call-log", p.getString("contact_source"))
    }

    @Test
    fun `device_id приложение не шлёт`() {
        val ev = EventJson.build(файл, звонок, ША, "m4a", null, мск)
        assertFalse("сервер берёт устройство из токена", ev.has("device_id"))
    }

    // ── сопоставление с журналом ──────────────────────────────────────────

    @Test
    fun `берём ближайший звонок`() {
        val ранний = звонок.copy(startMs = начало - 3_600_000)
        val got = CallLogMatcher.nearest(listOf(ранний, звонок), звонок.endMs)
        assertEquals(звонок, got)
    }

    @Test
    fun `mtime в начале записи тоже сопоставляется`() {
        assertEquals(звонок, CallLogMatcher.nearest(listOf(звонок), звонок.startMs))
    }

    @Test
    fun `далёкий звонок не притягиваем`() {
        val далеко = звонок.endMs + CallLogMatcher.WINDOW_MS + 1
        assertNull("лучше без атрибуции, чем с чужой", CallLogMatcher.nearest(listOf(звонок), далеко))
    }

    @Test
    fun `пустой журнал не роняет`() {
        assertNull(CallLogMatcher.nearest(emptyList(), начало))
    }

    // ── готовность файла ──────────────────────────────────────────────────

    @Test
    fun `растущий файл не берём`() {
        val потом = файл.copy(sizeBytes = файл.sizeBytes + 4096)
        assertFalse(FileReady.ready(файл, потом, FileReady.QUIET_MS * 2))
    }

    @Test
    fun `не выждав тишины, не берём`() {
        assertFalse(FileReady.ready(файл, файл, FileReady.QUIET_MS - 1))
    }

    @Test
    fun `первый раз увиденный файл не берём`() {
        assertFalse("сравнивать не с чем", FileReady.ready(null, файл, FileReady.QUIET_MS * 2))
    }

    @Test
    fun `пустой файл не берём никогда`() {
        val пусто = файл.copy(sizeBytes = 0)
        assertFalse(FileReady.ready(пусто, пусто, FileReady.QUIET_MS * 2))
    }

    @Test
    fun `отлежавшийся файл берём`() {
        assertTrue(FileReady.ready(файл, файл, FileReady.QUIET_MS))
    }

    // ── очередь ───────────────────────────────────────────────────────────

    @Test
    fun `событие принято — грузим аудио`() {
        assertEquals(JobState.POSTED,
            JobFlow.next(JobState.HASHED, ServerReply(200, "call_1", needBlob = true)))
    }

    @Test
    fun `дубль без запроса блоба закрывает работу`() {
        assertEquals("аудио на сервере уже есть", JobState.DONE,
            JobFlow.next(JobState.HASHED, ServerReply(200, "call_1", needBlob = false)))
    }

    @Test
    fun `сервер не сошёлся хешем — считаем заново`() {
        assertEquals("файл дописали, пока мы его читали", JobState.NEW,
            JobFlow.next(JobState.POSTED, ServerReply(409)))
    }

    @Test
    fun `сеть легла — состояние не меняем`() {
        assertEquals(JobState.POSTED, JobFlow.next(JobState.POSTED, ServerReply(0)))
        assertEquals(JobState.HASHED, JobFlow.next(JobState.HASHED, ServerReply(503)))
    }

    @Test
    fun `плохой токен повтором не лечится`() {
        assertEquals(JobState.FAILED, JobFlow.next(JobState.HASHED, ServerReply(401)))
    }

    @Test
    fun `415 терминален — сервер не принял содержимое`() {
        // Сервер сверяет содержимое со списком видов (ТЗ §6.2) и непонятое
        // кладёт в карантин, отвечая 415. Повтор даст то же самое: файл не
        // изменится. Эта строка держит контракт, на который опирается сервер, —
        // разреши тут повтор, и телефон будет лить один и тот же файл в
        // карантин до конца батареи. Локальную копию `FAILED` не удаляет: её
        // разбирает владелец через мастер.
        assertEquals(JobState.FAILED, JobFlow.next(JobState.POSTED, ServerReply(415)))
        assertEquals(JobState.FAILED, JobFlow.next(JobState.HASHED, ServerReply(415)))
    }

    @Test
    fun `503 останавливает прогон`() {
        // Потолок потоков на устройство: следующая работа упрётся в тот же
        // отказ, и перебор очереди до конца — это мегабайты трафика ради
        // повторения одного и того же 503.
        assertTrue(JobFlow.пауза(ServerReply(503)))
    }

    @Test
    fun `ноль и прочие пятисотки прогон не останавливают`() {
        // Ноль — «сети нет»: прогон обрывается и без паузы, а ждать сеть
        // WorkManager умеет сам по условию сети. 500 — поломка на одной
        // работе, из-за которой стоять всей очереди незачем.
        assertFalse("сеть", JobFlow.пауза(ServerReply(0)))
        assertFalse("поломка на одной работе", JobFlow.пауза(ServerReply(500)))
        assertFalse(JobFlow.пауза(ServerReply(502)))
    }

    @Test
    fun `успех и отказы паузой не считаются`() {
        assertFalse(JobFlow.пауза(ServerReply(200, "call_1")))
        assertFalse(JobFlow.пауза(ServerReply(413)))
        assertFalse(JobFlow.пауза(ServerReply(401)))
    }

    // ── сообщения: уведомления и SMS (спека 8–9) ──────────────────────────

    private val ув = NotificationParse.Seen(
        pkg = "com.whatsapp", postMs = начало, key = "0|com.whatsapp|1|null|10123",
        summary = false, ongoing = false, title = "Анна Петрова", text = "Купи хлеб",
        conversationTitle = null, group = false,
        lines = listOf(NotificationParse.Line("Анна Петрова", "Купи хлеб", начало)),
    )

    @Test
    fun `ключ сообщения совпадает с общим фиксом импортёра`() {
        val f = JSONObject(fixture("whatsapp-message-id.json").readText())
        assertEquals("разъехались с scripts/whatsapp_import.py — дубли перестанут отсеиваться",
            f.getString("source_id"),
            MessageId.of(f.getString("package"), f.getString("chat"), f.getString("sender"),
                f.getString("text"), f.getLong("at_ms")))
    }

    @Test
    fun `сводка группы и постоянное уведомление — не сообщения`() {
        assertTrue(NotificationParse.messages(ув.copy(summary = true)).isEmpty())
        assertTrue(NotificationParse.messages(ув.copy(ongoing = true)).isEmpty())
    }

    @Test
    fun `WhatsApp без строк MessagingStyle — это «N новых», а не сообщение`() {
        assertTrue(NotificationParse.messages(ув.copy(lines = emptyList(), text = "3 новых сообщения")).isEmpty())
    }

    @Test
    fun `SMS-приложению без стиля разрешён заголовок плюс текст`() {
        val m = NotificationParse.messages(ув.copy(pkg = "com.huawei.message", lines = emptyList(),
            title = "+79990000000", text = "код 1234")).single()
        assertEquals("sms", m.source)
        assertEquals("+79990000000", m.sender)
        assertEquals("код 1234", m.text)
        assertEquals("время — момент показа", начало, m.atMs)
    }

    @Test
    fun `чужой пакет не читаем`() {
        assertTrue(NotificationParse.messages(ув.copy(pkg = "com.example.bank")).isEmpty())
    }

    @Test
    fun `группа берёт беседу из conversationTitle, отправителя из строки`() {
        val m = NotificationParse.messages(ув.copy(conversationTitle = "Семья", group = true)).single()
        assertEquals("Семья", m.chat)
        assertEquals("Анна Петрова", m.sender)
        assertTrue(m.group)
    }

    @Test
    fun `строка без отправителя — свой ответ из шторки`() {
        val m = NotificationParse.messages(ув.copy(lines = listOf(NotificationParse.Line(null, "ок", начало)))).single()
        assertTrue(m.outgoing)
        assertEquals("", m.sender)
    }

    @Test
    fun `перепост тех же строк даёт тот же ключ`() {
        val a = NotificationParse.messages(ув).single().id
        val b = NotificationParse.messages(ув.copy(key = "другой ключ", postMs = начало + 5_000)).single().id
        assertEquals("WhatsApp перепощивает беседу на каждое новое — дубль должен отсеяться", a, b)
    }

    @Test
    fun `пробелы по краям и внутри ключ не меняют, NBSP — меняет`() {
        val a = MessageId.of("p", "c", "s", " a \n  b ", 0)
        assertEquals(a, MessageId.of("p", "c", "s", "a b", 0))
        assertNotEquals("Unicode-нормализации нет: у JVM и Python она разная",
            a, MessageId.of("p", "c", "s", "a\u00a0b", 0))
    }

    @Test
    fun `в теле уведомления только поля §5_1C`() {
        val p = MessageJson.build(NotificationParse.messages(ув).single(), мск).getJSONObject("payload")
        assertEquals(setOf("text", "outgoing", "via", "package", "chat_title", "chat_type",
            "sender_name", "notification_key_hash"), p.keys().asSequence().toSet())
        assertEquals("хеш ключа, а не сам ключ", 64, p.getString("notification_key_hash").length)
    }

    @Test
    fun `SMS из провайдера — номер, имя, направление`() {
        val m = Message("sms", MessageId.sms("+79990000000", начало, 2, "еду"), chat = "Анна Петрова",
            sender = "", text = "еду", atMs = начало, outgoing = true, via = "provider",
            number = "+79990000000", threadId = 3, read = true)
        val p = MessageJson.build(m, мск).getJSONObject("payload")
        assertEquals("outgoing", p.getString("direction"))
        assertEquals("Анна Петрова", p.getString("contact_name"))
        assertEquals("+79990000000", p.getString("number"))
        assertFalse(p.has("chat_title"))
    }

    @Test
    fun `ключ SMS различает входящее и исходящее с тем же текстом`() {
        assertNotEquals(MessageId.sms("+7", начало, 1, "ок"), MessageId.sms("+7", начало, 2, "ок"))
    }

    @Test
    fun `доставка сообщений — 200 готово, сеть повтор, 401 и 400 сдаёмся`() {
        assertEquals(JobState.DONE, MessageFlow.next(ServerReply(200, "message_1")))
        assertEquals(JobState.NEW, MessageFlow.next(ServerReply(0)))
        assertEquals(JobState.NEW, MessageFlow.next(ServerReply(503)))
        assertEquals(JobState.FAILED, MessageFlow.next(ServerReply(401)))
        assertEquals(JobState.FAILED, MessageFlow.next(ServerReply(400)))
    }

    // ── вспомогательное ───────────────────────────────────────────────────

    /** Сортируем ключи: JSONObject их порядок не хранит, а сверять надо целиком. */
    private fun canon(o: Any?): String = when (o) {
        is JSONObject -> o.keys().asSequence().sorted()
            .joinToString(",", "{", "}") { "\"$it\":" + canon(o.get(it)) }
        is JSONArray -> (0 until o.length()).joinToString(",", "[", "]") { canon(o.get(it)) }
        is String -> "\"$o\""
        else -> o.toString()
    }

    private fun fixture(name: String = "phone-call-event.json"): File {
        var d: File? = File("").absoluteFile
        while (d != null && !File(d, "tests/fixtures/$name").exists()) d = d.parentFile
        return File(requireNotNull(d) { "не нашёл tests/fixtures — где корень репозитория?" },
            "tests/fixtures/$name")
    }
    // ── адрес сервера ─────────────────────────────────────────────────────

    @Test
    fun `по http пускаем только в свою сеть`() {
        assertNull(Адрес.беда("http://192.168.0.2:8788"))
        assertNull(Адрес.беда("http://10.0.0.5:8788"))
        assertNull(Адрес.беда("http://doctor.local:8788"))
        assertNull(Адрес.беда("https://mara.example.ru"))
        // наружу открытым текстом — туда уедет токен, а следом записи
        assertNotNull(Адрес.беда("http://mara.example.ru"))
        assertNotNull(Адрес.беда("http://8.8.8.8:8788"))
        assertNotNull(Адрес.беда("ftp://192.168.0.2"))
        assertNotNull(Адрес.беда("192.168.0.2:8788"))
        assertNotNull(Адрес.беда(""))
        // 172.16/12 — частная, 172.32 уже нет
        assertNull(Адрес.беда("http://172.20.0.3:8788"))
        assertNotNull(Адрес.беда("http://172.32.0.3:8788"))
        // слэш на конце склеится в двойной: base + "/v1/..."
        assertNotNull(Адрес.беда("https://mara.example.ru/"))
    }

}
