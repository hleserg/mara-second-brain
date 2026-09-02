"""Пакет `now.md` для контекст-брокера (ТЗ §15).

Главный тест здесь — не про формат, а про границу. Карточка обязательства несёт
`cloud_allowed: false`, а пакет уезжает провайдеру модели каждый раз, когда
список меняется. Значит наружу едет whitelist из пяти полей, и всё остальное —
тело, цитаты, дословные фразы о сроке, номера — обязано остаться в волте.
"""
import os, sys, glob, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import context_pack as cp


def карточка(vault, name, **fm):
    """Карточка обязательства в волте. Тело всегда есть: его не должно быть в пакете."""
    поля = {"title": "прислать смету", "type": "commitment", "sensitive": "true",
            "cloud_allowed": "false", "status": "proposed", "owner": "sergey",
            "promised_to": "Анна", "origin": "call/call_1"}
    поля.update({k: v for k, v in fm.items() if v is not None})
    head = "\n".join("%s: %s" % (k, v) for k, v in поля.items())
    body = ("- Обещание: прислать смету\n"
            "- Откуда: [[2026-09-02-1405-anna]] · 04:12\n"
            "\nЛюди: [[anna]]\n")
    p = os.path.join(vault, "kb/commitments", name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("---\n%s\n---\n\n%s" % (head, body))
    return p


def волт(**kw):
    v = tempfile.mkdtemp()
    os.makedirs(os.path.join(v, ".git"))
    os.makedirs(os.path.join(v, "kb/commitments"))
    if kw.get("пусто"):
        return v
    карточка(v, "2026-09-02-smeta.md", due="2026-09-04")
    return v


class Состав(unittest.TestCase):
    def test_открытое_обязательство_в_пакете(self):
        text, items = cp.собрать(волт())
        self.assertIn("прислать смету", text)
        self.assertEqual(len(items), 1)

    def test_закрытое_обязательство_не_в_пакете(self):
        v = волт(пусто=True)
        карточка(v, "a.md", status="done", due="2026-09-04")
        text, items = cp.собрать(v)
        self.assertEqual(items, [], "сделанное не занимает бюджет каждый ход")
        self.assertNotIn("прислать смету", text)

    def test_статус_open_тоже_берётся(self):
        v = волт(пусто=True)
        карточка(v, "a.md", status="open")
        _, items = cp.собрать(v)
        self.assertEqual(len(items), 1)

    def test_срок_виден(self):
        text, _ = cp.собрать(волт())
        self.assertIn("2026-09-04", text)

    def test_порядок_по_сроку_без_срока_в_конце(self):
        v = волт(пусто=True)
        карточка(v, "c.md", title="без срока", due=None)
        карточка(v, "a.md", title="поздняя", due="2026-12-01")
        карточка(v, "b.md", title="ранняя", due="2026-09-03")
        text, _ = cp.собрать(v)
        порядок = [text.index(x) for x in ("ранняя", "поздняя", "без срока")]
        self.assertEqual(порядок, sorted(порядок))

    def test_пустой_набор_не_падает(self):
        text, items = cp.собрать(волт(пусто=True))
        self.assertEqual(items, [])
        self.assertIsInstance(text, str)


class Граница(unittest.TestCase):
    """ТЗ §15: raw никогда не инжектится, только дистиллят."""

    def test_тело_карточки_не_уезжает(self):
        text, _ = cp.собрать(волт())
        self.assertIn("прислать смету", text, "заголовок нужен, иначе пакет бесполезен")
        self.assertNotIn("Обещание:", text)
        self.assertNotIn("Откуда:", text)
        self.assertNotIn("04:12", text, "метка времени ведёт к цитате из разговора")

    def test_дословная_фраза_о_сроке_не_уезжает(self):
        v = волт(пусто=True)
        карточка(v, "a.md", **{"deadline_phrase": "'до пятницы, как договорились'"})
        text, _ = cp.собрать(v)
        self.assertNotIn("как договорились", text,
                         "это дословная фраза из звонка, а не дистиллят")

    def test_номер_вместо_имени_не_уезжает(self):
        v = волт(пусто=True)
        карточка(v, "a.md", promised_to="+79990000000")
        text, _ = cp.собрать(v)
        self.assertNotIn("79990000000", text,
                         "контакта не было в книге — в promised_to номер (ТЗ §11)")
        self.assertIn("прислать смету", text, "сама задача остаётся")

    def test_новое_поле_по_умолчанию_не_уезжает(self):
        v = волт(пусто=True)
        карточка(v, "a.md", **{"secret_field": "нечто из будущей спеки"})
        text, _ = cp.собрать(v)
        self.assertNotIn("нечто из будущей спеки", text, "whitelist, а не blacklist")

    def test_бюджет_не_превышается(self):
        v = волт(пусто=True)
        for i in range(200):
            карточка(v, "c%03d.md" % i, title="задача номер %d" % i, due="2026-09-04")
        text, items = cp.собрать(v)
        self.assertLessEqual(len(text.encode()), cp.MAX_BYTES)
        self.assertLess(len(items), 200, "лишнее отрезано, а не втиснуто")
        self.assertIn("ещё", text, "хвост должен быть назван, а не молча пропасть")


class Запись(unittest.TestCase):
    def test_файлы_пишутся_атомарно(self):
        v = волт()
        sha = cp.build_now(v)
        self.assertEqual(len(sha), 64)
        self.assertTrue(os.path.exists(os.path.join(v, "_system/context/now.md")))
        self.assertTrue(os.path.exists(os.path.join(v, "_system/context/manifest.json")))
        self.assertFalse(glob.glob(os.path.join(v, "_system/context/*.tmp")))

    def test_повторная_запись_даёт_ту_же_подпись(self):
        v = волт()
        self.assertEqual(cp.build_now(v), cp.build_now(v),
                         "подпись меняется от содержания, а не от времени запуска")

    def test_подпись_меняется_от_нового_обязательства(self):
        v = волт()
        было = cp.build_now(v)
        карточка(v, "b.md", title="перезвонить", due="2026-09-05")
        self.assertNotEqual(cp.build_now(v), было)


if __name__ == "__main__":
    unittest.main()
