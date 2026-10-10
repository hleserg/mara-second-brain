"""`purge-from-vault.sh`: отказ R2 — не «файла нет», отказ бандла — не ноль.

До правки скрипт читал любой отказ `rclone deletefile` как «уже нет», а
отказ `rclone lsf` на проверке — как пустой ответ: при недоступном R2 он
переписывал историю и выходил нулём, а файл в бакете оставался
(`docs/retention-policy.md` §3.2). Здесь скрипт исполняется целиком на
игрушечном волте с подделками `rclone` и `git-filter-repo`: подделка R2 —
каталог, подделка истории — `git filter-branch` с той же сигнатурой.
Отказ `vault-backup.sh` скрипт раньше глотал через `|| echo`: старые бандлы
снесены, нового нет, код ноль (Codex, PR #142). Проверка истории через
`git log | grep -q .` под pipefail на длинной истории ловила SIGPIPE и
читалась как «чисто» (ревью PR #143)."""
import os, shutil, stat, subprocess, tempfile, unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
СКРИПТ = os.path.join(ROOT, "scripts", "purge-from-vault.sh")

# Подделка rclone: бакет — каталог $FAKE_R2; каждый вызов дописывается в
# $FAKE_R2_LOG; $FAKE_R2_FAIL_AFTER=N валит все вызовы после N-го кодом 5
# (у rclone — временная ошибка); $FAKE_R2_MISSING_CODE — каким кодом
# отвечать на lsf отсутствующего пути (по умолчанию 0 и пусто, как S3);
# $FAKE_R2_DELETE_FAILS=1 — deletefile отказывает, файл остаётся;
# $FAKE_R2_DELETE_LIES=1 — deletefile отчитывается нулём, файл остаётся.
# Флаги вида --x и --x=y пропускаются, путь — последний аргумент без тире.
RCLONE = r'''#!/usr/bin/env bash
echo "$*" >>"$FAKE_R2_LOG"
n=$(wc -l <"$FAKE_R2_LOG")
if [ -n "${FAKE_R2_FAIL_AFTER:-}" ] && [ "$n" -gt "$FAKE_R2_FAIL_AFTER" ]; then
  echo "Failed to $1: connection refused" >&2; exit 5
fi
cmd=$1; shift
path=""
for a in "$@"; do case $a in --*) ;; *) path=${a#r2:bucket}; path=${path#/};; esac; done
case $cmd in
  lsf)
    if [ -f "$FAKE_R2/$path" ]; then basename "$path"
    elif [ -d "$FAKE_R2/$path" ]; then ls -1 "$FAKE_R2/$path"
    else exit "${FAKE_R2_MISSING_CODE:-0}"; fi ;;
  deletefile)
    [ -f "$FAKE_R2/$path" ] || { echo "object not found" >&2; exit 4; }
    [ -z "${FAKE_R2_DELETE_FAILS:-}" ] || { echo "AccessDenied" >&2; exit 2; }
    [ -z "${FAKE_R2_DELETE_LIES:-}" ] || exit 0
    rm -f "${FAKE_R2:?}/$path" ;;
  *) echo "подделка rclone: $cmd" >&2; exit 1 ;;
esac
'''

# Подделка git-filter-repo: те же `--invert-paths --path X ... --force`.
# Пути квотируются (`%q`), каталоги не поддерживаются (`git rm` без `-r`) —
# скрипт их и не пропускает. $FAKE_FR_NOOP=1 — ничего не переписывать:
# так проверяется, что финальная проверка истории не врёт.
FR = r'''#!/usr/bin/env bash
set -e
[ -z "${FAKE_FR_NOOP:-}" ] || exit 0
paths=""
while [ $# -gt 0 ]; do
  case $1 in --path) paths="$paths $(printf '%q' "$2")"; shift 2;; *) shift;; esac
done
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f \
  --index-filter "git rm -q --cached --ignore-unmatch -- $paths" -- --all >/dev/null 2>&1
rm -rf .git/refs/original
'''

АВТОР = dict(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")


def _git(репо, *args):
    return subprocess.run(["git", "-C", репо, *args], check=True,
                          capture_output=True, text=True,
                          env=dict(os.environ, **АВТОР)).stdout


class Вычистка(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        for имя, текст in (("rclone", RCLONE), ("git-filter-repo", FR)):
            p = os.path.join(bin_, имя)
            with open(p, "w", encoding="utf-8") as f:
                f.write(текст)
            os.chmod(p, 0o755)
        self.r2 = os.path.join(self.tmp, "r2")
        self.log = os.path.join(self.tmp, "rclone.log")
        self.vault = os.path.join(self.tmp, "vault")
        os.makedirs(os.path.join(self.vault, "secret"))
        os.makedirs(self.r2)
        _git(self.vault, "init", "-q", "-b", "main")
        self._пишу("a.md", "обычная карточка\n")
        self._пишу("secret/key.md", "token=первый\n")
        self._коммит("раз")
        self._пишу("secret/key.md", "token=второй\n")
        self._коммит("два")
        pass_ = os.path.join(self.tmp, "pass")
        with open(pass_, "w") as f:
            f.write("фраза\n")
        os.chmod(pass_, stat.S_IRUSR | stat.S_IWUSR)
        self.бандлы = os.path.join(self.tmp, "bundles")
        os.makedirs(self.бандлы)
        self.mirror = os.path.join(self.tmp, "mirror.git")
        # Всё, что скрипт и vault-backup.sh читают из окружения, прибито:
        # KEEP из окружения гейта с нулём снёс бы только что собранный бандл.
        self.env = dict(
            os.environ, VAULT=self.vault, MIRROR=self.mirror, REMOTE="r2:bucket",
            RCLONE=os.path.join(bin_, "rclone"),
            FR=os.path.join(bin_, "git-filter-repo"),
            TARGETS=self.бандлы, PASS=pass_, KEEP="8",
            WORK=os.path.join(self.tmp, "work"), MARA_BACKUP_ALLOW_SAME_DEV="1",
            FAKE_R2=self.r2, FAKE_R2_LOG=self.log, **АВТОР)
        for к in ("BUNDLES", "FAKE_R2_FAIL_AFTER", "FAKE_R2_MISSING_CODE",
                  "FAKE_R2_DELETE_FAILS", "FAKE_R2_DELETE_LIES", "FAKE_FR_NOOP"):
            self.env.pop(к, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _пишу(self, путь, текст):
        with open(os.path.join(self.vault, путь), "w", encoding="utf-8") as f:
            f.write(текст)

    def _коммит(self, сообщение):
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-q", "-m", сообщение)

    def _в_r2(self, путь, текст="token=второй\n"):
        p = os.path.join(self.r2, путь)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(текст)

    def _прогон(self, *файлы, **env):
        return subprocess.run(["bash", СКРИПТ, *файлы], capture_output=True,
                              text=True, env=dict(self.env, **env), timeout=120)

    def _в_истории(self, путь, репо=None):
        return bool(_git(репо or self.vault, "log", "--all", "--full-history",
                         "--format=%H", "--", путь).strip())

    def _вызовы(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [с.split()[0] for с in f.read().splitlines()]

    def _есть_в_r2(self, путь):
        return os.path.exists(os.path.join(self.r2, путь))

    def _бандлы(self):
        return [f for f in os.listdir(self.бандлы) if f.endswith(".bundle.gpg")]

    def test_обычный_ход_удаляет_из_r2_и_из_истории(self):
        self._в_r2("secret/key.md")
        r = self._прогон("secret/key.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self._есть_в_r2("secret/key.md"))
        self.assertFalse(self._в_истории("secret/key.md"))
        self.assertTrue(self._в_истории("a.md"))
        self.assertIn("чисто в R2:  secret/key.md", r.stdout)
        self.assertIn("чисто в git: secret/key.md", r.stdout)
        self.assertIn("deletefile", self._вызовы())
        # Зеркало несёт новую историю, а не старую: тот же HEAD, файла нет.
        self.assertFalse(self._в_истории("secret/key.md", self.mirror))
        self.assertEqual(_git(self.mirror, "rev-parse", "HEAD"),
                         _git(self.vault, "rev-parse", "HEAD"))
        self.assertEqual(len(self._бандлы()), 1, r.stdout + r.stderr)

    def test_файла_в_r2_уже_нет(self):
        r = self._прогон("secret/key.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("(уже нет)", r.stdout)
        self.assertNotIn("deletefile", self._вызовы())
        self.assertFalse(self._в_истории("secret/key.md"))

    def test_код_3_и_4_от_lsf_это_нет_файла(self):
        # На отсутствующий путь rclone отвечает по-разному: S3 — пусто и
        # ноль, другие бэкенды — кодом 3 (каталога нет) или 4 (файла нет).
        # Все три — «нет», а не отказ. Волт между кодами — свежий, иначе
        # второй прогон шёл бы по уже вычищенной истории.
        for код in ("3", "4"):
            with self.subTest(код=код):
                if код != "3":
                    self.tearDown()
                    self.setUp()
                r = self._прогон("secret/key.md", FAKE_R2_MISSING_CODE=код)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("(уже нет)", r.stdout)
                self.assertIn("чисто в R2", r.stdout)
                self.assertFalse(self._в_истории("secret/key.md"))

    def test_содержимое_каталога_в_r2_не_считается_остатком(self):
        # lsf по пути, под которым в бакете лежит «каталог» с тем же именем
        # или соседние файлы, возвращает их список; считается только точное
        # имя. Файл ушёл, соседи остались, код ноль.
        self._в_r2("secret/key.md")
        self._в_r2("secret/key.md.bak")
        r = self._прогон("secret/key.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("чисто в R2:  secret/key.md", r.stdout)
        self.assertTrue(self._есть_в_r2("secret/key.md.bak"))
        # Теперь по пути файла в бакете «каталог»: lsf отвечает его
        # содержимым, точного имени там нет — это «нет», не остаток.
        self._в_r2("secret/key.md/other")
        r = self._прогон("secret/key.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("(уже нет)", r.stdout)
        self.assertIn("чисто в R2:  secret/key.md", r.stdout)

    def test_путь_с_точкой_и_слешем_нормализуется(self):
        # `./secret/key.md` для git-filter-repo — не `secret/key.md`
        # (сравнение строк), а R2 ответил бы «нет»: скрипт срезает `./`.
        self._в_r2("secret/key.md")
        r = self._прогон("././secret/key.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self._есть_в_r2("secret/key.md"))
        self.assertFalse(self._в_истории("secret/key.md"))
        with open(self.log, encoding="utf-8") as f:
            журнал = f.read()
        self.assertIn("r2:bucket/secret/key.md", журнал)
        self.assertNotIn("./", журнал)

    def test_каталог_и_чужие_пути_отвергаются_до_изменений(self):
        self._в_r2("secret/key.md")
        for путь in ("secret", "secret/", "/srv/vault/secret/key.md",
                     "../x", "secret/../a.md", ""):
            with self.subTest(путь=путь):
                r = self._прогон(путь)
                self.assertEqual(r.returncode, 2, путь + r.stdout + r.stderr)
        self.assertEqual(self._вызовы(), [])
        self.assertTrue(os.path.exists(os.path.join(self.vault, "secret/key.md")))
        self.assertTrue(self._в_истории("secret/key.md"))
        self.assertTrue(self._есть_в_r2("secret/key.md"))

    def test_remote_без_бакета_отвергается(self):
        r = self._прогон("secret/key.md", REMOTE="r2:")
        self.assertEqual(r.returncode, 2)
        self.assertIn("нужен бакет", r.stderr)
        self.assertEqual(self._вызовы(), [])

    def test_r2_недоступен_с_начала_ничего_не_трогает(self):
        self._в_r2("secret/key.md")
        r = self._прогон("secret/key.md", FAKE_R2_FAIL_AFTER="0")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R2 недоступен", r.stderr)
        self.assertIn("не начата", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.vault, "secret/key.md")))
        self.assertTrue(self._в_истории("secret/key.md"))
        self.assertFalse(os.path.exists(self.mirror))
        self.assertEqual(self._вызовы(), ["lsf"])

    def test_r2_отвалился_на_удалении_история_не_переписана(self):
        # Первый вызов — проверка доступа, второй — lsf файла: он и падает.
        self._в_r2("secret/key.md")
        r = self._прогон("secret/key.md", FAKE_R2_FAIL_AFTER="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R2 не ответил", r.stderr)
        self.assertIn("остановлена до переписывания истории", r.stderr)
        self.assertTrue(self._в_истории("secret/key.md"))
        self.assertTrue(self._есть_в_r2("secret/key.md"))
        self.assertFalse(os.path.exists(self.mirror))
        self.assertEqual(self._вызовы(), ["lsf", "lsf"])
        # Повтор при живом R2 доделывает с того же места.
        os.remove(self.log)
        r = self._прогон("secret/key.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self._в_истории("secret/key.md"))
        self.assertFalse(self._есть_в_r2("secret/key.md"))

    def test_два_файла_отказ_на_втором_повтор_доделывает(self):
        # Доступ, lsf первого, deletefile первого — удачны; lsf второго
        # падает. Первый ушёл из R2 и из рабочей копии, второй — только из
        # рабочей копии, история цела; повтор доделывает оба.
        self._пишу("b.md", "вторая\n")
        self._коммит("три")
        self._в_r2("secret/key.md")
        self._в_r2("b.md", "вторая\n")
        r = self._прогон("secret/key.md", "b.md", FAKE_R2_FAIL_AFTER="3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("остановлена до переписывания истории", r.stderr)
        self.assertFalse(self._есть_в_r2("secret/key.md"))
        self.assertTrue(self._есть_в_r2("b.md"))
        self.assertTrue(self._в_истории("secret/key.md"))
        self.assertTrue(self._в_истории("b.md"))
        os.remove(self.log)
        r = self._прогон("secret/key.md", "b.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self._есть_в_r2("b.md"))
        self.assertFalse(self._в_истории("secret/key.md"))
        self.assertFalse(self._в_истории("b.md"))
        self.assertTrue(self._в_истории("a.md"))

    def test_deletefile_не_смог_останавливает(self):
        # R2 отвечает, файл есть, удалить не удалось: отказ, а не «уже нет».
        self._в_r2("secret/key.md")
        r = self._прогон("secret/key.md", FAKE_R2_DELETE_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("не удалось удалить secret/key.md", r.stderr)
        self.assertIn("AccessDenied", r.stderr)
        self.assertTrue(self._в_истории("secret/key.md"))
        self.assertTrue(self._есть_в_r2("secret/key.md"))

    def test_r2_отвалился_на_проверке_код_не_ноль(self):
        # Доступ, lsf файла, deletefile — три удачных, четвёртый (проверка)
        # падает: раньше это было «чисто в R2» и ноль.
        self._в_r2("secret/key.md")
        r = self._прогон("secret/key.md", FAKE_R2_FAIL_AFTER="3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("R2 НЕ ОТВЕТИЛ: secret/key.md", r.stderr)
        self.assertNotIn("чисто в R2", r.stdout)
        self.assertFalse(self._в_истории("secret/key.md"))

    def test_файл_остался_в_r2_после_удаления(self):
        # deletefile отчитался нулём, а на проверке файл всё ещё виден:
        # «ОСТАЛОСЬ В R2» и код не ноль, как и для истории.
        self._в_r2("secret/key.md")
        r = self._прогон("secret/key.md", FAKE_R2_DELETE_LIES="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ОСТАЛОСЬ В R2: secret/key.md", r.stderr)
        self.assertIn("чисто в git: secret/key.md", r.stdout)

    def test_длинная_история_не_прячется_за_sigpipe(self):
        # filter-repo промахнулся (подделка ничего не переписывает), в
        # истории файла сорок коммитов. `git log | grep -q .` под pipefail
        # давал бы 141 и «чисто в git»; проверка должна сказать «ОСТАЛОСЬ».
        for i in range(40):
            self._пишу("secret/key.md", "token=%d\n" % i)
            self._коммит("правка %d" % i)
        r = self._прогон("secret/key.md", FAKE_FR_NOOP="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ОСТАЛОСЬ В ИСТОРИИ: secret/key.md", r.stderr)
        self.assertNotIn("чисто в git", r.stdout)

    def test_бандл_не_собрался_код_не_ноль(self):
        # Старые бандлы снесены, парольной фразы нет — новый не собрался:
        # раньше `|| echo` и ноль, теперь код 1 с диагнозом.
        self._в_r2("secret/key.md")
        старый = os.path.join(self.бандлы, "vault-2026-01-05.bundle.gpg")
        open(старый, "wb").close()
        r = self._прогон("secret/key.md", PASS=os.path.join(self.tmp, "нет"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("НОВЫЙ БАНДЛ НЕ СОБРАЛСЯ", r.stderr)
        self.assertFalse(os.path.exists(старый))
        self.assertIn("чисто в R2:  secret/key.md", r.stdout)
        self.assertIn("чисто в git: secret/key.md", r.stdout)

    def test_bundles_синоним_targets(self):
        # Один список носителей на снос старых и на сборку нового: BUNDLES
        # без TARGETS доезжает до vault-backup.sh.
        self._в_r2("secret/key.md")
        env = dict(self.env, BUNDLES=self.бандлы)
        env.pop("TARGETS")
        r = subprocess.run(["bash", СКРИПТ, "secret/key.md"], capture_output=True,
                           text=True, env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self._бандлы()), 1)


if __name__ == "__main__":
    unittest.main()
