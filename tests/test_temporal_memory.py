#!/usr/bin/env python3
"""Tests for temporal_memory.py — the JSONL source-of-truth writer + engine.

Covers the data-integrity paths the UltraReview found untested:
  * SAFETY-2 — a torn/partial trailing JSONL line must NOT destroy the next event
  * concurrency — N processes appending must lose nothing (fcntl flock correctness)
plus record / round-trip / prune / restore round-trips.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_temporal_memory -v
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)


def _load(name):
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TM = _load("temporal_memory")
DELIM = TM.ENTRY_DELIMITER


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tmem_test_")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, "memories"), exist_ok=True)
        self._tms = []

    def tearDown(self):
        import shutil
        for t in self._tms:
            try:
                t.conn.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tm(self, **kw):
        t = TM.TemporalMemory(home=self.home, **kw)
        self._tms.append(t)
        return t

    def jsonl_path(self):
        return os.path.join(self.home, "memories", "_versions", "history.jsonl")


class TestRecordRetrieve(Base):
    def test_record_and_retrieve(self):
        tm = self.tm()
        ev = tm.record(fact_key="editor", content="Editor: User prefers vim.")
        self.assertIsNotNone(ev)
        cur = tm.current(key="editor")
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["content"], "Editor: User prefers vim.")
        self.assertEqual(len(tm.history("editor")), 1)

    def test_record_round_trip(self):
        tm = self.tm()
        tm.record(fact_key="editor", content="Editor: User prefers vim.")
        tm.record(fact_key="lang", content="Lang: User writes Python.")
        before = {r["fact_key"]: r["content"] for r in tm.current()}
        # fresh instance + forced rebuild from the JSONL source of truth
        tm2 = self.tm()
        tm2.rebuild(force=True)
        after = {r["fact_key"]: r["content"] for r in tm2.current()}
        self.assertEqual(before, after)
        self.assertEqual(after["editor"], "Editor: User prefers vim.")
        self.assertEqual(after["lang"], "Lang: User writes Python.")


class TestTornJsonl(Base):
    """SAFETY-2 regression — the core silent-data-loss bug."""

    def test_torn_jsonl_line_survives(self):
        tm = self.tm()
        a = tm.record(fact_key="alpha", content="Alpha: first durable fact.")
        self.assertIsNotNone(a)
        jp = self.jsonl_path()
        # Simulate a torn write: append a partial JSON record WITH NO trailing
        # newline (a crash mid-append, or an externally-edited log).
        with open(jp, "a", encoding="utf-8") as f:
            f.write('{"event_id":"trunc","fact_key":"beta","content":"par')  # no newline
        # The next record() must NOT glue onto the torn bytes and lose itself.
        b = tm.record(fact_key="beta", content="Beta: second durable fact.")
        self.assertIsNotNone(b)
        # Both survive a fresh rebuild from the source of truth.
        tm2 = self.tm()
        tm2.rebuild(force=True)
        keys = {r["fact_key"]: r["content"] for r in tm2.current()}
        self.assertIn("beta", keys, "new event was LOST by a torn trailing line (SAFETY-2)")
        self.assertEqual(keys["beta"], "Beta: second durable fact.")
        self.assertIn("alpha", keys, "pre-existing event was lost")
        # The torn fragment is healed out of the hot log (not re-glued): every
        # remaining line is valid JSON.
        for line in Path(jp).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                json.loads(line)   # raises if a poison line remains

    def test_unterminated_complete_line_no_glue(self):
        """A COMPLETE last line that simply lacks a trailing newline must be
        newline-healed so the next append lands on its own line."""
        tm = self.tm()
        tm.record(fact_key="alpha", content="Alpha: first.")
        jp = self.jsonl_path()
        raw = Path(jp).read_text(encoding="utf-8")
        # strip the trailing newline -> file ends mid-line-ish (complete JSON, no \n)
        Path(jp).write_text(raw.rstrip("\n"), encoding="utf-8")
        tm.record(fact_key="beta", content="Beta: second.")
        tm2 = self.tm()
        tm2.rebuild(force=True)
        keys = {r["fact_key"] for r in tm2.current()}
        self.assertEqual(keys, {"alpha", "beta"})

    def test_unicode_separator_content_survives(self):
        """Content containing U+2028/U+2029/U+0085 (emitted RAW by json.dumps) must
        NOT be shredded by the reader and then permanently deleted by a heal/prune
        rewrite. str.splitlines() splits on these; the fix splits on '\\n' only.
        (adversarial review: CRITICAL)"""
        seps = {"ls": " ", "ps": " ", "nel": ""}
        tm = self.tm()
        for k, sep in seps.items():
            tm.record(fact_key=k, content=f"Rule {k}:{sep}body line.", allow_duplicate=True)
        keys = {r["fact_key"]: r["content"] for r in tm.current()}
        for k, sep in seps.items():
            self.assertEqual(keys.get(k), f"Rule {k}:{sep}body line.", f"{k} lost on read")
        # Drive _rewrite_hot_log via record()'s corrupt_found heal branch...
        with open(self.jsonl_path(), "a", encoding="utf-8") as f:
            f.write('{"event_id":"trunc","fact_key":"x","content":"par')   # torn, no NL
        tm.record(fact_key="z", content="trigger heal.", allow_duplicate=True)
        # ...and via prune()'s hot-log rewrite (give it a real multi-version fact to prune).
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=300)).replace(
            microsecond=0).isoformat()
        tm.record(fact_key="m", content="m v1", recorded_at=old, allow_duplicate=True)
        tm.record(fact_key="m", content="m v2", recorded_at=old, allow_duplicate=True)
        tm.record(fact_key="m", content="m v3", allow_duplicate=True)
        self.assertGreaterEqual(tm.prune(days=90, keep_per_key=1)["pruned"], 1)
        tm2 = self.tm()
        tm2.rebuild(force=True)
        keys2 = {r["fact_key"]: r["content"] for r in tm2.current()}
        for k, sep in seps.items():
            self.assertIn(k, keys2, f"{k} PERMANENTLY LOST after heal/prune rewrite (CRITICAL)")
            self.assertEqual(keys2[k], f"Rule {k}:{sep}body line.")

    def test_dedup_write_heals_torn_line(self):
        """A no-op duplicate write must still heal a torn line out of the hot log
        so a duplicate-heavy workload can't leave it poisoned. (adversarial review: MEDIUM)"""
        tm = self.tm()
        tm.record(fact_key="alpha", content="Alpha: stable fact.")
        with open(self.jsonl_path(), "a", encoding="utf-8") as f:
            f.write('{"event_id":"trunc","fact_key":"x","content":"par')   # torn
        res = tm.record(fact_key="alpha", content="Alpha: stable fact.")   # duplicate -> no-op
        self.assertIsNone(res)
        for line in Path(self.jsonl_path()).read_text(encoding="utf-8").split("\n"):
            if line.strip():
                json.loads(line)   # raises if the poison line survived the no-op write

    def test_quarantine_idempotent_on_repeated_reads(self):
        """A persistent torn line is re-detected on every read; the .corrupt sidecar
        must NOT grow without bound. (adversarial review: MEDIUM)"""
        tm = self.tm()
        tm.record(fact_key="alpha", content="Alpha.")
        with open(self.jsonl_path(), "a", encoding="utf-8") as f:
            f.write('{"event_id":"trunc","fact_key":"x","content":"par')   # torn, persists
        corrupt = Path(self.jsonl_path()).with_name("history.jsonl.corrupt")
        tm._read_events()
        n1 = len([l for l in corrupt.read_text(encoding="utf-8").split("\n") if l.strip()])
        for _ in range(5):
            tm._read_events()
        n2 = len([l for l in corrupt.read_text(encoding="utf-8").split("\n") if l.strip()])
        self.assertGreaterEqual(n1, 1)
        self.assertEqual(n1, n2, "torn fragment re-quarantined on every read (unbounded growth)")


class TestConcurrentRecord(Base):
    def test_concurrent_record(self):
        if not hasattr(os, "fork"):
            self.skipTest("requires os.fork (POSIX)")
        shared_jsonl = self.jsonl_path()
        os.makedirs(os.path.dirname(shared_jsonl), exist_ok=True)
        n_proc, n_facts = 4, 10
        pids = []
        for p in range(n_proc):
            pid = os.fork()
            if pid == 0:  # child: own DB, SHARED jsonl (flock serializes appends)
                try:
                    ctm = TM.TemporalMemory(
                        home=self.home,
                        db_path=os.path.join(self.tmp, f"child_{p}.db"),
                        jsonl_path=shared_jsonl)
                    for i in range(n_facts):
                        ctm.record(fact_key=f"p{p}-f{i}",
                                   content=f"proc {p} fact {i} unique durable line.",
                                   allow_duplicate=True)
                    ctm.conn.close()
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            pids.append(pid)
        rcs = [os.waitpid(pid, 0)[1] for pid in pids]
        self.assertTrue(all(os.WIFEXITED(s) and os.WEXITSTATUS(s) == 0 for s in rcs),
                        f"a child writer failed: {rcs}")
        events = []
        with open(shared_jsonl, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    events.append(json.loads(ln))   # every line intact
        self.assertEqual(len(events), n_proc * n_facts,
                         "lost/torn events under concurrent append")
        self.assertEqual(len({e["event_id"] for e in events}), n_proc * n_facts,
                         "duplicate event_id under concurrent append")
        self.assertEqual(len({e["fact_key"] for e in events}), n_proc * n_facts)


class TestPrune(Base):
    def test_prune_keeps_birth_and_current(self):
        tm = self.tm()
        key = "rotator"
        # 5 distinct versions of ONE fact; v1..v4 backdated (old), v5 = now.
        for i, days in enumerate([200, 199, 198, 197, 0], start=1):
            ts = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).replace(
                microsecond=0).isoformat()
            tm.record(fact_key=key, content=f"Rotator: version {i} content.",
                      recorded_at=ts, allow_duplicate=True)
        self.assertEqual(len(tm.history(key)), 5)
        res = tm.prune(days=90, keep_per_key=1)
        self.assertEqual(res["pruned"], 3, res)        # v2,v3,v4 -> cold log
        contents = [h["content"] for h in tm.history(key)]
        self.assertIn("Rotator: version 1 content.", contents, "birth (v1) was pruned")
        self.assertIn("Rotator: version 5 content.", contents, "current (v5) was pruned")
        cur = tm.current(key=key)
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["content"], "Rotator: version 5 content.")

    def test_prune_keeps_current_even_when_old(self):
        """The is_current retention clause must hold even when the current version
        is OLD and outside the keep_per_key window (keep_per_key=0 removes the
        'recent' escape hatch, so ONLY is_current can save v3). (adversarial review)"""
        tm = self.tm()
        key = "k"

        def old(d):
            return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=d)).replace(
                microsecond=0).isoformat()

        tm.record(fact_key=key, content="v1", recorded_at=old(300), allow_duplicate=True)
        tm.record(fact_key=key, content="v2", recorded_at=old(250), allow_duplicate=True)
        tm.record(fact_key=key, content="v3 current", recorded_at=old(200), allow_duplicate=True)
        res = tm.prune(days=90, keep_per_key=0)   # no 'recent' slot at all
        self.assertEqual(res["pruned"], 1, res)   # only v2 is prunable
        contents = [h["content"] for h in tm.history(key)]
        self.assertIn("v1", contents, "birth lost")
        self.assertIn("v3 current", contents,
                      "current (old) version pruned — is_current clause not honored")
        self.assertNotIn("v2", contents)


class TestRestore(Base):
    def test_restore_atomic(self):
        tm = self.tm()
        mem = os.path.join(self.home, "memories", "MEMORY.md")
        v1 = "Editor: User prefers vim."
        v2 = "Editor: User prefers neovim now."
        key = TM.derive_key(v1)
        self.assertEqual(key, TM.derive_key(v2))   # same logical fact
        tm.record(fact_key=key, content=v1)
        tm.record(fact_key=key, content=v2)
        Path(mem).write_text(v2, encoding="utf-8")   # live file holds current (v2)
        res = tm.restore(key, version=1, apply=True)
        self.assertTrue(res["applied"])
        baks = [p for p in os.listdir(os.path.dirname(mem)) if p.startswith("MEMORY.md.bak.")]
        self.assertTrue(baks, "restore --apply did not archive a .bak")
        self.assertEqual(
            Path(os.path.join(os.path.dirname(mem), baks[0])).read_text(encoding="utf-8"), v2)
        entries = [e.strip() for e in Path(mem).read_text(encoding="utf-8").split(DELIM)
                   if e.strip()]
        self.assertIn(v1, entries)


if __name__ == "__main__":
    unittest.main(verbosity=2)
