"""Identity-preserving metadata export for existing Mara usage collectors.

Pure reduction: no second ledger, no price table, no transcript content in output.
Codex response usage is inclusive of cached input; Claude input excludes caches.
"""
import json
from datetime import datetime, timezone

TOKEN_FIELDS = ('input', 'output', 'cache_read', 'cache_write', 'reasoning')

def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp requires timezone')
    return parsed.timestamp()


def tokens(usage, provider):
    names = {'input':'input_tokens','output':'output_tokens','reasoning':'reasoning_output_tokens',
             'cache_read':'cached_input_tokens','cache_write':'cache_write_input_tokens'}
    if provider == 'anthropic':
        names.update(cache_read='cache_read_input_tokens',cache_write='cache_creation_input_tokens')
    result = {}
    for name, field in names.items():
        value = usage.get(field)
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError('invalid token measurement')
        result[name] = value
    return result


def records(path):
    # Only complete lines; a concurrently appended trailing partial record is ignored.
    with open(path, encoding='utf-8') as stream:
        for line in stream:
            if not line.endswith('\n'):
                continue
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def export(path, provider, observed_at, _records=None):
    timestamp(observed_at)
    session, model, turn = None, None, None
    output = []
    for row in (records(path) if _records is None else _records):
        payload = row.get('payload') or {}
        kind = row.get('type')
        if provider == 'openai':
            if kind == 'session_meta':
                session = payload.get('id')
            if kind == 'turn_context':
                model, turn = payload.get('model'), payload.get('turn_id')
            if kind != 'token_usage_record':
                continue
            identity = payload.get('response_id')
            row_session = payload.get('session_id') or payload.get('thread_id')
            if session and row_session != session:
                raise ValueError('record session differs from transcript metadata')
            measurement = tokens(payload.get('usage') or {}, provider)
            row_turn = payload.get('turn_id') or turn
        elif provider == 'anthropic':
            if kind != 'assistant':
                continue
            message = row.get('message') or {}
            model = message.get('model')
            if model in (None, '', '<synthetic>'):
                continue
            identity, row_session, row_turn = message.get('id'), row.get('sessionId'), None
            measurement = tokens(message.get('usage') or {}, provider)
        else:
            raise ValueError('unsupported provider')
        if not identity or not row_session:
            # Unknown identity cannot be silently attributed or deduplicated.
            continue
        measured_at = row.get('timestamp')
        timestamp(measured_at)
        output.append({'schema_version':1,'provider':provider,'session_id':row_session,
                       'call_id':identity,'turn_id':row_turn,'model':model or 'unknown',
                       'tokens':measurement,'input_includes_cache':provider=='openai',
                       'source':'provider_transcript','measured_at':measured_at,
                       'observed_at':observed_at,'quality':'native'})
    return output


def reconcile(observations):
    """One current fact per provider response. Late authoritative facts replace it.

    Measured event time, not delivery order, controls revisions. Observed time is
    freshness only. Native provider transcript wins over provisional live stream.
    An equal-authority equal-time contradiction fails closed instead of guessing.
    """
    current = {}
    priority = {'native_stream':1,'provider_transcript':2}
    for raw in observations:
        row = dict(raw)
        if row.get('source') not in priority or row.get('quality') != 'native':
            raise ValueError('unsupported observation provenance')
        key = (row['provider'], row['call_id'])
        if not all(isinstance(x,str) and x for x in (*key,row['session_id'])):
            raise ValueError('provider identity required')
        rank = (priority[row['source']], timestamp(row['measured_at']))
        timestamp(row['observed_at'])
        old = current.get(key)
        if old:
            if old['session_id'] != row['session_id'] or old.get('run_id') != row.get('run_id'):
                raise ValueError('provider identity has conflicting run/session attribution')
            old_rank = (priority[old['source']],timestamp(old['measured_at']))
            comparable = lambda r:{k:v for k,v in r.items() if k != 'observed_at'}
            if rank == old_rank and comparable(row) != comparable(old):
                raise ValueError('conflicting equal-version provider measurements')
            if rank < old_rank:
                continue
        current[key] = row
    return sorted(current.values(),key=lambda r:(r['provider'],r['call_id']))


def total(calls):
    # Missing measurement remains unknown rather than silently becoming zero.
    return {key:sum(row['tokens'][key] for row in calls)
            if calls and all(row['tokens'].get(key) is not None for row in calls) else None
            for key in TOKEN_FIELDS}


def reconcile_run(run, observations, scope, observed_at, transcript_complete=False):
    """Replace a fresh-session run aggregate only with matching full call coverage.

    Scope is supplied by the authenticated control-plane adapter, never inferred
    from cwd. Completion requires native terminal run, exact session, and totals
    matching at least input/output/cache-read. Otherwise retain native aggregate
    and expose provider calls as uncounted detail until coverage can be proved.
    """
    timestamp(observed_at)
    usage = run.get('usageJson') or {}
    session = run.get('sessionIdAfter') or usage.get('persistedSessionId')
    calls = reconcile(observations)
    for row in calls:
        if row['provider'] != usage.get('provider') or row['session_id'] != session or row.get('run_id') != run['id']:
            raise ValueError('provider observation outside native run/session')
    measured = total(calls)
    native = {'input':usage.get('inputTokens'),'output':usage.get('outputTokens'),
              'cache_read':usage.get('cachedInputTokens'),'cache_write':None,'reasoning':None}
    complete = (bool(calls) and run.get('status') in ('succeeded','failed','cancelled','timed_out')
                and usage.get('freshSession') is True and not run.get('sessionIdBefore')
                and (transcript_complete or all(native[k] is not None and measured[k] == native[k]
                        for k in ('input','output','cache_read'))))
    models = sorted({r['model'] for r in calls})
    return {'schema_version':1,'run_id':run['id'],**scope,'session_id':session,
            'provider':usage.get('provider','unknown'),'models':models or [usage.get('model','unknown')],
            'tokens':measured if complete else native,'call_count':len(calls) if complete else None,
            'measurement_source':'provider_transcript' if complete else 'paperclip_run',
            'native_run_aggregate_counted':not complete,'provider_calls_counted':complete,
            'provider_calls':calls,'coverage':('complete-transcript' if transcript_complete else 'complete-matched') if complete else 'unverified',
            'quality':'native' if usage else 'unknown','actual_spend_usd':None,
            'api_equivalent_usd':usage.get('costUsd'),
            'billing_type':usage.get('billingType','unknown'),'quota_allocation':None,
            'observed_at':observed_at,'correction_policy':'replace snapshot, never add history to live deltas'}


def export_snapshot(path, provider, observed_at):
    # One read of a possibly growing file. Never combine an older set of calls
    # with a newer completion marker. Discard all message/tool content immediately.
    metadata=[]
    for row in records(path):
        kind=row.get('type');payload=row.get('payload') or {}
        if provider=='anthropic' and kind=='assistant':
            message=row.get('message') or {}
            metadata.append({k:row.get(k) for k in ('type','timestamp','sessionId')} | {
                'message':{k:message.get(k) for k in ('id','model','usage')}})
        elif kind in ('session_meta','turn_context','token_usage_record'):
            metadata.append(row)
        elif kind=='event_msg' and payload.get('type')=='task_complete':
            metadata.append({'type':kind,'payload':{'type':'task_complete','turn_id':payload.get('turn_id')}})
    calls=export(path,provider,observed_at,metadata)
    sessions={r['session_id'] for r in calls}
    completed={r['payload'].get('turn_id') for r in metadata
               if r.get('type')=='event_msg' and r['payload'].get('type')=='task_complete'}
    turns={r.get('turn_id') for r in calls}
    eligible=sum(row.get('type')=='token_usage_record' for row in metadata) if provider=='openai' else len(metadata)
    missing_identity=max(0,eligible-len(calls))
    complete=bool(calls) and provider=='openai' and len(sessions)==1 and None not in turns and turns<=completed and missing_identity==0
    return {'observations':calls,'transcript_complete':complete,'unknown_identity_records':missing_identity,'observed_at':observed_at}
