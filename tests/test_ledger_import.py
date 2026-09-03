"""Разовый перенос карточек волта в ledger (ТЗ §4.1, ADR-0001).

Перенос аддитивный: файлы не трогаются вообще, в базу ложится строка на
карточку плюс отпечаток файла — тот самый, по которому будущая пересборка
поймёт, что файл правили мимо ledger.

Главное здесь — идемпотентность. Перенос запускают руками, и запустят его
дважды: один раз на пробу, второй всерьёз. Стабильный id обязан пережить
второй запуск (ТЗ §4.3: id не меняется никогда), иначе первый же откат
разъедется с волтом.
"""
import os, sys, hashlib, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import mara_ingest as mi
import ledger_import as li


def карточка(vault, rel, **fm):
    поля = {"title": "прислать смету", "type": "commitment", "status": "proposed",
            "owner": "sergey", "promised_to": "Анна", "due": "2026-09-04",
            "source_id": "commitment/call_1/requests/1", "origin": "call/call_1",
            "created": "2026-09-03T01:00:00+03:00",
            "occurred": "2026-09-02T14:05:00+03:00"}
    for k, v in fm.items():           # None — «поля в карточке нет»
        if v is None:
            поля.pop(k, None)
        else:
            поля[k] = v
    head = "\n".join("%s: %s" % (k, v) for k, v in поля.items())
    p = os.path.join(vault, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\n%s\n---\n\n- Обещание: прислать смету\n" % head)
    return p


class Перенос(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "blobs")
        os.makedirs(self.root)
        self.vault = os.path.join(self.tmp.name, "vault")
        self.con = mi.connect(self.root)

    def перенести(self, **kw):
        return li.run(self.con, self.vault, **kw)

    def строки(self, table):
        return [dict(r) for r in self.con.execute("select * from " + table)]

    def test_обязательство_переносится_с_полями_и_отпечатком(self):
        p = карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        итог = self.перенести()
        self.assertEqual(итог["обязательств"], 1)
        r, = self.строки("commitments")
        self.assertEqual(r["title"], "прислать смету")
        self.assertEqual(r["status"], "proposed")
        self.assertEqual(r["due"], "2026-09-04")
        self.assertEqual(r["source_native_id"], "commitment/call_1/requests/1")
        self.assertEqual(r["origin_event"], "call_1", "origin — это call/<id>")
        пр, = self.строки("projections")
        self.assertEqual(пр["path"], "kb/commitments/2026-09-03-smeta.md")
        self.assertEqual(пр["object_id"], r["id"])
        with open(p, "rb") as fh:
            self.assertEqual(пр["content_sha256"],
                             hashlib.sha256(fh.read()).hexdigest())

    def test_разговор_переносится_в_свою_таблицу(self):
        карточка(self.vault, "kb/conversations/2026-09-02-1405-anna.md",
                 type="conversation", title="Звонок · Анна · 14:05",
                 source_id="call/call_1", status=None, owner=None,
                 promised_to=None, due=None, origin=None)
        итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["разговоров"]), (0, 1))
        r, = self.строки("conversations")
        self.assertEqual(r["origin_event"], "call_1")

    def test_второй_запуск_не_меняет_id_и_не_плодит_строк(self):
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.перенести()
        было = self.строки("commitments")[0]["id"]
        итог = self.перенести()
        self.assertEqual(итог["обязательств"], 0, "второй раз новых нет")
        стало = self.строки("commitments")
        self.assertEqual(len(стало), 1)
        self.assertEqual(стало[0]["id"], было, "ТЗ §4.3: id не меняется никогда")

    def test_правка_карточки_подхватывается_с_прежним_id(self):
        p = карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.перенести()
        было = self.строки("commitments")[0]["id"]
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md", status="done")
        итог = self.перенести()
        self.assertEqual(итог["обновлено"], 1)
        r, = self.строки("commitments")
        self.assertEqual((r["id"], r["status"]), (было, "done"))
        пр, = self.строки("projections")
        with open(p, "rb") as fh:
            self.assertEqual(пр["content_sha256"],
                             hashlib.sha256(fh.read()).hexdigest(),
                             "отпечаток обязан догнать файл, иначе сторож соврёт")

    def test_переименованный_файл_не_заводит_второе_обязательство(self):
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.перенести()
        os.rename(os.path.join(self.vault, "kb/commitments/2026-09-03-smeta.md"),
                  os.path.join(self.vault, "kb/commitments/2026-09-03-smeta-2.md"))
        self.перенести()
        self.assertEqual(len(self.строки("commitments")), 1,
                         "ключ — source_id карточки, а не путь")
        self.assertEqual([r["path"] for r in self.строки("projections")],
                         ["kb/commitments/2026-09-03-smeta-2.md"])

    def test_карточка_без_source_id_опознаётся_по_пути(self):
        карточка(self.vault, "kb/commitments/2026-09-03-ruchnaya.md", source_id=None)
        self.перенести()
        self.перенести()
        r, = self.строки("commitments")
        self.assertEqual(r["source_native_id"], "vault:kb/commitments/2026-09-03-ruchnaya.md")

    def test_две_карточки_с_одним_source_id_не_схлопываются_молча(self):
        # копия карточки в Obsidian наследует source_id: без этой проверки
        # вторая заменила бы первую в ledger, а её файл стал бы невидимкой
        # порядок обхода — по имени файла, поэтому «а» заведомо первая
        карточка(self.vault, "kb/commitments/2026-09-03-a.md")
        карточка(self.vault, "kb/commitments/2026-09-03-b.md", status="done")
        итог = self.перенести()
        self.assertEqual(итог["обязательств"], 1)
        self.assertEqual(итог["спорных"], 1)
        r, = self.строки("commitments")
        self.assertEqual(r["status"], "proposed", "вторая не перетирает первую")
        self.assertEqual([п["path"] for п in self.строки("projections")],
                         ["kb/commitments/2026-09-03-a.md"])

    def test_два_обязательства_переносятся_оба(self):
        # без этого теста мимо гейта проходит потеря фильтра по object_id в
        # `delete from projections`: с одним объектом стирать нечего, а с
        # двумя вторая карточка сносит проекцию первой
        карточка(self.vault, "kb/commitments/2026-09-03-a.md",
                 source_id="commitment/call_1/requests/1")
        карточка(self.vault, "kb/commitments/2026-09-03-b.md",
                 source_id="commitment/call_2/requests/1", status="done")
        итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["спорных"]), (2, 0))
        self.assertEqual(len(self.строки("commitments")), 2)
        self.assertEqual(sorted(п["path"] for п in self.строки("projections")),
                         ["kb/commitments/2026-09-03-a.md",
                          "kb/commitments/2026-09-03-b.md"])
        # id у объектов разные, и каждая проекция смотрит на свой
        пары = {п["path"]: п["object_id"] for п in self.строки("projections")}
        self.assertEqual(len(set(пары.values())), 2)

    def test_проба_ничего_не_пишет(self):
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        итог = self.перенести(dry_run=True)
        self.assertEqual(итог["обязательств"], 1, "проба считает, как настоящий")
        self.assertEqual(self.строки("commitments"), [])
        self.assertEqual(self.строки("projections"), [])

    def test_проба_поверх_перенесённого_волта_тоже_не_пишет(self):
        # владелец запустит `--dry-run` вторым заходом, чтобы посмотреть, что
        # изменилось: на этом пути все карточки уже не новые, и проба обязана
        # молчать в базу так же, как на пустой
        p = карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.перенести()
        было = self.строки("commitments")[0]["id"]
        with open(p, "rb") as fh:
            отпечаток = hashlib.sha256(fh.read()).hexdigest()
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md", status="done")
        итог = self.перенести(dry_run=True)
        self.assertEqual((итог["обновлено"], итог["обязательств"]), (1, 0))
        r, = self.строки("commitments")
        self.assertEqual((r["id"], r["status"]), (было, "proposed"),
                         "проба не переписывает строку")
        пр, = self.строки("projections")
        self.assertEqual(пр["content_sha256"], отпечаток,
                         "проба не двигает отпечаток")

    def test_bom_и_crlf_не_прячут_шапку(self):
        # Obsidian через синк с винды кладёт и то, и другое. Без нормализации
        # шапка не матчится, и карточка заводится объектом со всеми NULL
        p = карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8", newline="") as fh:
            fh.write("\ufeff" + текст.replace("\n", "\r\n"))
        self.assertEqual(self.перенести()["обязательств"], 1)
        r, = self.строки("commitments")
        self.assertEqual(r["source_native_id"], "commitment/call_1/requests/1")
        self.assertEqual(r["title"], "прислать смету")

    def test_карточка_без_шапки_не_заводит_объект_из_пустоты(self):
        os.makedirs(os.path.join(self.vault, "kb/commitments"))
        with open(os.path.join(self.vault, "kb/commitments/черновик.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("просто заметка, шапки нет\n")
        итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["спорных"]), (0, 1))
        self.assertEqual(self.строки("commitments"), [])

    def test_списочное_поле_не_роняет_перенос(self):
        # `supersedes: [a, b]` человек напишет руками первым делом, а падение
        # уносит с собой все карточки, которые дальше по алфавиту
        p = карточка(self.vault, "kb/commitments/2026-09-03-a.md")
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("title: ", "supersedes:\n- x\n- y\ntitle: ", 1))
        карточка(self.vault, "kb/commitments/2026-09-03-b.md",
                 source_id="commitment/call_2/requests/1")
        итог = self.перенести()
        self.assertEqual(итог["обязательств"], 2, "вторая карточка не потерялась")
        r, = [x for x in self.строки("commitments")
              if x["source_native_id"].endswith("call_1/requests/1")]
        self.assertEqual(r["supersedes"], "x, y")

    def test_нечитаемый_файл_не_обрывает_прогон(self):
        os.makedirs(os.path.join(self.vault, "kb/commitments"))
        os.symlink(os.path.join(self.vault, "kb/commitments/нет.md"),
                   os.path.join(self.vault, "kb/commitments/0-битый.md"))
        os.makedirs(os.path.join(self.vault, "kb/commitments/1-папка.md"))
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.assertEqual(self.перенести()["обязательств"], 1)

    def test_один_source_id_у_разных_видов_не_съедает_разговор(self):
        # `source_native_id` уникален внутри таблицы, а не поперёк: обязательство
        # с `source_id: call/…`, поставленным руками, не повод потерять разговор
        карточка(self.vault, "kb/commitments/2026-09-03-a.md",
                 source_id="call/call_1")
        карточка(self.vault, "kb/conversations/2026-09-02-anna.md",
                 type="conversation", source_id="call/call_1", status=None,
                 owner=None, promised_to=None, due=None, origin=None)
        итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["разговоров"], итог["спорных"]),
                         (1, 1, 0))

    def test_пустой_волт_не_падает(self):
        self.assertEqual(self.перенести(),
                         {"обязательств": 0, "разговоров": 0, "обновлено": 0,
                          "спорных": 0})


class Идентификатор(unittest.TestCase):
    def test_uuid7_сортируется_по_времени_и_разбирается(self):
        import uuid
        ids = [mi.uuid7() for _ in range(50)]
        self.assertEqual(ids, sorted(ids), "строковый порядок = временной")
        self.assertEqual(len(set(ids)), 50, "в одну миллисекунду тоже разные")
        self.assertEqual(uuid.UUID(ids[0]).version, 7)

    def test_uuid7_не_повторяется_в_потоках(self):
        """contextd принимает загрузки в несколько потоков.

        Часы заморожены, чтобы все потоки оказались в одной миллисекунде — там,
        где хвост читается и пишется. На реализации без замка это даёт 9
        повторов из 4000 (проверено), с замком повтор невозможен вовсе.
        """
        import sys, threading
        собрано, замок = [], threading.Lock()
        часы, шаг = mi.time.time, sys.getswitchinterval()

        def work():
            свои = [mi.uuid7() for _ in range(500)]
            with замок:
                собрано.extend(свои)

        try:
            mi.time.time = lambda: 1_700_000_000.123
            sys.setswitchinterval(1e-6)          # шире окно гонки
            нити = [threading.Thread(target=work) for _ in range(8)]
            for н in нити:
                н.start()
            for н in нити:
                н.join()
        finally:
            mi.time.time = часы
            sys.setswitchinterval(шаг)
        self.assertEqual(len(set(собрано)), 4000, "повтор id между потоками")

    def test_uuid7_переживает_шаг_часов_назад(self):
        """ntp двигает часы назад — id всё равно обязан расти.

        Опорный id снят до шага: без него проверка ничего не ловит, потому что
        сами по себе id после шага упорядочены между собой.
        """
        до = mi.uuid7()
        часы = mi.time.time
        try:
            mi.time.time = lambda: часы() - 3600     # час назад, разом
            ряд = [до] + [mi.uuid7() for _ in range(3)]
        finally:
            mi.time.time = часы
        self.assertEqual(ряд, sorted(ряд), "id после шага часов встал перед прежним")

    def test_uuid7_несёт_настоящее_время(self):
        import time, uuid
        до = int(time.time() * 1000)
        ms = int(uuid.UUID(mi.uuid7()).hex[:12], 16)
        self.assertLessEqual(до, ms)
        self.assertLess(ms - до, 5000)


if __name__ == "__main__":
    unittest.main()
