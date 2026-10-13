"""Учение восстановления на синтетике (репетиция Т3б.1, ТЗ §5.3): раздел
«Восстановление» `docs/backup-core.md` проходится его же командами.

Команды не переписаны в тест, а берутся из блока `bash` ранбука при каждом
прогоне: поправили ранбук — учение идёт по новому тексту, разошёлся он с
кодом — тест красный. Пути подменяются на временные: `/srv/…`, `/mnt/…`,
`/var/tmp/…` — на те же пути под песочницей теста, `sudo ` снимается (лист
песочницы и так свой), `~` — это `HOME` песочницы, так что
`~/.config/mara/backup-pass` в командах остаётся как в ранбуке. Команда, в
которой после подмены остался абсолютный путь вне песочницы, не
запускается вовсе — учение падает раньше.

Сценарий — умерший диск doctor:

1. «Живая» система: корень блобов с реестром (`mi.connect`), звонок с
   аудио, расшифровкой и сегментом, извлечением и карточками проектора —
   разговор и обязательство с evidence (как `tests/test_restore_check.py`);
   волт — git-репозиторий, bare-зеркало рядом, как на doctor.
2. Бэкап штатными скриптами: `core-backup.py` (архив, зеркало аудио,
   ночное учение) и `vault-backup.sh` (бандл) на носитель-каталог, фраза —
   из файла.
3. Между архивом ядра и бандлом волт живёт дальше (бандл свежее архива —
   случай шага 4а): владелец дописал заметку рукой в карточку обязательства
   и завёл карточку рукой («Олег»), которой реестр не видел. Второй
   сценарий (`ПотерянныйЗвонок`) добавляет сюда целый звонок, пришедший
   после архива ядра.
4. Диск умирает: `/srv` песочницы стирается целиком — корень блобов, волт и
   зеркало; состояние под `HOME` тоже. Остаются носитель и фраза.
5. Шаги 2–8 ранбука — на этих чистых каталогах, отдельным процессом каждая
   команда, с проверкой кодов возврата там, где ранбук их называет.

Пропущены шаги 0, 1 и 9: это systemd, crontab, `sudo` и повторный вход в
Telegram и Gmail — на синтетике им проверять нечего, а в песочнице теста
их и не запустить. Их итог, нужный дальше, сделан руками: фраза лежит в
`~/.config/mara/backup-pass` с режимом 600 (шаг 0), писателей нет
изначально (шаг 1).

Находка, где код расходится с тем, что ранбук обещает, — отдельный тест с
описанием; первая такая (шаг 2 не сверял аудио на чистой машине) починена
в том же PR.
"""
import contextlib, hashlib, io, json, os, re, shutil, subprocess, sys, tempfile, time
import unittest

КОРЕНЬ = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(КОРЕНЬ, "scripts"))
import mara_ingest as mi
import call_project as cp
import call_asr
import contextd_reconcile as reconcile

ДОК = os.path.join(КОРЕНЬ, "docs", "backup-core.md")
ПРОПУСК = {"0", "1", "9"}          # systemd, crontab, sudo — см. докстринг
ФРАЗА = "учебная фраза восстановления\n"
ЗАМЕТКА = "Заметка рукой после копии базы: смету ждут к понедельнику."
КАРТОЧКА_РУКОЙ = "kb/commitments/2026-09-05-pozvonit-olegu.md"
ИНСТРУМЕНТЫ = ("gpg", "git", "tar", "find", "install")
ПУСТО = ("requests", "commitments", "decisions", "constraints", "open_questions",
         "changed_instructions", "followups", "people_mentioned", "projects_mentioned")


def ранбук(путь=ДОК):
    """Блок `bash` раздела «Восстановление» → `[(шаг, команда)]`.

    Шаг — метка из комментария `# N.` (`2`, `3`, `4`, `3а`, `4а`, `5–8`).
    Команда — логическая строка: продолжение `\\` и скобки подоболочки,
    открытые на одной строке и закрытые на другой, склеиваются; комментарии
    между командами отбрасываются."""
    with open(путь, encoding="utf-8") as fh:
        текст = fh.read()
    раздел = текст.split("\n## Восстановление\n", 1)[1]
    блок = раздел.split("```bash\n", 1)[1].split("\n```", 1)[0]
    шаг, out, буфер, глубина = None, [], [], 0
    for строка in блок.split("\n"):
        if not буфер:
            голая = строка.strip()
            m = re.match(r"#\s*(\d+(?:–\d+)?а?)\.", голая)
            if m:
                шаг = m.group(1)
                continue
            if not голая or голая.startswith("#"):
                continue
        буфер.append(строка)
        глубина += строка.count("(") - строка.count(")")
        if глубина > 0 or строка.rstrip().endswith("\\"):
            continue
        out.append((шаг, "\n".join(буфер)))
        буфер, глубина = [], 0
    if буфер:
        raise ValueError("ранбук: незакрытая команда %r" % "\n".join(буфер))
    return out


def подставить(команда, песок):
    """Пути ранбука → пути песочницы; `sudo` снят."""
    команда = re.sub(r"(?<![\w.~/-])/(srv|mnt|var/tmp)(?=/)", песок + r"/\1", команда)
    return re.sub(r"(?<![\w-])sudo ", "", команда)


def вне_песочницы(команда, песок):
    """Абсолютные пути команды, которые не под песочницей."""
    return [p for p in re.findall(r"(?<![\w.~$}-])/[^\s\"'();|&]+", команда)
            if not (p == песок or p.startswith(песок + "/"))]


def sha(путь):
    with open(путь, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=Мара учение", "-c",
                    "user.email=drill@example.invalid", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True)


def звонок(root, vault, source_id, когда, кто, текст, пункт, ключ):
    """Звонок как его оставляет конвейер: блоб, событие, расшифровка с одним
    сегментом, извлечение со ссылкой на сегмент, карточки проектора.
    → (event_id, sha аудио, путь блоба, записанные карточки)."""
    con = mi.connect(root)
    аудио = b"RIFF-sintetika-" + source_id.encode() + os.urandom(64)
    s = hashlib.sha256(аудио).hexdigest()
    путь = mi.blob_path(root, s, "wav")
    os.makedirs(os.path.dirname(путь), exist_ok=True)
    with open(путь, "wb") as fh:
        fh.write(аудио)
    con.execute("insert into blobs(sha256,path,bytes,mime,created) values(?,?,?,?,?)",
                (s, путь, len(аудио), "audio/wav", mi.now_iso()))
    eid, _ = mi.put_event(con, {
        "kind": "call", "source": "phone", "source_id": source_id,
        "occurred_at": когда[0], "ended_at": когда[1],
        "payload": {"contact_name": кто, "direction": "incoming"}, "blob": {"sha256": s}})
    call_asr.записать_сегменты(con, eid, s, [
        {"segment_id": "s0011", "start_ms": 250000, "end_ms": 275000, "text": текст}])
    sid = con.execute("select s.id from transcript_segments s join transcripts t on "
                      "t.id=s.transcript_id where t.event_id=?", (eid,)).fetchone()[0]
    извлечение = {k: [] for k in ПУСТО}
    извлечение[ключ] = [dict(пункт, evidence=[{"segment": "s0011", "segment_id": sid,
                                               "start_ms": 252000, "end_ms": 260000}])]
    mi.write_json(mi.extraction_path(root, eid), dict(извлечение, event_id=eid))
    con.close()
    with contextlib.redirect_stdout(io.StringIO()):     # «карточек 2» — не нам
        карточки = cp.run(eid, vault, root)
    return eid, s, путь, карточки


@unittest.skipUnless(all(shutil.which(t) for t in ИНСТРУМЕНТЫ),
                     "нет одного из %s — учение не пройти" % ", ".join(ИНСТРУМЕНТЫ))
class Сценарий(unittest.TestCase):
    """Подготовка, бэкап, смерть диска и прогон ранбука; тестов своих нет."""

    @classmethod
    def setUpClass(cls):
        if cls is Сценарий:
            raise unittest.SkipTest("основа сценариев")
        cls.песок = os.path.realpath(tempfile.mkdtemp(prefix="mara-drill."))
        п = cls.песок
        cls.root = п + "/srv/mara-blobs"
        cls.vault = п + "/srv/vault"
        cls.зеркало_волта = п + "/srv/backup/vault.git"
        cls.носитель = п + "/mnt/backup/mara"
        cls.home = п + "/home"
        cls.пароль = cls.home + "/.config/mara/backup-pass"
        for d in (п + "/var/tmp", cls.носитель):
            os.makedirs(d)
        cls.env = cls.окружение()
        cls.живая_система()
        cls.бэкап()
        cls.смерть_диска()
        cls.шаги = cls.пройти_ранбук()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.песок, ignore_errors=True)

    # --- подготовка ---------------------------------------------------------

    @classmethod
    def окружение(cls):
        """Окружение команд: умолчания `/srv/…` и носителей — на песочницу.

        `MARA_BLOBS` и `VAULT`/`MARA_VAULT` — те же умолчания, что на doctor
        (`/srv/mara-blobs`, `/srv/vault`), только под песочницей: шаг 2 и
        сверка в ранбуке зовутся без `--root`/`--vault`. `MARA_CORE_TARGETS`
        — умолчание носителей у сверки. Состояние (`MARA_STATE`), gpg и
        индекс Basic Memory — под `HOME` песочницы по умолчанию, поэтому их
        переменные убраны. `MARA_BACKUP_ALLOW_SAME_DEV` — носитель на той же
        ФС, что корень (`mi.смонтирован`): другого устройства у теста нет."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("MARA_", "GNUPG")) and k not in ("VAULT", "HOME")}
        env.update({
            "HOME": cls.home, "MARA_BLOBS": cls.root, "VAULT": cls.vault,
            "MARA_VAULT": cls.vault, "MARA_CORE_TARGETS": cls.носитель,
            "MARA_BACKUP_ALLOW_SAME_DEV": "1",
            "MARA_ENV_FILE": "/nonexistent/mara-env",
            "MARA_CONTEXTD_ENV": "/nonexistent/mara-contextd-env",
            "GIT_CONFIG_NOSYSTEM": "1"})
        return env

    @classmethod
    def живая_система(cls):
        os.makedirs(cls.root)
        for sub in reconcile.СКЕЛЕТ + reconcile.КАРТОЧКИ:
            os.makedirs(os.path.join(cls.vault, sub), exist_ok=True)
        # git пустых каталогов не возит: каркас волта — с файлами, как на doctor
        for sub in ("raw", "entities"):
            with open(os.path.join(cls.vault, sub, "README.md"), "w", encoding="utf-8") as fh:
                fh.write("# %s\n" % sub)
        cls.eid, cls.sha, cls.blob, карточки = звонок(
            cls.root, cls.vault, "drill-1",
            ("2026-09-02T14:05:00+03:00", "2026-09-02T14:23:11+03:00"), "Анна Петрова",
            "пришлите смету до пятницы",
            {"action": "прислать смету", "requester": "Анна Петрова", "explicit": True,
             "confidence": 0.93, "due_at": "2026-09-04", "deadline_explicit": True,
             "deadline_phrase": "до пятницы", "disposition": "task"}, "requests")
        cls.обязательство = [w for w in карточки if w.startswith("kb/commitments/")][0]
        con = mi.connect(cls.root)
        cls.счёт_до = {t: con.execute("select count(*) from %s" % t).fetchone()[0]
                       for t in ("events", "blobs", "transcript_segments", "commitments",
                                 "conversations", "projections", "evidence_refs")}
        con.close()
        git("init", "-q", "-b", "main", cwd=cls.vault)
        git("add", "-A", cwd=cls.vault)
        git("commit", "-q", "-m", "волт до копии базы", cwd=cls.vault)

    @classmethod
    def после_архива(cls):
        """Волт живёт дальше после архива ядра: правка рукой и карточка рукой."""
        with open(os.path.join(cls.vault, cls.обязательство), "a", encoding="utf-8") as fh:
            fh.write("\n" + ЗАМЕТКА + "\n")
        with open(os.path.join(cls.vault, КАРТОЧКА_РУКОЙ), "w", encoding="utf-8") as fh:
            fh.write("---\ntitle: позвонить Олегу\nstatus: open\nowner: me\n"
                     "promised_to: Олег\ncreated: 2026-09-05\n---\n\n"
                     "- Обещание: позвонить Олегу про сроки\n")

    @classmethod
    def бэкап(cls):
        """Штатные скрипты: архив ядра, затем волт живёт дальше, затем бандл."""
        os.makedirs(os.path.dirname(cls.пароль), mode=0o700)
        with open(cls.пароль, "w", encoding="utf-8") as fh:
            fh.write(ФРАЗА)
        os.chmod(cls.пароль, 0o600)
        r = subprocess.run(
            [sys.executable, "scripts/core-backup.py", "--root", cls.root,
             "--targets", cls.носитель, "--work", cls.песок + "/var/tmp/mara-backup"],
            cwd=КОРЕНЬ, env=cls.env, capture_output=True, text=True)
        if r.returncode:
            raise AssertionError("core-backup.py: код %d\n%s%s"
                                 % (r.returncode, r.stdout, r.stderr))
        cls.итог_бэкапа = json.loads(r.stdout)
        cls.после_архива()
        git("add", "-A", cwd=cls.vault)
        git("commit", "-q", "-m", "волт после копии базы", cwd=cls.vault)
        os.makedirs(os.path.dirname(cls.зеркало_волта))
        git("clone", "-q", "--mirror", cls.vault, cls.зеркало_волта, cwd=cls.песок)
        r = subprocess.run(
            ["bash", "scripts/vault-backup.sh"], cwd=КОРЕНЬ, capture_output=True, text=True,
            env=dict(cls.env, MIRROR=cls.зеркало_волта, TARGETS=cls.носитель,
                     WORK=cls.песок + "/var/tmp/mara-backup"))
        if r.returncode:
            raise AssertionError("vault-backup.sh: код %d\n%s%s"
                                 % (r.returncode, r.stdout, r.stderr))
        with open(os.path.join(cls.vault, cls.обязательство), encoding="utf-8") as fh:
            cls.обязательство_в_волте = fh.read()

    @classmethod
    def смерть_диска(cls):
        """Диск doctor умер: `/srv` целиком и состояние под `HOME`. Носитель и
        фраза (шаг 0 — из менеджера паролей) остаются."""
        shutil.rmtree(cls.песок + "/srv")
        for d in (".local", ".gnupg"):
            shutil.rmtree(os.path.join(cls.home, d), ignore_errors=True)

    @classmethod
    def пройти_ранбук(cls):
        """Шаги 2–8 по одной команде; результат каждой — в список."""
        шаги = []
        for шаг, команда in ранбук():
            if шаг is None or шаг.split("–")[0].rstrip("а") in ПРОПУСК:
                continue
            своя = подставить(команда, cls.песок)
            чужие = вне_песочницы(своя, cls.песок)
            if чужие:
                raise AssertionError("шаг %s: после подмены путь вне песочницы %s:\n%s"
                                     % (шаг, чужие, своя))
            t = time.monotonic()
            r = subprocess.run(["bash", "-c", своя], cwd=КОРЕНЬ, env=cls.env,
                               capture_output=True, text=True, timeout=120)
            шаги.append({"шаг": шаг, "команда": команда, "своя": своя,
                         "код": r.returncode, "out": r.stdout, "err": r.stderr,
                         "сек": time.monotonic() - t})
        return шаги

    # --- помощники ----------------------------------------------------------

    def найти(self, шаг, кусок):
        """Результат команды шага `шаг`, в тексте которой есть `кусок`."""
        подходят = [s for s in self.шаги if s["шаг"] == шаг and кусок in s["команда"]]
        self.assertEqual(len(подходят), 1, "в шаге %s ранбука команд с %r: %d"
                         % (шаг, кусок, len(подходят)))
        return подходят[0]

    def код(self, s, ждём):
        self.assertEqual(s["код"], ждём, "шаг %s, код %d, ждали %d:\n%s\n--- stdout\n%s"
                         "--- stderr\n%s" % (s["шаг"], s["код"], ждём, s["своя"],
                                             s["out"], s["err"]))

    def манифест_без_замечаний(self, s, проекций):
        """`vault_manifest --check` без единого замечания. Хвостовая «, » в
        итоговой строке — косметика `vault_manifest.строка` при пустом
        списке замечаний, поэтому сверяется начало строки и отсутствие
        строк-замечаний, а не строка целиком."""
        self.код(s, 0)
        строки = s["out"].rstrip("\n").split("\n")
        self.assertEqual(len(строки), 1, s["out"])
        self.assertRegex(строки[0], r"^манифест проекций: в реестре %d, в манифесте %d,? ?$"
                         % (проекций, проекций))


class Учение(Сценарий):
    """Бандл свежее архива на правку рукой и карточку рукой: шаги 2–8
    ранбука проходят с теми кодами, что ранбук обещает."""

    def test_ранбук_разобран_на_шаги(self):
        шаги = [s["шаг"] for s in self.шаги]
        for шаг in ("2", "3", "4", "3а", "4а", "5–8"):
            self.assertIn(шаг, шаги, "в ранбуке нет шага %s" % шаг)
        self.assertGreaterEqual(len(self.шаги), 20, "команд меньше, чем в ранбуке")
        for s in self.шаги:
            self.assertNotIn("Traceback", s["err"], s["своя"] + "\n" + s["err"])
            self.assertNotIn("sudo", s["своя"])

    def test_бэкап_до_смерти_диска_прошёл_с_учением(self):
        self.assertEqual(self.итог_бэкапа["проверка"]["аудио_сверено"], 1, self.итог_бэкапа)
        self.assertTrue(os.path.isdir(os.path.join(self.носитель, "calls")))
        self.assertEqual(len([f for f in os.listdir(self.носитель)
                              if f.startswith("vault-") and f.endswith(".bundle.gpg")]), 1)

    # --- шаг 2 --------------------------------------------------------------

    def test_шаг2_учение_с_носителя(self):
        s = self.найти("2", "--drill-only")
        self.код(s, 0)
        r = json.loads(s["out"])
        self.assertEqual(r["носитель"], self.носитель)
        for таблица, n in self.счёт_до.items():
            self.assertEqual(r["счётчики"][таблица], n, таблица)

    def test_шаг2_на_чистой_машине_аудио_сверено(self):
        """Найдено этой репетицией и починено в `проверить_аудио`: на чистой
        машине у строк `blobs` нет файлов на диске, и фильтр «файл есть»
        отбрасывал все — отчёт выходил `зеркало_проверено: 0` при одной живой
        строке в архиве, ноль вместо проверки. Копия сверяется без файла:
        хеш — из строки, шифротекст — с носителя."""
        r = json.loads(self.найти("2", "--drill-only")["out"])
        self.assertEqual((r.get("зеркало_проверено"), r.get("аудио_сверено")), (1, 1), r)
        self.assertNotIn("без_файла_и_копии", r)

    # --- шаг 3 --------------------------------------------------------------

    def test_шаг3_архив_расшифрован_и_развёрнут(self):
        self.код(self.найти("3", "core-*.tar.gz.gpg"), 0)
        self.код(self.найти("3", "tar -xzf"), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "contextd.db")))
        self.assertTrue(os.path.isfile(mi.extraction_path(self.root, self.eid)))

    def test_шаг3_старых_деревьев_нет_на_чистой_машине(self):
        """`[ -e … ] && mv …` на чистой машине: проверка ложна, строка
        отдаёт 1, `mv` не зовётся. Не отказ — так устроен `&&`; под
        `set -e` строка оболочку тоже не роняет (провал не в последней
        команде списка)."""
        for кусок in ("mv /srv/mara-blobs", "mv /srv/vault"):
            s = self.найти("3", кусок)
            self.код(s, 1)
            self.assertEqual(s["err"], "")

    def test_шаг3_листья_заведены(self):
        self.код(self.найти("3", "install -d -m 700"), 0)
        self.код(self.найти("3", "install -d -o"), 0)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)

    def test_шаг3_verify_развёрнутой_копии(self):
        s = self.найти("3", "--verify")
        self.код(s, 0)
        r = json.loads(s["out"])
        self.assertEqual((r["итог"], r["расхождения"]), ("ок", []), r)
        self.assertEqual(r["сверено_хешей"], 2, "база и извлечение")

    def test_шаг3_аудио_из_зеркала(self):
        self.код(self.найти("3", "rm /srv/mara-blobs/manifest.json"), 0)
        self.код(self.найти("3", "find calls"), 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "manifest.json")))
        self.assertFalse(os.path.exists(self.песок + "/var/tmp/core.tar.gz"))
        self.assertEqual(sha(self.blob), self.sha, "аудио расшифровалось не тем")

    def test_шаг3_волт_из_бандла(self):
        self.код(self.найти("3", "vault-*.bundle.gpg"), 0)
        self.код(self.найти("3", "git bundle verify"), 0)
        self.код(self.найти("3", "git clone"), 0)
        self.код(self.найти("3", "rm /var/tmp/vault.bundle"), 0)
        with open(os.path.join(self.vault, self.обязательство), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.обязательство_в_волте)
        self.assertTrue(os.path.isfile(os.path.join(self.vault, КАРТОЧКА_РУКОЙ)))

    # --- шаги 4, 3а, 4а -----------------------------------------------------

    def test_шаг4_миграция_и_целостность(self):
        s = self.найти("4", "--migrate")
        self.код(s, 0)
        self.assertIn("версия %d → %d, integrity_check: ok, foreign_key_check: ok"
                      % (mi.ВЕРСИЯ, mi.ВЕРСИЯ), s["out"])

    def test_шаг3а_манифест_проекций_до_переноса(self):
        """База из копии старее волта: правка рукой — «файлов не как в
        манифесте», строгое замечание, кодом не считается."""
        s = self.найти("3а", "vault_manifest.py --check")
        self.код(s, 0)
        self.assertIn("файлов не как в манифесте 1", s["out"])
        self.assertIn(self.обязательство + ": файл изменён после записи манифеста",
                      s["out"])

    def test_шаг4а_перенос_из_волта(self):
        проба = self.найти("4а", "--dry-run")
        self.код(проба, 0)
        self.assertIn("ledger_import (проба): обязательств 1, разговоров 0", проба["out"])
        self.assertIn("строки в базе нет (ключ vault:%s)" % КАРТОЧКА_РУКОЙ, проба["err"])
        всерьёз = self.найти("4а", "ledger_import.py --vault")
        self.код(всерьёз, 0)
        self.assertIn("спорных 0", всерьёз["out"])
        self.assertIn("без строки 0, строк без карточки 0", всерьёз["out"])
        con = mi.connect(self.root)
        try:
            ключи = {r[0] for r in con.execute("select source_native_id from commitments")}
        finally:
            con.close()
        self.assertIn("vault:" + КАРТОЧКА_РУКОЙ, ключи, "карточка рукой не перенесена")

    # --- шаги 5–8 -----------------------------------------------------------

    def test_шаги5_8_restore_check(self):
        """Правка рукой названа строкой «разошлось», но кодом не считается:
        `restore_check.расхождение` смотрит только целостность, блобы, id,
        evidence и «без файла»/«без источника реестра» у проекций."""
        s = self.найти("5–8", "restore_check.py")
        self.код(s, 0)
        self.assertIn("итог: сошлось", s["out"])
        self.assertIn("блобы: строк 1, без файла 0, размер не тот 0, хеш сверен 1", s["out"])
        self.assertIn("evidence: обязательств со ссылками 1, в выборке ссылок 1, "
                      "открывается 1", s["out"])
        self.assertIn("проекции: разошлось: " + self.обязательство, s["out"])

    def test_шаги5_8_пересборка_сухим_прогоном(self):
        """«Разошлось» у карточки, правленной рукой после копии базы, — код 1
        у `vault_rebuild --check`, в диффе ровно заметка рукой."""
        s = self.найти("5–8", "vault_rebuild.py --check --diff")
        self.код(s, 1)
        self.assertIn("пересборка: проекций 3, совпало 1, разошлось 1, без файла 0, "
                      "без источника 1", s["out"])
        self.assertIn("    -" + ЗАМЕТКА, s["out"])
        self.assertIn("без источника: %s — перенесена из волта" % КАРТОЧКА_РУКОЙ, s["out"])

    def test_шаги5_8_пересборка_в_пустой_каталог(self):
        s = self.найти("5–8", "--into")
        # сравнение с живым волтом идёт и тут: правка рукой — тот же код 1
        self.код(s, 1)
        self.assertIn("записано карточек: 2", s["out"])
        into = self.песок + "/var/tmp/vault-rebuilt"
        self.assertTrue(os.path.isfile(os.path.join(into, self.обязательство)))
        self.assertFalse(os.path.exists(os.path.join(into, КАРТОЧКА_РУКОЙ)),
                         "карточку рукой реестру рисовать не из чего")
        # комментарий ранбука: `--check --vault /var/tmp/vault-rebuilt` назовёт
        # карточки, которых пересборка не нарисовала («файлов нет» — из git)
        r = subprocess.run(
            [sys.executable, "scripts/vault_manifest.py", "--check", "--vault", into],
            cwd=КОРЕНЬ, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("файлов нет 1", r.stdout)
        self.assertIn(КАРТОЧКА_РУКОЙ + ": в манифесте есть, на диске нет", r.stdout)

    def test_шаги5_8_манифест_живого_волта_после_переноса_сходится(self):
        self.манифест_без_замечаний(self.найти("5–8", "vault_manifest.py --check"), 3)

    def test_шаги5_8_сверка_приёма(self):
        """Ни одной находки уровня error. Ждём одну: индекса Basic Memory на
        чистой машине нет, пока демон не поднят (шаг 9)."""
        s = self.найти("5–8", "contextd_reconcile.py")
        self.код(s, 0)
        находки = [l for l in s["out"].split("\n") if l.startswith("  [")]
        self.assertEqual(len(находки), 1, s["out"])
        self.assertTrue(находки[0].startswith("  [warn] лаг-индекса — базы Basic Memory нет"),
                        s["out"])


class ПотерянныйЗвонок(Сценарий):
    """Бандл свежее архива на целый звонок: Олег позвонил после ночного
    архива ядра, карточки звонка легли в волт и уехали в бандл, а событие,
    расшифровка, извлечение и аудио — только в живой корень, умерший с
    диском. Это потеря в пределах RPO, и ранбук должен её назвать, а не
    выдать за поломку разворачивания."""

    @classmethod
    def после_архива(cls):
        super().после_архива()
        cls.eid2, cls.sha2, cls.blob2, карточки = звонок(
            cls.root, cls.vault, "drill-2",
            ("2026-09-06T10:00:00+03:00", "2026-09-06T10:05:00+03:00"), "Олег",
            "договор подпишу в среду",
            {"action": "подписать договор", "owner": "me", "promised_to": "Олег",
             "explicit": True, "confidence": 0.9, "disposition": "task"}, "commitments")
        cls.карточки2 = sorted(карточки)

    def test_шаг3_аудио_потерянного_звонка_нет(self):
        self.assertFalse(os.path.exists(self.blob2))
        self.assertEqual(sha(self.blob), self.sha)

    def test_шаг3а_манифест_устарел_и_это_код_1(self):
        """Ранбук: «манифест устарел» здесь ожидаем — и это код 1."""
        s = self.найти("3а", "vault_manifest.py --check")
        self.код(s, 1)
        self.assertIn("манифест устарел 1", s["out"])
        self.assertNotIn("манифест повреждён", s["out"])

    def test_шаг4а_evidence_без_сегмента_названо(self):
        всерьёз = self.найти("4а", "ledger_import.py --vault")
        self.код(всерьёз, 0)
        self.assertIn("ссылок evidence без сегмента в реестре: 1 (не восстановлены)",
                      всерьёз["err"])

    def test_шаги5_8_restore_check_называет_потерянный_звонок(self):
        """Код 1 — и причина названа: у карточек звонка нет события в
        реестре. Блобы и evidence при этом сходятся: строк аудио в копии
        одна, и она на месте."""
        s = self.найти("5–8", "restore_check.py")
        self.код(s, 1)
        self.assertIn("без источника у карточек из реестра 2", s["out"])
        for rel in self.карточки2:
            self.assertIn("без источника у карточки из реестра: %s — события %s нет в реестре"
                          % (rel, self.eid2), s["out"])
        self.assertIn("блобы: строк 1, без файла 0", s["out"])
        self.assertIn("не открывается 0", s["out"])

    def test_шаги5_8_манифест_живого_волта_после_переноса_сходится(self):
        self.манифест_без_замечаний(self.найти("5–8", "vault_manifest.py --check"), 5)

    def test_шаги5_8_сверка_без_ошибок(self):
        s = self.найти("5–8", "contextd_reconcile.py")
        self.код(s, 0)
        self.assertNotIn("[error]", s["out"])
        self.assertIn("[warn] проекция-разошлась — волт и реестр разошлись: evidence "
                      "разошлось 1", s["out"])



class ЗеркалоБезФайлов(unittest.TestCase):
    """`проверить_аудио` без файлов на диске: копия есть — сверяется (это
    шаг 2 на чистой машине, тест выше); нет ни файла, ни копии — проверить
    нечем, и строка называется в итоге, а не исчезает молча."""

    def test_нет_ни_файла_ни_копии_названо(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "core_backup", os.path.join(КОРЕНЬ, "scripts", "core-backup.py"))
        cb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cb)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        root, цель = os.path.join(tmp, "blobs"), os.path.join(tmp, "target")
        os.makedirs(цель)
        строки = [{"sha256": "a" * 64, "path": os.path.join(root, "calls", "aa", "a.wav"),
                   "created": mi.now_iso()}]
        итог = cb.проверить_аудио(None, root, цель, "/нет-фразы", строки=строки,
                                  курсор=os.path.join(tmp, "курсор.json"))
        self.assertEqual(итог, {"аудио_сверено": 0, "аудио_перечитано": 0,
                                "зеркало_проверено": 0, "без_файла_и_копии": 1})


if __name__ == "__main__":
    unittest.main()
