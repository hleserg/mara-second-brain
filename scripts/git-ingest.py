#!/usr/bin/env python3
"""Git-ingest (ТЗ §6.5): что менялось в репозиториях за день.

Ночью обновляем зеркала, берём лог за сутки, кладём сырьё в `raw/git/` и
ставим задачу в очередь. Заметку пишет дистиллятор — здесь только сбор,
ровно как у session-note.py.

Почему зеркала, а не рабочие клоны: клоны на doctor отстают месяцами
(atman — на май), а работа идёт на других машинах. Зеркало же тянет прямо
с GitHub, и машина, где сидит человек, ни при чём (§13.6).

    python3 scripts/git-ingest.py --vault /srv/vault            # за вчера
    python3 scripts/git-ingest.py --date 2026-08-31 --dry-run
"""
import os, re, sys, json, fcntl, argparse, subprocess, contextlib
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vault_common import canon_map, link, scrub, yaml_str

OWNER = os.environ.get("MARA_GH_OWNER", "hleserg")
CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "../config/git-repos.txt")
# Сообщения коммитов, а не диффы. Дифф — самое секретосодержащее, что есть в
# репозитории, а этот текст едет в облако к дистиллятору. Пусть в нём
# структурно нечему утечь, даже если регэксп §8.3 что-то прозевает.
FMT = "%h %an%n%s%n%b"
MAX_CHARS = 60000          # лог за сутки столько не занимает даже в худший день

def repos(path=CONF):
    """(имя, own|fork, секретный ли). Третья колонка `sensitive` — репозиторий,
    который не отправляем дистиллятору в облако (§8.3.3): карточка и сырьё
    остаются, выжимки не будет."""
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line: continue
        cols = [c.strip() for c in line.split("\t") if c.strip()]
        out.append((cols[0], (cols[1:2] or ["own"])[0], "sensitive" in cols[2:]))
    return out

# Без этого git на приватном репозитории садится спрашивать логин, и ночной
# прогон встаёт колом до таймаута.
ENV = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", GCM_INTERACTIVE="never")
NOAUTH = re.compile(r"could not read Username|Authentication|not found|403|denied|Permission")

def git(args, cwd=None, timeout=180):
    p = subprocess.run(["git"] + args, cwd=cwd, timeout=timeout, env=ENV,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

# Зеркало, уже обновлённое в этом прогоне. Бэкфилл идёт по дням и заходит в
# каждый репозиторий сотни раз; без этого он сотни раз дёрнул бы и GitHub.
FETCHED = set()

def mirror(name, root):
    """Зеркало репозитория. Вернуть путь или (None, причина).
    Недоступное называем поимённо — молча терять репозитории нельзя."""
    path = os.path.join(root, name + ".git")
    if os.path.isdir(path):
        if name in FETCHED: return path, None
        rc, _, err = git(["remote", "update", "--prune"], cwd=path)
        if not rc: FETCHED.add(name); return path, None
    else:
        err = ""
        # https работает без ключей для всего публичного; приватное отдаётся
        # только по ssh — и то, если ключ пользовательский.
        for url in ("https://github.com/%s/%s.git" % (OWNER, name),
                    "git@github.com:%s/%s.git" % (OWNER, name)):
            rc, _, err = git(["clone", "--mirror", "--quiet", url, path], timeout=900)
            if not rc: return path, None
    return None, ("нет доступа" if NOAUTH.search(err)
                  else err.strip().split("\n")[-1][:80] or "rc=%d" % rc)

def days(start, end):
    """Дни включительно с обоих концов: один день — это [start], а не пусто."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]

def log(path, day, kind, author):
    """Коммиты за сутки по всем веткам. У форка — только свои: иначе в заметку
    поедет апстрим, которого человек в глаза не видел."""
    args = ["log", "--all", "--no-merges", "--date-order",
            "--since", "%s 00:00" % day, "--until", "%s 00:00" % (day + timedelta(days=1)),
            "--format=" + FMT, "--stat", "--stat-width=90"]
    if kind == "fork": args += ["--author", author]
    rc, out, err = git(args, cwd=path)
    if rc: raise RuntimeError(err.strip()[:200])
    return out.strip()

def card(name, day, text, canon, raw_rel, sensitive=False):
    n = len(re.findall(r"(?m)^[0-9a-f]{7,} ", text))
    fm = ["---",
          "title: " + yaml_str("%s: %d коммит%s за %s" % (
              name, n, "" if n % 10 == 1 and n % 100 != 11 else
              "а" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "ов", day)),
          "type: note",
          "source: git",
          "source_id: %s@%s" % (name, day),
          "created: " + datetime.now().astimezone().isoformat(timespec="seconds"),
          "occurred: " + day.isoformat(),
          "project: " + yaml_str(link(name, canon)),
          "tags: [git, %s]" % name.lower(),
          "sensitive: %s" % ("true" if sensitive else "false"),
          "distilled: false",
          "---", "",
          "# %s — %s" % (name, day.isoformat()), "",
          ("Рабочий репозиторий: в облако не отправляется (§8.3.3), выжимки не "
           "будет. Сырьё — `%s`." % raw_rel) if sensitive else
          "Ждёт дистилляции, сырьё — `%s`." % raw_rel, ""]
    return "\n".join(fm)

def write(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d): os.makedirs(d)
    tmp = path + ".tmp"                # рядом крутятся автокоммит и bisync
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
    os.replace(tmp, path)

@contextlib.contextmanager
def locked(vault):
    """Общий с автокоммитом и bisync флок (§13.8). Берём его только вокруг
    записи в волт: качать зеркала под ним — значит держать синк все те минуты,
    что идёт clone."""
    fh = open(os.path.join(vault, ".git/vault-git.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=os.environ.get("VAULT", "/srv/vault"))
    ap.add_argument("--mirrors", default=os.environ.get("MARA_MIRRORS", "/srv/git-mirrors"))
    ap.add_argument("--date", help="день в ISO; по умолчанию вчерашний")
    ap.add_argument("--since", help="бэкфилл: с этого дня по --date включительно")
    # Подстрока, а не адрес целиком: --author у git — регэксп по «Имя <почта>»,
    # а коммиты, сделанные через веб-морду GitHub, подписаны
    # hleserg@users.noreply.github.com. По полному адресу они бы потерялись.
    ap.add_argument("--author", default=os.environ.get("MARA_GIT_AUTHOR", "hleserg"),
                    help="чьи коммиты брать в форках (подстрока автора)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    end = (datetime.fromisoformat(a.date).date() if a.date
           else (datetime.now().date() - timedelta(days=1)))
    start = datetime.fromisoformat(a.since).date() if a.since else end
    if not os.path.isdir(a.mirrors): os.makedirs(a.mirrors)
    canon = canon_map(a.vault)
    made = skipped = 0
    unreachable = []
    for day, (name, kind, sens) in ((d, r) for d in days(start, end) for r in repos()):
        cardp = os.path.join(a.vault, "kb/notes", "git-%s-%s.md" % (name, day))
        if os.path.exists(cardp): skipped += 1; continue    # идемпотентность
        path, why = mirror(name, a.mirrors)
        if not path:
            unreachable.append("%s (%s)" % (name, why)); continue
        try:
            text = scrub(log(path, day, kind, a.author))[:MAX_CHARS]
        except Exception as e:
            unreachable.append("%s (%s)" % (name, e)); continue
        if not text: continue                                # тихий день
        raw_rel = "raw/git/%s/%s.txt" % (name, day)
        if a.dry_run:
            print("=" * 60, "\n%s %s\n" % (name, day), text[:1500]); made += 1; continue
        with locked(a.vault):
            write(os.path.join(a.vault, raw_rel), text + "\n")
            write(cardp, card(name, day, text, canon, raw_rel, sens))
            # Секретный репозиторий не ставим в очередь вовсе: воркер всё равно
            # придержал бы задачу навсегда, и она бы вечно висела в отчёте.
            if sens: made += 1; continue
            write(os.path.join(a.vault, "_system/queue", "git-%s-%s.json" % (name, day)),
                  json.dumps({"kind": "git", "source": "git", "source_id": "%s@%s" % (name, day),
                              "note": os.path.relpath(cardp, a.vault), "raw": raw_rel,
                              "queued": datetime.now().astimezone().isoformat(timespec="seconds")},
                             ensure_ascii=False, indent=2) + "\n")
        made += 1
    print("git-ingest %s: заметок %d, уже было %d"
          % (start if start == end else "%s..%s" % (start, end), made, skipped))
    if unreachable:
        print("не забрано: " + ", ".join(unreachable))
    return 0

def self_check():
    import tempfile
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "repos.txt")
    open(conf, "w").write("# коммент\n\nAttadipa\town\nazimut\tfork\nwork\town\tsensitive\n")
    assert repos(conf) == [("Attadipa", "own", False), ("azimut", "fork", False),
                           ("work", "own", True)]
    # секретный репозиторий помечен и в теле сказано, что выжимки не будет
    sc = card("work", datetime(2026, 8, 31).date(), "abc1234 С\nраз\n", {}, "r", True)
    assert "sensitive: true" in sc and "в облако не отправляется" in sc
    # заголовок склоняется, проект приводится к канону, ключ из сообщения коммита
    # не доезжает до карточки
    txt = "abc1234 Сергей\nчинил синк\n\ndef5678 Сергей\nещё раз\n"
    c = card("Attadipa", datetime(2026, 8, 31).date(), txt, {"attadipa": "attadipa"}, "raw/git/x.txt")
    assert "2 коммита за 2026-08-31" in c, c
    assert 'project: "[[attadipa]]"' in c
    assert "1 коммит за" in card("x", datetime(2026, 8, 31).date(), "abc1234 С\nраз\n", {}, "r")
    assert "5 коммитов за" in card("x", datetime(2026, 8, 31).date(),
                                   "".join("abc123%d С\nраз\n" % i for i in range(5)), {}, "r")
    assert 'project: "x"' in card("x", datetime(2026, 8, 31).date(), txt, {}, "r")
    # диапазон включает оба конца, один день — это один день, а не ноль
    d1, d3 = datetime(2026, 8, 31).date(), datetime(2026, 9, 2).date()
    assert days(d1, d1) == [d1] and len(days(d1, d3)) == 3
    assert "<API_KEY>" in scrub("ключ sk-or-v1-abcdefghijklmnopqrstuvwx в сообщении")
    print("git-ingest: самопроверка ок")
    return 0

if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
