package com.mara.capture

import androidx.work.WorkInfo
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Строка «сверка по расписанию» на экране здоровья. Смысл её один: отличить
 * «работа стоит, но система её душит» от «работы нет вовсе». Пока эти два
 * случая выглядели одинаково, диагноз ставился гаданием.
 */
class ScheduleWordsTest {

    @Test
    fun `пустой список значит расписания нет`() {
        assertEquals("не поставлена", SyncWorker.расписаниеСловами(emptyList()))
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
                    WorkInfo.State.SUCCEEDED,
                    WorkInfo.State.FAILED,
                    WorkInfo.State.CANCELLED,
                )
            )
        )
    }

    @Test
    fun `ожидающая работа названа по-человечески`() {
        assertEquals(
            "ждёт своего часа",
            SyncWorker.расписаниеСловами(listOf(WorkInfo.State.ENQUEUED))
        )
    }

    @Test
    fun `живая среди доигравших видна`() {
        assertEquals(
            "идёт сейчас",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.SUCCEEDED, WorkInfo.State.RUNNING)
            )
        )
    }

    @Test
    fun `две живые перечисляются обе`() {
        assertEquals(
            "ждёт своего часа, ждёт условий",
            SyncWorker.расписаниеСловами(
                listOf(WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED)
            )
        )
    }

    @Test
    fun `не спросили - так и сказано, а не молчаливое отсутствие`() {
        // `null` приходит, когда WorkManager не поднялся. Ответить «не
        // поставлена» значило бы обвинить систему вместо себя.
        assertEquals("спросить не вышло", SyncWorker.расписаниеСловами(null))
    }
}
