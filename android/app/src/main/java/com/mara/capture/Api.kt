package com.mara.capture

import org.json.JSONObject
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * Разговор с contextd. HttpURLConnection, а не OkHttp: тут три запроса, и
 * добавлять сетевую библиотеку ради них нечего.
 *
 * Шифрование транспорта — тайлнет (см. спеку 3): сервер живёт за WireGuard, и
 * своего TLS с пиннингом мы не городим.
 */
class Api(private val base: String, private val token: String) {

    private fun open(path: String, method: String, auth: Boolean): HttpURLConnection =
        (URL(base + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 15_000
            readTimeout = 60_000
            if (auth) setRequestProperty("Authorization", "Bearer $token")
        }

    /** Код ответа, либо 0 — «сети не было». Ноль отличается от 5xx только в логе. */
    private fun code(c: HttpURLConnection): Int = try {
        c.responseCode
    } catch (e: Exception) {
        0
    }

    fun postEvent(body: JSONObject) = post("/v1/ingest/event", body)

    /** Сообщения — тем же путём, что Telegram с doctor'а. */
    fun postMessage(body: JSONObject) = post("/v1/ingest/message", body)

    private fun post(path: String, body: JSONObject): ServerReply {
        val c = open(path, "POST", auth = true)
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
     */
    fun putAudio(eventId: String, bytes: Long, body: () -> InputStream): ServerReply {
        val c = open("/v1/ingest/audio?event=$eventId", "POST", auth = true)
        return try {
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/octet-stream")
            c.setFixedLengthStreamingMode(bytes)
            body().use { input -> c.outputStream.use { input.copyTo(it, 64 * 1024) } }
            ServerReply(code(c), eventId)
        } catch (e: Exception) {
            ServerReply(0)
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
