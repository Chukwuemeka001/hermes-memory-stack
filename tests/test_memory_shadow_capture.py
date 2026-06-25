#!/usr/bin/env python3
"""Tests for memory_shadow_capture.py."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "scripts", "memory_shadow_capture.py")


def make_home(entries):
    td = tempfile.TemporaryDirectory()
    home = td.name
    memdir = os.path.join(home, "memories")
    os.makedirs(memdir)
    with open(os.path.join(memdir, "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write("\n§\n".join(entries))
    with open(os.path.join(memdir, "USER.md"), "w", encoding="utf-8") as fh:
        fh.write("User prefers concise verified reports.")
    return td, home


class TestMemoryShadowCapture(unittest.TestCase):
    def test_requires_answer_text(self):
        td, home = make_home(["Important note about semantic retrieval."])
        self.addCleanup(td.cleanup)
        r = subprocess.run(["python3", SCRIPT, "--home", home, "--query", "semantic"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("answer text is required", r.stderr)

    def test_capture_writes_jsonl_and_reports(self):
        td, home = make_home([
            "Runbook: semantic retrieval uses Python 3.14 and a Chroma memories index.",
            "Safety: never expose API keys or raw credentials in reports.",
        ])
        self.addCleanup(td.cleanup)
        out = os.path.join(home, "shadow.jsonl")
        md = os.path.join(home, "report.md")
        js = os.path.join(home, "report.json")
        r = subprocess.run([
            "python3", SCRIPT,
            "--home", home,
            "--query", "semantic retrieval python",
            "--answer-text", "Semantic retrieval uses Python 3.14 and we must not expose API keys.",
            "--out", out,
            "--report-md", md,
            "--report-json", js,
            "--turn-id", "capture-test",
            "--json",
        ], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads(r.stdout)
        self.assertEqual(result["event_turn_id"], "capture-test")
        self.assertTrue(os.path.exists(out))
        self.assertTrue(os.path.exists(md))
        self.assertTrue(os.path.exists(js))
        with open(out, encoding="utf-8") as fh:
            event = json.loads(fh.readline())
        self.assertEqual(event["active_block"], "full")
        self.assertIsNotNone(event["answer_usage"])
        self.assertEqual(event["answer_usage"]["answer_chars"], len("Semantic retrieval uses Python 3.14 and we must not expose API keys."))
        self.assertNotIn("block", event["full"])
        with open(js, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertIn(report["status"], {"PASS", "WARN", "FAIL"})
        self.assertGreaterEqual(report["metrics"]["answer_usage_events"], 1)

    def test_strict_warn_exits_nonzero(self):
        td, home = make_home(["One unrelated memory entry."])
        self.addCleanup(td.cleanup)
        r = subprocess.run([
            "python3", SCRIPT,
            "--home", home,
            "--query", "something",
            "--answer-text", "No overlap here.",
            "--out", os.path.join(home, "shadow.jsonl"),
            "--strict",
            "--min-avg-savings", "99",
        ], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
