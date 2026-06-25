#!/usr/bin/env python3
"""Tests for memory_shadow.py.

Shadow mode must compute projected memory telemetry while keeping FULL memory as the
active answer source. It is read-only for hot-memory files; only the requested JSONL
log is appended.
"""
from __future__ import annotations

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
SCRIPTS = os.path.join(PKG, "scripts")
sys.path.insert(0, SCRIPTS)

SHADOW_PATH = os.path.join(SCRIPTS, "memory_shadow.py")


def _load():
    spec = importlib.util.spec_from_file_location("memory_shadow", SHADOW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MS = _load()
DELIM = MS.MP.ENTRY_DELIMITER
TODAY = MS._dt.date(2026, 6, 25)


def make_home(memory_entries=None, user_entries=None):
    root = tempfile.mkdtemp(prefix="shadow_home_")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    mem = os.path.join(root, "memories")
    os.makedirs(mem, exist_ok=True)
    with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write(DELIM.join(memory_entries or []))
    with open(os.path.join(mem, "USER.md"), "w", encoding="utf-8") as fh:
        fh.write(DELIM.join(user_entries or []))
    return root


class TestShadowCore(unittest.TestCase):
    def test_shadow_keeps_full_active_and_projects(self):
        root = make_home([
            "Safety: do not type passwords or API keys into tools.",
            "NCLEX beta blocker cards need mechanism plus side-effect cluster.",
            "Old unrelated design note about spacing and gradients.",
        ])
        event = MS.run_shadow(home=root, user_home=root, query="beta blocker side effect card",
                              budget=45, today=TODAY)
        self.assertEqual(event["mode"], "shadow")
        self.assertEqual(event["active_block"], "full")
        self.assertGreater(event["full"]["entries_total"], 0)
        self.assertLessEqual(event["projected"]["entries_selected"], event["full"]["entries_total"])
        self.assertIn("selected_refs", event["diff"])
        self.assertNotIn("block", event["full"])
        self.assertNotIn("block", event["projected"])

    def test_include_blocks_is_explicit(self):
        root = make_home(["Memory fact: alpha beta gamma."])
        event = MS.run_shadow(home=root, user_home=root, query="alpha", budget=100,
                              today=TODAY, include_blocks=True)
        self.assertIn("block", event["full"])
        self.assertIn("block", event["projected"])
        self.assertIn("alpha beta gamma", event["full"]["block"])

    def test_answer_usage_flags_projected_miss(self):
        root = make_home([
            "Important runbook: rotate leaked key, audit access logs, then re-issue to gateway.",
            "Low value filler note about unrelated design spacing.",
        ])
        event = MS.run_shadow(home=root, user_home=root, query="unrelated design",
                              budget=25, today=TODAY,
                              answer="We should rotate leaked key and audit access logs before re-issue.")
        usage = event["answer_usage"]
        self.assertIsNotNone(usage)
        self.assertGreaterEqual(usage["used_entry_count"], 1)
        self.assertIn("used_missing_from_projection", usage)

    def test_append_jsonl(self):
        root = make_home(["Memory fact: alpha beta gamma."])
        event = MS.run_shadow(home=root, user_home=root, query="alpha", budget=50, today=TODAY)
        out = os.path.join(tempfile.mkdtemp(prefix="shadow_out_"), "shadow.jsonl")
        MS.append_jsonl(out, event)
        with open(out, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], event["turn_id"])


class TestShadowCLI(unittest.TestCase):
    def test_cli_dry_run_does_not_write(self):
        root = make_home(["Memory fact: alpha beta gamma."])
        out = os.path.join(tempfile.mkdtemp(prefix="shadow_cli_"), "shadow.jsonl")
        r = subprocess.run([
            "python3", SHADOW_PATH, "--home", root, "--user-home", root,
            "--query", "alpha", "--budget", "50", "--today", "2026-06-25",
            "--out", out, "--dry-run", "--json",
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["active_block"], "full")
        self.assertFalse(os.path.exists(out))

    def test_cli_appends_when_not_dry_run(self):
        root = make_home(["Memory fact: alpha beta gamma."])
        out = os.path.join(tempfile.mkdtemp(prefix="shadow_cli_"), "shadow.jsonl")
        r = subprocess.run([
            "python3", SHADOW_PATH, "--home", root, "--user-home", root,
            "--query", "alpha", "--budget", "50", "--today", "2026-06-25",
            "--out", out, "--json",
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        data = json.loads(r.stdout)
        self.assertEqual(data["wrote"], os.path.abspath(out))
        with open(out, encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
