package com.mara.capture

import androidx.work.WorkInfo
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Строка «сверка по расписанию» на экране здоровья. Смысл её один: отличить
 * «работа стоит, но система её душит» от «работы нет вовсе». Пока эти два
 * случая выглядели одинаково, диагноз ставился гаданием.
 *
 * Состояния мало: `ENQUEUED` стоит и у «через семь минут», и у «должна была
 * три дня назад». Поэтому на вход идут пары «состояние, срок», а время
 * подаётся снаружи — иначе тест мерил бы часы, а не функцию.
 */
class ScheduleWordsTest {

    private val сейчас = 1_700_000_000_000L
    private val МИНУТА = 60_000L
    private val ЧАС = 3_600_000L

    @Test
    fun `пустой список значит расписания нет`() {
        assertEquals("не поставлена", SyncWorker.расписаниеСловами(emptyList(), сейчас))
    }

    @Test
    fun `доигравшие работы за расписание не считаются`() {
        // WorkManager помнит завершённые прогоны; если их принять за живую
        // работу, экран соврёт «всё хорошо» ровно в том случае, ради которого
        // строка и заведена.
        assertEquals(
            "не поставлена",
            SyncWorker.расписаниеСловами(
                listOf(
                    WorkInfo.State.SUCCEEDED to Long.MAX_VALUE,
                    WorkInfo.State.FAILED to Long.MAX_VALUE,
                    WorkInfo.State.CANCELLED to Long.MAX_VALUE,
                ),
                сейчас,
            )
        )
    }

    @Test
    fun `ожидающая работа названа по-человечески и со сроком`() {
        assertEquals(
            "ждёт своего часа (через 7 мин)",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to сейчас + 7 * МИНУТА),
                сейчас,
            )
        )
    }

    @Test
    fun `живая среди доигравших видна`() {
        assertEquals(
            "идёт сейчас",
            SyncWorker.расписаниеСловами(
                listOf(
                    WorkInfo.State.SUCCEEDED to Long.MAX_VALUE,
                    WorkInfo.State.RUNNING to Long.MAX_VALUE,
                ),
                сейчас,
            )
        )
    }

    @Test
    fun `две живые перечисляются обе`() {
        assertEquals(
            "ждёт своего часа (через 2 ч), ждёт условий",
            SyncWorker.расписаниеСловами(
                listOf(
                    WorkInfo.State.ENQUEUED to сейчас + 2 * ЧАС,
                    WorkInfo.State.BLOCKED to Long.MAX_VALUE,
                ),
                сейчас,
            )
        )
    }

    @Test
    fun `не спросили - так и сказано, а не молчаливое отсутствие`() {
        // `null` приходит, когда WorkManager не поднялся. Ответить «не
        // поставлена» значило бы обвинить систему вместо себя.
        assertEquals("спросить не вышло", SyncWorker.расписаниеСловами(null, сейчас))
    }

    @Test
    fun `просроченная на сутки названа просроченной, а не ждущей`() {
        // Та самая вторая гипотеза: работа стоит в очереди, но прошивка её не
        // пускает. До этой правки строка была неотличима от здоровой.
        assertEquals(
            "просрочена на 3 дн",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to сейчас - 3 * 24 * ЧАС),
                сейчас,
            )
        )
    }

    @Test
    fun `просрочка в несколько часов считается часами`() {
        assertEquals(
            "просрочена на 3 ч",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to сейчас - 3 * ЧАС),
                сейчас,
            )
        )
    }

    @Test
    fun `опоздание внутри периода тревогой не считается`() {
        // Пятнадцать минут WorkManager вправе задержать сам. Кричать о них
        // значит кричать всегда, и тогда крик перестают слышать.
        assertEquals(
            "ждёт своего часа",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to сейчас - 10 * МИНУТА),
                сейчас,
            )
        )
        // А сразу за порогом — уже тревога, иначе порог можно было бы снять.
        assertEquals(
            "просрочена на 20 мин",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to сейчас - 20 * МИНУТА),
                сейчас,
            )
        )
    }

    @Test
    fun `неизвестный срок не превращается в тысячи лет`() {
        // Незапланированной работе WorkManager ставит `Long.MAX_VALUE`.
        // Вычесть из него «сейчас» — получить «через 4085 дн» на экране.
        assertEquals(
            "ждёт своего часа",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to Long.MAX_VALUE),
                сейчас,
            )
        )
    }

    @Test
    fun `нулевой срок тоже неизвестен, а не полвека просрочки`() {
        // Вторая половина той же заставы: ноль — не «первое января 1970»,
        // а «не спрашивали». Разность по нему дала бы «просрочена на 19849 дн».
        assertEquals(
            "ждёт своего часа",
            SyncWorker.расписаниеСловами(listOf(WorkInfo.State.ENQUEUED to 0L), сейчас)
        )
    }

    @Test
    fun `до запуска меньше минуты - хвоста нет`() {
        // «через 0 мин» — не строка, а опечатка на экране.
        assertEquals(
            "ждёт своего часа",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED to сейчас + 30_000L),
                сейчас,
            )
        )
    }
}
