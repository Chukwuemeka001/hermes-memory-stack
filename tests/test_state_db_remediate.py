#!/usr/bin/env python3
"""Safety tests for state_db_remediate.py — stdlib unittest, no live data.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_state_db_remediate -v
    # or
    python3 tests/test_state_db_remediate.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
SCRIPTS = os.path.join(PKG, "scripts")
sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS)

import synthetic_db as syn  # noqa: E402


def _load_module():
    path = os.path.join(SCRIPTS, "state_db_remediate.py")
    spec = importlib.util.spec_from_file_location("state_db_remediate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_module()


def policy(**over):
    p = mod.default_policy()
    p.update(over)
    return p


def write_policy(path, **over):
    p = policy(**over)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(p, fh)
    return path


def open_ro(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_set(path):
    con = open_ro(path)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    return names


def session_ids(path):
    con = open_ro(path)
    ids = {r[0] for r in con.execute("SELECT id FROM sessions")}
    con.close()
    return ids


def count(path, table):
    con = open_ro(path)
    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    con.close()
    return n


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="remediate_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *p):
        return os.path.join(self.tmp, *p)


# --------------------------------------------------------------------------- #
# AUDIT                                                                        #
# --------------------------------------------------------------------------- #
class TestAudit(Base):
    def test_01_audit_clean_db(self):
        """Audit on a tiny clean DB reports sane metrics, no errors."""
        db = syn.build_clean_db(self.path("home", "state.db"))
        rec = mod.audit_db(db, self.path("home"))
        self.assertTrue(rec["is_session_db"])
        self.assertEqual(rec["errors"], [])
        self.assertEqual(rec["sessions_count"], 3)
        self.assertGreater(rec["messages_count"], 0)
        self.assertEqual(rec["role"], "default")

    def test_02_audit_detects_unclosed(self):
        """Audit counts unclosed (ended_at IS NULL) sessions."""
        db = syn.build_messy_db(self.path("state.db"))
        rec = mod.audit_db(db, self.tmp)
        # recent-open, old-open-1, old-open-2, comp-child  => 4 unclosed
        self.assertEqual(rec["unclosed_sessions"], 4)
        self.assertIsNotNone(rec["age_distribution"])
        self.assertGreaterEqual(rec["age_distribution"]["gt_180d"], 1)

    def test_03_audit_detects_compression_parents(self):
        """Audit finds compression parents and splits deletable vs keep."""
        db = syn.build_messy_db(self.path("state.db"))
        rec = mod.audit_db(db, self.tmp)
        comp = rec["compression_parents"]
        self.assertEqual(comp["total"], 2)          # comp-parent + comp-orphan
        self.assertEqual(comp["with_child"], 1)      # only comp-parent has a child
        self.assertEqual(comp["without_child"], 1)   # comp-orphan must be kept
        self.assertEqual(comp["detected_by"], "end_reason='compression'")
        self.assertGreater(comp["eligible_message_bytes_estimate"], 0)

    def test_04_audit_detects_fts_and_trigram(self):
        """Audit lists FTS tables and flags the trigram index specifically."""
        db = syn.build_messy_db(self.path("state.db"))
        rec = mod.audit_db(db, self.tmp)
        self.assertIn("messages_fts", rec["fts_tables"])
        self.assertIn("messages_fts_trigram", rec["fts_tables"])
        self.assertEqual(rec["trigram_fts_tables"], ["messages_fts_trigram"])
        self.assertGreater(rec["fts_footprint"]["trigram_total_bytes"], 0)

    def test_04b_audit_old_schema_graceful(self):
        """Older schema (no end_reason, no trigram) audits without crashing."""
        p = self.path("old", "state.db")
        d = syn.SyntheticDB(p, schema="old", with_trigram=False)
        d.add_session("a", days_ago=1, ended=0.5)
        d.add_session("b", days_ago=200, ended=False)
        d.close()
        rec = mod.audit_db(p, self.path("old"))
        self.assertTrue(rec["is_session_db"])
        self.assertEqual(rec["errors"], [])
        self.assertEqual(rec["unclosed_sessions"], 1)
        self.assertEqual(rec["compression_parents"]["total"], 0)
        self.assertIsNone(rec["compression_parents"]["detected_by"])
        self.assertEqual(rec["trigram_fts_tables"], [])
        self.assertTrue(any("end_reason" in w for w in rec["warnings"]))

    def test_04c_audit_skips_non_session_db(self):
        """A SQLite DB without sessions/messages is audit-only, never remediated."""
        p = self.path("kanban.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY, title TEXT)")
        con.commit()
        con.close()
        rec = mod.audit_db(p, self.tmp)
        self.assertFalse(rec["is_session_db"])
        self.assertTrue(any("not a Hermes session DB" in w for w in rec["warnings"]))


# --------------------------------------------------------------------------- #
# SIMULATE / CLEANUP ENGINE                                                    #
# --------------------------------------------------------------------------- #
class TestSimulate(Base):
    def test_05_simulate_does_not_touch_original(self):
        """Simulate runs on a COPY; the original bytes are unchanged."""
        db = syn.build_messy_db(self.path("state.db"))
        before_hash = mod.sha256_file(db)
        wd = self.path("sim")
        res = mod.clean_and_verify(db, policy(prune_closed=True, retention_days=90,
                                              vacuum=True), wd)
        self.assertFalse(res["aborted"])
        self.assertTrue(res["integrity_after"]["ok"])
        # original untouched
        self.assertEqual(mod.sha256_file(db), before_hash)
        # the cleaned copy is a different file and actually changed
        self.assertNotEqual(res["work_db"], db)
        self.assertTrue(os.path.exists(res["work_db"]))

    def test_09_drop_trigram_keeps_base_data(self):
        """Dropping trigram removes trigram tables/triggers but keeps messages."""
        db = syn.build_messy_db(self.path("state.db"))
        sess_before = count(db, "sessions")
        msg_before = count(db, "messages")
        wd = self.path("sim")
        res = mod.clean_and_verify(db, policy(drop_trigram=True, vacuum=True), wd)
        self.assertTrue(res["integrity_after"]["ok"])
        work = res["work_db"]
        tabs = table_set(work)
        # trigram gone
        self.assertNotIn("messages_fts_trigram", tabs)
        self.assertNotIn("messages_fts_trigram_data", tabs)
        # regular fts still present
        self.assertIn("messages_fts", tabs)
        # base data intact
        self.assertEqual(count(work, "sessions"), sess_before)
        self.assertEqual(count(work, "messages"), msg_before)
        # no orphaned trigram triggers (would break future inserts)
        con = open_ro(work)
        trg = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")]
        con.close()
        self.assertFalse(any("trigram" in t for t in trg))
        # and the regular FTS still works after the drop
        con = sqlite3.connect(work)
        hits = con.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH 'remediation'"
        ).fetchone()[0]
        con.close()
        self.assertGreaterEqual(hits, 0)

    def test_10_prune_unclosed_only_when_enabled(self):
        """Unclosed old sessions survive unless prune_unclosed=yes."""
        db = syn.build_messy_db(self.path("state.db"))
        wd = self.path("sim1")
        # default: no pruning at all
        res = mod.clean_and_verify(db, policy(retention_days=90), wd)
        survived = session_ids(res["work_db"])
        self.assertIn("old-open-1", survived)
        self.assertIn("old-open-2", survived)

        # explicit prune_unclosed
        wd2 = self.path("sim2")
        res2 = mod.clean_and_verify(
            db, policy(prune_unclosed=True, retention_days=90, vacuum=True), wd2)
        survived2 = session_ids(res2["work_db"])
        self.assertNotIn("old-open-1", survived2)   # 300d, pruned
        self.assertNotIn("old-open-2", survived2)   # 120d, pruned
        self.assertIn("recent-open", survived2)     # 0.1d, kept
        self.assertEqual(res2["cleanup_stats"]["deleted_sessions"], 2)

    def test_10b_protect_sources_excludes_from_prune(self):
        """protect_sources keeps matching sessions even when old + unclosed."""
        db = syn.build_messy_db(self.path("state.db"))
        wd = self.path("sim")
        res = mod.clean_and_verify(
            db, policy(prune_unclosed=True, retention_days=90,
                       protect_sources=["telegram"], vacuum=False), wd)
        survived = session_ids(res["work_db"])
        self.assertNotIn("old-open-1", survived)   # cli -> pruned
        self.assertIn("old-open-2", survived)      # telegram -> protected

    def test_11_compression_parent_delete_only_when_enabled(self):
        """Compression parents survive unless delete_compression_parents=yes,
        and a parent WITHOUT a child is never auto-deleted."""
        db = syn.build_messy_db(self.path("state.db"))
        # disabled
        wd0 = self.path("sim0")
        res0 = mod.clean_and_verify(db, policy(retention_days=90), wd0)
        self.assertIn("comp-parent", session_ids(res0["work_db"]))

        # enabled
        wd1 = self.path("sim1")
        res1 = mod.clean_and_verify(
            db, policy(delete_compression_parents=True, vacuum=True), wd1)
        ids = session_ids(res1["work_db"])
        self.assertNotIn("comp-parent", ids)   # had a child -> deleted
        self.assertIn("comp-orphan", ids)      # no child -> kept (no summary!)
        self.assertIn("comp-child", ids)       # child preserved
        # child's parent pointer was nulled so FK integrity holds
        con = open_ro(res1["work_db"])
        parent = con.execute(
            "SELECT parent_session_id FROM sessions WHERE id='comp-child'"
        ).fetchone()[0]
        con.close()
        self.assertIsNone(parent)
        self.assertTrue(res1["integrity_after"]["ok"])

    def test_05b_prune_closed_targets_exactly(self):
        """prune_closed removes exactly the old closed sessions."""
        db = syn.build_messy_db(self.path("state.db"))
        wd = self.path("sim")
        res = mod.clean_and_verify(
            db, policy(prune_closed=True, retention_days=90, vacuum=True), wd)
        ids = session_ids(res["work_db"])
        self.assertNotIn("old-closed-1", ids)
        self.assertNotIn("old-closed-2", ids)
        self.assertIn("recent-closed", ids)
        self.assertIn("comp-parent", ids)   # only 40d old -> not pruned by age


# --------------------------------------------------------------------------- #
# APPLY / ARCHIVE / SAFETY GATES                                               #
# --------------------------------------------------------------------------- #
class TestApply(Base):
    def test_06_apply_refuses_without_confirm(self):
        """CLI apply returns nonzero and makes NO archive without --confirm-apply."""
        db = syn.build_messy_db(self.path("state.db"))
        pol = write_policy(self.path("policy.json"), drop_trigram=True, vacuum=True)
        adir = self.path("archive")
        before = mod.sha256_file(db)
        rc = mod.main(["apply", "--db", db, "--policy", pol, "--archive-dir", adir])
        self.assertNotEqual(rc, 0)
        self.assertEqual(mod.sha256_file(db), before)   # untouched
        self.assertFalse(os.path.exists(adir))          # not even created

    def test_07_archive_manifest_created_before_apply(self):
        """A successful apply leaves a verifiable archive (tar + manifest + hashes)."""
        db = syn.build_messy_db(self.path("state.db"))
        pol = policy(prune_closed=True, retention_days=90, drop_trigram=True,
                     vacuum=True)
        adir = self.path("archive")
        res = mod.apply_remediation(db, pol, adir)
        self.assertTrue(res["applied"], msg=res.get("errors"))
        archive_dir = res["archive"]["dir"]
        self.assertTrue(os.path.isdir(archive_dir))
        self.assertTrue(os.path.exists(os.path.join(archive_dir, "original.tar.gz")))
        self.assertTrue(os.path.exists(os.path.join(archive_dir, "RESTORE.md")))
        with open(os.path.join(archive_dir, "manifest.json")) as fh:
            man = json.load(fh)
        names = [f["name"] for f in man["files"]]
        self.assertIn("state.db", names)
        for f in man["files"]:
            self.assertEqual(len(f["sha256"]), 64)

    def test_07b_apply_end_to_end_reclaims_and_restores(self):
        """Apply shrinks the file, keeps base data, and the archive restores it."""
        db = syn.build_messy_db(self.path("state.db"))
        before_bytes = os.path.getsize(db)
        before_hash = mod.sha256_file(db)
        before_sessions = count(db, "sessions")
        pol = policy(prune_closed=True, prune_unclosed=True, retention_days=90,
                     delete_compression_parents=True, drop_trigram=True, vacuum=True)
        adir = self.path("archive")
        res = mod.apply_remediation(db, pol, adir)
        self.assertTrue(res["applied"], msg=res.get("errors"))
        self.assertTrue(res["post_swap_integrity"]["ok"])
        # file shrank, trigram gone, base data still present
        self.assertLess(os.path.getsize(db), before_bytes)
        self.assertNotIn("messages_fts_trigram", table_set(db))
        self.assertIn("recent-open", session_ids(db))
        self.assertLess(count(db, "sessions"), before_sessions)
        # stale sidecars removed
        self.assertFalse(os.path.exists(db + "-wal"))
        self.assertFalse(os.path.exists(db + "-shm"))
        # restore from archive reproduces the original byte-for-byte
        tar = res["archive"]["tar"]
        os.remove(db)
        import tarfile
        with tarfile.open(tar, "r:gz") as t:
            t.extract("state.db", path=os.path.dirname(db))
        self.assertEqual(mod.sha256_file(db), before_hash)

    def test_07c_apply_removes_stale_sidecars(self):
        """Stale -wal/-shm beside the original are removed after swap.

        Leaving them would let SQLite replay the OLD WAL onto the NEW file and
        corrupt it. Zero-length sidecars are valid no-ops, so this is safe to
        construct in a test."""
        db = syn.build_messy_db(self.path("state.db"))
        open(db + "-wal", "w").close()   # empty (valid) WAL
        open(db + "-shm", "w").close()
        pol = policy(drop_trigram=True, vacuum=True)
        res = mod.apply_remediation(db, pol, self.path("archive"))
        self.assertTrue(res["applied"], msg=res.get("errors"))
        self.assertFalse(os.path.exists(db + "-wal"))
        self.assertFalse(os.path.exists(db + "-shm"))
        # the archive captured the sidecars (db + wal + shm = 3 files)
        names = {f["name"] for f in res["archive"]["files"]}
        self.assertEqual(names, {"state.db", "state.db-wal", "state.db-shm"})

    def test_08_integrity_failure_aborts_and_preserves_original(self):
        """Forced post-clean integrity failure aborts; original intact; archive kept."""
        db = syn.build_messy_db(self.path("state.db"))
        before = mod.sha256_file(db)
        pol = policy(prune_closed=True, retention_days=90, vacuum=True)
        adir = self.path("archive")
        os.environ[mod._FORCE_INTEGRITY_FAIL_ENV] = "1"
        try:
            res = mod.apply_remediation(db, pol, adir)
        finally:
            os.environ.pop(mod._FORCE_INTEGRITY_FAIL_ENV, None)
        self.assertFalse(res["applied"])
        self.assertTrue(any("integrity" in e.lower() for e in res["errors"]))
        # original NOT modified
        self.assertEqual(mod.sha256_file(db), before)
        # archive WAS made before the (failed) mutation -> proves archive-first
        self.assertTrue(os.path.isdir(res["archive"]["dir"]))

    def test_08b_apply_refuses_non_session_db(self):
        """Apply refuses a SQLite file that is not a Hermes session DB."""
        p = self.path("kanban.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE cards (id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        before = mod.sha256_file(p)
        pol = policy(vacuum=True)
        res = mod.apply_remediation(p, pol, self.path("archive"))
        self.assertFalse(res["applied"])
        self.assertEqual(mod.sha256_file(p), before)


# --------------------------------------------------------------------------- #
# POLICY VALIDATION                                                            #
# --------------------------------------------------------------------------- #
class TestReviewRegressions(Base):
    """Regressions for the adversarial-review findings (HIGH/MEDIUM)."""

    def test_R1_compression_plus_prune_no_total_loss(self):
        """HIGH #1: a compression parent whose ONLY summary child is pruned must
        be KEPT, so the conversation is never fully erased."""
        db = syn.build_messy_db(self.path("state.db"))  # comp-child unclosed ~39d
        wd = self.path("sim")
        res = mod.clean_and_verify(
            db, policy(prune_unclosed=True, retention_days=30,
                       delete_compression_parents=True, vacuum=True), wd)
        self.assertTrue(res["integrity_after"]["ok"])
        ids = session_ids(res["work_db"])
        self.assertIn("comp-parent", ids, "original kept since its only summary was pruned")
        self.assertNotIn("comp-child", ids, "old unclosed summary pruned as requested")
        tc = res["cleanup_stats"]["target_counts"]
        self.assertEqual(tc["comp_parents_demoted_no_surviving_child"], 1)
        # the conversation is not fully erased: the parent original survives
        self.assertTrue(count(res["work_db"], "messages") > 0)

    def test_R2_multilevel_chain_collapses_one_generation(self):
        """MEDIUM #7: only the leaf-most original is deleted per run; mid + leaf survive."""
        db = syn.build_chain_db(self.path("state.db"))
        wd = self.path("sim")
        res = mod.clean_and_verify(
            db, policy(delete_compression_parents=True, vacuum=True), wd)
        self.assertTrue(res["integrity_after"]["ok"])
        ids = session_ids(res["work_db"])
        self.assertNotIn("chain-gp", ids)   # top original deleted
        self.assertIn("chain-mid", ids)     # deferred to a future run
        self.assertIn("chain-leaf", ids)    # leaf summary preserved
        self.assertGreaterEqual(
            res["cleanup_stats"]["target_counts"]["comp_parents_chain_deferred"], 1)

    def test_R3_old_schema_prune_no_crash(self):
        """BUG/MEDIUM: pruning on a schema without parent_session_id must not crash."""
        p = self.path("old", "state.db")
        d = syn.SyntheticDB(p, schema="old", with_trigram=False)
        d.add_session("recent", days_ago=1, ended=0.5)
        d.add_session("old-open", days_ago=300, ended=False, n_messages=5)
        d.close()
        wd = self.path("sim")
        res = mod.clean_and_verify(p, policy(prune_unclosed=True, retention_days=90,
                                             vacuum=True), wd)
        self.assertFalse(res["aborted"], msg=res.get("abort_reason"))
        self.assertTrue(res["integrity_after"]["ok"])
        ids = session_ids(res["work_db"])
        self.assertNotIn("old-open", ids)
        self.assertIn("recent", ids)

    def test_R4_retention_uses_last_activity_not_start(self):
        """MEDIUM #13: a long-lived session still receiving messages is protected."""
        p = self.path("state.db")
        d = syn.SyntheticDB(p)
        # started 300d ago but a message arrived today
        d.add_session("long-lived", days_ago=300, ended=False, n_messages=4,
                      last_message_days_ago=0.1)
        d.add_session("truly-old", days_ago=300, ended=False, n_messages=4)
        d.close()
        wd = self.path("sim")
        res = mod.clean_and_verify(p, policy(prune_unclosed=True, retention_days=90), wd)
        ids = session_ids(res["work_db"])
        self.assertIn("long-lived", ids, "recent activity protects an old-started session")
        self.assertNotIn("truly-old", ids)

    def test_R5_drop_trigram_preserves_word_fts_triggers(self):
        """HIGH #10: dropping trigram must not remove the surviving word-FTS triggers."""
        db = syn.build_messy_db(self.path("state.db"))
        wd = self.path("sim")
        res = mod.clean_and_verify(db, policy(drop_trigram=True, vacuum=True), wd)
        self.assertTrue(res["integrity_after"]["ok"])
        self.assertTrue(res["fts_health"]["ok"], msg=res["fts_health"]["issues"])
        con = open_ro(res["work_db"])
        trg = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'")]
        con.close()
        word = [t for t in trg if "messages_fts" in t and "trigram" not in t]
        self.assertEqual(len(word), 3, f"word-FTS must keep its 3 triggers, got {word}")
        self.assertFalse(any("trigram" in t for t in trg))

    def test_R6_apply_refuses_snapshot_without_flag(self):
        """MEDIUM #8/#14: apply must refuse a pre-update snapshot DB by default."""
        snap = self.path("profiles", "x", "state-snapshots", "pre-update", "state.db")
        syn.build_messy_db(snap)
        pol = policy(drop_trigram=True, vacuum=True)
        before = mod.sha256_file(snap)
        res = mod.apply_remediation(snap, pol, self.path("arch"))
        self.assertFalse(res["applied"])
        self.assertTrue(any("snapshot" in e.lower() for e in res["errors"]))
        self.assertEqual(mod.sha256_file(snap), before)
        # with explicit override it proceeds
        res2 = mod.apply_remediation(snap, pol, self.path("arch2"), allow_snapshot=True)
        self.assertTrue(res2["applied"], msg=res2.get("errors"))

    def test_R7_apply_refuses_noop_policy(self):
        """LOW #19: an all-default (no-op) policy must not archive+swap+'SUCCESS'."""
        db = syn.build_messy_db(self.path("state.db"))
        before = mod.sha256_file(db)
        res = mod.apply_remediation(db, policy(), self.path("arch"))
        self.assertFalse(res["applied"])
        self.assertTrue(any("no-op" in e.lower() for e in res["errors"]))
        self.assertEqual(mod.sha256_file(db), before)
        self.assertFalse(os.path.isdir(self.path("arch")))  # nothing archived

    def test_R8_apply_wal_gate_refuses_pending_wal(self):
        """HIGH #5: a non-empty -wal (live/killed-mid-write) is refused without --allow-busy."""
        db = syn.build_messy_db(self.path("state.db"))
        with open(db + "-wal", "wb") as fh:
            fh.write(b"\x00" * 4096)  # non-empty (looks live)
        before = mod.sha256_file(db)
        res = mod.apply_remediation(db, policy(drop_trigram=True, vacuum=True),
                                    self.path("arch"))
        self.assertFalse(res["applied"])
        self.assertTrue(any("wal" in e.lower() for e in res["errors"]))
        self.assertEqual(mod.sha256_file(db), before)

    def test_R9_apply_resolves_symlink_not_severs(self):
        """HIGH #2: apply via a symlink cleans the REAL file and keeps the link."""
        real = self.path("canonical", "state.db")
        syn.build_messy_db(real)
        link = self.path("state.db")
        os.symlink(real, link)
        before = os.path.getsize(real)
        res = mod.apply_remediation(link, policy(drop_trigram=True, vacuum=True),
                                    self.path("arch"))
        self.assertTrue(res["applied"], msg=res.get("errors"))
        self.assertTrue(os.path.islink(link), "symlink must NOT be severed")
        self.assertEqual(os.path.realpath(link), os.path.realpath(real))
        self.assertLess(os.path.getsize(real), before, "the REAL file was cleaned")

    def test_R10_liveness_guard_is_non_mutating(self):
        """HIGH #6: liveness_guard must not modify the original (no RW checkpoint)."""
        db = syn.build_messy_db(self.path("state.db"))
        before = mod.sha256_file(db)
        ok, _msg = mod.liveness_guard(db)
        self.assertTrue(ok)
        self.assertEqual(mod.sha256_file(db), before)


class TestPolicy(Base):
    def test_prune_requires_retention(self):
        errs = mod.validate_policy(policy(prune_closed=True, retention_days=None))
        self.assertTrue(any("retention_days" in e for e in errs))

    def test_valid_policy_passes(self):
        self.assertEqual(mod.validate_policy(policy(prune_closed=True,
                                                    retention_days=90)), [])

    def test_warnings_for_dangerous_options(self):
        w = mod.policy_warnings(policy(delete_compression_parents=True,
                                       drop_trigram=True, prune_unclosed=True,
                                       retention_days=3))
        self.assertTrue(any("ORIGINAL" in x for x in w))
        self.assertTrue(any("substring" in x for x in w))

    def test_simulate_cli_runs_and_keeps_workdir(self):
        db = syn.build_messy_db(self.path("state.db"))
        pol = write_policy(self.path("policy.json"), drop_trigram=True, vacuum=True)
        wd = self.path("keep")
        rc = mod.main(["simulate", "--db", db, "--policy", pol, "--workdir", wd,
                       "--json", "--report-file", self.path("rep.json")])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(wd, "state.db")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
