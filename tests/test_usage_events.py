import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec=importlib.util.spec_from_file_location('usage_events',Path(__file__).resolve().parents[1]/'scripts/usage_events.py')
u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
T='2026-09-09T15:50:00Z'

class UsageIdentityTest(unittest.TestCase):
    def row(self, **changes):
        row={'provider':'openai','call_id':'resp_real','session_id':'session','run_id':'run',
             'model':'gpt-test','source':'native_stream','measured_at':T,'observed_at':T,'quality':'native',
             'tokens':{'input':100,'output':10,'cache_read':50,'cache_write':0,'reasoning':0}}
        row.update(changes);return row

    def test_duplicate_and_late_authoritative_correction_are_order_independent(self):
        live=self.row();late=self.row(source='provider_transcript',tokens={**live['tokens'],'output':12})
        for records in ([live,live,late],[late,live,late]):
            selected=u.reconcile(records)
            self.assertEqual(len(selected),1);self.assertEqual(u.total(selected)['output'],12)
        contradiction=self.row(tokens={**live['tokens'],'output':13})
        with self.assertRaises(ValueError):u.reconcile([live,contradiction])

    def test_same_response_cannot_be_reattributed_to_another_run(self):
        with self.assertRaises(ValueError):u.reconcile([self.row(),self.row(run_id='other')])

    def test_native_aggregate_and_calls_are_never_both_counted(self):
        run={'id':'run','status':'succeeded','sessionIdAfter':'session','sessionIdBefore':None,
             'usageJson':{'provider':'openai','inputTokens':100,'outputTokens':10,'cachedInputTokens':50,'freshSession':True}}
        with self.assertRaises(ValueError):u.reconcile_run(run,[self.row(provider='anthropic')],{},T)
        complete=u.reconcile_run(run,[self.row()],{},T)
        self.assertFalse(complete['native_run_aggregate_counted']);self.assertEqual(complete['tokens']['input'],100)
        late=self.row(source='provider_transcript',tokens={**self.row()['tokens'],'output':12})
        corrected=u.reconcile_run(run,[self.row(),late],{},T,transcript_complete=True)
        self.assertEqual(corrected['tokens']['output'],12)
        unknown=u.reconcile_run({**run,'sessionIdBefore':'session'},[self.row()],{},T,True)
        self.assertTrue(unknown['native_run_aggregate_counted']);self.assertIsNone(unknown['call_count'])

    def test_export_never_reads_message_body_into_result_or_sums_cumulative_rows(self):
        lines=[{'type':'session_meta','payload':{'id':'session'}},
               {'type':'turn_context','payload':{'model':'gpt-test','turn_id':'turn'}},
               {'type':'response_item','payload':{'content':'PRIVATE prompt'}},
               {'type':'token_usage_record','timestamp':T,'payload':{'session_id':'session','response_id':'resp_real',
                 'usage':{'input_tokens':100,'output_tokens':10,'cached_input_tokens':50},'thread_token_usage':{'input_tokens':999}}},
               {'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':999}}}},
               {'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn'}}]
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in lines))
            snapshot=u.export_snapshot(path,'openai',T)
        self.assertTrue(snapshot['transcript_complete']);self.assertEqual(len(snapshot['observations']),1)
        self.assertEqual(snapshot['observations'][0]['tokens']['input'],100)
        self.assertIsNone(snapshot['observations'][0]['tokens']['cache_write'])
        self.assertNotIn('PRIVATE',json.dumps(snapshot));self.assertNotIn('999',json.dumps(snapshot))

    def test_missing_response_identity_cannot_claim_complete_coverage(self):
        lines=[{'type':'session_meta','payload':{'id':'session'}},
               {'type':'turn_context','payload':{'model':'gpt-test','turn_id':'turn'}},
               {'type':'token_usage_record','timestamp':T,'payload':{'session_id':'session','response_id':None,'usage':{}}},
               {'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn'}}]
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'source.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in lines))
            snapshot=u.export_snapshot(path,'openai',T)
        self.assertFalse(snapshot['transcript_complete']);self.assertEqual(snapshot['unknown_identity_records'],1)
