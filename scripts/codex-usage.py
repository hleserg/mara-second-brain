#!/usr/bin/env python3
"""Codex rollout → quotas and metadata-only statistics; no model/API calls.

Reuse Mara's Claude quota-window reconciliation, atomic report writer and
canonical project registry. Codex caches/reasoning are SUBSETS, not extra tokens.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import csv
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vault_common

spec = importlib.util.spec_from_file_location("claude_usage_agg", Path(__file__).with_name("claude-usage-agg.py"))
claude = importlib.util.module_from_spec(spec)
spec.loader.exec_module(claude)
FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")
ROOT = "Codex Usage"


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def usage(value):
    if not isinstance(value, dict) or not all(number(value.get(k, 0)) and value.get(k, 0) >= 0 for k in FIELDS):
        return None
    out = {k: int(value.get(k, 0)) for k in FIELDS}
    if out['cached_input_tokens'] + out['cache_write_input_tokens'] > out['input_tokens'] or out['reasoning_output_tokens'] > out['output_tokens']:
        return None
    out['total_tokens'] = out['input_tokens'] + out['output_tokens']
    return out


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def epoch(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (AttributeError, ValueError, OverflowError):
        return 0


def parse(path):
    """Read only metadata events. Prompt, reasoning, args and tool outputs are discarded."""
    sid, model, turn, project = path.stem, 'unknown', '', 'unknown'
    records, fallback, ticks, tools, snapshots = {}, {}, {}, {}, set()
    previous = {k: 0 for k in FIELDS}
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            try: event = json.loads(line)
            except ValueError: continue  # last live JSONL line may be incomplete
            if not isinstance(event, dict): continue
            payload = event.get('payload')
            if not isinstance(payload, dict): continue
            kind, ts = event.get('type'), event.get('timestamp')
            at = epoch(ts)
            if kind == 'session_meta':
                sid = str(payload.get('id') or payload.get('session_id') or sid)
                project = Path(str(payload.get('cwd') or '').replace('\\', '/')).name or 'unknown'
            elif kind == 'turn_context':
                model = str(payload.get('model') or model)
                turn = payload.get('turn_id') or turn
                project = Path(str(payload.get('cwd') or '').replace('\\', '/')).name or project
            elif kind == 'token_usage_record' and at:
                got = usage(payload.get('usage'))
                total = usage(payload.get('thread_token_usage'))
                response_id = payload.get('response_id')
                if got is None or not response_id: continue
                owner = str(payload.get('thread_id') or payload.get('session_id') or sid)
                key = identity(['response', response_id])
                records[key] = {'event_id': key, 'response_id': response_id, 'session_id': owner,
                    'epoch': at, 'timestamp': ts, 'model': model, 'project': project,
                    'source': 'token_usage_record', '_snapshot': (owner, tuple(total[k] for k in FIELDS)) if total else None, **got}
                if total: snapshots.add((owner, tuple(total[k] for k in FIELDS)))
            elif kind == 'event_msg' and payload.get('type') == 'token_count' and at:
                limits = payload.get('rate_limits')
                if isinstance(limits, dict):
                    # primary can be the weekly window. Never infer duration from its position.
                    for slot in ('primary', 'secondary'):
                        window = limits.get(slot)
                        if not isinstance(window, dict): continue
                        pct, reset, minutes = (window.get(k) for k in ('used_percent', 'resets_at', 'window_minutes'))
                        if not all(number(v) for v in (pct, reset, minutes)) or not 0 <= pct <= 100 or reset <= 0 or minutes <= 0: continue
                        tick = {'epoch': at, 'epoch_last': at, 'pct': pct, 'reset': int(reset),
                                'window_minutes': int(minutes), 'limit_id': str(limits.get('limit_id') or 'codex')}
                        ticks[identity(tick)] = tick
                info = payload.get('info')
                total = usage(info.get('total_token_usage')) if isinstance(info, dict) else None
                if total is None: continue
                snapshot = tuple(total[k] for k in FIELDS)
                # Decreasing cumulative counters cannot establish incremental consumption.
                if any(total[k] < previous[k] for k in FIELDS):
                    previous = total
                    continue
                delta = usage({k: total[k] - previous[k] for k in FIELDS})
                previous = total
                if delta and delta['total_tokens']:
                    key = identity(['legacy', turn or sid, ts, snapshot])
                    fallback[key] = {'event_id': key, 'response_id': None, 'session_id': sid,
                        'epoch': at, 'timestamp': ts, 'model': model, 'project': project,
                        'source': 'cumulative_delta', '_snapshot': (sid, snapshot), **delta}
            elif kind == 'response_item' and payload.get('type') in ('function_call', 'custom_tool_call') and at:
                call = payload.get('call_id')
                if not isinstance(call, str): continue
                tools[call] = {'call_id': call, 'session_id': sid, 'epoch': at, 'model': model,
                               'project': project, 'name': str(payload.get('name') or 'unknown')}
    for key, row in fallback.items():
        if row['_snapshot'] not in snapshots: records.setdefault(key, row)
    return records, ticks, tools


def scan(roots, vault):
    records, ticks, tools = {}, {}, {}
    # ponytail: rebuild all metadata; add an incremental cursor if archive scan exceeds 5 minutes.
    for path in sorted({p for root in roots for p in Path(root).expanduser().rglob('*.jsonl')}):
        try: rr, tt, cc = parse(path)
        except OSError: continue  # an in-flight rotation must not abort every report
        for source, target in ((rr, records), (tt, ticks), (cc, tools)):
            for key, row in source.items(): target.setdefault(key, row)
    snapshots = {r['_snapshot'] for r in records.values() if r['source'] == 'token_usage_record' and r['_snapshot']}
    records = {k: r for k, r in records.items() if r['source'] != 'cumulative_delta' or r['_snapshot'] not in snapshots}
    for row in records.values(): row.pop('_snapshot', None)
    canon = vault_common.canon_map(vault)
    for row in list(records.values()) + list(tools.values()):
        row['project'] = canon.get(row['project'].lower(), row['project'])
    return {'records': sorted(records.values(), key=lambda r: (r['epoch'], r['event_id'])),
            'ticks': sorted(ticks.values(), key=lambda r: (r['epoch'], r['reset'], r['pct'])),
            'tools': sorted(tools.values(), key=lambda r: (r['epoch'], r['call_id']))}


def quotas(ticks, now):
    groups = defaultdict(list)
    for tick in ticks: groups[(tick['limit_id'], tick['window_minutes'])].append(tick)
    result = []
    for (limit, minutes), rows in sorted(groups.items()):
        rows = sorted(rows, key=lambda r: (r['epoch'], r['reset'], r['pct']))
        reset = max(r['reset'] for r in rows)
        # CLI observations of the same reset differ by a few seconds between sessions.
        current = [{**r, 'reset': reset} for r in rows if reset - r['reset'] <= 60]
        observed = max(r['epoch_last'] for r in current)
        used = max(r['pct'] for r in current)
        # A subscription window can change before the old reset (e.g. account rollover).
        # Establish its own baseline instead of attributing pre-observation usage.
        steps = claude.steps(current, 'pct', 'reset', minutes * 60)
        recent = [s for s in steps if s['window'] == reset and not s['baseline'] and observed - 3600 <= s['t_from'] < s['t_to'] <= observed]
        rate = None
        if recent and observed - recent[0]['t_from'] >= 300:
            rate = sum(s['delta'] for s in recent) * 60 / (observed - recent[0]['t_from'])
        stale = now - observed > 600 or now >= reset
        eta = (100-used)/rate if rate and not stale else None
        if eta is not None and eta * 60 >= reset - now: eta = None
        result.append({'limit_id': limit, 'window_minutes': minutes, 'used_percent': used,
            'remaining_percent': 100-used if now < reset else None, 'resets_at': reset,
            'observed_at': observed, 'age_seconds': max(0, int(now-observed)), 'stale': stale,
            'recent_pp_per_minute': round(rate, 4) if rate else None,
            'estimated_minutes_remaining': round(eta, 1) if eta is not None else None,
            'observed_growth_pp': round(sum(s['delta'] for s in steps if s['window'] == reset and not s['baseline']), 2)})
    return result


def statistics(data):
    grouped = {name: defaultdict(Counter) for name in ('daily', 'weekly', 'models', 'projects', 'sessions')}
    for row in data['records']:
        dt = datetime.fromtimestamp(row['epoch'], timezone.utc)
        for name, key in [('daily', dt.strftime('%Y-%m-%d')), ('weekly', dt.strftime('%G-W%V')),
                          ('models', row['model']), ('projects', row['project']), ('sessions', row['session_id'])]:
            grouped[name][key].update({k: row[k] for k in (*FIELDS, 'total_tokens')})
            grouped[name][key]['responses'] += 1
    return {name: [{'name': key, **values} for key, values in sorted(groups.items())] for name, groups in grouped.items()}


def report(data, now):
    totals = Counter()
    for row in data['records']: totals.update({k: row[k] for k in (*FIELDS, 'total_tokens')})
    return {'provider': 'codex', 'schema_version': 1, 'sessions': len({r['session_id'] for r in data['records']}),
            'responses': len(data['records']), 'tool_calls': len(data['tools']), 'tokens': dict(totals),
            'api_usd_equiv': None, 'actual_spend_usd': None, 'quotas': quotas(data['ticks'], now),
            'legacy_responses': sum(r['source'] == 'cumulative_delta' for r in data['records'])}


def cell(value):
    return vault_common.scrub(str(value)).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')


def emit(data, vault):
    root = Path(vault)/ROOT
    stamp = max([r['epoch'] for r in data['records']] + [r['epoch'] for r in data['ticks']] + [r['epoch'] for r in data['tools']] or [0])
    summary, stats = report(data, stamp), statistics(data)
    summary.pop('quotas', None)
    # Relative freshness/ETA are computed by `--status`, never baked into a timeless note.
    limits = quotas(data['ticks'], stamp)
    for q in limits:
        for key in ('age_seconds', 'stale', 'estimated_minutes_remaining'): q.pop(key, None)
    summary['quotas'] = limits
    paths = {}
    for name, rows in [('responses', data['records']), ('tools', data['tools']), ('quota-observations', data['ticks'])]:
        paths[f'_data/derived/{name}.jsonl'] = ''.join(json.dumps(r, ensure_ascii=False, sort_keys=True)+'\n' for r in rows)
    paths['_data/derived/summary.json'] = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)+'\n'
    for name, rows in stats.items():
        out = io.StringIO()
        writer = csv.DictWriter(out, lineterminator='\n', fieldnames=['name', *FIELDS, 'total_tokens', 'responses'])
        writer.writeheader(); writer.writerows(rows)
        paths[f'_data/derived/{name}.csv'] = out.getvalue()
    iso = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    def note(title, body):
        return f'---\ntitle: "{title}"\ntype: note\nsource: codex\ncreated: {iso}\nsensitive: false\ntags: [codex-usage]\n---\n\n{body}\n\nДанные по {iso}. Пересобирает scripts/codex-usage.py.\n'
    text = '# Расход Codex\n\nКвота общая для аккаунта; проценты параллельных сессий не суммируются.\n\n'
    text += claude.table(['Лимит', 'Окно, минут', 'Использовано', 'Осталось на момент наблюдения', 'Наблюдение UTC', 'Сброс UTC'],
        [[cell(q['limit_id']), q['window_minutes'], str(q['used_percent'])+'%', str(q['remaining_percent'])+'%' if q['remaining_percent'] is not None else 'неизвестно',
          datetime.fromtimestamp(q['observed_at'], timezone.utc).isoformat(),
          datetime.fromtimestamp(q['resets_at'], timezone.utc).isoformat()] for q in limits])
    text += f"\nСессий: {summary['sessions']}; ответов: {summary['responses']}; вызовов инструментов: {summary['tool_calls']}.\n"
    text += '\nКэш входит во входные токены, reasoning — в выходные. Стоимость и доля квоты каждого проекта неизвестны.\n'
    text += '\nАктуальность: сравните время данных внизу с текущим временем; live quota/прогноз доступны через `--status`.\n'
    paths['Dashboard.md'] = note('Codex: расход и квоты', text)
    for name, title in [('daily','Дни'), ('weekly','Недели UTC'), ('models','Модели'), ('projects','Проекты')]:
        rows = [[cell(r['name']), r['responses'], r['input_tokens'], r['cached_input_tokens'], r['output_tokens'], r['reasoning_output_tokens'], r['total_tokens']] for r in stats[name]]
        paths[{'daily':'Daily.md','weekly':'Weekly.md','models':'Models.md','projects':'Projects.md'}[name]] = note('Codex: '+title,
            claude.table([title,'Ответы','Вход','Кэш внутри входа','Выход','Reasoning внутри выхода','Всего'], rows))
    tools = Counter(r['name'] for r in data['tools'])
    paths['Tools & MCP.md'] = note('Codex: инструменты',
        claude.table(['Инструмент','Вызовы'], [[cell(k),v] for k,v in sorted(tools.items())]) +
        '\nУчитываются только явные события вызова. Вложенные инструменты functions.exec и применение skills не угадываются.\n')
    updated = 0
    for name, text in paths.items():
        path = root/name
        if path.suffix == '.md':
            try: previous = path.read_text(encoding='utf-8')
            except FileNotFoundError: previous = ''
            if previous.startswith('---\n') and '\n---\n' in previous:
                header, _, body = previous.partition('\n---\n')
                new_body = text.partition('\n---\n')[2]
                # Basic Memory owns frontmatter (including permalink); we own the report body.
                if body.strip() == new_body.strip(): continue
                text = header + '\n---\n' + new_body
        updated += claude.write(str(path), text)
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', default=os.environ.get('MARA_VAULT', '/srv/vault'))
    parser.add_argument('--roots', nargs='+')
    parser.add_argument('--status', action='store_true', help='read-only JSON; default source is local Codex sessions')
    parser.add_argument('--dry-run', action='store_true', help='read-only JSON from mirrored sources')
    args = parser.parse_args()
    roots = args.roots or ([str(Path.home()/'.codex/sessions')] if args.status else [str(Path(args.vault)/'raw/codex')])
    data = scan(roots, args.vault)
    summary = report(data, time.time())
    if not (args.status or args.dry_run) and any(data.values()):
        with vault_common.locked(args.vault): summary['files_updated'] = emit(data, args.vault)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
