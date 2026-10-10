"""Т3б.2 (ТЗ §5.3 шаги 5, 7, 8; §17.3 п.6): после восстановления проекции
пересобираются, блобы сверяются с реестром, стабильные id на месте, образец
evidence открывается. Проверка ничего не пишет и отвечает кодом.
"""
import os, sys, json, hashlib, tempfile, unittest, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import mara_ingest as mi
import ledger_import as li
import call_project as cp
import call_asr
import restore_check as rc
import vault_drift as vd
import contextd_reconcile as reconcile

EVENT = {"occurred": "2026-09-02T14:05:00+03:00", "ended": "2026-09-02T14:23:11+03:00",
         "payload": {"contact_name": "Анна", "direction": "incoming"}}
СКРИПТ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts",
                      "restore_check.py")


class Проверка(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.root, self.vault = os.path.join(tmp, "b"), os.path.join(tmp, "v")
        os.makedirs(self.root)
        for sub in reconcile.СКЕЛЕТ + reconcile.КАРТОЧКИ:
            os.makedirs(os.path.join(self.vault, sub), exist_ok=True)
        self.con = mi.connect(self.root)
        тело = b"audio-bytes"
        self.sha = hashlib.sha256(тело).hexdigest()
        self.blob = mi.blob_path(self.root, self.sha, "wav")
        os.makedirs(os.path.dirname(self.blob))
        with open(self.blob, "wb") as fh:
            fh.write(тело)
        self.con.execute("insert into blobs(sha256,path,bytes,mime,created) values(?,?,?,?,?)",
                         (self.sha, self.blob, len(тело), "audio/wav", mi.now_iso()))
        self.eid, _ = mi.put_event(self.con, {
            "kind": "call", "source": "phone", "source_id": "d1",
            "occurred_at": EVENT["occurred"], "ended_at": EVENT["ended"],
            "payload": EVENT["payload"], "blob": {"sha256": self.sha}})
        call_asr.записать_сегменты(self.con, self.eid, self.sha, [
            {"segment_id": "s0011", "start_ms": 250000, "end_ms": 275000, "text": "смета"}])
        sid = self.con.execute("select id from transcript_segments").fetchone()[0]
        mi.write_json(mi.extraction_path(self.root, self.eid), {
            "requests": [{"action": "прислать смету", "requester": "Анна", "explicit": True,
                          "confidence": 0.93, "due_at": "2026-09-04", "deadline_explicit": True,
                          "deadline_phrase": "до пятницы", "disposition": "task",
                          "evidence": [{"segment": "s0011", "segment_id": sid,
                                        "start_ms": 252000, "end_ms": 260000}]}],
            "commitments": [], "decisions": [], "constraints": [], "open_questions": [],
            "changed_instructions": [], "followups": [], "people_mentioned": [],
            "projects_mentioned": [], "event_id": self.eid})
        self.written = cp.run(self.eid, self.vault, self.root)
        self.card = [w for w in self.written if "prislat" in w][0]

    def проверка(self, **kw):
        return rc.проверить(self.con, self.root, self.vault, seed=1, **kw)

    def test_после_восстановления_всё_сходится(self):
        сводка, з = self.проверка()
        self.assertEqual(з, [])
        self.assertFalse(rc.расхождение(сводка, з))
        self.assertEqual((сводка["блобы"]["строк"], сводка["блобы"]["хеш сверен"]), (1, 1))
        self.assertEqual((сводка["проекции"]["совпало"], сводка["id"]["проверено"]), (2, 2))
        self.assertEqual((сводка["evidence"]["ссылок"], сводка["evidence"]["открывается"]), (1, 1))

    def test_блоб_без_файла_и_с_чужим_хешем(self):
        with open(self.blob, "wb") as fh:
            fh.write(b"other-bytes")             # тот же размер, другой хеш
        сводка, з = self.проверка(полностью=True)
        self.assertEqual(сводка["блобы"]["хеш не тот"], 1)
        self.assertTrue(rc.расхождение(сводка, з))
        os.remove(self.blob)
        сводка, з = self.проверка()
        self.assertEqual(сводка["блобы"]["без файла"], 1)
        self.assertEqual(сводка["evidence"]["не открывается"], 1, "аудио пропало — ссылка не ведёт")

    def test_осиротевший_файл_считается_и_не_трогается(self):
        чужой = os.path.join(os.path.dirname(self.blob), "deadbeef.wav")
        open(чужой, "wb").close()
        сводка, з = self.проверка()
        self.assertEqual(сводка["блобы"]["без строки"], 1)
        self.assertTrue(os.path.exists(чужой))
        self.assertTrue(rc.расхождение(сводка, з), "о сироте сказано")

    def test_аудио_стёртое_по_ретеншену_не_поломка(self):
        os.remove(self.blob)
        self.con.execute("update blobs set purged_at=? where sha256=?", (mi.now_iso(), self.sha))
        сводка, з = self.проверка()
        self.assertEqual(з, [])
        self.assertEqual(сводка["evidence"]["аудио стёрто по ретеншену"], 1)

    def test_чужой_id_в_шапке(self):
        text = open(os.path.join(self.vault, self.card), encoding="utf-8").read()
        oid = [l for l in text.splitlines() if l.startswith("id: ")][0][4:]
        with open(os.path.join(self.vault, self.card), "w", encoding="utf-8") as fh:
            fh.write(text.replace("id: " + oid, "id: 00000000-0000-7000-8000-000000000000"))
        сводка, з = self.проверка()
        self.assertEqual(сводка["id"]["id не тот"], 1)
        self.assertTrue(rc.расхождение(сводка, з))

    def test_правка_рукой_в_волте_из_git_не_расхождение_восстановления(self):
        """Волт из git новее копии базы — карточка «разошлось» у пересборки,
        но восстановление этим не ломается: код 0, строка в отчёте."""
        with open(os.path.join(self.vault, self.card), "a", encoding="utf-8") as fh:
            fh.write("заметка рукой\n")
        сводка, з = self.проверка()
        self.assertEqual(сводка["проекции"]["разошлось"], 1)
        self.assertIn(("проекции", "разошлось: " + self.card), з)
        self.assertFalse(rc.расхождение(сводка, з))

    def test_старая_схема_требует_миграции(self):
        """Настоящая копия прежней версии (откат схемы), не подменённый номер:
        таблиц новой версии в ней нет, и проверка обязана остановиться на
        подсказке, а не упасть на них (ревью)."""
        # откат 5 → 4 проходит только при пустых колонках происхождения
        self.con.execute("update transcripts set config_json=null, pipeline_version=null")
        self.con.close()
        mi.migrate(self.root, mi.ВЕРСИЯ - 1).close()
        con = vd.только_чтение(self.root)
        сводка, з = rc.проверить(con, self.root, self.vault, seed=1)
        self.assertTrue(any("схема версии" in т and "--migrate" in т for _, т in з), з)
        self.assertTrue(сводка["прервано"] and rc.расхождение(сводка, з))
        self.assertIn("прервана", rc.строки_сводки(сводка)[0])

    def test_битая_база_и_битое_извлечение(self):
        self.con.close()
        env = dict(os.environ, MARA_BLOBS=self.root, MARA_VAULT=self.vault)
        with open(mi.extraction_path(self.root, self.eid), "w", encoding="utf-8") as fh:
            fh.write("{broken")
        r = subprocess.run([sys.executable, СКРИПТ, "--root", self.root, "--vault", self.vault],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("без источника 2", r.stdout,
                      "битое извлечение — карточки без источника, не падение")
        # WAL после прошлого чтения хранит страницы — иначе мусор в файле
        # базы читался бы из него как целая база
        for хвост in ("-wal", "-shm"):
            try:
                os.remove(os.path.join(self.root, "contextd.db" + хвост))
            except FileNotFoundError:
                pass
        with open(os.path.join(self.root, "contextd.db"), "wb") as fh:
            fh.write(b"not a database at all, just bytes" * 100)
        r = subprocess.run([sys.executable, СКРИПТ, "--root", self.root, "--vault", self.vault],
                           env=env, capture_output=True, text=True)
        # битая база — расхождение шага 5, названное строкой, не трейсбек
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("integrity_check: DatabaseError", r.stdout)
        self.assertIn("прервана", r.stdout)
        self.assertNotIn("Traceback", r.stderr)

    def test_копия_на_чистом_каталоге(self):
        """Пути в `blobs.path` — с живого корня; на другом `--root` файл
        ищется под ним по хвосту `calls/…` (ревью): учение Т3б.1 идёт на
        чистом каталоге."""
        import shutil
        self.con.close()
        другой = self.root + "-restored"
        shutil.copytree(self.root, другой)
        shutil.rmtree(os.path.join(self.root, "calls"))     # живого аудио больше нет
        con = vd.только_чтение(другой)
        сводка, з = rc.проверить(con, другой, self.vault, seed=1)
        self.assertEqual(з, [], з)
        self.assertEqual((сводка["блобы"]["хеш сверен"], сводка["блобы"]["без строки"],
                          сводка["evidence"]["открывается"]), (1, 0, 1))

    def test_командная_строка(self):
        self.con.close()
        env = dict(os.environ, MARA_BLOBS=self.root, MARA_VAULT=self.vault)
        r = subprocess.run([sys.executable, СКРИПТ, "--root", self.root, "--vault", self.vault],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("итог: сошлось", r.stdout)
        self.assertIn("evidence: обязательств со ссылками 1", r.stdout)
        os.remove(self.blob)
        r = subprocess.run([sys.executable, СКРИПТ, "--root", self.root, "--vault", self.vault],
                           env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("файла нет", r.stdout)
        r = subprocess.run([sys.executable, СКРИПТ, "--root", os.path.join(self.root, "нет"),
                            "--vault", self.vault], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
