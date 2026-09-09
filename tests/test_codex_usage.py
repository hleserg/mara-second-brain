"""Synthetic rollouts: duplicated transports, legacy counters, shared quota and reports."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/codex-usage.py"
spec = importlib.util.spec_from_file_location("codex_usage", SCRIPT)
codex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex)


class CodexUsageTest(unittest.TestCase):
    def test_rollouts_to_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "raw/codex/pc"
            raw.mkdir(parents=True)
            (root / "_system").mkdir()
            (root / "_system/entity-index.json").write_text(json.dumps([
                {"canonical": "Mara", "aliases": ["mara-second-brain"]}]))
            def event(kind, payload, second=0):
                return {"timestamp": f"2026-09-09T15:00:{second:02d}Z", "type": kind, "payload": payload}
            def count(total):
                return event("event_msg", {"type": "token_count", "info": {"total_token_usage": total},
                    "rate_limits": {"limit_id": "codex", "primary": {
                        "used_percent": 54, "window_minutes": 10080, "resets_at": 1790000000}}}, 2)
            one = dict(input_tokens=100, cached_input_tokens=60, output_tokens=20, reasoning_output_tokens=5)
            two = dict(input_tokens=250, cached_input_tokens=150, output_tokens=50, reasoning_output_tokens=10)
            events = [event("session_meta", {"id": "s1", "cwd": r"C:\code\mara-second-brain"}),
                      event("turn_context", {"turn_id": "t1", "model": "gpt-6-astra"}),
                      event("response_item", {"type": "message", "content": "PRIVATE_PROMPT_MARKER"}),
                      event("token_usage_record", {"response_id": "r1", "thread_id": "s1", "usage": one, "thread_token_usage": one}, 1),
                      count(one), count(one),
                      event("token_usage_record", {"response_id": "r2", "thread_id": "s1", "usage": {
                          k: two[k] - one[k] for k in one}, "thread_token_usage": two}, 3), count(two),
                      event("response_item", {"type": "function_call", "call_id": "c1", "name": "functions.exec", "arguments": "PRIVATE_ARGS_MARKER"}, 4)]
            def save(name, rows):
                (raw / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n{partial")
            save("original.jsonl", events)
            save("mirror.jsonl", events)
            # A legacy copy of the same thread must not add consumption again.
            save("legacy-copy.jsonl", [e for e in events if e["type"] != "token_usage_record"])
            legacy = [event("session_meta", {"id": "s2", "cwd": "/code/other"}),
                      event("turn_context", {"turn_id": "t2", "model": "gpt-5"}), count(one), count(one), count(two)]
            save("legacy.jsonl", legacy)
            data = codex.scan([raw, raw], root)
            summary = codex.report(data, codex.epoch("2026-09-09T15:00:05Z"))
            self.assertEqual((summary["sessions"], summary["responses"], summary["tool_calls"]), (2, 4, 1))
            self.assertEqual(summary["tokens"]["total_tokens"], 600)
            self.assertEqual(summary["tokens"]["cached_input_tokens"], 300)
            self.assertEqual(summary["legacy_responses"], 2)
            self.assertEqual(summary["quotas"][0]["remaining_percent"], 46)
            self.assertIsNone(summary["actual_spend_usd"])
            stats = codex.statistics(data)
            self.assertEqual([r["name"] for r in stats["projects"]], ["Mara", "other"])
            self.assertEqual(stats["weekly"][0]["name"], "2026-W37")
            # CLI status must not create reports or a vault lock.
            subprocess.run([sys.executable, str(SCRIPT), "--status", "--roots", str(raw), "--vault", str(root)], check=True, capture_output=True)
            self.assertFalse((root / "Codex Usage").exists())
            self.assertGreater(codex.emit(data, root), 0)
            self.assertEqual(codex.emit(data, root), 0)
            for path in (root / "Codex Usage").rglob("*"):
                if path.is_file(): self.assertNotIn("PRIVATE_", path.read_text())
            self.assertTrue((root / "Codex Usage/_data/derived/sessions.csv").exists())
            # Basic Memory reformats YAML and adds its own permalink after ingest.
            dashboard = root / "Codex Usage/Dashboard.md"
            header, _, body = dashboard.read_text().partition("\n---\n")
            header = header.replace('title: "Codex: расход и квоты"', "title: 'Codex: расход и квоты'")
            header += "\npermalink: vault/codex-usage/dashboard"
            dashboard.write_text(header + "\n---\n" + body.rstrip() + "\n")
            self.assertEqual(codex.emit(data, root), 0)
            data["records"][0]["epoch"] += 86400
            self.assertGreater(codex.emit(data, root), 0)
            self.assertTrue(dashboard.read_text().startswith(header + "\n---\n"))
            self.assertEqual(codex.emit(data, root), 0)

    def test_quota_resets_out_of_order_and_forecast(self):
        def tick(at, pct, reset=700000):
            return dict(epoch=at, epoch_last=at, pct=pct, reset=reset, window_minutes=10080, limit_id="codex")
        rows = [tick(100000, 40), tick(100600, 50), tick(100650, 48), tick(100660, 99, 600000)]
        q = codex.quotas(rows, 100660)[0]
        self.assertEqual(q["used_percent"], 50)
        self.assertEqual(q["observed_growth_pp"], 10)
        self.assertAlmostEqual(q["estimated_minutes_remaining"], 54.2, places=1)
        self.assertIsNone(codex.quotas(rows, 101300)[0]["estimated_minutes_remaining"])
        self.assertIsNone(codex.quotas(rows, 700001)[0]["remaining_percent"])
        jitter = rows + [tick(100670, 51, 699997)]
        self.assertEqual(codex.quotas(jitter, 100670)[0]["used_percent"], 51)
        # A window can be replaced before the previous reset: no negative duration.
        early_reset = [tick(99000, 90, 600000), tick(100000, 40), tick(100600, 50)]
        self.assertEqual(codex.quotas(early_reset, 100600)[0]["estimated_minutes_remaining"], 50)
        rows.append(tick(700100, 2, 1304800))
        self.assertEqual(codex.quotas(rows, 700101)[0]["remaining_percent"], 98)
        # No forecast past a known reset.
        self.assertIsNone(codex.quotas([tick(100000, 40, 101000), tick(100600, 50, 101000)], 100600)[0]["estimated_minutes_remaining"])
        self.assertIsNone(codex.usage({"input_tokens": 10, "cached_input_tokens": 11}))
        self.assertIsNone(codex.usage({"output_tokens": float("nan")}))


if __name__ == "__main__":
    unittest.main()
