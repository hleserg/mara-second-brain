#!/usr/bin/env python3
"""Шаг 3 уборки: волтовские копии проектных доков против репозиториев.

Вопрос ровно один — не потеряются ли правки, сделанные в Obsidian и не
вернувшиеся в репу. Он решается механически: сверить копию не с сегодняшней
репой, а с КАЖДОЙ исторической версией файла. Сошлось в ноль хоть на одной —
волт просто снимок того дня, терять нечего.

Две ловушки, на которых предыдущий заход дал неверный ответ:
  * фронтматтер Basic Memory и переписанные ```text ловятся как расхождение,
    хотя содержания не меняют;
  * сравнивать надо с origin/main, а не с рабочим деревом клона: клон на
    doctor отставал на 933 коммита, и выходило, будто волт новее репы.

    python3 scripts/vault-vs-repo.py [--json]
"""
import os, re, sys, json, difflib, subprocess

VAULT = os.environ.get("VAULT", "/srv/vault")
PAIRS = [("kb/howto/atman", "~/projects/atman"),
         ("kb/howto/smart-home/iot stack", "~/projects/ha-mqtt-sensor-hub")]

def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True,
                          text=True, errors="replace").stdout

def norm(s):
    s = re.sub(r"\A---\n.*?\n---\n", "", s, flags=re.S)
    s = re.sub(r"(?m)^```text\s*$", "```", s)
    return [l.rstrip() for l in s.split("\n") if l.strip()]

def only_vault(repo_lines, vault_lines):
    n = 0
    for tag, _, _, j1, j2 in difflib.SequenceMatcher(
            None, repo_lines, vault_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"): n += j2 - j1
    return n

def scan():
    for vsub, repo in PAIRS:
        repo = os.path.expanduser(repo)
        ref = next((b for b in ("origin/main", "origin/master")
                    if git(repo, "rev-parse", "--verify", "-q", b).strip()), "HEAD")
        idx = {p.lower(): p for p in git(repo, "ls-tree", "-r", "--name-only", ref).split("\n")
               if p.endswith(".md")}
        vdir = os.path.join(VAULT, vsub)
        for root, _, files in os.walk(vdir):
            for fn in sorted(f for f in files if f.endswith(".md")):
                vp = os.path.join(root, fn)
                sub = os.path.relpath(vp, vdir)
                rp = next((idx[c.lower()] for c in (sub, "docs/" + sub) if c.lower() in idx), None)
                if not rp:
                    # Репа сама уехала вперёд и убрала майские доки в docs/archive/.
                    # По имени ищем, только если оно в репе единственное: иначе
                    # сравним заметку с первым попавшимся README.md.
                    same = [p for p in idx.values() if os.path.basename(p).lower() == fn.lower()]
                    rp = same[0] if len(same) == 1 else None
                yield os.path.relpath(vp, VAULT), repo, ref, rp, vp

def main():
    out = []
    for rel, repo, ref, rp, vp in scan():
        v = norm(open(vp, encoding="utf-8", errors="replace").read())
        if not rp:
            out.append({"file": rel, "verdict": "нет тёзки"}); continue
        today = only_vault(norm(git(repo, "show", f"{ref}:{rp}")), v)
        best, seen = (today, ref, ""), set()
        for line in git(repo, "log", "--follow", "--format=%H %as", ref, "--", rp).split("\n"):
            if not line.strip(): continue
            sha, date = line.split(" ", 1)
            blob = git(repo, "rev-parse", f"{sha}:{rp}").strip()
            if not blob or blob in seen: continue
            seen.add(blob)
            n = only_vault(norm(git(repo, "cat-file", "blob", blob)), v)
            if n < best[0]: best = (n, sha[:8], date)
            if n == 0: break
        out.append({"file": rel, "repo_path": rp, "versions": len(seen),
                    "min_only_vault": best[0], "at": best[1], "date": best[2],
                    "verdict": "копия репы" if best[0] == 0 else "есть свои строки"})
    if "--json" in sys.argv:
        print(json.dumps(out, ensure_ascii=False, indent=1)); return
    from collections import Counter
    for k, n in Counter(r["verdict"] for r in out).most_common():
        print(f"{n:4d}  {k}")
    for r in sorted((r for r in out if r["verdict"] == "есть свои строки"),
                    key=lambda r: -r["min_only_vault"]):
        print(f"      {r['min_only_vault']:4d} своих строк  {r['file']}")

if __name__ == "__main__":
    main()
