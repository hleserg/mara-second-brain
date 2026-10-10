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

    // ── сопоставление по номеру (Т4.3) ────────────────────────────────────

    // недозвон соседке через минуту после разговора с Анной: по времени ближе он
    private val соседка = CallLogEntry("+79990000001", "Борис", "outgoing", звонок.endMs + 60_000, 0)
    private val uriACR = "content://com.android.externalstorage.documents/document/" +
        "primary%3ARecord%2F2026%2F09%2F02%2F%2B79990000000%2Fcall.m4a call.m4a"

    @Test
    fun `номер в пути записи перебивает соседа по времени`() {
        // mtime — у недозвона соседке, но каталог ACR назван номером Анны
        val ms = соседка.startMs
        val м = CallLogMatcher.match(listOf(звонок, соседка), ms, uriACR)
        assertEquals(звонок, м?.entry)
        assertEquals("number", м?.by)
        assertEquals("без подсказки — как раньше, ближайший", соседка, CallLogMatcher.nearest(listOf(звонок, соседка), ms))
    }

    @Test
    fun `номер в относительном пути медиатеки — тоже подсказка`() {
        // медиатека отдаёт uri без пути (`media/42`), номер ACR — в RELATIVE_PATH
        val изМедиатеки = Recording("content://media/external/audio/media/42", "call.m4a", 4_210_688,
            соседка.startMs, "com.nll.cb", "Music/Recordings/2026/09/02/+79990000000/")
        val м = CallLogMatcher.match(listOf(звонок, соседка), изМедиатеки.modifiedMs, изМедиатеки.подсказка())
        assertEquals(звонок, м?.entry)
        assertEquals("number", м?.by)
        val безПути = изМедиатеки.copy(path = null)
        assertEquals("без пути — по времени", соседка,
            CallLogMatcher.match(listOf(звонок, соседка), безПути.modifiedMs, безПути.подсказка())?.entry)
    }

    @Test
    fun `нет номера в подсказке — по времени, и это сказано`() {
        val м = CallLogMatcher.match(listOf(звонок, соседка), звонок.endMs, "content://media/external/audio/media/42 call.m4a")
        assertEquals(звонок, м?.entry)
        assertEquals("time", м?.by)
        assertNull(CallLogMatcher.match(listOf(звонок), звонок.endMs + CallLogMatcher.WINDOW_MS + 1, uriACR))
    }

    @Test
    fun `номер вне окна по времени не притягивается и по номеру`() {
        val давно = звонок.copy(startMs = начало - 3_600_000)
        assertNull(CallLogMatcher.match(listOf(давно), начало + 7_200_000, uriACR))
    }

    @Test
    fun `скрытый номер и короткие цифры номером не считаются`() {
        assertNull(CallLogMatcher.хвостНомера(null))
        assertNull(CallLogMatcher.хвостНомера("123456"))
        assertEquals("9990000000", CallLogMatcher.хвостНомера("+7 (999) 000-00-00"))
        assertEquals("9990000000", CallLogMatcher.хвостНомера("89990000000"))
        assertFalse(CallLogMatcher.номерВ(uriACR, null))
        assertTrue(CallLogMatcher.номерВ(uriACR, "8 999 000 00 00"))
        assertTrue("без %XX тоже", CallLogMatcher.номерВ("2026/09/02/+79990000000/call.m4a", "+79990000000"))
        assertFalse("кривой процент не роняет", CallLogMatcher.номерВ("%ZZ nope", "+79990000000"))
        assertFalse("цифры через `/` не склеиваются",
            CallLogMatcher.номерВ("2026/09/9900000/00.m4a", "+79990000000"))
        assertTrue("номер с пробелами и дефисами в имени — один кусок",
            CallLogMatcher.номерВ("call +7 (999) 000-00-00 in.m4a", "+79990000000"))
    }

    @Test
    fun `непрозрачный uri медиатеки — не номер`() {
        // номер строки медиатеки совпал бы с коротким местным номером
        val запись = Recording("content://media/external/audio/media/1234567", "call.m4a", 1024, начало)
        assertEquals("call.m4a", запись.подсказка())
        val местный = звонок.copy(number = "1234567")
        assertEquals("time", CallLogMatcher.match(listOf(местный), начало, запись.подсказка())?.by)
        assertTrue("uri SAF несёт путь — остаётся", файл.подсказка().startsWith("uri://1/"))
    }

    @Test
    fun `два звонка на один номер в окне — уверенность по времени`() {
        // разговор и перезвон на тот же номер сразу после: номер не различает
        val перезвон = звонок.copy(direction = "outgoing", startMs = звонок.endMs, durationS = 0)
        val м = CallLogMatcher.match(listOf(звонок, перезвон), звонок.endMs, uriACR)
        assertEquals("time", м?.by)
        assertTrue(м?.entry == звонок || м?.entry == перезвон)
        assertEquals("а соседку номер всё равно отсекает", "time",
            CallLogMatcher.match(listOf(соседка, звонок, перезвон), перезвон.startMs, uriACR)?.by)
        assertNotEquals(соседка, CallLogMatcher.match(listOf(соседка, звонок, перезвон), перезвон.startMs, uriACR)?.entry)
    }

    @Test
    fun `событие несёт чем подтверждено сопоставление`() {
        val p = EventJson.build(файл, звонок, ША, "m4a", null, мск, "number").getJSONObject("payload")
        assertEquals("number", p.getString("match"))
        val q = EventJson.build(файл, звонок, ША, "m4a", null, мск).getJSONObject("payload")
        assertEquals("по умолчанию — по времени", "time", q.getString("match"))
        assertFalse(EventJson.build(файл, null, ША, "m4a", null, мск).getJSONObject("payload").has("match"))
    }

    @Test
    fun `строка сопоставления в отчёте называет способ`() {
        assertEquals("<контакт> · incoming · 1091 с · по номеру", Затирание.контакт(звонок, "number"))
        assertEquals("<контакт> · incoming · 1091 с · по времени", Затирание.контакт(звонок, "time"))
        assertEquals("<контакт> · incoming · 1091 с", Затирание.контакт(звонок))
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

    private fun работа(state: JobState = JobState.POSTED, attempts: Int = 0) =
        Job("j1", "2026-09-11 20-00 Аня.m4a", 1024L, 1_788_000_000_000L,
            state, attempts)

    @Test
    fun `местный сбой не хоронит работу молча`() {
        // `FAILED` тут почти терминален: `Store.pending()` его не отдаёт, а
        // поднимает работу только `retryFailed()` при пересохранении токена.
        // Отозванное на минуту разрешение не имеет права стоить разговора.
        val было = работа(JobState.POSTED, attempts = 3)
        val стало = JobFlow.послеСбоя(было, "SecurityException: нет доступа")
        assertEquals(JobState.POSTED, стало.state)
        assertEquals(4, стало.attempts)
        assertEquals("SecurityException: нет доступа", стало.error)
    }

    @Test
    fun `местный сбой не сбрасывает уже посчитанное`() {
        // Мутант «вернуть job как есть» проходит первую проверку состояния,
        // но теряет счёт попыток и беду; мутант «обнулить sha256» отправил бы
        // запись считаться заново на каждой отозванной секунде разрешения.
        val было = работа(JobState.POSTED).copy(sha256 = "abc", eventId = "call_1")
        val стало = JobFlow.послеСбоя(было, "IOException")
        assertEquals("abc", стало.sha256)
        assertEquals("call_1", стало.eventId)
        assertEquals(1, стало.attempts)
    }

    @Test
    fun `местный сбой на любом состоянии оставляет его прежним`() {
        for (s in listOf(JobState.NEW, JobState.HASHED, JobState.POSTED))
            assertEquals("состояние $s не должно меняться от местного сбоя",
                s, JobFlow.послеСбоя(работа(s), "беда").state)
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
    fun `повтор в ту же минуту берёт свой ключ, первый — прежний`() {
        val f = JSONObject(fixture("whatsapp-message-id.json").readText())
        val а = arrayOf(f.getString("package"), f.getString("chat"), f.getString("sender"))
        assertEquals(f.getString("source_id"),
            MessageId.of(а[0], а[1], а[2], f.getString("text"), f.getLong("at_ms"), 0))
        assertEquals("суффикс повтора разошёлся с scripts/whatsapp_import.py",
            f.getString("source_id_repeat"),
            MessageId.of(а[0], а[1], а[2], f.getString("text"), f.getLong("at_ms"), 1))
    }

    @Test
    fun `два одинаковых сообщения в одном уведомлении — два события`() {
        val строка = NotificationParse.Line("Анна Петрова", "ок", начало)
        val got = NotificationParse.messages(ув.copy(lines = listOf(строка, строка)))
        assertEquals(2, got.size)
        assertNotEquals(got[0].id, got[1].id)
        assertEquals("первому суффикс не приписан — иначе переедут разосланные ключи",
            MessageId.of("com.whatsapp", "Анна Петрова", "Анна Петрова", "ок", начало), got[0].id)
    }

    @Test
    fun `повтор считается по нормализованному тексту, а не по сырому`() {
        val got = NotificationParse.messages(ув.copy(lines = listOf(
            NotificationParse.Line("Анна Петрова", "ок  ок", начало),
            NotificationParse.Line("Анна Петрова", "ок ок", начало))))
        assertNotEquals("оба текста дают один ключ — в счёт они обязаны идти как одно",
            got[0].id, got[1].id)
    }

    @Test
    fun `разные сообщения в одном уведомлении суффикса не получают`() {
        val got = NotificationParse.messages(ув.copy(lines = listOf(
            NotificationParse.Line("Анна Петрова", "ок", начало),
            NotificationParse.Line("Анна Петрова", "два", начало))))
        assertEquals(MessageId.of("com.whatsapp", "Анна Петрова", "Анна Петрова", "два", начало), got[1].id)
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

    // ── затиралка отчёта мастера (Т3.5) ──────────────────────────────────

    @Test
    fun `номер уходит из отчёта, размеры и даты остаются`() {
        val отчёт = listOf(
            "последний файл: +79990000000_20260907_1405.m4a, 4210688 Б, 2026-09-07 14:05",
            "в медиатеке записей: 1234",
            "звонков в журнале за неделю: 12",
            "каталог 89990000001 и международный 441234567890",
        ).joinToString("\n")
        val чисто = Затирание.текст(отчёт)
        assertFalse(чисто.contains("79990000000"))
        assertFalse(чисто.contains("89990000001"))
        assertFalse(чисто.contains("441234567890"))
        assertTrue(чисто.contains("${Затирание.НОМЕР_ВМЕСТО}_20260907_1405.m4a"))
        assertTrue(чисто.contains("4210688 Б"))
        assertTrue(чисто.contains("2026-09-07 14:05"))
        assertTrue(чисто.contains("записей: 1234"))
        assertTrue(чисто.contains("за неделю: 12"))
        assertEquals(чисто, Затирание.текст(чисто))   // второй проход ничего не портит
    }

    @Test
    fun `плюс с короткими цифрами тоже номер, а восемь цифр без плюса нет`() {
        assertEquals("<номер> и <номер>", Затирание.текст("+1234567 и +79990000000"))
        // запись в час — восемь цифр байт; это размер, а не номер
        assertEquals("файл 41234567 Б", Затирание.текст("файл 41234567 Б"))
        // хеш из шестнадцатеричных знаков цифрами подряд не является
        assertEquals(ША, Затирание.текст(ША))
    }

    @Test
    fun `пороги затиралки пришпилены с обеих сторон`() {
        // с плюсом: шесть цифр — не номер, семь — номер
        assertEquals("+123456", Затирание.текст("+123456"))
        assertEquals("<номер>", Затирание.текст("+1234567"))
        // без плюса: девять цифр остаются, десять — номер, пятнадцать — номер,
        // шестнадцать и больше — не номер (метка времени, id — не затираем вслепую)
        assertEquals("id 123456789", Затирание.текст("id 123456789"))
        assertEquals("<номер>", Затирание.текст("9990000000"))
        assertEquals("<номер>", Затирание.текст("123456789012345"))
        assertEquals("1234567890123456", Затирание.текст("1234567890123456"))
        // номер внутри слова и после разделителя находится, а дата рядом цела
        assertEquals("ab<номер>", Затирание.текст("ab79990000000"))
        assertEquals("20260907_<номер>", Затирание.текст("20260907_79990000000"))
    }

    @Test
    fun `имя файла уезжает формой, а не именем контакта`() {
        val ф = Затирание.файл("Анна Петрова_20260907_1405.m4a")
        assertFalse(ф.contains("Анна"))
        assertFalse(ф.contains("Петрова"))
        assertEquals("<файл>.m4a (30 зн.: цифр 13, букв 13)", ф)
        // номер с пробелами и скобками в имени файла — тоже не наружу
        val н = Затирание.файл("Call_+7 (999) 000-00-00.wav")
        assertFalse(н.contains("999"))
        assertTrue(н.startsWith("<файл>.wav"))
        // расширение — только из белого списка звука: хвост после точки
        // у файла из медиатеки бывает чем угодно, включая фамилию
        assertTrue(Затирание.файл("запись").startsWith("<файл>.?"))
        assertTrue(Затирание.файл("a.verylongext").startsWith("<файл>.?"))
        val х = Затирание.файл("Call Анна.Иван")
        assertTrue(х.startsWith("<файл>.?"))
        assertFalse(х.contains("Иван"))
        assertTrue(Затирание.файл("ЗАПИСЬ.M4A").startsWith("<файл>.m4a"))
    }

    @Test
    fun `строка сопоставления не несёт ни имени, ни номера`() {
        val строка = Затирание.контакт(звонок)
        assertEquals("<контакт> · incoming · 1091 с", строка)
        assertFalse(строка.contains("Анна"))
        assertFalse(строка.contains("7999"))
        assertEquals("ни с чем", Затирание.контакт(null))
        // имени нет, номер есть — строка та же: номер в отчёт не попадает
        assertEquals(строка, Затирание.контакт(звонок.copy(name = null)))
    }

    @Test
    fun `заголовок беседы уезжает длиной, а не текстом`() {
        assertEquals("ещё не было", Затирание.беседа(""))
        val б = Затирание.беседа("Анна Петрова")
        assertFalse(б.contains("Анна"))
        assertTrue(б.contains("12 зн."))
    }

    // ── здоровье захвата (Т4.2, ТЗ §8.4–8.6) ─────────────────────────────

    private val сутки = 24 * 3600_000L

    /** Записи без номера в имени: сопоставляются только по времени. */
    private fun зап(vararg ms: Long) = ms.map { Recording("uri://$it", "call.m4a", 1, it) }

    /** Приметы здорового телефона по умолчанию; тест ломает ровно одну. */
    private fun приметы(
        звонки: List<CallLogEntry> = emptyList(),
        записи: List<Recording> = emptyList(),
        сейчас: Long = начало + 3600_000,
        загрузка: Long = начало - сутки,
        разрешенийНет: List<String> = emptyList(),
        папкаЧитается: Boolean? = null,
        рекордерЕсть: Boolean = true,
        расписаниеЖиво: Boolean? = true,
        свободноБайт: Long? = null,
        уведомленияРазрешены: Boolean = true,
        наблюдениеС: Long = 0L,
    ) = Приметы(сейчас, загрузка, разрешенийНет, папкаЧитается, рекордерЕсть,
        звонки, записи, расписаниеЖиво, свободноБайт, уведомленияРазрешены, наблюдениеС)

    @Test
    fun `без доказательств — unknown, а не healthy`() {
        val о = Здоровье.оценить(приметы())
        assertEquals(Состояние.unknown, о.состояние)
        assertNull(о.тревога)
    }

    @Test
    fun `разговор и запись за ним после перезагрузки — healthy`() {
        val о = Здоровье.оценить(приметы(звонки = listOf(звонок), записи = зап(звонок.endMs)))
        assertEquals(Состояние.healthy, о.состояние)
        assertEquals("healthy: после перезагрузки звонок записался", Здоровье.словами(о))
    }

    @Test
    fun `файл после перезагрузки без звонка после неё — не доказательство`() {
        // рекордер дописал или переложил старый файл: mtime свежий, звонка не было
        val давний = звонок.copy(startMs = начало - 3 * сутки)
        val о = Здоровье.оценить(приметы(звонки = listOf(давний), записи = зап(начало)))
        assertEquals(Состояние.unknown, о.состояние)
    }

    @Test
    fun `пропуск между двумя звонками не теряется`() {
        // за первым записи нет, за вторым есть — тревога по первому, самому раннему
        val другой = звонок.copy(startMs = звонок.endMs + 20 * 60_000)
        val о = Здоровье.оценить(приметы(звонки = listOf(другой, звонок), записи = зап(другой.endMs),
            сейчас = другой.endMs + Здоровье.ОКНО_МС + 1))
        assertEquals(Состояние.unhealthy, о.состояние)
        assertEquals(звонок.startMs, о.тревога)
        assertEquals(listOf(звонок), Здоровье.безЗаписи(приметы(звонки = listOf(другой, звонок),
            записи = зап(другой.endMs))))
        // файл, дописанный в окне после отбоя, — за этим звонком, хоть следующий уже начался
        val подряд = звонок.copy(startMs = звонок.endMs + 60_000)
        assertTrue(Здоровье.безЗаписи(приметы(звонки = listOf(звонок, подряд),
            записи = зап(звонок.endMs + 120_000, подряд.endMs))).isEmpty())
    }

    @Test
    fun `запись с чужим номером в пути звонок не покрывает`() {
        // ACR назвал каталог номером соседки: это её запись, не Анны —
        // а голосовая заметка без номера по времени сойдёт (иначе никак)
        val чужая = Recording("content://x/doc/primary%3ARecord%2F%2B79990000001%2Fcall.m4a", "call.m4a", 1,
            звонок.endMs + 60_000)
        val своя = чужая.copy(id = "content://x/doc/primary%3ARecord%2F%2B79990000000%2Fcall.m4a")
        val сейчас = звонок.endMs + Здоровье.ОКНО_МС + 1
        assertEquals(listOf(звонок),
            Здоровье.безЗаписи(приметы(звонки = listOf(звонок, соседка), записи = listOf(чужая), сейчас = сейчас)))
        assertTrue(Здоровье.безЗаписи(приметы(звонки = listOf(звонок, соседка),
            записи = listOf(своя, чужая), сейчас = сейчас)).isEmpty())
        assertTrue(Здоровье.безЗаписи(приметы(звонки = listOf(звонок), записи = зап(звонок.endMs + 60_000),
            сейчас = сейчас)).isEmpty())
        // без номера — только внутри окна после отбоя: заметка через час звонок не покрывает,
        // а запись с его номером через час (поздняя копия) — покрывает
        assertEquals(listOf(звонок), Здоровье.безЗаписи(приметы(звонки = listOf(звонок),
            записи = зап(звонок.endMs + 3600_000), сейчас = сейчас)))
        assertTrue(Здоровье.безЗаписи(приметы(звонки = listOf(звонок),
            записи = listOf(своя.copy(modifiedMs = звонок.endMs + 3600_000)), сейчас = сейчас)).isEmpty())
        // открытую тревогу чужая запись тоже не закрывает
        val о = Здоровье.оценить(приметы(звонки = listOf(звонок, соседка), записи = listOf(чужая), сейчас = сейчас))
        assertEquals(звонок.startMs, о.тревога)
        assertNull(Здоровье.событие(о, звонок.startMs, приметы(звонки = listOf(звонок, соседка),
            записи = listOf(чужая), сейчас = сейчас)))
    }

    @Test
    fun `звонки до начала наблюдения не считаются`() {
        // приложение поставили после звонка: его записи у нас быть не могло
        val п = приметы(звонки = listOf(звонок), наблюдениеС = звонок.endMs + 1,
            сейчас = звонок.endMs + Здоровье.ОКНО_МС + 1)
        assertTrue(Здоровье.безЗаписи(п).isEmpty())
        assertEquals(Состояние.unknown, Здоровье.оценить(п).состояние)
    }

    @Test
    fun `звонок был, записи нет, окно вышло — unhealthy с тревогой на этот звонок`() {
        val о = Здоровье.оценить(приметы(звонки = listOf(звонок),
            сейчас = звонок.endMs + Здоровье.ОКНО_МС + 1))
        assertEquals(Состояние.unhealthy, о.состояние)
        assertEquals(звонок.startMs, о.тревога)
        assertEquals("звонок был, записи нет", о.причина)
    }

    @Test
    fun `в окне после отбоя — recovering, тревоги нет`() {
        val о = Здоровье.оценить(приметы(звонки = listOf(звонок), сейчас = звонок.endMs + 60_000))
        assertEquals(Состояние.recovering, о.состояние)
        assertNull(о.тревога)
        assertTrue(о.причина.contains("ждём"))
    }

    @Test
    fun `пропущенный и недозвон записи не ждут`() {
        val пропущен = звонок.copy(direction = "missed", durationS = 0, startMs = звонок.endMs + 3600_000)
        val недозвон = звонок.copy(direction = "outgoing", durationS = 0, startMs = звонок.endMs + 7200_000)
        val о = Здоровье.оценить(приметы(звонки = listOf(звонок, пропущен, недозвон),
            записи = зап(звонок.endMs), сейчас = начало + сутки))
        assertEquals(Состояние.healthy, о.состояние)
        assertNull(о.тревога)
    }

    @Test
    fun `старая запись при звонках после неё доказательством не считается`() {
        // §8.5: healthy «из-за последней записи недельной давности» запрещён
        val о = Здоровье.оценить(приметы(звонки = listOf(звонок),
            записи = зап(начало - 6 * сутки), загрузка = начало - 7 * сутки,
            сейчас = начало + сутки))
        assertEquals(Состояние.unhealthy, о.состояние)
        assertEquals(звонок.startMs, о.тревога)
    }

    @Test
    fun `после перезагрузки без контрольного звонка — unknown, даже с записью до неё`() {
        // §8.6 п.6: рекордер под подозрением, пока звонок не оставит запись
        val давний = звонок.copy(startMs = начало - 3 * сутки)
        val о = Здоровье.оценить(приметы(звонки = listOf(давний), записи = зап(давний.endMs),
            загрузка = начало - сутки))
        assertEquals(Состояние.unknown, о.состояние)
        assertTrue(о.причина.contains("контрольный звонок"))
    }

    @Test
    fun `без разрешения или папки — unhealthy раньше остального`() {
        val здоров = приметы(звонки = listOf(звонок), записи = зап(звонок.endMs))
        val о = Здоровье.оценить(здоров.copy(разрешенийНет = listOf("READ_CALL_LOG")))
        assertEquals(Состояние.unhealthy, о.состояние)
        assertTrue(о.причина.contains("READ_CALL_LOG"))
        assertNull("это не тревога по звонку", о.тревога)
        assertEquals(Состояние.unhealthy, Здоровье.оценить(здоров.copy(папкаЧитается = false)).состояние)
        assertEquals("папка не выбрана — медиатека, это норма",
            Состояние.healthy, Здоровье.оценить(здоров.copy(папкаЧитается = null)).состояние)
    }

    @Test
    fun `известный риск при живом доказательстве — degraded`() {
        val здоров = приметы(звонки = listOf(звонок), записи = зап(звонок.endMs))
        assertEquals(Состояние.degraded, Здоровье.оценить(здоров.copy(расписаниеЖиво = false)).состояние)
        assertEquals(Состояние.degraded, Здоровье.оценить(здоров.copy(расписаниеЖиво = null)).состояние)
        assertEquals(Состояние.degraded, Здоровье.оценить(здоров.copy(рекордерЕсть = false)).состояние)
        assertEquals(Состояние.degraded, Здоровье.оценить(здоров.copy(свободноБайт = 1L)).состояние)
        assertEquals(Состояние.degraded, Здоровье.оценить(здоров.copy(уведомленияРазрешены = false)).состояние)
        assertEquals(Состояние.healthy,
            Здоровье.оценить(здоров.copy(свободноБайт = Здоровье.СВОБОДНО_МИН)).состояние)
    }

    @Test
    fun `тревога одна на звонок и закрывается только появлением записи`() {
        val пусто = приметы(звонки = listOf(звонок), сейчас = звонок.endMs + Здоровье.ОКНО_МС + 1)
        val без = Здоровье.оценить(пусто)
        assertEquals(Здоровье.Событие.ТРЕВОГА, Здоровье.событие(без, 0L, пусто))
        assertNull("та же тревога второй раз не поднимается", Здоровье.событие(без, звонок.startMs, пусто))
        val сЗаписью = приметы(звонки = listOf(звонок), записи = зап(звонок.endMs))
        val есть = Здоровье.оценить(сЗаписью)
        assertEquals(Здоровье.Событие.ВОССТАНОВЛЕНО, Здоровье.событие(есть, звонок.startMs, сЗаписью))
        assertNull("закрытой тревоге восстанавливаться нечему", Здоровье.событие(есть, 0L, сЗаписью))
        // новый звонок без записи при старой открытой — новая тревога, не воскрешение старой
        val другой = звонок.copy(startMs = звонок.endMs + 3600_000)
        val пЕщё = приметы(звонки = listOf(звонок, другой), записи = зап(звонок.endMs),
            сейчас = другой.endMs + Здоровье.ОКНО_МС + 1)
        val ещё = Здоровье.оценить(пЕщё)
        assertEquals(другой.startMs, ещё.тревога)
        assertEquals(Здоровье.Событие.ТРЕВОГА, Здоровье.событие(ещё, звонок.startMs, пЕщё))
        // старая открыта, записи за ней нет, новый звонок ещё в окне — тревога та же, не новая
        val пВОкне = приметы(звонки = listOf(звонок, другой), сейчас = другой.endMs + 60_000)
        val вОкне = Здоровье.оценить(пВОкне)
        assertEquals(Состояние.unhealthy, вОкне.состояние)
        assertEquals(звонок.startMs, вОкне.тревога)
        assertNull(Здоровье.событие(вОкне, звонок.startMs, пВОкне))
        // запись за поздним звонком открытую тревогу по раннему не закрывает
        val пЧужая = приметы(звонки = listOf(звонок, другой), записи = зап(другой.endMs),
            сейчас = другой.endMs + Здоровье.ОКНО_МС + 1)
        val чужая = Здоровье.оценить(пЧужая)
        assertEquals(звонок.startMs, чужая.тревога)
        assertNull(Здоровье.событие(чужая, звонок.startMs, пЧужая))
        // сломалось разрешение при открытой тревоге — тревога не закрывается и не «восстанавливается»
        val пСломано = пусто.copy(разрешенийНет = listOf("READ_CALL_LOG"))
        assertNull(Здоровье.событие(Здоровье.оценить(пСломано), звонок.startMs, пСломано))
        // звонок ушёл из недельного журнала — тревога истекла, не восстановлена
        val пУшёл = приметы(звонки = emptyList(), сейчас = звонок.endMs + 8 * сутки)
        assertEquals(Здоровье.Событие.ИСТЕКЛА, Здоровье.событие(Здоровье.оценить(пУшёл), звонок.startMs, пУшёл))
    }

}
