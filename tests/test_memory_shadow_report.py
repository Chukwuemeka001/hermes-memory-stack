#!/usr/bin/env python3
"""Tests for memory_shadow_report.py."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "scripts", "memory_shadow_report.py")


def event(turn_id="t1", *, savings=55.0, source="memories-index:20 hits via daemon",
          answer_usage=None, include_blocks=False, active_block="full", over_budget=False,
          projected_sha="proj", safety_drop=False, retrieval_path="daemon",
          retrieval_latency_ms=80.0, retrieval_telemetry=True):
    full = {"entries_total": 4, "tokens": 2000, "sha256": "full"}
    projected = {
        "entries_selected": 2,
        "entries_skipped": 2,
        "tokens": 900,
        "savings_pct": savings,
        "sha256": projected_sha,
        "pinned_count": 1,
        "pin_breakdown": {"safety": 1},
        "relevance_source": source,
        "relevance_reserved_count": 2,
        "over_budget": over_budget,
    }
    if retrieval_telemetry:
        projected["retrieval_telemetry"] = {
            "path": retrieval_path,
            "retrieval_latency_ms": retrieval_latency_ms,
            "n_requested": 20,
            "hits_returned": 20,
            "candidate_pool_size": 61,
            "collection": "memories",
        }
    if include_blocks:
        full["block"] = "raw full memory"
    return {
        "tool": "memory_shadow",
        "mode": "shadow",
        "active_block": active_block,
        "turn_id": turn_id,
        "generated_at": f"2026-06-25T00:00:{turn_id[-1:] if turn_id[-1:].isdigit() else '0'}Z",
        "query_sha256": "query",
        "budget_tokens": 1200,
        "full": full,
        "projected": projected,
        "diff": {"selected_refs": ["memory#1", "memory#2"], "skipped_refs": ["memory#3", "user#1"], "selected_count": 2, "skipped_count": 2},
        "per_entry": [
            {"entry_ref": "memory#1", "selected": not safety_drop, "pin_class": "safety"},
            {"entry_ref": "memory#3", "selected": False, "pin_class": "none"},
        ],
        "answer_usage": answer_usage,
    }


class TestShadowReport(unittest.TestCase):
    def write_jsonl(self, rows):
        td = tempfile.TemporaryDirectory()
        path = os.path.join(td.name, "shadow.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        self.addCleanup(td.cleanup)
        return path

    def run_report(self, path, *args):
        return subprocess.run(["python3", SCRIPT, path, "--json", *args], capture_output=True, text=True, timeout=30)

    def test_pass_with_semantic_savings_and_no_missing(self):
        path = self.write_jsonl([event(f"t{i}", answer_usage={"used_missing_from_projection": []}) for i in range(5)])
        r = self.run_report(path)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(data["metrics"]["avg_savings_pct"], 55.0)
        self.assertEqual(data["metrics"]["semantic_source_rate"], 1.0)

    def test_warns_without_answer_usage(self):
        path = self.write_jsonl([event()])
        r = self.run_report(path)
        data = json.loads(r.stdout)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(data["status"], "WARN")
        self.assertTrue(any("no answer_usage" in w for w in data["warnings"]))

    def test_fail_on_raw_blocks(self):
        path = self.write_jsonl([event(include_blocks=True, answer_usage={"used_missing_from_projection": []})])
        r = self.run_report(path)
        data = json.loads(r.stdout)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(data["status"], "FAIL")
        self.assertTrue(any("raw memory blocks" in f for f in data["failures"]))

    def test_fail_on_used_missing_safety_pin(self):
        usage = {"used_missing_from_projection": [{"entry_ref": "memory#9", "pin_class": "safety", "overlap": 0.5, "reason": "token", "preview": "do not expose secrets"}]}
        path = self.write_jsonl([event(answer_usage=usage)])
        r = self.run_report(path)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertIn("memory#9", dict(data["top_used_missing_refs"]))

    def test_strict_warn_exits_nonzero(self):
        path = self.write_jsonl([event(savings=20.0, answer_usage={"used_missing_from_projection": []})])
        r = self.run_report(path, "--strict")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "WARN")
        self.assertEqual(r.returncode, 1)

    def test_fail_on_safety_pin_drop(self):
        path = self.write_jsonl([event(safety_drop=True, answer_usage={"used_missing_from_projection": []})])
        r = self.run_report(path)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertEqual(data["metrics"]["safety_pin_drops"], 1)

    def test_fail_on_determinism_violation(self):
        rows = [
            event("a", projected_sha="one", answer_usage={"used_missing_from_projection": []}),
            event("b", projected_sha="two", answer_usage={"used_missing_from_projection": []}),
        ]
        path = self.write_jsonl(rows)
        r = self.run_report(path)
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertEqual(data["metrics"]["determinism_violations"], 1)

    def test_duplicate_turn_keeps_latest(self):
        rows = [
            event("t1", projected_sha="old", answer_usage={"used_missing_from_projection": []}),
            event("t1", projected_sha="old", answer_usage={"used_missing_from_projection": []}),
        ]
        path = self.write_jsonl(rows)
        r = self.run_report(path, "--min-answer-turns", "1")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(data["inputs"]["shadow_events"], 1)
        self.assertEqual(data["metrics"]["duplicate_turns"], 1)

    def test_markdown_out_file(self):
        path = self.write_jsonl([event(answer_usage={"used_missing_from_projection": []})])
        out = os.path.join(os.path.dirname(path), "report.md")
        r = subprocess.run(["python3", SCRIPT, path, "--out", out, "--min-answer-turns", "1"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("# Memory Shadow Report", text)
        self.assertIn("**Status:** PASS", text)
        self.assertIn("Daemon path rate", text)

    def test_fail_on_missing_retrieval_telemetry(self):
        path = self.write_jsonl([event(answer_usage={"used_missing_from_projection": []}, retrieval_telemetry=False)])
        r = self.run_report(path, "--min-answer-turns", "1")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertTrue(any("retrieval telemetry" in f for f in data["failures"]))

    def test_fail_on_subprocess_fallback_when_daemon_required(self):
        path = self.write_jsonl([event(answer_usage={"used_missing_from_projection": []}, retrieval_path="subprocess", source="memories-index:20 hits via subprocess:python3.14")])
        r = self.run_report(path, "--min-answer-turns", "1")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertEqual(data["metrics"]["subprocess_fallback_rate"], 1.0)
        self.assertTrue(any("subprocess fallback rate" in f for f in data["failures"]))

    def test_fail_on_high_retrieval_latency(self):
        path = self.write_jsonl([event(answer_usage={"used_missing_from_projection": []}, retrieval_latency_ms=999.0)])
        r = self.run_report(path, "--min-answer-turns", "1", "--max-p95-retrieval-latency-ms", "100")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertEqual(data["metrics"]["p95_retrieval_latency_ms"], 999.0)
        self.assertTrue(any("p95 retrieval latency" in f for f in data["failures"]))

    def test_fail_daemon_rate_below_threshold(self):
        rows = [event(f"d{i}", answer_usage={"used_missing_from_projection": []}, retrieval_path="daemon") for i in range(18)]
        rows += [event(f"x{i}", answer_usage={"used_missing_from_projection": []}, retrieval_path="direct", source="memories-index:20 hits via direct") for i in range(2)]
        path = self.write_jsonl(rows)
        r = self.run_report(path, "--min-answer-turns", "1")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "FAIL")
        self.assertEqual(data["metrics"]["daemon_path_rate"], 0.9)
        self.assertTrue(any("daemon retrieval path rate" in f for f in data["failures"]))

    def test_daemon_rate_threshold_boundary_passes(self):
        rows = [event(f"d{i}", answer_usage={"used_missing_from_projection": []}, retrieval_path="daemon") for i in range(19)]
        rows += [event("x0", answer_usage={"used_missing_from_projection": []}, retrieval_path="direct", source="memories-index:20 hits via direct")]
        path = self.write_jsonl(rows)
        r = self.run_report(path, "--min-answer-turns", "1")
        data = json.loads(r.stdout)
        self.assertEqual(data["status"], "PASS")
        self.assertEqual(data["metrics"]["daemon_path_rate"], 0.95)


if __name__ == "__main__":
    unittest.main()
