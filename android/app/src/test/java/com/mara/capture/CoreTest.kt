package com.mara.capture

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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

    // ── вспомогательное ───────────────────────────────────────────────────

    /** Сортируем ключи: JSONObject их порядок не хранит, а сверять надо целиком. */
    private fun canon(o: Any?): String = when (o) {
        is JSONObject -> o.keys().asSequence().sorted()
            .joinToString(",", "{", "}") { "\"$it\":" + canon(o.get(it)) }
        is JSONArray -> (0 until o.length()).joinToString(",", "[", "]") { canon(o.get(it)) }
        is String -> "\"$o\""
        else -> o.toString()
    }

    private fun fixture(): File {
        var d: File? = File("").absoluteFile
        while (d != null && !File(d, "tests/fixtures/phone-call-event.json").exists()) d = d.parentFile
        return File(requireNotNull(d) { "не нашёл tests/fixtures — где корень репозитория?" },
            "tests/fixtures/phone-call-event.json")
    }
}
