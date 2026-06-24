#!/usr/bin/env python3
"""Tests for memory_maintenance.py (Area 5) — stdlib unittest, no live data.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_memory_maintenance -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)


def _load(name):
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MM = _load("memory_maintenance")
TM = _load("temporal_memory")
O = _load("temporal_migrate_onboard")
DELIM = TM.ENTRY_DELIMITER


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memmaint_test_")
        self.memdir = os.path.join(self.tmp, "memories")
        os.makedirs(self.memdir, exist_ok=True)
        self.mem = os.path.join(self.memdir, "MEMORY.md")
        self.usr = os.path.join(self.memdir, "USER.md")
        self._tms = []
        self.write(["Long-form notes live in ~/.hermes/notes/. Read INDEX.md.",
                    "Trading: full context ~/.hermes/notes/trading/.",
                    "User prefers blunt correction, always."],
                   ["Emeka is money-minded; ships with guardrails."])

    def tearDown(self):
        import shutil
        for t in self._tms:
            try:
                t.conn.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, mem_entries, usr_entries):
        with open(self.mem, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(mem_entries))
        with open(self.usr, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(usr_entries))

    def migrate(self):
        t = TM.TemporalMemory(home=self.tmp)
        self._tms.append(t)
        O.sync(t, {"MEMORY.md": self.mem, "USER.md": self.usr}, confirm=True)

    def hot_hashes(self):
        return (sha(self.mem), sha(self.usr))


class TestOrchestration(Base):
    def test_runs_all_steps_in_order(self):
        rep = MM.run_maintenance(self.tmp)
        steps = [s["step"] for s in rep["steps"]]
        self.assertEqual(steps, MM.STEP_ORDER)
        self.assertEqual(len(steps), 6)

    def test_no_hot_file_mutation(self):
        self.migrate()
        before = self.hot_hashes()
        rep = MM.run_maintenance(self.tmp)
        self.assertEqual(self.hot_hashes(), before)
        self.assertTrue(rep["hot_files_untouched"])

    def test_apply_temporal_sync_writes_only_temporal(self):
        # apply mode records drift into the temporal layer, never the hot files
        self.migrate()
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write(DELIM + "New external entry to capture into temporal.")
        before_hot = self.hot_hashes()
        jsonl = os.path.join(self.memdir, "_versions", "history.jsonl")
        before_jsonl = sha(jsonl)
        rep = MM.run_maintenance(self.tmp, apply_sync=True)
        self.assertEqual(self.hot_hashes(), before_hot, "hot files must never change")
        self.assertNotEqual(sha(jsonl), before_jsonl, "temporal layer should capture the drift")
        self.assertEqual(rep["mode"], "apply-temporal-sync")

    def test_skip_steps(self):
        rep = MM.run_maintenance(self.tmp, skips={"audit", "auto_extract"})
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["audit"], "skipped")
        self.assertEqual(by["auto_extract"], "skipped")
        self.assertIn(by["capacity"], ("ok", "alert"))

    def test_partial_failure_continues(self):
        # force the audit step to crash; the rest must still run
        import memory_audit as MA
        orig = MA.run_audit
        MA.run_audit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            rep = MM.run_maintenance(self.tmp, skips={"temporal_sync", "temporal_verify"})
        finally:
            MA.run_audit = orig
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["audit"], "error")
        self.assertEqual(by["capacity"], "ok")  # ran despite audit failure
        # report still produced; orchestrator did not abort

    def test_partial_failure_hard_crash_in_step(self):
        # replace a step fn with one that raises OUTSIDE its own try/except
        orig = MM._STEP_FNS["capacity"]
        MM._STEP_FNS["capacity"] = lambda ctx: (_ for _ in ()).throw(RuntimeError("hard"))
        try:
            rep = MM.run_maintenance(self.tmp, skips={"temporal_sync", "temporal_verify", "auto_extract"})
        finally:
            MM._STEP_FNS["capacity"] = orig
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["capacity"], "error")
        self.assertIn(by["audit"], ("ok", "alert"))  # audit still ran despite capacity crashing


class TestReviewRegressions(Base):
    def test_whitespace_diff_not_alert(self):
        """MEDIUM: a benign whitespace-only diff must NOT raise a temporal_verify
        alert (otherwise the weekly cron alerts forever on a trailing newline)."""
        self.migrate()
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write("\n")
        rep = MM.run_maintenance(self.tmp, skips={"auto_extract", "audit", "capacity", "temporal_sync"})
        v = next(s for s in rep["steps"] if s["step"] == "temporal_verify")
        self.assertEqual(v["status"], "ok", "whitespace-only diff must not alert")
        self.assertFalse(any("temporal_verify" in a for a in rep["alerts"]))


class TestOutputsAndExit(Base):
    def test_json_and_markdown(self):
        rep = MM.run_maintenance(self.tmp)
        md = MM.render_markdown(rep)
        self.assertIn("# Memory Stack Maintenance", md)
        self.assertIn("Steps (in order)", md)
        # JSON round-trips
        s = json.dumps(rep, default=str)
        self.assertIn("hot_files_untouched", s)

    def test_main_exit_zero_with_alerts(self):
        # USER.md critical -> alerts present, but exit must be 0
        self.write(["Header."], ["x" * 5800])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = MM.main(["--home", self.tmp, "--summary"])
        self.assertEqual(rc, 0)
        self.assertIn("memory maintenance".lower(), buf.getvalue().lower())

    def test_main_json_no_mutation(self):
        self.migrate()
        before = self.hot_hashes()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = MM.main(["--home", self.tmp, "--json"])
        self.assertEqual(rc, 0)
        obj = json.loads(buf.getvalue())
        self.assertTrue(obj["hot_files_untouched"])
        self.assertEqual(self.hot_hashes(), before)

    def test_fresh_user_no_temporal(self):
        # brand-new home, never migrated: temporal steps skip, pass still succeeds
        rep = MM.run_maintenance(self.tmp)
        by = {s["step"]: s["status"] for s in rep["steps"]}
        # temporal steps skipped (no layer) but audit/capacity ran
        self.assertIn(by["temporal_sync"], ("skipped", "alert", "ok"))
        self.assertIn(by["capacity"], ("ok", "alert"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
