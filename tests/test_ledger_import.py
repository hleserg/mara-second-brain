"""Разовый перенос карточек волта в ledger (ТЗ §4.1, ADR-0001).

Перенос аддитивный: файлы не трогаются вообще, в базу ложится строка на
карточку плюс отпечаток файла — тот самый, по которому будущая пересборка
поймёт, что файл правили мимо ledger.

Главное здесь — идемпотентность. Перенос запускают руками, и запустят его
дважды: один раз на пробу, второй всерьёз. Стабильный id обязан пережить
второй запуск (ТЗ §4.3: id не меняется никогда), иначе первый же откат
разъедется с волтом.
"""
import os, sys, io, hashlib, contextlib, tempfile, unittest

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

    def test_проба_отчитывается_о_споре_так_же_как_боевой_прогон(self):
        # §9 обещает: вывод пробы — тот самый отчёт, по которому владелец
        # решает, запускать ли настоящий прогон. Обещание держится ровно на
        # том, что `видели` заполняется до `if dry_run: continue`. Опусти
        # отметку ниже — и проба насчитает два обязательства и промолчит про
        # спор, то есть соврёт именно там, куда смотрят.
        карточка(self.vault, "kb/commitments/2026-09-03-a.md")
        карточка(self.vault, "kb/commitments/2026-09-03-b.md", status="done")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            проба = self.перенести(dry_run=True)
        жалоба = поток.getvalue()
        боевой = self.перенести()
        self.assertEqual((проба["обязательств"], проба["спорных"]),
                         (боевой["обязательств"], боевой["спорных"]),
                         "проба насчитала не то, что боевой прогон")
        self.assertIn("перенесён первый", жалоба,
                      "проба смолчала про дубль")

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


    def test_обязательство_из_поправки_помнит_событие(self):
        # поправка — такое же событие в `events`, и `call_project.py:409`
        # пишет в карточку `origin: correction/<id>`. Пока разбирался один
        # префикс `call/`, у обязательств из поправок `origin_event` уходил
        # пустым — то есть связь с поправкой терялась при самом переносе.
        карточка(self.vault, "kb/commitments/2026-09-03-popravka.md",
                 origin="correction/ev_c1")
        self.перенести()
        r, = self.строки("commitments")
        self.assertEqual(r["origin_event"], "ev_c1")

    def test_source_id_проставленный_позже_уходит_в_спор_а_не_в_дубль(self):
        # Карточку завели руками, без `source_id` — ключом стал путь. Потом
        # `source_id` проставили, и ключ сменился. На `main` здесь молча
        # заводился второй объект, а первый оставался вообще без проекции.
        #
        # Слить их нечем, и в этом вся суть. Ровно так же из базы выглядит
        # чужая карточка, легшая на освободившийся путь, — и путь она займёт
        # ровно при совпадении заголовка, потому что складывается из даты и
        # `slug(...)[:40]` (`call_project.py:159`, `:391`). Значит спор, а не
        # догадка: строка ledger цела, дубля нет, причина названа вслух.
        rel = "kb/commitments/2026-09-03-ruchnaya.md"
        карточка(self.vault, rel, source_id=None)
        self.перенести()
        было, = [(r["id"], r["status"]) for r in self.строки("commitments")]
        карточка(self.vault, rel, source_id="commitment/call_1/requests/1",
                 status="done")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["обновлено"],
                          итог["спорных"]), (0, 0, 1))
        self.assertIn("ключ сменился", поток.getvalue(),
                      "причина спора не названа")
        self.assertEqual([(r["id"], r["status"])
                          for r in self.строки("commitments")], [было],
                         "объект тронут, хотя доказательства не было")

    def test_чужой_source_id_на_месте_объекта_не_сливает_два_в_один(self):
        # у карточки по пути уже стоит объект Б, а объявленный `source_id`
        # принадлежит объекту А. Слить их — то самое схлопывание двух в один,
        # без доказательства, что это одно событие (ТЗ §4.3): сверка
        # обязана остановиться и сказать вслух.
        а = "kb/commitments/2026-09-03-a.md"
        б = "kb/commitments/2026-09-03-b.md"
        карточка(self.vault, а, source_id="commitment/call_1/requests/1")
        карточка(self.vault, б, source_id=None)
        self.перенести()
        ид = {r["source_native_id"]: r["id"] for r in self.строки("commitments")}
        os.remove(os.path.join(self.vault, а))
        карточка(self.vault, б, source_id="commitment/call_1/requests/1")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            итог = self.перенести()
        self.assertEqual(
            (итог["обязательств"], итог["обновлено"], итог["спорных"]), (0, 0, 1))
        self.assertIn("по ключу стоит другой объект", поток.getvalue(),
                      "причина спора не названа")
        self.assertEqual(len(self.строки("commitments")), 2, "объект А не пропал")
        self.assertEqual(
            {(п["path"], п["object_id"]) for п in self.строки("projections")},
            {(а, ид["commitment/call_1/requests/1"]), (б, ид["vault:" + б])})


    def test_чужая_карточка_на_месте_объекта_не_затирает_его(self):
        # карточку с `source_id` удалили, а на том же пути завели другую, без
        # `source_id`. Так пишет не проектор — `call_project.py` ставит
        # `source_id` всегда (`:176`, `:400`), — а рука в Obsidian: имя файла
        # складывается из даты и первых сорока знаков заголовка, и повторить
        # его нетрудно. По ключу не найдётся ничего, по пути найдётся чужой
        # объект — взять его значит стереть строку ledger, которая никуда не
        # девалась.
        rel = "kb/commitments/2026-09-03-a.md"
        карточка(self.vault, rel, source_id="commitment/call_1/requests/1")
        self.перенести()
        было, = [(r["id"], r["title"]) for r in self.строки("commitments")]
        os.remove(os.path.join(self.vault, rel))
        карточка(self.vault, rel, source_id=None, title="совсем другое")
        итог = self.перенести()
        self.assertEqual(
            (итог["обязательств"], итог["обновлено"], итог["спорных"]), (0, 0, 1))
        self.assertIn(было, [(r["id"], r["title"])
                             for r in self.строки("commitments")],
                      "объект с прежним ключом затёрт чужой карточкой")

    def test_снятый_source_id_не_меняет_ключ_молча(self):
        # обратный случай к тому же: `source_id` из карточки убрали, ключом
        # снова стал путь. Тот же файл это или другой — из базы не видно, и
        # разойтись эти два случая не могут. Значит спорная, а не догадка.
        rel = "kb/commitments/2026-09-03-a.md"
        карточка(self.vault, rel, source_id="commitment/call_1/requests/1")
        self.перенести()
        карточка(self.vault, rel, source_id=None)
        итог = self.перенести()
        self.assertEqual(
            (итог["обязательств"], итог["обновлено"], итог["спорных"]), (0, 0, 1))
        r, = self.строки("commitments")
        self.assertEqual(r["source_native_id"], "commitment/call_1/requests/1",
                         "ключ сменился без ведома человека")

    def test_другая_карточка_на_освободившемся_пути_не_затирает_объект(self):
        # Зеркало к «`source_id` проставили позже», и из базы неотличимо:
        # там та же карточка получила ключ, здесь на её место легла чужая.
        # Заголовок в решении не участвует намеренно — он расходится далеко
        # не всегда, а слияние стирало бы строку ledger в обоих случаях.
        rel = "kb/commitments/2026-09-03-a.md"
        карточка(self.vault, rel, source_id=None)
        self.перенести()
        было, = [(r["id"], r["title"]) for r in self.строки("commitments")]
        os.remove(os.path.join(self.vault, rel))
        карточка(self.vault, rel, source_id="commitment/call_9/requests/7",
                 title="совсем другое")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["обновлено"],
                          итог["спорных"]), (0, 0, 1))
        self.assertIn("ключ сменился", поток.getvalue(),
                      "причина спора не названа")
        self.assertEqual([п["object_id"] for п in self.строки("projections")],
                         [было[0]], "проекция уведена на чужой объект")
        self.assertIn(было, [(r["id"], r["title"])
                             for r in self.строки("commitments")],
                      "объект затёрт карточкой, которая заняла его путь")

    def test_карточка_ушедшая_в_спор_не_считается_перенесённой(self):
        # `видели` отмечает `source_id` уже перенесённых карточек. Пока
        # отметка стояла выше заставы, она врала: первая карточка уходила в
        # спор, а второй с тем же `source_id` сообщалось «перенесён первый»
        # — при том, что первый не перенесён никуда.
        а = "kb/commitments/2026-09-03-a.md"
        б = "kb/commitments/2026-09-03-b.md"
        карточка(self.vault, а, source_id=None)
        self.перенести()
        карточка(self.vault, а, source_id="commitment/call_1/requests/1")
        карточка(self.vault, б, source_id="commitment/call_1/requests/1")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["спорных"]), (1, 1))
        self.assertNotIn("перенесён первый", поток.getvalue(),
                         "спорная карточка объявлена перенесённой")

    def test_проекция_на_снесённую_строку_объекта_не_даёт_вечный_спор(self):
        # Строку объекта снесли руками, проекцию за ней никто не почистил.
        # Соединение в запросе обычное, не `left`: пары из пустот не будет,
        # проекция прочтётся как отсутствующая, и карточка заведётся заново.
        # При `left join` ключ не совпал бы ни с чем и спор стал бы вечным.
        rel = "kb/commitments/2026-09-03-a.md"
        карточка(self.vault, rel)
        self.перенести()
        self.con.execute("delete from commitments")
        итог = self.перенести()
        self.assertEqual((итог["обязательств"], итог["спорных"]), (1, 0))
        self.assertEqual(len(self.строки("commitments")), 1)

    def test_нечисловой_confidence_не_уходит_молча(self):
        # пустым `confidence` делает `_число`, а не база: `real` в SQLite
        # — affinity, «высокая» легла бы туда как есть. Терять поле молча
        # нельзя: карточка выглядела бы перенесённой целиком.
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md",
                 confidence="высокая")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            итог = self.перенести()
        r, = self.строки("commitments")
        self.assertIsNone(r["confidence"])
        self.assertEqual(итог["спорных"], 0, "одно поле — не повод ронять прогон")
        self.assertIn("2026-09-03-smeta.md", поток.getvalue())
        self.assertIn("высокая", поток.getvalue())


class Идентификатор(unittest.TestCase):
    def test_uuid7_сортируется_по_времени_и_разбирается(self):
        import uuid
        ids = [mi.uuid7() for _ in range(50)]
        self.assertEqual(ids, sorted(ids), "строковый порядок = временной")
        self.assertEqual(len(set(ids)), 50, "в одну миллисекунду тоже разные")
        self.assertEqual(uuid.UUID(ids[0]).version, 7)

    def test_uuid7_трогает_общее_состояние_только_под_замком(self):
        """НБ8 из #39: застава на замок вместо ловли гонки потоками.

        Общее у потоков ровно одно — `_ПОСЛЕДНИЙ`. Подменяем его списком,
        который скандалит, когда к нему обратились не внутри замка. Снятый
        замок и замок, сужённый до одной записи, падают теперь на первом же
        вызове, а не один прогон из сотни.
        """
        состояние = {"внутри": False}

        class Замок:
            def __enter__(self):
                состояние["внутри"] = True

            def __exit__(self, *_):
                состояние["внутри"] = False

        class Сторож(list):
            """Список, у которого нельзя спросить ничего вне замка."""

            def __getitem__(self, i):
                if not состояние["внутри"]:
                    raise AssertionError("хвост прочитан вне замка")
                return list.__getitem__(self, i)

            def __setitem__(self, i, v):
                if not состояние["внутри"]:
                    raise AssertionError("хвост записан вне замка")
                return list.__setitem__(self, i, v)

        замок, последний = mi._ЗАМОК, mi._ПОСЛЕДНИЙ
        try:
            mi._ЗАМОК, mi._ПОСЛЕДНИЙ = Замок(), Сторож(последний)
            ряд = [mi.uuid7() for _ in range(3)]
        finally:
            mi._ЗАМОК, mi._ПОСЛЕДНИЙ = замок, последний
        self.assertEqual(len(set(ряд)), 3)
        self.assertFalse(состояние["внутри"], "замок не отпущен")

    def test_uuid7_не_повторяется_в_потоках(self):
        """Дым на настоящих потоках: заставу держит тест выше, не этот.

        Ловит он ровно то, что успел свести планировщик: на beta-pi падает
        на мутанте со снятым замком 20 раз из 20 (проверено), но это
        свойство машины и её загрузки, а не кода — на другой оно другое.
        Оставлен потому, что ложно упасть не умеет: дубль id тут либо
        есть, либо его нет.
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


class Запуск(unittest.TestCase):
    """`main()`: коды возврата и цена пробы. До этого класса он не звался."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "blobs")
        self.vault = os.path.join(self.tmp.name, "vault")

    def запустить(self, *флаги):
        argv = sys.argv
        sys.argv = (["ledger_import", "--root", self.root, "--vault", self.vault]
                    + list(флаги))
        поток = io.StringIO()
        try:
            with contextlib.redirect_stdout(поток), \
                    contextlib.redirect_stderr(поток):
                код = li.main()
        finally:
            sys.argv = argv
        return код, поток.getvalue()

    def test_проба_на_чистой_машине_не_заводит_базу(self):
        # `--dry-run` — вопрос «что бы перенеслось», а не команда завести
        # каталог блобов со схемой: `mi.connect` звался до проверки флага.
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        код, вывод = self.запустить("--dry-run")
        self.assertEqual(код, 0)
        self.assertFalse(os.path.exists(self.root), "проба завела " + self.root)
        self.assertIn("обязательств 1", вывод)

    def test_спорная_карточка_даёт_единицу(self):
        карточка(self.vault, "kb/commitments/2026-09-03-a.md")
        карточка(self.vault, "kb/commitments/2026-09-03-b.md")
        код, вывод = self.запустить()
        self.assertEqual(код, 1, "спорную карточку крон обязан заметить")
        self.assertIn("спорных 1", вывод)

    def test_проба_помечена_в_выводе(self):
        """Крон пишет обе строки в один лог. Без пометки проба в нём
        неотличима от настоящего переноса, и «обязательств 1» читается
        как «перенесено», хотя не перенесено ничего."""
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.assertIn("(проба)", self.запустить("--dry-run")[1])
        self.assertNotIn("(проба)", self.запустить()[1])

    def test_самопроверка_проходит(self):
        """Кода возврата мало: `main`, потерявший заставу на `--self-check`,
        доходит до обычного переноса и на пустом волте отдаёт тот же 0."""
        код, вывод = self.запустить("--self-check")
        self.assertEqual(код, 0)
        self.assertIn("self-check: ок", вывод)
        self.assertNotIn("обязательств", вывод,
                          "это не самопроверка, а перенос")
