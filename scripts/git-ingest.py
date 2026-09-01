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
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line: continue
        name, _, kind = line.partition("\t")
        out.append((name.strip(), (kind or "own").strip()))
    return out

def git(args, cwd=None, timeout=180):
    p = subprocess.run(["git"] + args, cwd=cwd, timeout=timeout,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")

def mirror(name, root):
    """Зеркало репозитория. Вернуть путь или (None, причина).
    Приватные репозитории по https недоступны: на doctor лежит deploy-key от
    одного mara-second-brain, а не пользовательский ключ. Такие пропускаем и
    называем поимённо — молча терять половину репозиториев нельзя."""
    path = os.path.join(root, name + ".git")
    if os.path.isdir(path):
        rc, _, err = git(["remote", "update", "--prune"], cwd=path)
    else:
        rc, _, err = git(["clone", "--mirror", "--quiet",
                          "https://github.com/%s/%s.git" % (OWNER, name), path], timeout=900)
    if rc:
        why = "нет доступа" if re.search(r"Authentication|not found|403|denied", err) else err.strip().split("\n")[-1][:80]
        return None, why
    return path, None

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

def card(name, day, text, canon, raw_rel):
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
          "sensitive: false",
          "distilled: false",
          "---", "",
          "# %s — %s" % (name, day.isoformat()), "",
          "Дистилляция не прошла: заметка соберётся из `%s`." % raw_rel, ""]
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
    ap.add_argument("--author", default=os.environ.get("MARA_GIT_AUTHOR", "hleserg@gmail.com"),
                    help="чьи коммиты брать в форках")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    day = (datetime.fromisoformat(a.date).date() if a.date
           else (datetime.now().date() - timedelta(days=1)))
    if not os.path.isdir(a.mirrors): os.makedirs(a.mirrors)
    canon = canon_map(a.vault)
    made = skipped = 0
    unreachable = []
    for name, kind in repos():
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
            write(cardp, card(name, day, text, canon, raw_rel))
            write(os.path.join(a.vault, "_system/queue", "git-%s-%s.json" % (name, day)),
                  json.dumps({"kind": "git", "source": "git", "source_id": "%s@%s" % (name, day),
                              "note": os.path.relpath(cardp, a.vault), "raw": raw_rel,
                              "queued": datetime.now().astimezone().isoformat(timespec="seconds")},
                             ensure_ascii=False, indent=2) + "\n")
        made += 1
    print("git-ingest %s: заметок %d, уже было %d" % (day, made, skipped))
    if unreachable:
        print("не забрано: " + ", ".join(unreachable))
    return 0

def self_check():
    import tempfile
    d = tempfile.mkdtemp()
    conf = os.path.join(d, "repos.txt")
    open(conf, "w").write("# коммент\n\nAttadipa\town\nazimut\tfork\n")
    assert repos(conf) == [("Attadipa", "own"), ("azimut", "fork")]
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
    assert "<API_KEY>" in scrub("ключ sk-or-v1-abcdefghijklmnopqrstuvwx в сообщении")
    print("git-ingest: самопроверка ок")
    return 0

if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
