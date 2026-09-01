#!/usr/bin/env python3
"""Агрегатор расхода лимитов Claude Code и отчёты в волт (бриф §4–§7).

Ни одного вызова модели. Считает только то, что уже лежит на диске: тики
statusline (`Claude Usage/_data/<хост>/ticks-*.jsonl`) и транскрипты
(`raw/claude-code/`).

Пересобирает всё с нуля каждый прогон. Состояния нет намеренно: транскрипты с
мака приезжают только в конце сессии, и интервал недельной давности может
уточниться сегодня. Пересборка с нуля — это и приёмка §11 («rebuild даёт те же
отчёты»), и способ не городить инкрементальный кэш ради полутора секунд.

    python3 scripts/claude-usage-agg.py                 # пересобрать и записать
    python3 scripts/claude-usage-agg.py --dry-run       # посчитать и показать
"""
import os, re, sys, json, glob, math, bisect, argparse, importlib.util, collections
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vault_common

_spec = importlib.util.spec_from_file_location(
    "claude_usage", os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude-usage.py"))
cu = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(cu)

SCHEMA = 1
VAULT = os.environ.get("MARA_VAULT", "/srv/vault")
ROOT = "Claude Usage"
WEEK, FIVE = 7 * 86400, 5 * 3600
# Стартовый прайор весов (§5). Это именно прайор: как подписка считает лимит,
# Anthropic не публикует, поэтому масштаб подбирается на чистых интервалах, а
# соотношение типов остаётся отсюда.
PRIOR = {"in": 1.0, "out": 5.0, "cw": 1.25, "cr": 0.1}
MONTH_SESSIONS = 50            # ориентир из брифа §7, чтобы было с чем сравнить
MARK = "<!-- собрано scripts/claude-usage-agg.py по данным на %s. Правки внутри файла пропадут. -->"


# ─── чтение ───────────────────────────────────────────────────────────────────

def load_ticks(data_dir):
    """Тики всех хостов в одну ленту (§1: проценты глобальны на аккаунт, и чья
    сессия их отрепортила — неважно). v1 не знал про серии — достраиваем."""
    rows = []
    for p in sorted(glob.glob(os.path.join(data_dir, "*", "ticks-*.jsonl"))):
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line: continue
            try: r = json.loads(line)
            except ValueError: continue
            if not isinstance(r, dict) or "epoch" not in r: continue
            r.setdefault("ts_last", r["ts"]); r.setdefault("epoch_last", r["epoch"])
            r.setdefault("n", 1)
            rows.append(r)
    rows.sort(key=lambda r: r["epoch"])
    return rows


def load_toolsets(data_dir):
    out = []
    for p in sorted(glob.glob(os.path.join(data_dir, "*", "toolsets.jsonl"))):
        for line in open(p, encoding="utf-8", errors="replace"):
            try: out.append(json.loads(line))
            except ValueError: pass
    return out


# ─── лента лимита ─────────────────────────────────────────────────────────────

def steps(ticks, pct_key, reset_key, length):
    """Тики → события «процент вырос»: (откуда, докуда, на сколько).

    Три вещи, из-за которых наивная разность соседних тиков врёт:

    * **Процент целый.** 5h и 7d приходят целыми числами, так что между
      соседними тиками разность почти всегда ноль. Считать надо не по тикам, а
      по шагам процента, и интервал шага — это минуты работы, а не секунды.
    * **Значение не монотонно.** Параллельные сессии отдают его вразнобой
      (32, 32, 31, 32 — видно на живых данных): у каждой оно своей свежести.
      Берём бегущий максимум внутри окна, минус на ровном месте не бывает.
    * **Сброс окна** — не отрицательный расход (§1). Новое окно начинается в
      момент `resets_at` старого, счётчик там ноль.

    Первый шаг окна отсчитывается от начала окна: процент, который мы застали,
    кто-то уже съел, и без этого недельная сумма не сойдётся с самим 7d%.
    У самого первого окна это начало вычислено, а не увидено — такой шаг
    помечается `baseline` и доверия ему нет. У окна после сброса начало известно
    точно (это `resets_at` предыдущего), и оно полноценно."""
    out, win, cur, prev_t, guessed = [], None, 0.0, None, False
    for r in ticks:
        p, w = r.get(pct_key), r.get(reset_key)
        if p is None or not w: continue
        p, w = round(float(p), 2), int(w)
        if win is None:
            win, cur, prev_t, guessed = w, 0.0, w - length, True
        elif w != win:
            if w < win: continue                  # тик со старым окном — просрочен
            win, cur, prev_t = w, 0.0, win        # сброс: окно началось в момент старого resets_at
        if p > cur:
            out.append({"t_from": prev_t, "t_to": r["epoch"], "delta": round(p - cur, 2),
                        "window": win, "baseline": guessed})
            cur, prev_t, guessed = p, r["epoch"], False
    return out


def latest(ticks, pct_key, reset_key):
    """Последнее известное значение процента и время сброса."""
    for r in reversed(ticks):
        if r.get(pct_key) is not None and r.get(reset_key):
            return round(float(r[pct_key]), 2), int(r[reset_key]), r["epoch_last"]
    return None, None, None


# ─── веса и атрибуция ─────────────────────────────────────────────────────────

def units(tok, scale=1.0):
    return scale * sum(PRIOR[f] * tok.get(f, 0) for f in PRIOR)


def span_turns(turns, keys, t_from, t_to):
    return turns[bisect.bisect_right(keys, t_from):bisect.bisect_right(keys, t_to)]


def calibrate(step_list, turns, keys, week_of):
    """Масштаб «процент на миллион взвешенных единиц» по каждой модели.

    Считается на чистых шагах — тех, где почти все токены интервала принадлежат
    одной модели: там процент не с кем делить, и деление даёт прямое измерение.
    Соотношение типов токенов остаётся из прайора.

    ponytail: одна ручка на модель вместо регрессии по типам токенов. При
    целом проценте регрессия на четыре неизвестных сейчас ловила бы шум; когда
    чистых шагов накопится сотня, здесь встанет МНК на те же данные."""
    acc = collections.defaultdict(lambda: [0.0, 0.0, 0])       # δ%, единицы, шагов
    for st in step_list:
        if st["baseline"]: continue
        by = collections.Counter()
        for t in span_turns(turns, keys, st["t_from"], st["t_to"]):
            by[t["model"]] += units(t)
        tot = sum(by.values())
        if not tot: continue
        model, u = by.most_common(1)[0]
        if u < 0.95 * tot: continue                            # интервал делят модели — не чистый
        w = week_of(st["t_to"])
        for k in ((w, model), (w, "*")):
            acc[k][0] += st["delta"]; acc[k][1] += u; acc[k][2] += 1
    coef = {}
    for (w, m), (d, u, n) in acc.items():
        if u > 0: coef[(w, m)] = {"week_id": w, "model_id": m, "source": "fitted",
                                  "n_steps": n, "pct_per_munit": d / u * 1e6,
                                  "delta_pct": round(d, 2), "weights": PRIOR}
    return coef


def scale_for(coef, week, model):
    """Во что обошёлся миллион взвешенных единиц. Внутри шага важны только
    отношения масштабов, так что незнакомая модель берёт общий, а при пустой
    калибровке все получают единицу — атрибуция вырождается в прайор."""
    for k in ((week, model), (week, "*")):
        if k in coef: return coef[k]["pct_per_munit"]
    fitted = [c["pct_per_munit"] for (w, m), c in coef.items() if m == "*"]
    return sum(fitted) / len(fitted) if fitted else 1.0


def attribute(step_list, turns, keys, coef, week_of):
    """Разложить каждый шаг процента по сессиям пропорционально взвешенным
    токенам и записать долю прямо в ход. Дальше любой срез — день, проект,
    модель, час — это просто сумма по ходам.

    Сумма долей внутри шага равна самому шагу: доли — это доли (§11)."""
    intervals = []
    for t in turns: t["pct"], t["confidence"] = 0.0, ""
    for st in step_list:
        w = week_of(st["t_to"])
        inside = span_turns(turns, keys, st["t_from"], st["t_to"])
        tot = sum(units(t, scale_for(coef, w, t["model"])) for t in inside)
        conf = "low"
        sids = {t["session_id"] for t in inside}
        if tot:
            if st["baseline"]: conf = "low"
            elif len(sids) == 1: conf = "high"
            elif len(sids) == 2: conf = "medium"
        for t in inside:
            t["pct"] = st["delta"] * units(t, scale_for(coef, w, t["model"])) / tot if tot else 0.0
            t["confidence"] = conf
        by = collections.defaultdict(lambda: {"in": 0, "out": 0, "cw": 0, "cr": 0,
                                              "weighted_units": 0.0, "attributed_pct": 0.0})
        for t in inside:
            a = by[(t["session_id"], t["model"])]
            for f in ("in", "out", "cw", "cr"): a[f] += t[f]
            a["weighted_units"] += units(t, scale_for(coef, w, t["model"]))
            a["attributed_pct"] += t["pct"]
        act = [{"session_id": sid, "model_id": mm,
                "tokens_by_type": {f: a[f] for f in ("in", "out", "cw", "cr")},
                "weighted_units": round(a["weighted_units"], 1),
                "attributed_pct": round(a["attributed_pct"], 4)}
               for (sid, mm), a in sorted(by.items(),
                                          key=lambda kv: (-kv[1]["attributed_pct"], kv[0]))]
        # Округление одиннадцати долей до четырёх знаков даёт сумму 5.0001 при
        # шаге 5.00. Остаток кладём самой крупной доле: §11 требует, чтобы
        # сумма оценок равнялась глобальному Δ, а не почти равнялась.
        if act: act[0]["attributed_pct"] = round(
            act[0]["attributed_pct"] + st["delta"] - sum(x["attributed_pct"] for x in act), 4)
        intervals.append({
            "schema_version": SCHEMA, "week_id": w,
            "t_from": iso(st["t_from"]), "t_to": iso(st["t_to"]),
            "seven_day_delta_pct": st["delta"], "is_clean": len(sids) == 1,
            "estimated": True, "confidence": conf,
            "data_quality": "window-baseline" if st["baseline"] else
                            ("no-tokens" if not tot else "ok"),
            "active_sessions": act})
    return intervals


# ─── форматирование ───────────────────────────────────────────────────────────

MONTHS = ("января февраля марта апреля мая июня июля августа сентября октября "
          "ноября декабря").split()


def iso(e):
    return datetime.fromtimestamp(e, timezone.utc).isoformat(timespec="seconds")


def local(e):
    return datetime.fromtimestamp(e)


def day_of(e):
    return local(e).strftime("%Y-%m-%d")


def ru_dt(e, with_time=True):
    d = local(e)
    s = "%d %s" % (d.day, MONTHS[d.month - 1])
    return s + d.strftime(" в %H:%M") if with_time else s


def ru_span(sec):
    sec = int(max(0, sec))
    d, h, m = sec // 86400, sec % 86400 // 3600, sec % 3600 // 60
    if d: return "%d д %d ч" % (d, h)
    if h: return "%d ч %d мин" % (h, m)
    return "%d мин" % m


def human(n):
    n = float(n or 0)
    for u, k in (("млрд", 1e9), ("млн", 1e6), ("тыс", 1e3)):
        if abs(n) >= k: return "%.1f %s" % (n / k, u)
    return "%d" % n


def pct(x, digits=1):
    return ("%%.%df%%%%" % digits) % (x or 0)


def plink(name, canon):
    """`[[канон|как называется каталог]]`. Именно с подписью: `mara` и
    `mara-second-brain` ведут на одну карточку проекта, и без подписи две
    строки таблицы выглядели бы одинаково."""
    return vault_common.linkify([name], canon)[0] if name else "?"


def table(head, rows):
    if not rows: return "_нет данных_\n"
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out) + "\n"


def note(m, title, source_id, occurred, body):
    """Все даты в шапке — из данных, а не из `now()`.

    Прогон раз в пять минут обязан быть тихим, когда ничего не происходило:
    иначе автокоммит и bisync будут гонять одни и те же файлы круглые сутки.
    Достаточно одного `created: сейчас`, чтобы файл менялся каждый прогон."""
    fm = ["---", 'title: "%s"' % title, "type: note", "source: claude-code",
          "source_id: %s" % source_id,
          "created: %s" % local(m["now"]).astimezone().isoformat(timespec="seconds"),
          "occurred: %s" % occurred, "tags:", "- claude-usage", "sensitive: false", "---", ""]
    return "\n".join(fm) + body.rstrip() + "\n\n" + (MARK % m["last_ts"]) + "\n"


def write(path, text, dry=False):
    """Атомарно и только если изменилось: рядом крутятся автокоммит и bisync,
    а прогон раз в пять минут не должен трогать файлы ни за чем."""
    try:
        if open(path, encoding="utf-8").read() == text: return False
    except OSError: pass
    if dry: return True
    d = os.path.dirname(path)
    if d and not os.path.isdir(d): os.makedirs(d)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
    os.replace(tmp, path)
    return True


# ─── сборка ───────────────────────────────────────────────────────────────────

def make_week_of(anchors):
    """Любой момент → идентификатор недельного окна (`resets_at` его конца).
    Окна ровно семидневные, так что одного известного `resets_at` хватает,
    чтобы разметить и то время, когда тиков ещё не было."""
    if not anchors: return lambda e: 0
    a = float(min(anchors))
    return lambda e: int(a + math.ceil((e - a) / WEEK) * WEEK)


def week_label(w):
    return datetime.fromtimestamp(w - WEEK).strftime("%G-W%V")


def active_seconds(epochs, gap=300):
    """Активное время (§5): сумма промежутков между ходами, длинный простой не
    считаем работой. Wall-clock сессии врёт втрое — агент ждёт человека."""
    e = sorted(epochs)
    return sum(min(e[i + 1] - e[i], gap) for i in range(len(e) - 1))


def build(vault, roots):
    data = os.path.join(vault, ROOT, "_data")
    ticks = load_ticks(data)
    sessions, heavy, turns = cu.scan(roots, None)
    sd = {s["session_id"]: s for s in sessions}
    keys = [t["epoch"] for t in turns]
    week_of = make_week_of([r["seven_day_resets_at"] for r in ticks
                            if r.get("seven_day_resets_at")])
    st7 = steps(ticks, "seven_day_pct", "seven_day_resets_at", WEEK)
    st5 = steps(ticks, "five_hour_pct", "five_hour_resets_at", FIVE)
    coef = calibrate(st7, turns, keys, week_of)
    intervals = attribute(st7, turns, keys, coef, week_of)
    for t in turns:                                   # приклеиваем сессию к ходу
        s = sd.get(t["session_id"]) or {}
        t["day"] = day_of(t["epoch"])
        t["week"] = week_of(t["epoch"])
        t["proj"] = os.path.basename(s.get("cwd") or "") or (s.get("project_key") or "?")
        t["host"] = s.get("host", "unknown")
        t["effort"] = s.get("effort", "")
        t["tokens"] = t["in"] + t["out"] + t["cw"] + t["cr"]
    return {"vault": vault, "ticks": ticks, "sessions": sessions, "heavy": heavy,
            "turns": turns, "intervals": intervals, "coef": coef, "week_of": week_of,
            "st7": st7, "st5": st5, "toolsets": load_toolsets(data),
            "canon": vault_common.canon_map(vault),
            "now": max([t["epoch"] for t in turns] + [r["epoch_last"] for r in ticks] or [0]),
            "last_ts": iso(max([t["epoch"] for t in turns] +
                               [r["epoch_last"] for r in ticks] or [0]))}


def group(rows, key, *fields):
    out = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        a = out[key(r)]
        for f in fields: a[f] += r.get(f, 0) or 0
        a["n"] += 1
    return out


def sess_pct(m):
    p = collections.Counter()
    for t in m["turns"]: p[t["session_id"]] += t["pct"]
    return p


# ─── отчёты ───────────────────────────────────────────────────────────────────

def dashboard(m):
    now = m["now"]
    five_p, five_r, _ = latest(m["ticks"], "five_hour_pct", "five_hour_resets_at")
    week_p, week_r, last_e = latest(m["ticks"], "seven_day_pct", "seven_day_resets_at")
    if week_p is None:
        return None
    b = ["# Расход лимитов Claude Code", "",
         "Данные на %s. Проценты — факт из statusline, оценки по сессиям помечены доверием." % ru_dt(last_e), "",
         "## Сколько осталось", ""]
    b.append(table(["Окно", "Съедено", "Осталось", "Обнулится"], [
        ["Пять часов", pct(five_p, 0), pct(100 - five_p, 0),
         "%s (через %s)" % (ru_dt(five_r), ru_span(five_r - now))] if five_p is not None else
        ["Пять часов", "нет данных", "—", "—"],
        ["Неделя", pct(week_p, 0), pct(100 - week_p, 0),
         "%s (через %s)" % (ru_dt(week_r), ru_span(week_r - now))]]))
    h1 = sum(s["delta"] for s in m["st7"] if s["t_to"] > now - 3600 and not s["baseline"])
    h24 = sum(s["delta"] for s in m["st7"] if s["t_to"] > now - 86400 and not s["baseline"])
    b += ["", "## Скорость", "",
          "- за последний час: **%s** недельного лимита" % pct(h1),
          "- за сутки: %s, это %s в час" % (pct(h24), pct(h24 / 24))]
    left = 100 - week_p
    for name, rate in (("по скорости последнего часа", h1), ("по средней за сутки", h24 / 24)):
        if rate > 0:
            when = now + left / rate * 3600
            b.append("- %s неделя кончится %s — %s сброса"
                     % (name, ru_dt(when), "раньше" if when < week_r else "позже"))
    if h1 <= 0 and h24 <= 0:
        b.append("- расхода за сутки не зафиксировано")
    cur = m["week_of"](now)
    fable = sum(t["pct"] for t in m["turns"] if t["week"] == cur and "fable" in t["model"])
    b += ["", "Fable за неделю: **%s** из 50 (оценка: отдельного счётчика Fable "
          "в statusline нет, считаем по токенам)." % pct(fable), "", "## Кто сейчас работает", ""]
    live = {}
    for r in m["ticks"]:
        if r["epoch_last"] > now - 1800: live.setdefault(r["session_id"], []).append(r)
    p = sess_pct(m)
    rows = []
    for sid, rs in sorted(live.items(), key=lambda kv: -kv[1][-1]["epoch_last"]):
        s = next((x for x in m["sessions"] if x["session_id"] == sid), {})
        conf = collections.Counter(t["confidence"] for t in m["turns"]
                                   if t["session_id"] == sid and t["confidence"])
        rows.append([plink(os.path.basename(rs[-1]["cwd"] or "") or "?", m["canon"]),
                     rs[-1]["model_name"] or rs[-1]["model_id"], rs[-1]["effort"] or "—",
                     ru_span(rs[-1]["epoch_last"] - rs[0]["epoch"]),
                     human(s.get("tokens_total", 0)), pct(p.get(sid, 0), 2),
                     conf.most_common(1)[0][0] if conf else "—"])
    b.append(table(["Проект", "Модель", "Усилие", "В работе", "Токенов", "% недели", "Доверие"], rows))
    mon = local(now).strftime("%Y-%m")
    n = sum(1 for s in m["sessions"] if (s["day"] or "").startswith(mon))
    b += ["", "Сессий в этом месяце: **%d** (ориентир %d)." % (n, MONTH_SESSIONS)]
    return note(m, "Claude Code: расход лимитов", "claude-usage/dashboard",
                day_of(now), "\n".join(b))


def by_key(m, turns, keyf, canon=False):
    g = group(turns, keyf, "pct", "tokens", "in", "out", "cw", "cr")
    rows = []
    for k, a in sorted(g.items(), key=lambda kv: -kv[1]["pct"]):
        rows.append([plink(k, m["canon"]) if canon else k,
                     pct(a["pct"], 2), human(a["tokens"]), a["n"]])
    return rows


def daily(m, day):
    turns = [t for t in m["turns"] if t["day"] == day]
    if not turns: return None
    ss = sorted({t["session_id"] for t in turns})
    sd = {s["session_id"]: s for s in m["sessions"]}
    total = sum(t["pct"] for t in turns)
    tok = sum(t["tokens"] for t in turns)
    usd = sum(sd[s]["api_usd_equiv"] for s in ss if s in sd)
    d = datetime.strptime(day, "%Y-%m-%d")
    b = ["# Расход за %d %s" % (d.day, MONTHS[d.month - 1]), "",
         "Съедено **%s** недельного лимита (оценка), токенов %s, по ценам API $%.2f, "
         "сессий %d." % (pct(total), human(tok), usd, len(ss)), "",
         "## По проектам", "", table(["Проект", "% недели", "Токенов", "Ходов"],
                                     by_key(m, turns, lambda t: t["proj"], canon=True)),
         "", "## По моделям", "", table(["Модель", "% недели", "Токенов", "Ходов"],
                                        by_key(m, turns, lambda t: t["model"])),
         "", "## Сессии", ""]
    p = sess_pct(m)
    rows = []
    for sid in sorted(ss, key=lambda s: -sum(t["pct"] for t in turns if t["session_id"] == s)):
        s = sd.get(sid) or {}
        my = [t for t in turns if t["session_id"] == sid]
        conf = collections.Counter(t["confidence"] for t in my if t["confidence"])
        rows.append([local(min(t["epoch"] for t in my)).strftime("%H:%M"),
                     plink(os.path.basename(s.get("cwd") or "") or "?", m["canon"]),
                     s.get("effort") or "—", len(my),
                     "%s / %s / %s" % (human(sum(t["in"] for t in my)),
                                       human(sum(t["out"] for t in my)),
                                       human(sum(t["cr"] + t["cw"] for t in my))),
                     s.get("n_tool_calls", 0), ", ".join(sorted(s.get("mcp_servers") or {})) or "—",
                     pct(sum(t["pct"] for t in my), 2),
                     conf.most_common(1)[0][0] if conf else "—",
                     "$%.2f" % (s.get("api_usd_equiv") or 0),
                     "[[%s|%s]]" % (sid, sid[:8])])
    b.append(table(["Начало", "Проект", "Усилие", "Ходов", "Вход/выход/кэш",
                    "Инстр.", "MCP", "% недели", "Доверие", "$", "Карточка"], rows))
    hv = [h for h in m["heavy"] if h["ts"][:10] == day]
    if hv:
        hv.sort(key=lambda h: -h["result_bytes"])
        b += ["", "## Самые тяжёлые ответы инструментов", "",
              table(["Время", "Инструмент", "КБ"],
                    [[h["ts"][11:16], h["tool_name"], "%.0f" % (h["result_bytes"] / 1024)]
                     for h in hv[:10]])]
    comp = sum(sd[s].get("n_compactions", 0) for s in ss if s in sd)
    if comp: b += ["", "Компакций за день: %d." % comp]
    return note(m, "Расход Claude Code за %s" % day, "claude-usage/daily/%s" % day,
                day, "\n".join(b))


def weekly(m, w):
    turns = [t for t in m["turns"] if t["week"] == w]
    if not turns: return None
    lab = week_label(w)
    sd = {s["session_id"]: s for s in m["sessions"]}
    total = sum(t["pct"] for t in turns)
    ws = [s for s in m["st7"] if m["week_of"](s["t_to"]) == w]
    head = ("Съедено **%s** (факт по statusline), разложено по сессиям %s (оценка)."
            % (pct(sum(s["delta"] for s in ws)), pct(total)) if ws else
            "Данных о лимите за эту неделю нет — statusline тогда ещё не писал тики. "
            "Ниже только токены и деньги, они из транскриптов и это факт.")
    b = ["# Неделя %s" % lab, "",
         "Окно с %s по %s. %s" % (ru_dt(w - WEEK, False), ru_dt(w, False), head), "",
         "## По проектам", "", table(["Проект", "% недели", "Токенов", "Ходов"],
                                     by_key(m, turns, lambda t: t["proj"], canon=True)),
         "", "## По моделям", "", table(["Модель", "% недели", "Токенов", "Ходов"],
                                        by_key(m, turns, lambda t: t["model"])),
         "", "## По уровню усилия", "", table(["Усилие", "% недели", "Токенов", "Ходов"],
                                              by_key(m, turns, lambda t: t["effort"] or "—")),
         "", "## Коэффициенты калибровки", ""]
    cs = [c for (ww, _), c in sorted(m["coef"].items()) if ww == w]
    prev = {c["model_id"]: c for (ww, _), c in m["coef"].items() if ww == w - WEEK}
    if cs:
        rows = []
        for c in sorted(cs, key=lambda c: c["model_id"]):
            was = prev.get(c["model_id"])
            rows.append([c["model_id"], "%.4f" % c["pct_per_munit"], c["n_steps"],
                         "%+.1f%%" % ((c["pct_per_munit"] / was["pct_per_munit"] - 1) * 100)
                         if was and was["pct_per_munit"] else "—"])
        b.append(table(["Модель", "% на млн взвешенных единиц", "Чистых шагов", "К прошлой неделе"], rows))
    else:
        b.append("_Чистых интервалов не набралось: калибровать не на чем, "
                 "работал прайор весов._\n")
    p = sess_pct(m)
    top = sorted({t["session_id"] for t in turns}, key=lambda s: (-p[s], s))[:5]
    b += ["", "## Пять самых дорогих сессий", "",
          table(["Дата", "Проект", "Модель", "% недели", "Токенов", "Ходов", "$", "Карточка"],
                [[sd[s]["day"], plink(os.path.basename(sd[s].get("cwd") or "") or "?", m["canon"]),
                  ", ".join(sd[s]["model_ids"] or sorted({t["model"] for t in turns
                                                          if t["session_id"] == s})) or "—",
                  pct(p[s], 2),
                  human(sd[s]["tokens_total"]), sd[s]["n_turns"],
                  "$%.2f" % sd[s]["api_usd_equiv"], "[[%s|%s]]" % (s, s[:8])]
                 for s in top if s in sd])]
    b += ["", "## Что можно сделать", ""] + (advice(m, turns) or ["- ничего не нашлось"])
    return note(m, "Расход Claude Code, неделя %s" % lab, "claude-usage/weekly/%s" % lab,
                day_of(w - WEEK), "\n".join(b))


def advice(m, turns):
    """Рекомендации только на данных, без всякой модели (§7)."""
    out = []
    used = collections.defaultdict(set)
    for s in m["sessions"]:
        for srv in (s.get("mcp_servers") or {}): used[os.path.basename(s.get("cwd") or "")].add(srv)
    for t in m["toolsets"]:
        proj = os.path.basename(t["cwd"])
        idle = [s for s in t["mcp_servers"] if s not in used.get(proj, set())]
        if idle and any(x["proj"] == proj for x in turns):
            out.append("- в проекте **%s** подключены, но ни разу не вызывались: %s"
                       % (proj, ", ".join(idle)))
    mine = {t["session_id"] for t in turns}
    heavy = collections.Counter()
    for h in m["heavy"]:
        if h["session_id"] in mine and h["tool_name"] != "?":
            heavy[h["tool_name"]] += h["result_bytes"]
    for name, b in heavy.most_common(3):
        out.append("- ответы инструмента **%s** за неделю весили %.1f МБ — это самый "
                   "тяжёлый источник контекста" % (name, b / 1e6))
    cm = collections.Counter(t["model"] for t in turns)
    if len(cm) > 1:
        out.append("- моделей в неделе несколько (%s) — сравнение по цене хода "
                   "в [[Claude Usage/Models|Models]]" % ", ".join(cm))
    return out


def models_note(m):
    """Накопительное сравнение моделей и уровней усилия (§7). Ради этого всё и
    затевалось: «Fable 5.1 на high реально экономнее Opus 5 или нет»."""
    sd = {s["session_id"]: s for s in m["sessions"]}
    rows = []
    for key in ("model", "effort"):
        g = collections.defaultdict(list)
        for t in m["turns"]: g[t[key] or "—"].append(t)
        for name, ts in sorted(g.items(), key=lambda kv: -sum(t["tokens"] for t in kv[1])):
            sess = {t["session_id"] for t in ts}
            act = active_seconds([t["epoch"] for t in ts])
            tok = sum(t["tokens"] for t in ts)
            cr = sum(t["cr"] for t in ts)
            lines = sum(sd[s].get("lines_added", 0) + sd[s].get("lines_removed", 0)
                        for s in sess if s in sd)
            p = sum(t["pct"] for t in ts)
            rows.append([("модель" if key == "model" else "усилие"), name, len(sess), len(ts),
                         human(tok), human(tok / len(ts)),
                         pct(p / (act / 3600), 2) if act > 600 and p else "—",
                         human(tok / lines) if lines else "—",
                         pct(cr / tok * 100, 0) if tok else "—",
                         ru_span(act / len(sess)) if sess else "—"])
    usd, nsess = collections.Counter(), collections.Counter()
    for s in m["sessions"]:
        for k, v in s["models"].items():
            usd[k] += v.get("costUSD", 0.0) or 0.0
            nsess[k] += 1
    b = ["# Модели и уровни усилия", "",
         "Всё, что удалось намерять, накопительно. Проценты недели — оценка "
         "(они разложены по сессиям пропорционально токенам), токены и деньги — факт.", "",
         table(["Что", "Значение", "Сессий", "Ходов", "Токенов", "На ход",
                "% в час", "Токенов на строку кода", "Доля чтения кэша", "Активного времени на сессию"],
               rows), "",
         "«% в час» считается по активному времени: промежутки между ходами длиннее "
         "пяти минут за работу не считаются, иначе ожидание человека выглядело бы "
         "экономией. Прочерк — данных о лимите ещё нет.", "",
         "## Деньги по ценам API", "",
         table(["Модель", "$", "Сессий"],
               [[k, "%.2f" % usd[k], nsess[k]] for k, _ in usd.most_common()])]
    return note(m, "Claude Code: модели", "claude-usage/models",
                day_of(m["now"]), "\n".join(b))


def median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else 0


def tools_note(m):
    """Кто подключён, кто вызывался и во что обходится подключение (§4)."""
    sd = {s["session_id"]: s for s in m["sessions"]}
    conn = collections.defaultdict(set)                 # сервер → проекты, где подключён
    for t in m["toolsets"]:
        for s in t["mcp_servers"]: conn[s].add(os.path.basename(t["cwd"]))
    calls, insess, bytes_ = collections.Counter(), collections.Counter(), collections.Counter()
    for s in m["sessions"]:
        for srv, n in (s.get("mcp_servers") or {}).items():
            calls[srv] += n; insess[srv] += 1
    for h in m["heavy"]:
        if h["mcp_server"]: bytes_[h["mcp_server"]] += h["result_bytes"]
    rows = []
    for srv in sorted(set(conn) | set(calls)):
        projs = conn.get(srv, set())
        with_ = [s["first_turn_input_tokens"] for s in m["sessions"]
                 if os.path.basename(s.get("cwd") or "") in projs and s["first_turn_input_tokens"]]
        without = [s["first_turn_input_tokens"] for s in m["sessions"]
                   if os.path.basename(s.get("cwd") or "") not in projs and s["first_turn_input_tokens"]]
        # Разница медиан на трёх сессиях — это шум, а не оценка: пока сравнивать
        # не на чем, честнее не показывать число вовсе.
        over = ("мало данных" if min(len(with_), len(without)) < 5
                else "%s (оценка)" % human(median(with_) - median(without))
                ) if with_ and without else "нет с чем сравнить"
        rows.append([srv, ", ".join(sorted(projs)) or "—", insess[srv], calls[srv],
                     "%.1f МБ" % (bytes_[srv] / 1e6) if bytes_[srv] else "—", over])
    sk, sksess = collections.Counter(), collections.Counter()
    for s in m["sessions"]:
        for k, n in (s.get("skills") or {}).items(): sk[k] += n; sksess[k] += 1
    tools = collections.Counter()
    for s in m["sessions"]:
        for k, n in (s.get("tools") or {}).items(): tools[k] += n
    idle = [(srv, ", ".join(sorted(conn[srv]))) for srv in conn if not calls[srv]]
    b = ["# Инструменты, MCP и скиллы", "",
         "Подключение сервера стоит токенов в каждой сессии, даже если его не звали: "
         "описания инструментов уезжают в системный контекст. Оценка накладных — разница "
         "медиан первого хода в проектах, где сервер подключён и где нет; это грубо и "
         "помечено как оценка.", "",
         "## MCP-серверы", "",
         table(["Сервер", "Подключён в проектах", "Сессий с вызовами", "Вызовов",
                "Тяжёлых ответов", "Накладные на сессию"], rows), ""]
    if idle:
        b += ["## Кандидаты на отключение", "",
              table(["Сервер", "Проекты"], [[s, p] for s, p in sorted(idle)]),
              "", "Подключены, но ни разу не вызывались за всю историю.", ""]
    b += ["## Скиллы", "",
          table(["Скилл", "Сессий", "Вызовов"],
                [[k, sksess[k], n] for k, n in sk.most_common(25)]), "",
          "## Встроенные инструменты", "",
          table(["Инструмент", "Вызовов"], [[k, n] for k, n in tools.most_common(20)])]
    return note(m, "Claude Code: инструменты и MCP", "claude-usage/tools",
                day_of(m["now"]), "\n".join(b))


def alerts_note(m):
    """Лог событий (§7). Тоже пересобирается с нуля: событие — это свойство
    данных, а не запись, которую надо не потерять."""
    ev = []
    for name, st, thr in (("Пятичасовое окно", m["st5"], [80]),
                          ("Недельное окно", m["st7"], [70, 90])):
        cur, win = 0.0, None
        for s in st:
            if s["window"] != win: cur, win = 0.0, s["window"]
            was, cur = cur, cur + s["delta"]
            for t in thr:
                if was < t <= cur:
                    ev.append((s["t_to"], "%s перевалило за %d%%" % (name, t)))
    fable, cur_w = 0.0, None
    for t in sorted(m["turns"], key=lambda t: t["epoch"]):
        if t["week"] != cur_w: fable, cur_w = 0.0, t["week"]
        if "fable" not in t["model"]: continue
        was, fable = fable, fable + t["pct"]
        if was < 45 <= fable:
            ev.append((t["epoch"], "доля Fable за неделю перевалила за 45% (оценка)"))
    hourly = collections.Counter()
    for s in m["st7"]:
        if not s["baseline"]: hourly[int(s["t_to"] // 3600)] += s["delta"]
    med = median([v for v in hourly.values() if v])
    for h, v in sorted(hourly.items()):
        if med and v > 3 * med:
            ev.append((h * 3600, "скорость %s в час — втрое выше обычной (%s)"
                       % (pct(v), pct(med))))
    nolim = [r for r in m["ticks"] if not r.get("has_rate_limits")]
    if nolim:
        ev.append((nolim[-1]["epoch_last"], "в statusline не было rate_limits: %d тиков, "
                   "последний %s" % (len(nolim), ru_dt(nolim[-1]["epoch_last"]))))
    seen = set()
    for t in sorted(m["toolsets"], key=lambda t: t["captured_at"]):
        k = (t["host"], os.path.basename(t["cwd"]))
        if k in seen:
            ev.append((cu.epoch_of(t["captured_at"]), "сменился набор инструментов: %s на %s"
                       % (k[1], k[0])))
        seen.add(k)
    ev.sort(key=lambda e: -e[0])
    b = ["# События", "",
         "Пересобирается с нуля при каждом прогоне: событие — это свойство данных.", "",
         table(["Когда", "Что"], [[ru_dt(e), txt] for e, txt in ev[:60]])]
    return note(m, "Claude Code: события", "claude-usage/alerts",
                day_of(m["now"]), "\n".join(b))


README = """# Как устроен учёт лимитов

Считает `scripts/claude-usage-agg.py` из репозитория `mara-second-brain`,
крон на doctor. Ни одного вызова модели: только транскрипты и тики.

## Откуда данные

| Слой | Где | Кто пишет |
|---|---|---|
| Транскрипты сессий | `raw/claude-code/` | SessionEnd-хук и шиппер по крону |
| Тики statusline | `Claude Usage/_data/<хост>/ticks-*.jsonl` | `claude-usage-ship.py`, крон `*/5` на машине с Claude Code |
| Снимки набора инструментов | `Claude Usage/_data/<хост>/toolsets.jsonl` | он же |
| Производное | `Claude Usage/_data/derived/` | агрегатор |
| Отчёты | `Claude Usage/*.md` | агрегатор |

Один писатель на файл: сырьё каждой машины лежит в папке своего хоста,
отчёты собирает только doctor. `_data` не уезжает в R2 — тики нужны
агрегатору, а не телефону.

## Факт и оценка

**Факт:** проценты 5h и 7d в момент тика, токены и деньги из транскриптов,
число вызовов инструментов, компакции.

**Оценка:** всё, где написано «% недели» напротив сессии, проекта или модели.
Проценты глобальны на аккаунт, а сессий одновременно несколько — разложить их
можно только пропорционально взвешенным токенам. У каждой оценки есть доверие:

| Доверие | Когда |
|---|---|
| `high` | в интервале работала ровно одна сессия — это прямое измерение |
| `medium` | две сессии |
| `low` | три и больше, либо интервал упирается в начало окна и часть расхода случилась до первого тика |

## Как пересобрать

```bash
python3 ~/mara-second-brain/scripts/claude-usage-agg.py
```

Состояния нет: каждый прогон считает всё с нуля из сырья и переписывает
только те файлы, содержимое которых изменилось.

## Чего система не знает

- **Отдельного счётчика Fable в statusline нет.** Доля Fable — оценка по
  токенам, а не то число из `/usage`.
- **Проценты целые.** Шаг измерения — 1% недели, поэтому короткие интервалы
  сливаются, а калибровка требует дней накопления.
- **Значение приходит вразнобой.** Параллельные сессии отдают процент своей
  свежести; берётся бегущий максимум внутри окна.
- **Машины без правки statusline невидимы для тиков.** Их токены считаются из
  транскриптов, но в проценты лимита они попадут только как чужой расход в
  общих интервалах.
"""


# ─── запись ───────────────────────────────────────────────────────────────────

def derived(m, out, dry):
    n = 0
    rows = collections.defaultdict(list)
    for i in m["intervals"]: rows[i["t_from"][:7]].append(i)
    for month, rs in rows.items():
        n += write(os.path.join(out, "intervals-%s.jsonl" % month),
                   "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rs), dry)
    n += write(os.path.join(out, "sessions.jsonl"),
               "".join(json.dumps(s, ensure_ascii=False) + "\n" for s in m["sessions"]), dry)
    n += write(os.path.join(out, "heavy-results.jsonl"),
               "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in m["heavy"]), dry)
    n += write(os.path.join(out, "coefficients.jsonl"),
               "".join(json.dumps(c, ensure_ascii=False) + "\n"
                       for _, c in sorted(m["coef"].items())), dry)
    def csv(name, head, rows):
        return write(os.path.join(out, name), ",".join(head) + "\n" +
                     "".join(",".join(str(c) for c in r) + "\n" for r in rows), dry)
    p = sess_pct(m)
    n += csv("daily.csv", ["day", "pct", "tokens", "turns", "sessions", "usd"],
             [[d, "%.4f" % a["pct"], a["tokens"], a["n"],
               len({t["session_id"] for t in m["turns"] if t["day"] == d}),
               "%.2f" % sum(s["api_usd_equiv"] for s in m["sessions"] if s["day"] == d)]
              for d, a in sorted(group(m["turns"], lambda t: t["day"], "pct", "tokens").items())])
    n += csv("weekly.csv", ["week", "week_id", "pct", "tokens", "turns"],
             [[week_label(w), w, "%.4f" % a["pct"], a["tokens"], a["n"]]
              for w, a in sorted(group(m["turns"], lambda t: t["week"], "pct", "tokens").items())])
    n += csv("models.csv", ["model", "pct", "tokens", "turns", "sessions"],
             [[k, "%.4f" % a["pct"], a["tokens"], a["n"],
               len({t["session_id"] for t in m["turns"] if t["model"] == k})]
              for k, a in sorted(group(m["turns"], lambda t: t["model"], "pct", "tokens").items())])
    n += csv("tools.csv", ["tool", "calls", "sessions"],
             sorted(((k, sum((s.get("tools") or {}).get(k, 0) for s in m["sessions"]),
                      sum(1 for s in m["sessions"] if k in (s.get("tools") or {})))
                     for k in {k for s in m["sessions"] for k in (s.get("tools") or {})}),
                    key=lambda r: (-r[1], r[0])))
    return n


def emit(m, dry=False):
    root = os.path.join(m["vault"], ROOT)
    n = derived(m, os.path.join(root, "_data", "derived"), dry)
    n += write(os.path.join(root, "_meta", "README.md"), README, dry)
    for name, text in (("Dashboard.md", dashboard(m)), ("Models.md", models_note(m)),
                       ("Tools & MCP.md", tools_note(m)), ("Alerts.md", alerts_note(m))):
        if text: n += write(os.path.join(root, name), text, dry)
    for day in sorted({t["day"] for t in m["turns"]}):
        text = daily(m, day)
        if text: n += write(os.path.join(root, "Daily", "%s.md" % day), text, dry)
    now = datetime.now().timestamp()
    for w in sorted({t["week"] for t in m["turns"]}):
        if w > now: continue                       # неделя ещё идёт — рано подводить итог
        text = weekly(m, w)
        if text: n += write(os.path.join(root, "Weekly", "%s.md" % week_label(w)), text, dry)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=VAULT)
    ap.add_argument("--roots", nargs="+", default=[os.environ.get(
        "CLAUDE_TRANSCRIPTS", os.path.join(VAULT, "raw/claude-code"))])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    m = build(a.vault, a.roots)
    if a.dry_run:
        n = emit(m, dry=True)
    else:
        with vault_common.locked(a.vault):         # флок общий с автокоммитом и bisync
            n = emit(m)
    print("claude-usage-agg: тиков %d, шагов недели %d, сессий %d, ходов %d, "
          "файлов %s %d" % (len(m["ticks"]), len(m["st7"]), len(m["sessions"]),
                            len(m["turns"]), "изменилось бы" if a.dry_run else "обновлено", n))
    return 0


def self_check():
    import tempfile, shutil
    v = tempfile.mkdtemp()
    tr = os.path.join(v, "raw/claude-code/-home-hleserg-x"); os.makedirs(tr)
    dd = os.path.join(v, ROOT, "_data", "BetaPi"); os.makedirs(dd)
    t0 = 1788300000.0
    W1, W2 = t0 + 1000, t0 + 1000 + WEEK

    def tick(e, five, seven, w=W1, sid="s1", five_w=None):
        return {"schema_version": 2, "ts": iso(e), "epoch": e, "ts_last": iso(e),
                "epoch_last": e, "n": 1, "host": "BetaPi", "session_id": sid,
                "session_name": "", "model_id": "claude-opus-5", "model_name": "Opus 5",
                "effort": "high", "cc_version": "2.1.252", "cwd": "/home/hleserg/x",
                "five_hour_pct": five, "five_hour_resets_at": five_w or (t0 + 600),
                "seven_day_pct": seven, "seven_day_resets_at": w,
                "has_rate_limits": five is not None, "ctx_used_tokens": 1000, "ctx_pct": 1,
                "cost_usd": 1.0, "duration_ms": 1, "api_duration_ms": 1, "lines_added": 0,
                "lines_removed": 0, "cache_hit_ratio": 0.9, "cache_write_tokens": 1,
                "cache_misses": 0, "fast_mode": False, "thinking": True}

    rows = [tick(t0 + 10, 10, 5), tick(t0 + 20, 10, 5, sid="s2"),
            tick(t0 + 30, 9, 5, sid="s2"),              # отставший тик: 9 после 10
            tick(t0 + 40, 11, 6), tick(t0 + 50, 11, 6, sid="s2"),
            tick(t0 + 60, None, None),                  # rate_limits пропали
            tick(t0 + 1200, 2, 1, w=W2, five_w=t0 + 20000)]   # окно сброшено
    open(os.path.join(dd, "ticks-2026-09.jsonl"), "w", encoding="utf-8").write(
        "".join(json.dumps(r) + "\n" for r in rows))

    def turn(e, mid, model="claude-opus-5", out=100):
        return {"type": "assistant", "sessionId": "", "timestamp": iso(e),
                "cwd": "/home/hleserg/x", "version": "2.1.252", "effort": "high",
                "message": {"id": mid, "model": model,
                            "usage": {"input_tokens": 10, "output_tokens": out,
                                      "cache_creation_input_tokens": 100,
                                      "cache_read_input_tokens": 1000}}}
    open(os.path.join(tr, "s1.jsonl"), "w", encoding="utf-8").write("".join(
        json.dumps(r) + "\n" for r in
        [turn(t0 + 15, "a"), turn(t0 + 35, "b"), turn(t0 + 1100, "c")] +
        [{"type": "cost-state", "totalCostUSD": 2.0, "totalLinesAdded": 10,
          "modelUsage": {"claude-opus-5": {"inputTokens": 20, "outputTokens": 200,
                                           "costUSD": 2.0}}}]))
    open(os.path.join(tr, "s2.jsonl"), "w", encoding="utf-8").write("".join(
        json.dumps(r) + "\n" for r in
        [turn(t0 + 25, "d", "claude-fable-5", out=50), turn(t0 + 45, "e", "claude-fable-5")]))

    m = build(v, [os.path.join(v, "raw/claude-code")])

    # бегущий максимум съедает откат 10 → 9 → 11: шаг один, на 1%
    fives = [s["delta"] for s in m["st5"]
             if not s["baseline"] and s["window"] == t0 + 600]
    assert fives == [1.0], fives
    # сброс окна не даёт отрицательного расхода и открывает новое окно
    assert all(s["delta"] > 0 for s in m["st7"]), m["st7"]
    assert {s["window"] for s in m["st7"]} == {W1, W2}, m["st7"]
    # первый шаг окна отсчитывается от его начала, а не от первого тика
    assert m["st7"][0]["baseline"] and m["st7"][0]["t_from"] == W1 - WEEK
    reset = [s for s in m["st7"] if s["window"] == W2][0]
    assert reset["t_from"] == W1 and reset["delta"] == 1, reset

    # сумма оценок по сессиям равна глобальному шагу (§11)
    for i in m["intervals"]:
        got = sum(a["attributed_pct"] for a in i["active_sessions"])
        assert not i["active_sessions"] or got == i["seven_day_delta_pct"], i
    # интервал, где работала одна сессия, — прямое измерение
    step2 = [i for i in m["intervals"] if i["confidence"] == "high"]
    assert step2 and step2[0]["is_clean"], m["intervals"]
    # две сессии в интервале → доверие ниже, но обе получили долю
    mixed = [i for i in m["intervals"] if len(i["active_sessions"]) > 1]
    assert mixed and mixed[0]["confidence"] in ("medium", "low"), mixed
    # чужие токены не пропали: обе модели видны
    assert {t["model"] for t in m["turns"]} == {"claude-opus-5", "claude-fable-5"}
    # тик без rate_limits не уронил разбор и попал в события
    assert any(not r["has_rate_limits"] for r in m["ticks"])
    assert "rate_limits" in alerts_note(m)

    # все отчёты собираются
    for f in (dashboard, models_note, tools_note, alerts_note):
        assert f(m) and "---" in f(m), f.__name__
    assert weekly(m, W1) and daily(m, day_of(t0 + 15))

    # пересборка с нуля даёт то же самое: второй прогон не трогает ни файла (§11).
    # Второй прогон — отдельным процессом с другим PYTHONHASHSEED: множества строк
    # обходятся в порядке хеша, и сортировка без второго ключа даёт разные файлы
    # на одних данных. Внутри одного процесса это не поймать.
    assert emit(m) > 0
    m2 = build(v, [os.path.join(v, "raw/claude-code")])
    assert emit(m2) == 0, "повторная сборка не должна трогать файлы"
    import subprocess
    os.makedirs(os.path.join(v, ".git"), exist_ok=True)
    for seed in ("1", "777"):
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "--vault", v,
                            "--roots", os.path.join(v, "raw/claude-code")],
                           env=dict(os.environ, PYTHONHASHSEED=seed),
                           capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.strip().endswith("обновлено 0"), \
            "сборка недетерминирована при PYTHONHASHSEED=%s: %s" % (seed, r.stdout + r.stderr)
    assert os.path.exists(os.path.join(v, ROOT, "Dashboard.md"))
    assert glob.glob(os.path.join(v, ROOT, "_data/derived/intervals-*.jsonl"))

    # тиков нет вовсе — считаются одни токены, отчёты не падают
    v2 = tempfile.mkdtemp()
    shutil.copytree(os.path.join(v, "raw"), os.path.join(v2, "raw"))
    m3 = build(v2, [os.path.join(v2, "raw/claude-code")])
    assert dashboard(m3) is None and emit(m3) > 0
    shutil.rmtree(v); shutil.rmtree(v2)
    print("claude-usage-agg: самопроверка ок")
    return 0


if __name__ == "__main__":
    sys.exit(self_check() if "--self-check" in sys.argv else main())
