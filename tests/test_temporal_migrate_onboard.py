#!/usr/bin/env python3
"""Tests for temporal_migrate_onboard.py (Area 4) — stdlib unittest, no live data.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_temporal_migrate_onboard -v
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest

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
MA = _load("memory_audit")
MR = _load("memory_rewrite")
O = _load("temporal_migrate_onboard")
DELIM = TM.ENTRY_DELIMITER


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="area4_test_")
        self.home = os.path.join(self.tmp, "home")
        self.memdir = os.path.join(self.home, "memories")
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

    def tm(self):
        # isolated temporal layer under the temp home
        t = TM.TemporalMemory(home=self.home)
        self._tms.append(t)
        return t

    def live_files(self):
        return {"MEMORY.md": self.mem, "USER.md": self.usr}

    def jsonl(self):
        return os.path.join(self.memdir, "_versions", "history.jsonl")


# --------------------------------------------------------------------------- #
class TestFirstMigrationAndVerify(Base):
    def test_first_migration_then_verify_exact(self):
        mem = ["Long-form notes live in ~/.hermes/notes/. Read INDEX.md.",
               "Trading: POIWatcher + LIT. Full context ~/.hermes/notes/trading/.",
               "Routing (2026-06-21): Xiaomi default, then openrouter, then grok.",
               "User prefers blunt correction, always."]
        usr = ["Emeka: trader-engineer, money-minded, ships with guardrails.",
               "Core execution: brief plan then action, no fluff."]
        self.write(mem, usr)
        tm = self.tm()
        # empty temporal -> first migration
        d = O.sync(tm, self.live_files(), confirm=True)
        self.assertTrue(d["stores"]["MEMORY.md"]["first_migration"])
        self.assertFalse(d["drift_detected"])
        # verify reconstructs both files exactly
        v = O.verify(self.tm(), self.live_files())
        self.assertTrue(v["all_match"])
        self.assertTrue(v["stores"]["MEMORY.md"]["exact_match"])
        self.assertTrue(v["stores"]["USER.md"]["exact_match"])
        self.assertEqual(v["facts"], 6)

    def test_verify_is_read_only(self):
        self.write(["A durable fact here.", "Another fact here."], ["Pref one."])
        O.sync(self.tm(), self.live_files(), confirm=True)
        before = (sha(self.mem), sha(self.usr), sha(self.jsonl()))
        O.verify(self.tm(), self.live_files())
        self.assertEqual((sha(self.mem), sha(self.usr), sha(self.jsonl())), before)


class TestDrift(Base):
    def _migrated(self):
        self.write(["Header notes live in ~/.hermes/notes/.",
                    "Trading: full context ~/.hermes/notes/trading/.",
                    "User prefers blunt correction."], ["Emeka is money-minded."])
        O.sync(self.tm(), self.live_files(), confirm=True)

    def test_sync_detects_drift(self):
        self._migrated()
        # external edits: add an entry, change an entry, remove an entry
        new_mem = ["Header notes live in ~/.hermes/notes/.",
                   "Trading: full context ~/.hermes/notes/trading/ and new detail.",  # changed
                   "Brand new external entry not seen by temporal."]               # added; "User prefers" removed
        with open(self.mem, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(new_mem))
        before_jsonl = sha(self.jsonl())
        d = O.sync(self.tm(), self.live_files(), confirm=False)  # dry-run
        ms = d["stores"]["MEMORY.md"]
        self.assertTrue(d["drift_detected"])
        self.assertGreaterEqual(len(ms["new"]), 1)
        self.assertGreaterEqual(len(ms["updated"]), 1)
        self.assertGreaterEqual(len(ms["removed"]), 1)
        # dry-run wrote nothing to the temporal log
        self.assertEqual(sha(self.jsonl()), before_jsonl)

    def test_sync_apply_records_drift(self):
        self._migrated()
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write(DELIM + "Newly added external entry to capture.")
        before_jsonl = sha(self.jsonl())
        d = O.sync(self.tm(), self.live_files(), confirm=True)
        self.assertTrue(d["applied"])
        self.assertNotEqual(sha(self.jsonl()), before_jsonl)  # event recorded
        # after capture, verify reconstructs exactly again
        self.assertTrue(O.verify(self.tm(), self.live_files())["all_match"])


class TestRecordRewrite(Base):
    def test_record_rewrite_chain_and_tombstone(self):
        dump = ("NCLEX details: lots of inline content about phases and cards and pipeline "
                "described at length here. Full context ~/.hermes/notes/nclex/status.md.")
        status = "Gateway fixed on 2026-01-01: now works, build passes."
        mem = ["Header notes live in ~/.hermes/notes/.", dump, status,
               "User prefers blunt correction."]
        self.write(mem, ["Emeka is money-minded."])
        O.sync(self.tm(), self.live_files(), confirm=True)
        pointer = "NCLEX: phases + cards. Full context ~/.hermes/notes/nclex/status.md."
        manifest = {"schema": "hermes-memory-rewrite-manifest/1", "proposals": [
            {"ref": "memory#1", "store": "memory", "rewrite_action": "rewrite_to_pointer",
             "old_text": dump, "new_text": pointer, "archive": {"destination": "/x.md"}},
            {"ref": "memory#2", "store": "memory", "rewrite_action": "remove",
             "old_text": status, "new_text": None, "archive": {"destination": "/y.md"}},
        ]}
        before = sha(self.jsonl())
        # dry-run records nothing
        r0 = O.record_rewrite(self.tm(), manifest, confirm=False)
        self.assertEqual(sha(self.jsonl()), before)
        self.assertGreater(r0["events_planned"], 0)
        # apply
        r = O.record_rewrite(self.tm(), manifest, confirm=True)
        self.assertTrue(r["applied"])
        tm = self.tm()
        # rewrite_to_pointer: chain old->new, current==pointer
        key = TM.derive_key(dump)
        hist = tm.history(key)
        self.assertGreaterEqual(len(hist), 2)
        cur = [h for h in hist if h["is_current"]]
        self.assertEqual(len(cur), 1)
        self.assertEqual(cur[0]["content"], pointer)
        # remove: tombstoned, not current, but retained in history
        rk = TM.derive_key(status)
        cur_keys = {c["fact_key"] for c in tm.current()}
        self.assertNotIn(rk, cur_keys)
        self.assertTrue(tm.history(rk))  # provenance retained


class TestEndToEnd(Base):
    def test_audit_rewrite_record_verify_against_proposed(self):
        """Areas 2→3→4: migrate live, render a rewrite, record it, and verify the
        temporal DB reconstructs the PROPOSED files exactly."""
        mem = ["Long-form notes live in ~/.hermes/notes/. Read INDEX.md.",
               "User prefers blunt, ROI-focused correction, always.",
               ("Trading architecture: order blocks, liquidity inducement, POI logic across "
                "many timeframes and venues with full execution routing and risk controls "
                "described at length inline. Canonical doc ~/.hermes/notes/trading/spec.md."),
               "Gateway fixed on 2026-01-01: restarted, now works, build passes, metrics 9/10."]
        usr = ["Emeka is a trader-engineer; money-minded; ships with guardrails.",
               "Core execution: blunt correction, brief plan then action."]
        self.write(mem, usr)
        # Area 2 + 3 (render proposed)
        report = MA.run_audit(self.mem, self.usr, self.home, user_home=self.home)
        plan = MR.build_plan(report, user_home=self.home)
        out_dir = os.path.join(self.tmp, "proposed")
        res = MR.render(plan, out_dir)
        # Area 4: migrate ORIGINAL live -> temporal, verify exact
        O.sync(self.tm(), self.live_files(), confirm=True)
        self.assertTrue(O.verify(self.tm(), self.live_files())["all_match"])
        # record the rewrite
        with open(res["manifest"]) as _mf:
            manifest = json.load(_mf)
        O.record_rewrite(self.tm(), manifest, confirm=True)
        # temporal now reconstructs the PROPOSED files exactly
        v = O.verify(self.tm(), {"MEMORY.md": res["proposed_files"]["memory"],
                                 "USER.md": res["proposed_files"]["user"]})
        self.assertTrue(v["stores"]["MEMORY.md"]["exact_match"],
                        msg=v["stores"]["MEMORY.md"])
        self.assertTrue(v["stores"]["USER.md"]["exact_match"])
        # live files were never touched by any of this
        # (Area 3 render + Area 4 only wrote temporal + the proposed dir)


class TestEdgeCases(Base):
    def test_empty_memory(self):
        self.write([], ["Only a user pref here."])
        O.sync(self.tm(), self.live_files(), confirm=True)
        v = O.verify(self.tm(), self.live_files())
        self.assertTrue(v["stores"]["MEMORY.md"]["exact_match"])  # "" == ""
        self.assertEqual(v["stores"]["MEMORY.md"]["reconstruct_chars"], 0)

    def test_single_entry(self):
        self.write(["The one and only memory entry."], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        self.assertTrue(O.verify(self.tm(), self.live_files())["stores"]["MEMORY.md"]["exact_match"])

    def test_duplicate_entries_disambiguated(self):
        # two entries that derive the same key but differ in content
        dup_a = "Routing: provider A is the default for now."
        dup_b = "Routing: provider B is the default for now."
        self.write(["Header notes.", dup_a, dup_b], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        v = O.verify(self.tm(), self.live_files())
        self.assertTrue(v["stores"]["MEMORY.md"]["exact_match"],
                        msg="duplicate-key entries must still round-trip in order")

    def test_unknown_creation_date_ok(self):
        # entries with no date -> valid_from None; must still migrate + verify
        self.write(["A timeless preference with no date at all.",
                    "Another dateless durable fact."], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        self.assertTrue(O.verify(self.tm(), self.live_files())["all_match"])


class TestCliGates(Base):
    def test_sync_cli_dryrun_writes_nothing(self):
        self.write(["A fact.", "B fact."], ["Pref."])
        # first migration via CLI apply
        rc = O.main(["sync", "--home", self.home, "--confirm-apply"])
        self.assertEqual(rc, 0)
        # external edit, then a DRY-RUN sync must not change the log or live files
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write(DELIM + "external add")
        before = (sha(self.mem), sha(self.usr), sha(self.jsonl()))
        rc = O.main(["sync", "--home", self.home])  # no --confirm-apply
        self.assertEqual(rc, 0)
        self.assertEqual((sha(self.mem), sha(self.usr), sha(self.jsonl())), before)

    def test_record_rewrite_requires_manifest(self):
        rc = O.main(["record-rewrite", "--home", self.home])
        self.assertEqual(rc, 2)

    def test_verify_cli_exit_code(self):
        self.write(["A fact here.", "B fact here."], ["Pref."])
        O.main(["sync", "--home", self.home, "--confirm-apply"])
        self.assertEqual(O.main(["verify", "--home", self.home]), 0)  # matches
        # introduce drift -> verify should report mismatch (exit 1)
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write(DELIM + "drifted external entry")
        self.assertEqual(O.main(["verify", "--home", self.home]), 1)


class TestReviewRegressions(Base):
    """Regressions for the Area 4 adversarial review (2 HIGH + 3 MEDIUM)."""

    def _manifest(self, props):
        return {"schema": "hermes-memory-rewrite-manifest/1", "proposals": props}

    def test_record_rewrite_idempotent(self):
        """HIGH: re-running the same manifest must be a no-op (no fabricated history)."""
        dump = "NCLEX: long dump with detail. Full context ~/.hermes/notes/nclex.md."
        status = "Gateway fixed on 2026-01-01: now works, build passes."
        self.write(["Header notes.", dump, status], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        mf = self._manifest([
            {"ref": "memory#1", "store": "memory", "rewrite_action": "rewrite_to_pointer",
             "old_text": dump, "new_text": "NCLEX: pointer. Full context ~/.hermes/notes/nclex.md.",
             "archive": {"destination": "/x"}},
            {"ref": "memory#2", "store": "memory", "rewrite_action": "remove",
             "old_text": status, "new_text": None, "archive": {"destination": "/y"}},
        ])
        O.record_rewrite(self.tm(), mf, confirm=True)
        tm = self.tm()
        h_dump = len(tm.history(TM.derive_key(dump)))
        h_stat = len(tm.history(TM.derive_key(status)))
        before_jsonl = sha(self.jsonl())
        # second identical run -> nothing recorded, history unchanged
        r2 = O.record_rewrite(self.tm(), mf, confirm=True)
        self.assertEqual(r2["events_recorded"], 0, "re-run must record no events")
        self.assertEqual(sha(self.jsonl()), before_jsonl)
        tm2 = self.tm()
        self.assertEqual(len(tm2.history(TM.derive_key(dump))), h_dump)
        self.assertEqual(len(tm2.history(TM.derive_key(status))), h_stat)

    def test_record_rewrite_same_derived_key_collision(self):
        """MEDIUM: two entries with the same derived key are both rewritten (the
        '-2' fact is reused, not buried)."""
        a = "Routing: provider A is default."
        b = "Routing: provider B is fallback."   # same derive_key -> 'routing' / 'routing-2'
        self.write(["Header notes.", a, b], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        pa = "Routing A: pointer. Full context ~/.hermes/notes/r.md."
        pb = "Routing B: pointer. Full context ~/.hermes/notes/r.md."
        mf = self._manifest([
            {"ref": "memory#1", "store": "memory", "rewrite_action": "rewrite_to_pointer",
             "old_text": a, "new_text": pa, "archive": {"destination": "/x"}},
            {"ref": "memory#2", "store": "memory", "rewrite_action": "rewrite_to_pointer",
             "old_text": b, "new_text": pb, "archive": {"destination": "/y"}},
        ])
        O.record_rewrite(self.tm(), mf, confirm=True)
        cur = {c["content"] for c in self.tm().current(store="MEMORY.md")}
        self.assertIn(pa, cur, "entry A's rewrite must be current")
        self.assertIn(pb, cur, "entry B's rewrite must not be buried")

    def test_verify_dropped_duplicate_is_content_drift(self):
        """MEDIUM: a dropped duplicate shows as content drift, not 'order differs'."""
        self.write(["Keep this one.", "Dup exactly here.", "Dup exactly here."], [])
        O.sync(self.tm(), self.live_files(), confirm=True)  # temporal collapses the dup
        ms = O.verify(self.tm(), self.live_files())["stores"]["MEMORY.md"]
        self.assertFalse(ms["exact_match"])
        self.assertTrue(ms["content_drift"])
        self.assertFalse(ms["order_differs"])
        self.assertGreaterEqual(ms["entries_only_in_live"], 1)

    def test_verify_whitespace_only_diff(self):
        """MEDIUM: a trailing newline is reported as whitespace-only, not drift."""
        self.write(["A fact here.", "B fact here."], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        with open(self.mem, "a", encoding="utf-8") as fh:
            fh.write("\n")  # editor-added trailing newline
        ms = O.verify(self.tm(), self.live_files())["stores"]["MEMORY.md"]
        self.assertFalse(ms["exact_match"])
        self.assertTrue(ms["whitespace_only_diff"])
        self.assertFalse(ms["content_drift"])
        self.assertFalse(ms["order_differs"])

    def test_sync_dry_matches_apply_for_duplicates(self):
        """MEDIUM: dry preview's NEW count matches what apply actually creates."""
        self.write(["Header notes."], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        with open(self.mem, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(["Header notes.", "Same new entry.", "Same new entry.", "Same new entry."]))
        dry = O.sync(self.tm(), self.live_files(), confirm=False)
        self.assertEqual(len(dry["stores"]["MEMORY.md"]["new"]), 1, "dedup: one unique new fact")
        applied = O.sync(self.tm(), self.live_files(), confirm=True)
        self.assertEqual(applied["ingest_summary"]["created"], 1)

    def test_backdated_pointer_no_inverted_interval(self):
        """HIGH (clamp): a backdated successor never yields a negative valid interval."""
        dump = "Foo: dump updated 2026-06-23 with detail. Full context ~/.hermes/notes/foo.md."
        self.write(["Header notes.", dump], [])
        O.sync(self.tm(), self.live_files(), confirm=True)
        # abnormal manifest: pointer's archived date is OLDER than the dump's date
        ptr = '↪ Foo → archived 2026-01-01. Find: session_search("foo").'
        mf = self._manifest([{"ref": "memory#1", "store": "memory",
                              "rewrite_action": "archive_pointer", "old_text": dump,
                              "new_text": ptr, "archive": {"destination": "/x"}}])
        O.record_rewrite(self.tm(), mf, confirm=True)
        tm = self.tm()
        key = TM.derive_key(dump)
        for r in tm.conn.execute(
                "SELECT eff_valid_from, valid_to FROM versions WHERE fact_key=?", (key,)):
            if r["eff_valid_from"] and r["valid_to"]:
                self.assertGreaterEqual(r["valid_to"], r["eff_valid_from"],
                                        "valid interval must never be inverted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
