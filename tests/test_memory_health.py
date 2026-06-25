#!/usr/bin/env python3
"""Tests for memory_health.py (Area 5) — stdlib unittest, no live data.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_memory_health -v
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


H = _load("memory_health")
TM = _load("temporal_memory")
O = _load("temporal_migrate_onboard")
DELIM = TM.ENTRY_DELIMITER


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memhealth_test_")
        self.memdir = os.path.join(self.tmp, "memories")
        os.makedirs(self.memdir, exist_ok=True)
        self.mem = os.path.join(self.memdir, "MEMORY.md")
        self.usr = os.path.join(self.memdir, "USER.md")
        self._tms = []

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


class TestCapacity(Base):
    def test_reads_metrics(self):
        self.write(["Header notes.", "A durable fact.", "Another fact."],
                   ["A user preference here."])
        rep = H.run_health(self.tmp)
        cap = rep["checks"]["capacity"]
        self.assertEqual(cap["files"]["memory"]["entries"], 3)
        self.assertEqual(cap["files"]["user"]["entries"], 1)
        self.assertTrue(cap["files"]["memory"]["exists"])

    def test_critical_capacity_flag(self):
        # fill USER.md past 90%
        self.write(["Header."], ["x" * 5600])  # 5600/6000 = 93%
        rep = H.run_health(self.tmp)
        self.assertEqual(rep["checks"]["capacity"]["files"]["user"]["status"], "critical")
        self.assertEqual(rep["overall"], "red")
        self.assertTrue(any("USER.md CRITICAL" in a for a in rep["alerts"]))

    def test_entry_ceiling_flag(self):
        entries = ["Header notes."] + [f"Durable fact number {i} kept here." for i in range(40)]
        self.write(entries, ["pref"])
        ep = H.run_health(self.tmp)["checks"]["capacity"]["entry_pressure"]
        self.assertEqual(ep["status"], "critical")  # > ceiling 35
        self.assertGreater(ep["count"], 35)

    def test_green_when_healthy(self):
        self.write(["Header notes.", "One small durable fact."], ["One pref."])
        self.migrate()  # so temporal matches -> no drift
        rep = H.run_health(self.tmp)
        self.assertEqual(rep["checks"]["capacity"]["status"], "ok")
        self.assertEqual(rep["overall"], "green")

    def test_broken_pointers_surface_as_actionable_alert(self):
        self.write(["Header notes.", "Dead pointer: Full context: /definitely/missing/path.md."], ["Pref."])
        rep = H.run_health(self.tmp)
        ha = rep["checks"]["hot_audit"]
        self.assertEqual(ha["status"], "warning")
        self.assertIn("memory#1", ha["broken_pointers"])
        self.assertTrue(any("broken hot-memory pointers" in a for a in rep["alerts"]))

    def test_capacity_counts_code_points_not_bytes(self):
        self.write(["Header notes.", "Emoji fact 😀 with section § marker as content-safe text."], ["Pref."])
        cap = H.run_health(self.tmp)["checks"]["capacity"]["files"]["memory"]
        self.assertEqual(cap["chars"], len(open(self.mem, encoding="utf-8").read()))


class TestMissingFiles(Base):
    def test_missing_files_graceful(self):
        # no MEMORY.md / USER.md written
        rep = H.run_health(self.tmp)  # must not raise
        self.assertEqual(rep["checks"]["capacity"]["files"]["memory"]["exists"], False)
        # overall is not red just because files are absent (fresh install)
        self.assertIn(rep["overall"], ("green", "yellow"))

    def test_main_exit_zero_on_missing(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = H.main(["--home", self.tmp, "--summary"])
        self.assertEqual(rc, 0)


class TestTemporal(Base):
    def test_detects_drift(self):
        self.write(["Header notes.", "Fact one here.", "Fact two here."], ["Pref."])
        self.migrate()
        # external edit -> drift
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write(DELIM + "Sneaky new entry not in temporal.")
        rep = H.run_health(self.tmp)
        t = rep["checks"]["temporal"]
        self.assertEqual(t["status"], "warning")
        self.assertTrue(t["content_drift"])
        self.assertTrue(any("DRIFT" in a for a in rep["alerts"]))

    def test_no_temporal_layer_is_unknown_not_red(self):
        self.write(["Header.", "A fact."], ["Pref."])  # never migrated
        t = H.run_health(self.tmp)["checks"]["temporal"]
        self.assertEqual(t["status"], "unknown")

    def test_temporal_check_is_read_only(self):
        self.write(["Header notes.", "Fact one.", "Fact two."], ["Pref."])
        self.migrate()
        db = os.path.join(self.tmp, "memory_versions.db")
        jsonl = os.path.join(self.memdir, "_versions", "history.jsonl")
        before = (sha(self.mem), sha(self.usr), sha(db), sha(jsonl))
        H.run_health(self.tmp)  # runs temporal verify on a COPY
        self.assertEqual((sha(self.mem), sha(self.usr), sha(db), sha(jsonl)), before)


class TestCronsAndStateDb(Base):
    def test_reads_cron_registry(self):
        crondir = os.path.join(self.tmp, "cron")
        os.makedirs(crondir, exist_ok=True)
        jobs = {"jobs": [
            {"id": "1", "name": "Memory Curator — Capacity Monitor", "last_status": "error",
             "last_run_at": "2026-06-23T18:00:00", "state": "enabled"},
            {"id": "2", "name": "Unrelated job", "last_status": "error"},
        ]}
        with open(os.path.join(crondir, "jobs.json"), "w") as fh:
            json.dump(jobs, fh)
        self.write(["Header."], ["Pref."])
        c = H.run_health(self.tmp)["checks"]["crons"]
        self.assertIn("Memory Curator — Capacity Monitor", c["errors"])
        self.assertNotIn("Unrelated job", str(c["errors"]))  # non-memory job ignored

    def test_paused_error_cron_not_flagged(self):
        crondir = os.path.join(self.tmp, "cron")
        os.makedirs(crondir, exist_ok=True)
        with open(os.path.join(crondir, "jobs.json"), "w") as fh:
            json.dump({"jobs": [{"id": "1", "name": "Memory thing", "last_status": "error",
                                 "state": "paused", "paused_at": "x"}]}, fh)
        self.write(["Header."], ["Pref."])
        c = H.run_health(self.tmp)["checks"]["crons"]
        self.assertEqual(c["errors"], [])  # paused jobs don't alarm

    def test_state_db_sizes(self):
        # a small fake state.db at home root
        with open(os.path.join(self.tmp, "state.db"), "wb") as fh:
            fh.write(b"x" * 1024)
        self.write(["Header."], ["Pref."])
        sdb = H.run_health(self.tmp)["checks"]["state_db"]
        self.assertTrue(any(d["path"] == "state.db" for d in sdb["dbs"]))
        self.assertEqual(sdb["status"], "ok")


class TestReviewRegressions(Base):
    def _write_jobs(self, obj):
        crondir = os.path.join(self.tmp, "cron")
        os.makedirs(crondir, exist_ok=True)
        with open(os.path.join(crondir, "jobs.json"), "w") as fh:
            json.dump(obj, fh)

    def test_malformed_jobs_json_does_not_crash(self):
        """HIGH: a valid-JSON-but-wrong-shape registry must NOT crash the report
        or leak a non-zero exit."""
        self.write(["Header.", "A fact."], ["Pref."])
        for bad in ({"updated_at": "x"},          # dict missing 'jobs'
                    {"jobs": None},               # jobs not a list
                    {"jobs": [{"name": "memory thing", "last_status": "ok"}, None, 42]},  # bad elements
                    "a string", 42, ["x", 123]):
            self._write_jobs(bad)
            rep = H.run_health(self.tmp)  # must not raise
            self.assertIn(rep["overall"], ("green", "yellow", "red"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = H.main(["--home", self.tmp, "--summary"])
            self.assertEqual(rc, 0, f"malformed jobs.json must still exit 0 (got {bad!r})")

    def test_whitespace_diff_not_yellow(self):
        """MEDIUM: a trailing-newline (whitespace-only) diff must keep temporal
        status ok and not flip the badge yellow or raise a DRIFT alert."""
        self.write(["Header notes.", "Fact one.", "Fact two."], ["Pref."])
        self.migrate()
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write("\n")  # benign trailing newline
        rep = H.run_health(self.tmp)
        self.assertEqual(rep["checks"]["temporal"]["status"], "ok")
        self.assertFalse(any("DRIFT" in a for a in rep["alerts"]))
        self.assertEqual(rep["overall"], "green")


class TestExitConvention(Base):
    def test_red_health_still_exits_zero(self):
        self.write(["Header."], ["x" * 5800])  # critical
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = H.main(["--home", self.tmp, "--json"])
        self.assertEqual(rc, 0, "a RED health report must still exit 0 (alert is in content)")
        obj = json.loads(buf.getvalue())
        self.assertEqual(obj["overall"], "red")

    def test_json_and_summary_outputs(self):
        self.write(["Header.", "Fact."], ["Pref."])
        buf = io.StringIO()
        with redirect_stdout(buf):
            H.main(["--home", self.tmp, "--json"])
        json.loads(buf.getvalue())  # valid JSON
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            H.main(["--home", self.tmp, "--summary"])
        self.assertIn("Memory health", buf2.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
