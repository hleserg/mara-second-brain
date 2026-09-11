package com.mara.capture

import org.json.JSONObject
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * Разговор с contextd. HttpURLConnection, а не OkHttp: тут три запроса, и
 * добавлять сетевую библиотеку ради них нечего.
 *
 * Шифрование транспорта — чужая забота: сервер живёт в домашней локалке, куда
 * снаружи пускает только VPN роутера. Внутри локалки запрос идёт открытым
 * текстом; своего TLS с пиннингом мы не городим.
 */
class Api(private val base: String, private val token: String) {

    private fun open(path: String, method: String, auth: Boolean): HttpURLConnection =
        (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            readTimeout = 60_000
            if (auth) setRequestProperty("Authorization", "Bearer $token")
        }

    /** Почему был ноль: класс и начало сообщения; токена в них не бывает. */
    @Volatile var lastError: String? = null

    /** Код ответа, либо 0 — «сети не было». Ноль отличается от 5xx только в логе. */
    private fun code(c: HttpURLConnection): Int = try {
        c.responseCode.also { lastError = null }
    } catch (e: Exception) {
        lastError = e.javaClass.simpleName + (e.message?.let { ": " + it.take(100) } ?: "")
        0
    }

    /**
     * null — соединение не собралось. `open()` звали вне `try`, и бросок
     * `URL()`/`openConnection()`/`requestMethod` уходил мимо всех перехватов:
     * утверждение «Api не бросает вовсе, оно отдаёт код 0» на нём не
     * держалось, а `SyncWorker` на это утверждение опирался.
     */
    private fun соединение(path: String, method: String): HttpURLConnection? = try {
        open(path, method, auth = true)
    } catch (e: Exception) {
        lastError = e.javaClass.simpleName + (e.message?.let { ": " + it.take(100) } ?: "")
        null
    }

    fun postEvent(body: JSONObject) = post("/v1/ingest/event", body)

    /** Сообщения — тем же путём, что Telegram с doctor'а. */
    fun postMessage(body: JSONObject) = post("/v1/ingest/message", body)

    private fun post(path: String, body: JSONObject): ServerReply {
        val c = соединение(path, "POST") ?: return ServerReply(0)
        return try {
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            val code = code(c)
            if (code != 200) return ServerReply(code)
            val j = JSONObject(c.inputStream.bufferedReader().readText())
            ServerReply(200, j.optString("event_id", null), j.optBoolean("need_blob"))
        } catch (e: Exception) {
            ServerReply(0)
        } finally {
            c.disconnect()
        }
    }

    /**
     * Аудио потоком: часовой разговор в память не поднимаем.
     * setFixedLengthStreamingMode заодно избавляет от буферизации целиком.
     *
     * `Expect: 100-continue` — чтобы отказ обходился в заголовки, а не в
     * запись целиком (#73). Сервер отвечает 413 на слишком большое тело и
     * 503 на исчерпанный потолок потоков; без этого заголовка он обязан
     * сначала вычитать мегабайты, которые всё равно выбросит, и телефон
     * платит за отказ трафиком. Разрешения ждёт транспорт, не мы: код ниже
     * пишет тело как раньше.
     */
    fun putAudio(eventId: String, bytes: Long, body: () -> InputStream): ServerReply {
        val c = соединение("/v1/ingest/audio?event=$eventId", "POST")
            ?: return ServerReply(0, eventId)
        return try {
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/octet-stream")
            c.setRequestProperty("Expect", "100-continue")
            c.setFixedLengthStreamingMode(bytes)
            body().use { input -> c.outputStream.use { input.copyTo(it, 64 * 1024) } }
            ServerReply(code(c), eventId)
        } catch (e: Exception) {
            // Отказ до тела рвёт запись: сервер ответил и закрыл поток, наш
            // `write` упал. Ответ при этом уже пришёл, и `responseCode` его
            // отдаёт. Прежний безусловный ноль называл этот отказ
            // отсутствием сети — очередь повторяла запись немедленно и
            // бесконечно, вместо того чтобы разобрать 413 (терминально) или
            // переждать 503. Настоящий обрыв связи `code` не спасёт: там
            // `responseCode` бросит снова, и ноль вернётся сам.
            ServerReply(code(c), eventId)
        } finally {
            c.disconnect()
        }
    }

    /** Самопроверка: сервер жив. Без токена — это единственный открытый путь. */
    fun health(): Int = open("/healthz", "GET", auth = false).let { c ->
        try { code(c) } finally { c.disconnect() }
    }

    /**
     * Самопроверка: токен принят. 404 — принят (работы нет, и не должно быть),
     * 401 — не принят. Любое обращение двигает last_seen устройства на сервере.
     */
    fun tokenOk(): Int = open("/v1/jobs/no-such-job", "GET", auth = true).let { c ->
        try { code(c) } finally { c.disconnect() }
    }
}
