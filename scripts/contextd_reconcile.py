#!/usr/bin/env python3
"""Сверка состояния приёма (спека §9, ТЗ §17). Крон раз в час.

Пять инвариантов. То, что чинится однозначно, сверка чинит сама: ставит
пропущенную работу извлечения, снимает с ретраев работу, у которой пропал
исходник. То, где нужен человек, она только докладывает — файлы не удаляет
никогда, даже осиротевшие: единственная копия личного разговора стирается по
ретеншену или по прямой команде, а не по догадке.

    python3 scripts/contextd_reconcile.py
    python3 scripts/contextd_reconcile.py --json
    python3 scripts/contextd_reconcile.py --self-check
"""
import os, sys, json, glob, sqlite3, argparse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mara_ingest as mi

VAULT = os.environ.get("VAULT", "/srv/vault")
BM_DB = os.environ.get("MARA_BM_DB", os.path.expanduser("~/.basic-memory/memory.db"))
КАРТОЧКИ = ("kb/conversations", "kb/commitments")


def находка(check, level, detail, **kw):
    d = {"check": check, "level": level, "detail": detail}
    d.update(kw)
    return d


def манифест_без_блоба(con, root):
    """Манифест есть, записи нет, и она не убрана по ретеншену — это поломка."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "manifests", "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                man = json.load(fh)
        except (OSError, ValueError) as e:
            out.append(находка("манифест-не-читается", "error", "%s: %s" % (path, e)))
            continue
        if man.get("purged"):
            continue
        sha = (man.get("recording") or {}).get("audio_sha256")
        if not sha:
            continue
        b = con.execute("select path, purged_at from blobs where sha256=?",
                        (sha,)).fetchone()
        if b and b["purged_at"]:
            continue
        if b and b["path"] and os.path.exists(b["path"]):
            continue
        eid = man.get("id") or os.path.basename(path)[:-5]
        con.execute("update jobs set state='dlq', last_error=? "
                    "where event_id=? and state='ready'",
                    ("манифест есть, записи нет: " + sha[:12], eid))
        out.append(находка("манифест-без-блоба", "error",
                           "у события %s пропала запись %s" % (eid, sha[:12]),
                           event_id=eid))
    return out


def блоб_без_манифеста(con, root):
    """Осиротевший файл. Только доклад: удалять единственную копию не наше дело."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "calls", "*", "*", "*"))):
        sha = os.path.basename(path).split(".")[0]
        if con.execute("select 1 from blobs where sha256=?", (sha,)).fetchone():
            continue
        out.append(находка("блоб-без-манифеста", "warn",
                           "файл %s не знаком базе, руками решить что с ним" % path,
                           path=path, bytes=os.path.getsize(path)))
    return out


def транскрипт_без_извлечения(con, root):
    """Расшифровка есть, извлечения нет и работу никто не поставил — поставить."""
    out = []
    for path in sorted(glob.glob(os.path.join(root, "transcripts", "*.jsonl"))):
        eid = os.path.basename(path)[:-6]
        if os.path.exists(mi.extraction_path(root, eid)):
            continue
        занято = con.execute(
            "select 1 from jobs where event_id=? and kind in ('extract','project') "
            "and state in ('ready','dlq','done')", (eid,)).fetchone()
        if занято:
            continue
        if not con.execute("select 1 from events where id=?", (eid,)).fetchone():
            out.append(находка("транскрипт-без-события", "warn",
                               "расшифровка %s без события в базе" % eid))
            continue
        mi.add_job(con, eid, "extract")
        out.append(находка("извлечение-поставлено", "fixed",
                           "у %s была расшифровка без работы извлечения" % eid,
                           event_id=eid))
    return out


def лаг_индекса(vault, bm_db):
    """Карточки, которых Basic Memory ещё не видит. Только счёт, чинить нечего."""
    if not vault or not os.path.exists(bm_db or ""):
        return []
    свои = set()
    for sub in КАРТОЧКИ:
        for p in glob.glob(os.path.join(vault, sub, "*.md")):
            свои.add(os.path.relpath(p, vault))
    if not свои:
        return []
    con = sqlite3.connect("file:%s?mode=ro" % bm_db, uri=True, timeout=10)
    try:
        видит = {r[0] for r in con.execute("select file_path from entity")}
    except sqlite3.Error as e:
        return [находка("лаг-индекса", "warn", "база Basic Memory не читается: %s" % e)]
    finally:
        con.close()
    нет = sorted(свои - видит)
    if not нет:
        return []
    return [находка("лаг-индекса", "warn",
                    "не проиндексировано карточек: %d" % len(нет),
                    count=len(нет), sample=нет[:5])]


def ретеншен_просрочен(con):
    """Уборка не отработала. Не чиним здесь: у уборки свой крон и свой лог."""
    import blob_retention as br
    late = br.просроченные(con)
    if not late:
        return []
    return [находка("ретеншен-просрочен", "warn",
                    "записей ждут уборки: %d" % len(late), count=len(late))]


def dlq(con):
    n = con.execute("select count(*) from jobs where state='dlq'").fetchone()[0]
    if not n:
        return []
    row = con.execute("select kind, last_error from jobs where state='dlq' "
                      "order by updated desc limit 1").fetchone()
    return [находка("работы-в-dlq", "warn", "в DLQ работ: %d, последняя %s — %s"
                    % (n, row["kind"], (row["last_error"] or "")[:120]), count=n)]


def run(con, root=None, vault=VAULT, bm_db=BM_DB):
    root = root or mi.ROOT
    out = []
    out += манифест_без_блоба(con, root)
    out += блоб_без_манифеста(con, root)
    out += транскрипт_без_извлечения(con, root)
    out += лаг_индекса(vault, bm_db)
    out += ретеншен_просрочен(con)
    out += dlq(con)
    return out


def код(находки):
    """Ненулевой код только на настоящей поломке: крон не должен кричать зря."""
    return 1 if any(f["level"] == "error" for f in находки) else 0


def main():
    ap = argparse.ArgumentParser(description="сверка состояния приёма")
    ap.add_argument("--root", default=mi.ROOT)
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--bm-db", default=BM_DB)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--self-check", action="store_true", dest="self_check")
    a = ap.parse_args()
    if a.self_check:
        return self_check()
    mi.ROOT = a.root
    находки = run(mi.connect(a.root), a.root, a.vault, a.bm_db)
    if a.as_json:
        print(json.dumps(находки, ensure_ascii=False, indent=2))
    elif not находки:
        print("%s сверка: всё сходится" % mi.now_iso())
    else:
        print("%s сверка: находок %d" % (mi.now_iso(), len(находки)))
        for f in находки:
            print("  [%s] %s — %s" % (f["level"], f["check"], f["detail"]))
    return код(находки)


def self_check():
    import tempfile
    root = tempfile.mkdtemp()
    mi.ROOT = root
    con = mi.connect(root)
    assert run(con, root, vault=None) == [], "на пустой базе находок быть не должно"
    assert код([]) == 0
    eid, _ = mi.put_event(con, {"kind": "call", "source": "sc", "source_id": "1",
                                "blob": {"sha256": "d" * 64, "ext": "m4a"}})
    mi.write_json(mi.manifest_path(root, eid),
                  {"id": eid, "recording": {"audio_sha256": "d" * 64}, "purged": None})
    mi.add_job(con, eid, "asr")
    f = run(con, root, vault=None)
    assert [x for x in f if x["check"] == "манифест-без-блоба"], "пропавший блоб не найден"
    assert код(f) == 1, "поломка должна давать ненулевой код"
    assert con.execute("select state from jobs where event_id=?",
                       (eid,)).fetchone()["state"] == "dlq", "работа осталась в ретраях"
    tp = mi.transcript_path(root, eid)
    os.makedirs(os.path.dirname(tp), mode=0o700, exist_ok=True)
    open(tp, "w").close()
    run(con, root, vault=None)
    kinds = {r["kind"] for r in con.execute("select kind from jobs where event_id=?",
                                            (eid,))}
    assert "extract" in kinds, "извлечение не поставлено"
    n = con.execute("select count(*) from jobs where kind='extract'").fetchone()[0]
    run(con, root, vault=None)
    assert con.execute("select count(*) from jobs where kind='extract'").fetchone()[0] == n, \
        "повторная сверка плодит работы"
    print("contextd_reconcile self-check: ок")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
