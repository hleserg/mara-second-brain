"""Разовый перенос карточек волта в ledger (ТЗ §4.1, ADR-0001).

Перенос аддитивный: файлы не трогаются вообще, в базу ложится строка на
карточку плюс отпечаток файла — тот самый, по которому будущая пересборка
поймёт, что файл правили мимо ledger.

Главное здесь — идемпотентность. Перенос запускают руками, и запустят его
дважды: один раз на пробу, второй всерьёз. Стабильный id обязан пережить
второй запуск (ТЗ §4.3: id не меняется никогда), иначе первый же откат
разъедется с волтом.
"""
import os, sys, io, json, hashlib, contextlib, sqlite3, tempfile, unittest

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


class _СтендПереноса(unittest.TestCase):
    """Пустые реестр и волт; тестов не несёт."""

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


class Перенос(_СтендПереноса):
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
                          "спорных": 0, "правок": 0})


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
        self.assertIn("без правок 1, без следа 0", вывод, "проба печатает сверку Т2.0")

    def test_проба_на_живой_базе_не_мигрирует(self):
        # `mi.connect` — это и есть миграция (Т0.8, migration-plan.md): проба
        # через него на doctor докатила бы схему до кода дерева мимо Г4
        mi.connect(self.root).execute("drop table digests")
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        код, вывод = self.запустить("--dry-run")
        self.assertEqual(код, 0)
        self.assertIn("обязательств 1", вывод)
        con = sqlite3.connect(os.path.join(self.root, "contextd.db"))
        self.assertIsNone(con.execute("select name from sqlite_master "
                                      "where name='digests'").fetchone(),
                          "проба прогнала схему")

    def test_проба_поверх_базы_до_миграции_2_не_падает_на_истории(self):
        # на doctor база останется версии 1 до Т2.8, а пробу гоняют и до него
        mi.migrate(self.root, 1).close()
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        код, вывод = self.запустить("--dry-run")
        self.assertEqual(код, 0, вывод)
        self.assertIn("нет таблицы corrections", вывод)
        self.assertEqual(mi._версия(mi._открыть(self.root)), 1, "проба мигрировала")

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


def с_журналом(p, *строки):
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("\nПравки:\n" + "".join(s + "\n" for s in строки))


class История(unittest.TestCase):
    """Т2.0: статус живёт во фронтматтере, а объяснение — в журнале «Правки:».
    Сверка говорит, какой статус чем объяснён, до любой смены авторитета."""

    ПУТЬ = "- 2026-09-21T15:08, Мара, correction/correction_1: статус proposed → open; "

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.v = self.tmp.name

    def test_строка_пути_правок_разбирается(self):
        записи, мусор = li.журнал("x\n\nПравки:\n" + self.ПУТЬ +
                                  "\n- 2026-09-22T10:00, Мара, correction/c2: "
                                  "срок не был → 2026-10-01; позвонить сначала\n")
        self.assertEqual(мусор, [])
        self.assertEqual(записи[0]["event"], "correction_1")
        self.assertEqual(записи[0]["status"], ("proposed", "open"))
        self.assertEqual(записи[1]["due"], ("не был", "2026-10-01"))
        self.assertEqual(записи[1]["notes"], ["позвонить сначала"])

    def test_заметка_рукой_и_мусор(self):
        записи, мусор = li.журнал("\nПравки:\n- 2026-09-21T18:30, Мара: почистить всё\n"
                                  "Правки:\nчто-то своё\n")
        self.assertIsNone(записи[0]["event"])
        self.assertEqual(записи[0]["notes"], ["почистить всё"])
        self.assertEqual(мусор, ["что-то своё"], "повторный заголовок — не мусор")

    def test_сверка_раскладывает_статусы(self):
        к = "kb/commitments/"
        с_журналом(карточка(self.v, к + "a.md", status="open"), self.ПУТЬ)
        с_журналом(карточка(self.v, к + "b.md", status="cancelled",
                            source_id="b"), "- 2026-09-21T18:30, Мара: чистый лист")
        карточка(self.v, к + "c.md", status="cancelled", source_id="c")
        с_журналом(карточка(self.v, к + "d.md", status="done", source_id="d"),
                   self.ПУТЬ)
        карточка(self.v, к + "e.md", source_id="e")
        карточка(self.v, к + "f.md", status="open", source_id="f",
                 origin="correction/c9")
        итог, замечания = li.история(self.v)
        self.assertEqual(итог["по пути правок"], 2)     # a и заведённая правкой f
        self.assertEqual(итог["рукой без события"], 1)  # b
        self.assertEqual(итог["без следа"], 1)          # c
        self.assertEqual(итог["разошлось"], 1)          # d: журнал говорит open
        self.assertEqual(итог["без правок"], 1)         # e
        self.assertTrue(any("d.md" in z and "разошлось" in z for z in замечания))


class Запись(unittest.TestCase):
    """Т2.0, шаг 3а: журнал «Правки:» ложится в `corrections`, статус без
    объяснения получает отметку, сверка говорит, сошлось ли."""

    ПУТЬ = ("- 2026-09-21T15:08, Мара, correction/correction_1: статус proposed → open; "
            "срок не был → 2026-10-01; позвонить сначала")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "blobs")
        self.vault = os.path.join(self.tmp.name, "vault")
        self.con = mi.connect(self.root)

    def правки(self, **где):
        q = "select * from corrections"
        if где:
            q += " where " + " and ".join("%s=?" % k for k in где)
        return [dict(r) for r in self.con.execute(q, list(где.values()))]

    def test_строка_журнала_раскладывается_по_полям(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="open",
                            due="2026-10-01"), self.ПУТЬ)
        итог = li.run(self.con, self.vault)
        self.assertEqual(итог["правок"], 2, "статус и срок — две строки")
        oid = self.con.execute("select id from commitments").fetchone()[0]
        статус, = self.правки(field="status")
        self.assertEqual((статус["object_kind"], статус["object_id"]), ("commitment", oid))
        self.assertEqual((json.loads(статус["old_json"]), json.loads(статус["new_json"])),
                         ("proposed", "open"))
        self.assertEqual(статус["origin_event"], "correction_1")
        self.assertEqual((статус["actor_type"], статус["actor_id"]), ("human", "Мара"))
        self.assertIn("позвонить сначала", статус["reason"], "заметка — это «почему»")
        self.assertEqual(статус["occurred"], "2026-09-21T15:08:00+03:00",
                         "сдвиг возвращён (§5.1)")
        срок, = self.правки(field="due")
        self.assertIsNone(срок["old_json"], "«не был» — это пустота, а не строка")
        self.assertEqual(json.loads(срок["new_json"]), "2026-10-01")

    def test_повтор_переноса_не_удваивает_историю(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="open"),
                   self.ПУТЬ)
        li.run(self.con, self.vault)
        self.assertEqual(li.run(self.con, self.vault)["правок"], 0)
        self.assertEqual(len(self.правки()), 2)

    def test_заметка_рукой_без_события(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md"),
                   "- 2026-09-21T18:30, Мара: почистить всё")
        li.run(self.con, self.vault)
        з, = self.правки()
        self.assertEqual((з["field"], json.loads(з["new_json"])), ("note", "почистить всё"))
        self.assertIsNone(з["origin_event"])
        self.assertIn("рукой", з["reason"])

    def test_статус_без_следа_получает_отметку_переноса(self):
        карточка(self.vault, "kb/commitments/a.md", status="cancelled",
                 valid_from="2026-09-28T12:00:00+03:00")
        li.run(self.con, self.vault)
        о, = self.правки()
        self.assertEqual((о["field"], о["old_json"], json.loads(о["new_json"])),
                         ("status", None, "cancelled"))
        self.assertEqual((о["actor_type"], о["actor_id"]), ("import", li.ПЕРЕНОС))
        self.assertIn("без строки в журнале", о["reason"])
        # `valid_from` карточки ставит только `_поправить`; у статуса без
        # журнала настоящего времени нет, и дата создания карточки им не
        # является (ревью P2-3) — честнее момент переноса с оговоркой
        self.assertIn("момент переноса", о["reason"])
        self.assertEqual(о["occurred"][:10], mi.now_iso()[:10])
        self.assertNotEqual(о["occurred"], "2026-09-28T12:00:00+03:00")

    def test_статус_сменили_рукой_повторно_и_отметка_новая_а_сверка_сходится(self):
        """Ревью P2-1: ключ отметки без статуса оставлял в базе прежнюю
        отметку, а сверка по множеству id зеленила историю, которой нет."""
        p = карточка(self.vault, "kb/commitments/a.md", status="cancelled")
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: cancelled", "status: done"))
        self.assertEqual(li.run(self.con, self.vault)["правок"], 1, "новая отметка")
        отметки = sorted(json.loads(о["new_json"]) for о in self.правки(actor_type="import"))
        self.assertEqual(отметки, ["cancelled", "done"], "обе отметки — история")
        self.assertEqual(self.con.execute("select status from commitments").fetchone()[0],
                         "done")
        счёт, замечания = li.сверка(self.con, self.vault)
        self.assertTrue(li.сошлось(счёт), (dict(счёт), замечания))
        self.assertEqual(счёт["правок чужих"], 0, "своя прежняя отметка — не чужая")

    def test_повтор_не_сбрасывает_колонки_проектора_в_projections(self):
        """Ревью P2-2: `insert or replace` заводил строку проекции заново и
        обнулял `projector_version`/`manifest_hash`. `ledger_version` с Т2.6
        ведёт сам перенос — версия объекта, которую отражает проекция."""
        p = карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        self.con.execute("update projections set projector_version=1, manifest_hash='h'")
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: proposed", "status: open"))
        li.run(self.con, self.vault)
        r = self.con.execute("select ledger_version, projector_version, manifest_hash, "
                             "content_sha256 from projections").fetchone()
        self.assertEqual(tuple(r)[:3], (2, 1, "h"), "ledger_version = версия после правки")
        self.assertEqual(r["content_sha256"],
                         hashlib.sha256(текст.replace("status: proposed", "status: open")
                                        .encode("utf-8")).hexdigest(),
                         "а отпечаток — свежий")

    def test_удалённая_карточка_это_строка_без_карточки(self):
        """Codex по #117, P1: у удалённой карточки проекция остаётся, и по
        ней сверка зеленила объект, который проектор воскресил бы."""
        p = карточка(self.vault, "kb/commitments/a.md", status="open")
        li.run(self.con, self.vault)
        os.remove(p)
        счёт, замечания = li.сверка(self.con, self.vault)
        self.assertEqual((счёт["карточек"], счёт["строк без карточки"]), (0, 1))
        self.assertFalse(li.сошлось(счёт))

    def test_сверка_называет_спорную_карточку_а_не_чужое_расхождение(self):
        """Ревью P3-1: дубль source_id сравнивался с объектом первой карточки
        и выглядел как расхождение переноса."""
        карточка(self.vault, "kb/commitments/a.md", status="open")
        карточка(self.vault, "kb/commitments/a2.md", status="done")
        li.run(self.con, self.vault)
        счёт, замечания = li.сверка(self.con, self.vault)
        self.assertEqual((счёт["спорных"], счёт["статус разошёлся"], счёт["правок чужих"]),
                         (1, 0, 0), (dict(счёт), замечания))
        self.assertFalse(li.сошлось(счёт))
        self.assertTrue(any("a2.md: спорная" in z for z in замечания), замечания)

    def test_разошлось_шапка_с_журналом_помечено(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="done"),
                   self.ПУТЬ)
        li.run(self.con, self.vault)
        отметки = self.правки(actor_type="import")
        self.assertEqual(len(отметки), 1)
        self.assertIn("журнал говорит open", отметки[0]["reason"])
        self.assertEqual(json.loads(отметки[0]["new_json"]), "done",
                         "в базе статус шапки — она сегодня авторитет")

    def test_proposed_и_заведённая_правкой_отметки_не_получают(self):
        карточка(self.vault, "kb/commitments/a.md")
        карточка(self.vault, "kb/commitments/b.md", status="open", source_id="b",
                 origin="correction/c9")
        self.assertEqual(li.run(self.con, self.vault)["правок"], 0)

    def test_проба_истории_не_пишет(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="open"),
                   self.ПУТЬ)
        self.assertEqual(li.run(self.con, self.vault, dry_run=True)["правок"], 0)
        self.assertEqual(self.правки(), [])

    def test_сверка_сходится_после_переноса(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="open"),
                   self.ПУТЬ)
        карточка(self.vault, "kb/commitments/b.md", status="cancelled", source_id="b")
        li.run(self.con, self.vault)
        счёт, замечания = li.сверка(self.con, self.vault)
        self.assertTrue(li.сошлось(счёт), (dict(счёт), замечания))
        self.assertEqual((счёт["карточек"], счёт["строк"], счёт["правок ожидается"]),
                         (2, 2, 3))
        self.assertEqual(замечания, [])

    def test_сверка_видит_карточку_без_строки_и_чужую_правку(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="open"),
                   self.ПУТЬ)
        li.run(self.con, self.vault)
        # карточка появилась после переноса
        карточка(self.vault, "kb/commitments/b.md", status="open", source_id="b")
        # строка, которой перенос не писал: правка журнала руками задним числом
        oid = self.con.execute("select id from commitments").fetchone()[0]
        self.con.execute("insert into corrections(id,object_kind,object_id,field,"
                         "new_json,occurred) values('x','commitment',?,'note','\"\"',"
                         "'2026-10-01T00:00:00+03:00')", (oid,))
        счёт, замечания = li.сверка(self.con, self.vault)
        self.assertFalse(li.сошлось(счёт))
        self.assertEqual((счёт["без строки"], счёт["правок чужих"]), (1, 1))
        self.assertEqual(len(замечания), 2, замечания)

    def test_сверка_видит_статус_и_строку_без_карточки(self):
        p = карточка(self.vault, "kb/commitments/a.md", status="open")
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: open", "status: done"))
        self.con.execute("insert into commitments(id, source_native_id, status) "
                         "values('сирота', 'vault:нет', 'open')")
        счёт, _ = li.сверка(self.con, self.vault)
        self.assertEqual((счёт["статус разошёлся"], счёт["строк без карточки"]), (1, 1))

    def test_правка_строки_журнала_руками_оставляет_старую_строку_чужой(self):
        p = карточка(self.vault, "kb/commitments/a.md", status="open")
        с_журналом(p, self.ПУТЬ)
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("позвонить сначала", "позвонить потом"))
        self.assertEqual(li.run(self.con, self.vault)["правок"], 2,
                         "изменённая строка — новые id")
        счёт, _ = li.сверка(self.con, self.vault)
        self.assertEqual((счёт["правок нет в базе"], счёт["правок чужих"]), (0, 2))

    def test_main_после_переноса_печатает_сверку_и_падает_на_расхождении(self):
        с_журналом(карточка(self.vault, "kb/commitments/a.md", status="open"),
                   self.ПУТЬ)
        код, вывод = Запуск.запустить(self)
        self.assertEqual(код, 0, вывод)
        self.assertIn("правок 2", вывод)
        self.assertIn("сверка Т2.0: карточек 1, строк 1", вывод)
        self.con.execute("delete from corrections")
        код, вывод = Запуск.запустить(self)
        self.assertEqual(код, 0, "повтор дописывает недостающее и сходится")
        self.assertIn("правок 2", вывод)


class Идентичность(unittest.TestCase):
    """Т2.2: id по старшинству — реестр, шапка, новый; `--write-ids`
    вписывает его в карточки, у которых нет."""

    ID = "01999999-0000-7000-8000-000000000001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "blobs")
        self.vault = os.path.join(self.tmp.name, "vault")
        os.makedirs(os.path.join(self.vault, ".git"))
        self.con = mi.connect(self.root)

    def test_id_из_шапки_попадает_в_реестр(self):
        карточка(self.vault, "kb/commitments/a.md", id=self.ID)
        li.run(self.con, self.vault)
        self.assertEqual(self.con.execute("select id from commitments").fetchone()[0],
                         self.ID)

    def test_реестр_старше_шапки(self):
        p = карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        было = self.con.execute("select id from commitments").fetchone()[0]
        with open(p, encoding="utf-8") as fh:
            текст = fh.read().replace("title:", "id: %s\ntitle:" % self.ID, 1)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст)
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            li.run(self.con, self.vault)
        self.assertEqual(self.con.execute("select id from commitments").fetchone()[0], было)
        self.assertIn("верю реестру", поток.getvalue())

    def test_write_ids_вписывает_и_обновляет_отпечаток(self):
        p = карточка(self.vault, "kb/commitments/a.md")
        карточка(self.vault, "kb/conversations/c.md", type="conversation",
                 source_id="call/call_1", status=None, owner=None, due=None,
                 promised_to=None, origin=None)
        li.run(self.con, self.vault)
        self.assertEqual(li.вписать_id(self.con, self.vault, dry_run=True), (2, 0))
        with open(p, encoding="utf-8") as fh:
            self.assertNotIn("\nid: ", fh.read(), "проба записала")
        self.assertEqual(li.вписать_id(self.con, self.vault), (2, 0))
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        oid = self.con.execute("select id from commitments").fetchone()[0]
        self.assertRegex(текст, r"^---\ntitle: [^\n]+\nid: %s\n" % oid)
        self.assertEqual(self.con.execute(
            "select content_sha256 from projections where path='kb/commitments/a.md'"
        ).fetchone()[0], hashlib.sha256(текст.encode("utf-8")).hexdigest(),
            "отпечаток проекции не обновлён — сверка сочтёт правку чужой")
        self.assertEqual(li.вписать_id(self.con, self.vault), (0, 0))
        # и перенос после этого верит шапке: id тот же
        li.run(self.con, self.vault)
        self.assertEqual(self.con.execute("select id from commitments").fetchone()[0], oid)

    def test_id_из_шапки_занятый_другим_ключом_это_спор_а_не_падение(self):
        """Ревью P2-5: копия карточки без `source_id` несла id первой, вставка
        падала `UNIQUE constraint failed` и уносила всё после по алфавиту."""
        карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        li.вписать_id(self.con, self.vault)
        with open(os.path.join(self.vault, "kb/commitments/a.md"), encoding="utf-8") as fh:
            копия = fh.read().replace("source_id: commitment/call_1/requests/1\n", "")
        with open(os.path.join(self.vault, "kb/commitments/b-copy.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(копия)
        карточка(self.vault, "kb/commitments/z.md", source_id="z")
        поток = io.StringIO()
        with contextlib.redirect_stderr(поток):
            итог = li.run(self.con, self.vault)
        self.assertEqual((итог["спорных"], итог["обязательств"]), (1, 1), итог)
        self.assertIn("не сливаем", поток.getvalue())
        self.assertEqual(self.con.execute("select count(*) from commitments").fetchone()[0],
                         2, "z.md после копии по алфавиту перенесена")
        счёт, _ = li.сверка(self.con, self.vault)
        self.assertEqual(счёт["спорных"], 1)

    def test_write_ids_обходит_спорную_карточку(self):
        """Ревью P3-11: карточка на пути удалённой соседки (её проекция
        осталась) — спор для переноса; `--write-ids` падал на ключе
        `projections.path` посреди прогона."""
        карточка(self.vault, "kb/commitments/a.md", source_id="a")
        карточка(self.vault, "kb/commitments/b.md", source_id="b")
        карточка(self.vault, "kb/commitments/c.md", source_id="c")
        li.run(self.con, self.vault)
        os.remove(os.path.join(self.vault, "kb/commitments/b.md"))
        os.rename(os.path.join(self.vault, "kb/commitments/a.md"),
                  os.path.join(self.vault, "kb/commitments/b.md"))
        self.assertEqual(li.вписать_id(self.con, self.vault), (1, 1), "c вписана, b пропущена")
        with open(os.path.join(self.vault, "kb/commitments/b.md"), encoding="utf-8") as fh:
            self.assertNotIn("\nid: ", fh.read())

    def test_write_ids_без_строки_в_реестре_не_выдумывает(self):
        карточка(self.vault, "kb/commitments/a.md")
        self.assertEqual(li.вписать_id(self.con, self.vault), (0, 1))

    def test_перенести_карточку_чужой_каталог_и_своя(self):
        self.assertIsNone(li.перенести_карточку(self.con, self.vault, "entities/people/x.md"))
        карточка(self.vault, "kb/commitments/a.md", id=self.ID)
        self.assertEqual(li.перенести_карточку(self.con, self.vault, "kb/commitments/a.md"),
                         self.ID)
        self.assertEqual(self.con.execute("select count(*) from projections").fetchone()[0], 1)

    def test_main_write_ids(self):
        карточка(self.vault, "kb/commitments/a.md")
        self.con.close()
        self.root = os.path.join(self.tmp.name, "нет-базы")
        код, вывод = Запуск.запустить(self, "--write-ids")
        self.assertEqual(код, 2, "без базы вписывать нечего: " + вывод)
        self.assertFalse(os.path.exists(self.root), "--write-ids завёл базу")
        self.root = os.path.join(self.tmp.name, "blobs")
        self.assertEqual(Запуск.запустить(self)[0], 0)
        код, вывод = Запуск.запустить(self, "--write-ids", "--dry-run")
        self.assertEqual(код, 0, вывод)
        self.assertIn("(проба): вписано 1", вывод)
        код, вывод = Запуск.запустить(self, "--write-ids")
        self.assertEqual(код, 0, вывод)
        self.assertIn("вписано 1", вывод)
        self.assertIn("вписано 0", Запуск.запустить(self, "--write-ids")[1])


class Версия(unittest.TestCase):
    """ADR-0003 п.1–2: `version` растёт на принятое изменение, `revisions`
    хранит только изменившиеся поля; перерисовка без изменений версию не
    трогает и ревизии не плодит."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "blobs")
        self.vault = os.path.join(self.tmp.name, "vault")
        self.con = mi.connect(self.root)

    def ревизии(self):
        return [dict(r) for r in self.con.execute(
            "select * from revisions order by version")]

    def test_новый_объект_версия_1_и_ревизия_1(self):
        карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        r = self.con.execute("select version from commitments").fetchone()
        self.assertEqual(r["version"], 1)
        рев, = self.ревизии()
        self.assertEqual((рев["version"], рев["actor_type"], рев["actor_id"]),
                         (1, "import", li.ПЕРЕНОС))
        self.assertEqual(json.loads(рев["changed_json"])["status"], [None, "proposed"])

    def test_повтор_без_изменений_версию_не_трогает(self):
        карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        li.run(self.con, self.vault)
        self.assertEqual(self.con.execute("select version from commitments").fetchone()[0], 1)
        self.assertEqual(len(self.ревизии()), 1)

    def test_изменение_поля_поднимает_версию_и_пишет_дифф(self):
        p = карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: proposed", "status: done"))
        li.run(self.con, self.vault)
        r = self.con.execute("select version, status, updated from commitments").fetchone()
        self.assertEqual((r["version"], r["status"]), (2, "done"))
        self.assertIsNotNone(r["updated"])
        рев = self.ревизии()[-1]
        self.assertEqual(рев["version"], 2)
        self.assertEqual(json.loads(рев["changed_json"]), {"status": ["proposed", "done"]},
                         "только изменившееся поле, до и после")

    def test_перерисовка_не_откатывает_версию_на_единицу(self):
        """`insert or replace` делал ровно это — заводил строку заново с
        `version` по умолчанию."""
        p = карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        self.con.execute("update commitments set version=7")
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("due: 2026-09-04", "due: 2026-09-05"))
        li.run(self.con, self.vault)
        self.assertEqual(self.con.execute("select version from commitments").fetchone()[0], 8)

    def test_сбой_посреди_карточки_не_оставляет_версию_без_ревизии(self):
        """Ревью P3-8: объект, ревизия, проекция и история — одна транзакция."""
        import unittest.mock
        p = карточка(self.vault, "kb/commitments/a.md")
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("status: proposed", "status: open"))
        with unittest.mock.patch.object(li, "правки_в_базу", side_effect=RuntimeError("бум")):
            with self.assertRaises(RuntimeError):
                li.run(self.con, self.vault)
        r = self.con.execute("select version, status from commitments").fetchone()
        self.assertEqual((r["version"], r["status"]), (1, "proposed"), "откатилось целиком")
        self.assertEqual(len(self.ревизии()), 1)
        self.assertFalse(self.con.in_transaction)

    def test_перерисованный_created_не_ревизия(self):
        """Проектор ставит `created: now_iso()` при каждой перерисовке; это не
        изменение объекта, и версия от него расти не должна."""
        p = карточка(self.vault, "kb/commitments/a.md", created="2026-09-03T01:00:00+03:00")
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("2026-09-03T01:00:00+03:00", "2026-10-05T09:00:00+03:00"))
        li.run(self.con, self.vault)
        r = self.con.execute("select version, created from commitments").fetchone()
        self.assertEqual((r["version"], r["created"]), (1, "2026-09-03T01:00:00+03:00"))
        self.assertEqual(len(self.ревизии()), 1)

    def test_актор_ревизии_от_зовущего(self):
        карточка(self.vault, "kb/commitments/a.md")
        li.перенести_карточку(self.con, self.vault, "kb/commitments/a.md",
                              актор=("human", "owner", "correction/c1"))
        рев, = self.ревизии()
        self.assertEqual((рев["actor_type"], рев["actor_id"], рев["reason"]),
                         ("human", "owner", "correction/c1"))

    def test_у_разговора_версии_нет_и_обновление_проходит(self):
        p = карточка(self.vault, "kb/conversations/c.md", type="conversation",
                     source_id="call/call_1", status=None, owner=None, due=None,
                     promised_to=None, origin=None, title="Звонок")
        li.run(self.con, self.vault)
        with open(p, encoding="utf-8") as fh:
            текст = fh.read()
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(текст.replace("title: Звонок", "title: Звонок с Анной"))
        li.run(self.con, self.vault)
        self.assertEqual(self.con.execute("select title from conversations").fetchone()[0],
                         "Звонок с Анной")
        self.assertEqual(self.ревизии(), [])




class Аудит(_СтендПереноса):
    """Т2.5, §5.2: объект + ревизия + событие аудита — одной транзакцией.
    В аудите — имена полей и версия, без значений."""

    def аудит(self):
        return [dict(r) for r in self.con.execute(
            "select * from audit_events order by occurred, id")]

    def test_создание_и_изменение_оставляют_след(self):
        p = карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        self.перенести()
        а, = self.аудит()
        oid = self.строки("commitments")[0]["id"]
        self.assertEqual((а["action"], а["object_kind"], а["object_id"], а["actor_type"],
                          а["actor_id"]), ("object.created", "commitment", oid, "import",
                                           li.ПЕРЕНОС))
        д = json.loads(а["detail_json"])
        self.assertEqual(д["version"], 1)
        self.assertIn("title", д["fields"])
        self.assertNotIn("смету", а["detail_json"], "содержимого в аудите нет")
        self.перенести()
        self.assertEqual(len(self.аудит()), 1, "перерисовка без изменений следа не оставляет")
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md", status="open")
        self.перенести()
        а = self.аудит()[-1]
        self.assertEqual(а["action"], "object.updated")
        self.assertEqual(json.loads(а["detail_json"]), {"version": 2, "fields": ["status"],
                                                        "reason": li.ПЕРЕНОС_АКТОР[2]})

    def test_разговор_тоже_в_аудите_но_без_версии(self):
        карточка(self.vault, "kb/conversations/2026-09-02-1405-anna.md",
                 type="conversation", title="Анна, звонок", source_id="call/call_1",
                 origin=None, due=None, promised_to=None)
        self.перенести()
        а, = self.аудит()
        self.assertEqual((а["action"], а["object_kind"]), ("object.created", "conversation"))
        self.assertIsNone(json.loads(а["detail_json"])["version"])

    def test_аудит_откатывается_вместе_с_объектом(self):
        карточка(self.vault, "kb/commitments/2026-09-03-smeta.md")
        было = mi.audit

        def упасть(*a, **kw):
            raise RuntimeError("смоделированный сбой аудита")
        mi.audit = упасть
        try:
            with self.assertRaises(RuntimeError):
                self.перенести()
        finally:
            mi.audit = было
        self.assertEqual(self.строки("commitments"), [], "объект без следа не записан")
        self.assertEqual(self.строки("revisions"), [])


if __name__ == "__main__":
    unittest.main()
