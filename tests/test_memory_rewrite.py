#!/usr/bin/env python3
"""Tests for memory_rewrite.py (Area 3) — stdlib unittest, synthetic data only.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_memory_rewrite -v
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


MA = _load("memory_audit")
MR = _load("memory_rewrite")
DELIM = MA.ENTRY_DELIMITER


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def mk_entry(store, index, text, action, *, kind="preference_fact", paths=None,
             has_target=False, duplicate_of=None, quality=0.5, key=None):
    return {
        "ref": f"{store}#{index}", "store": store, "index": index, "chars": len(text),
        "preview": text[:80], "text": text, "key": key or MA._derive_key(text), "kind": kind,
        "signals": MA.signals_for(text),
        "flags": {"duplicate_of": duplicate_of, "has_real_target": has_target,
                  "paths_referenced": bool(paths), "paths_missing": [], "broken_pointer": False,
                  "too_long": len(text) > 350, "too_short": False, "volatile_claim": False,
                  "dup_jaccard": None, "possible_contradiction": None},
        "scores": {"overall_quality": quality, "durability": 0.5, "hot_memory_fit": 0.5,
                   "pointer_quality": 0.5, "specificity_actionability": 0.5, "staleness_risk": 0.0},
        "dates": [], "paths_referenced": paths or [], "recommended_action": action, "rationale": "",
    }


def mk_report(mem_entries, usr_entries, *, mem_path, usr_path):
    def f(store, path, entries):
        text = DELIM.join(e["text"] for e in entries)
        return {"store": store, "path": path, "exists": True, "sha256": MA.sha256_text(text),
                "char_count": len(text), "char_limit": 15000 if store == "memory" else 6000,
                "capacity_pct": 0.0, "entry_count": len(entries), "entries": entries}
    return {"tool": "memory_audit", "tool_version": "test", "generated_at": "2026-06-23",
            "home": "/tmp", "params": {}, "files": [f("memory", mem_path, mem_entries),
                                                    f("user", usr_path, usr_entries)],
            "duplicate_pairs": [], "contradiction_pairs": [], "summary": {}}


def proposal_for(plan, ref):
    return next(p for p in plan["proposals"] if p["ref"] == ref)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memrw_test_")
        self.mem = os.path.join(self.tmp, "MEMORY.md")
        self.usr = os.path.join(self.tmp, "USER.md")
        self.realdoc = os.path.join(self.tmp, "trading.md")
        with open(self.realdoc, "w") as fh:
            fh.write("# trading\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_live(self, mem_entries, usr_entries):
        with open(self.mem, "w") as fh:
            fh.write(DELIM.join(e["text"] for e in mem_entries))
        with open(self.usr, "w") as fh:
            fh.write(DELIM.join(e["text"] for e in usr_entries))

    def report(self, mem_entries, usr_entries, write=False):
        if write:
            self.write_live(mem_entries, usr_entries)
        return mk_report(mem_entries, usr_entries, mem_path=self.mem, usr_path=self.usr)


# --------------------------------------------------------------------------- #
class TestKeepAndRoundTrip(Base):
    def test_keep_byte_for_byte(self):
        m = [mk_entry("memory", 0, "Header: Long-form notes live in ~/.hermes/notes/.", "keep"),
             mk_entry("memory", 1, "User prefers blunt correction, always.", "keep")]
        plan = MR.build_plan(self.report(m, []))
        self.assertEqual(plan["_out_files_text"]["memory"]["proposed_text"],
                         DELIM.join(e["text"] for e in m))
        for p in plan["proposals"]:
            self.assertEqual(p["rewrite_action"], "keep")
            self.assertEqual(p["new_text"], p["old_text"])

    def test_delimiter_round_trip_exact(self):
        m = [mk_entry("memory", i, f"Durable fact number {i} that is kept verbatim.", "keep")
             for i in range(4)]
        u = [mk_entry("user", 0, "Emeka is a trader-engineer; money-minded.", "keep")]
        plan = MR.build_plan(self.report(m, u))
        self.assertEqual(plan["_out_files_text"]["memory"]["proposed_text"],
                         DELIM.join(e["text"] for e in m))
        self.assertEqual(plan["_out_files_text"]["user"]["proposed_text"],
                         DELIM.join(e["text"] for e in u))


class TestRewriteToPointer(Base):
    def test_with_existing_path_condenses(self):
        text = (f"Trading architecture: order blocks and liquidity inducement across "
                f"many timeframes with detailed POI logic and execution routing. "
                f"Canonical doc {self.realdoc}.")
        m = [mk_entry("memory", 0, text, "rewrite_to_pointer", kind="content_dump",
                      paths=[self.realdoc], has_target=True)]
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        p = proposal_for(plan, "memory#0")
        self.assertEqual(p["rewrite_action"], "rewrite_to_pointer")
        self.assertIn("Full context:", p["new_text"])
        self.assertIn(self.realdoc, p["new_text"])
        self.assertLess(len(p["new_text"]), len(text))
        self.assertTrue(p["new_text"].startswith("Trading architecture:"))

    def test_without_destination_becomes_review(self):
        text = ("Big knowledge dump about the system with no path reference at all, "
                "lots of inline detail that should not be turned into a fake pointer.")
        m = [mk_entry("memory", 0, text, "rewrite_to_pointer", kind="content_dump",
                      paths=[], has_target=False)]
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        p = proposal_for(plan, "memory#0")
        self.assertEqual(p["rewrite_action"], "review")
        self.assertEqual(p["status"], "review_needed")
        self.assertEqual(p["new_text"], text)  # preserved, no fabricated path
        self.assertNotIn("Full context:", p["new_text"])


class TestArchiveAndRemove(Base):
    def test_archive_to_note_pointer_only_in_render_dir(self):
        # long enough that the archive pointer is shorter than the original
        text = ("Memory Curator (2026-06-18): automated stale-item cleanup with pointer "
                "replacement; daily sweep at 3:50AM, monitor every 6h, weekly LLM Sundays, "
                "archive→spine ingestion auto-wired, never-lose guarantee with pointers left "
                "in MEMORY.md when archiving old entries. Lots of design detail inline here.")
        m = [mk_entry("memory", 0, text, "archive_to_note", kind="status_update")]
        self.write_live(m, [])
        before = sha(self.mem)
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        p = proposal_for(plan, "memory#0")
        self.assertEqual(p["rewrite_action"], "archive_pointer")
        self.assertTrue(p["new_text"].startswith(MR.POINTER_SIGIL))
        self.assertIsNotNone(p["archive"])
        # plan alone writes nothing live
        self.assertEqual(sha(self.mem), before)
        # render writes archive under out-dir only
        out = os.path.join(self.tmp, "proposed")
        res = MR.render(plan, out)
        self.assertEqual(sha(self.mem), before, "live file must be untouched")
        self.assertTrue(res["archives"])
        arch_path = res["archives"][0]["scratch_path"]
        self.assertTrue(arch_path.startswith(os.path.abspath(out)))
        self.assertIn(text, read(arch_path))  # original preserved

    def test_remove_after_archive_never_drops_without_manifest(self):
        text = "Telegram watchdog fixed on 2026-01-01: now works, metrics 9/10, build passes."
        m = [mk_entry("memory", 0, "Header: notes live in ~/.hermes/notes/.", "keep"),
             mk_entry("memory", 1, text, "remove_after_archive", kind="status_update")]
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        p = proposal_for(plan, "memory#1")
        self.assertEqual(p["rewrite_action"], "remove")
        self.assertIsNone(p["new_text"])  # dropped from hot memory
        self.assertIsNotNone(p["archive"])  # but archive destination recorded
        # removed from proposed text, original recoverable in manifest
        self.assertNotIn(text, plan["_out_files_text"]["memory"]["proposed_text"])
        out = os.path.join(self.tmp, "proposed")
        res = MR.render(plan, out)
        man = json.load(open(res["manifest"]))
        old_texts = [pp["old_text"] for pp in man["proposals"]]
        self.assertIn(text, old_texts, "removed entry's original must be in the manifest")
        # and written to an archive file
        self.assertTrue(any(text in read(a["scratch_path"]) for a in res["archives"]))


class TestMerge(Base):
    def test_merge_preserves_both_facts(self):
        survivor = mk_entry("memory", 0, "NCLEX UI polish: 16 techniques applied. Doc trading.md.",
                            "keep", quality=0.6, paths=[self.realdoc])
        loser = mk_entry("memory", 1, "Design skill installed: make-interfaces-feel-better, 16 micro-techniques.",
                         "merge", duplicate_of="memory#0", quality=0.4)
        plan = MR.build_plan(self.report([survivor, loser], []), user_home=self.tmp)
        lp = proposal_for(plan, "memory#1")
        sp = proposal_for(plan, "memory#0")
        self.assertEqual(lp["rewrite_action"], "merge_absorb")
        self.assertIsNone(lp["new_text"])  # absorbed -> dropped
        self.assertEqual(sp["new_text"], survivor["text"])  # survivor kept
        self.assertTrue(sp.get("absorbs"))
        self.assertEqual(sp["absorbs"][0]["text"], loser["text"])  # loser fact preserved
        # loser absent from proposed; survivor present
        proposed = plan["_out_files_text"]["memory"]["proposed_text"]
        self.assertNotIn(loser["text"], proposed)
        self.assertIn(survivor["text"], proposed)

    def test_cross_file_merge(self):
        survivor = mk_entry("memory", 0, "Opus delegation: always verify independently. Doc trading.md.",
                            "keep", quality=0.7, paths=[self.realdoc])
        loser = mk_entry("user", 0, "Complex delegation: verify Opus output independently before trusting.",
                         "merge", duplicate_of="memory#0", quality=0.5)
        plan = MR.build_plan(self.report([survivor], [loser]), user_home=self.tmp)
        self.assertNotIn(loser["text"], plan["_out_files_text"]["user"]["proposed_text"])
        self.assertTrue(proposal_for(plan, "memory#0").get("absorbs"))


class TestPreserveReviewAndPrefs(Base):
    def test_verify_and_user_review_preserved(self):
        m = [mk_entry("memory", 0, "Xiaomi mimo is DEFAULT (2026-06-21). Routing volatile.", "verify_current"),
             mk_entry("memory", 1, "Some ambiguous note that needs a human eye.", "user_review")]
        plan = MR.build_plan(self.report(m, []))
        for ref in ("memory#0", "memory#1"):
            p = proposal_for(plan, ref)
            self.assertEqual(p["rewrite_action"], "review")
            self.assertEqual(p["status"], "review_needed")
            self.assertEqual(p["new_text"], p["old_text"])

    def test_user_preferences_not_collapsed(self):
        prefs = [
            mk_entry("user", 0, "Emeka prefers blunt, ROI-focused correction over reassurance.", "keep"),
            mk_entry("user", 1, "Core execution: brief plan then action; hates fluff and option trees.", "keep"),
            # even a long pref with no target must not be fabricated into a pointer
            mk_entry("user", 2, "User prefers " + "very detailed standing workflow rules " * 8,
                     "rewrite_to_pointer", kind="preference_fact", paths=[], has_target=False),
        ]
        plan = MR.build_plan(self.report([], prefs), user_home=self.tmp)
        self.assertEqual(proposal_for(plan, "user#0")["new_text"], prefs[0]["text"])
        self.assertEqual(proposal_for(plan, "user#1")["new_text"], prefs[1]["text"])
        p2 = proposal_for(plan, "user#2")
        self.assertEqual(p2["rewrite_action"], "review")  # not collapsed
        self.assertEqual(p2["new_text"], prefs[2]["text"])


class TestEndToEndReaudit(Base):
    def _messy(self):
        m = [
            "Long-form notes live in `~/.hermes/notes/`. Read INDEX.md first.",
            "User prefers blunt, ROI-focused correction, always.",
            f"Trading architecture details: order blocks, liquidity inducement, POI logic "
            f"across many timeframes and venues with full execution routing and risk "
            f"controls described inline at length here. Canonical doc {self.realdoc}.",
            "Gateway fixed on 2026-01-01: restarted, now works, build passes, metrics 9/10.",
            "TODO: wire the watchdog cron next session.",
        ]
        u = ["Emeka is a trader-engineer; money-minded; ships with guardrails.",
             "Core execution: blunt correction, brief plan then action, no fluff."]
        return m, u

    def test_proposed_reaudits_cleanly_and_no_mutation(self):
        mem_texts, usr_texts = self._messy()
        with open(self.mem, "w") as fh:
            fh.write(DELIM.join(mem_texts))
        with open(self.usr, "w") as fh:
            fh.write(DELIM.join(usr_texts))
        before = (sha(self.mem), sha(self.usr))
        # real audit -> rewrite -> render
        report = MA.run_audit(self.mem, self.usr, self.tmp, user_home=self.tmp)
        plan = MR.build_plan(report, user_home=self.tmp)
        out = os.path.join(self.tmp, "proposed")
        res = MR.render(plan, out)
        # live untouched
        self.assertEqual((sha(self.mem), sha(self.usr)), before)
        # proposed re-audits with zero errors and parses
        rep2 = MA.run_audit(res["proposed_files"]["memory"], res["proposed_files"]["user"],
                            self.tmp, user_home=self.tmp)
        self.assertFalse(any(f.get("errors") for f in rep2["files"]))
        # proposed entry counts match plan
        mem_file = next(f for f in rep2["files"] if f["store"] == "memory")
        self.assertEqual(mem_file["entry_count"], plan["files"]["memory"]["proposed_entries"])


class TestSafetyGates(Base):
    def test_plan_out_refuses_live_input(self):
        m = [mk_entry("memory", 0, "Header: notes.", "keep")]
        self.write_live(m, [])
        audit_path = os.path.join(self.tmp, "audit.json")
        with open(audit_path, "w") as fh:
            json.dump(self.report(m, []), fh)
        before = sha(self.mem)
        rc = MR.main(["plan", "--audit", audit_path, "--out", self.mem])
        self.assertEqual(rc, 2)
        self.assertEqual(sha(self.mem), before)

    def test_render_never_writes_live(self):
        m = [mk_entry("memory", 0, "Header: notes.", "keep"),
             mk_entry("memory", 1, "Old status fixed on 2026-01-01: works now.", "remove_after_archive")]
        self.write_live(m, [])
        before = sha(self.mem)
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        MR.render(plan, os.path.join(self.tmp, "proposed"))
        self.assertEqual(sha(self.mem), before)

    def test_apply_refuses_without_confirm(self):
        m = [mk_entry("memory", 0, "Header: notes.", "keep")]
        self.write_live(m, [])
        audit_path = os.path.join(self.tmp, "audit.json")
        with open(audit_path, "w") as fh:
            json.dump(self.report(m, []), fh)
        before = sha(self.mem)
        rc = MR.main(["apply", "--audit", audit_path, "--archive-dir", os.path.join(self.tmp, "arch")])
        self.assertEqual(rc, 2)
        self.assertEqual(sha(self.mem), before)  # live untouched
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "arch")))  # nothing archived

    def test_apply_with_confirm_archives_first(self):
        m = [mk_entry("memory", 0, "Header: notes live in ~/.hermes/notes/.", "keep"),
             mk_entry("memory", 1, "Status fixed on 2026-01-01: works now, build passes.",
                      "remove_after_archive")]
        self.write_live(m, [])
        original = read(self.mem)
        orig_sha = sha(self.mem)
        audit_path = os.path.join(self.tmp, "audit.json")
        with open(audit_path, "w") as fh:
            json.dump(self.report(m, []), fh)
        arch = os.path.join(self.tmp, "arch")
        rc = MR.main(["apply", "--audit", audit_path, "--archive-dir", arch, "--confirm-apply"])
        self.assertEqual(rc, 0)
        # live changed (entry removed)
        self.assertNotEqual(sha(self.mem), orig_sha)
        self.assertNotIn("Status fixed on 2026-01-01", read(self.mem))
        # original archived first, byte-for-byte
        archived = [os.path.join(arch, f) for f in os.listdir(arch) if "MEMORY.md.pre-rewrite" in f]
        self.assertTrue(archived)
        self.assertEqual(read(archived[0]), original)


class TestReviewRegressions(Base):
    """Regressions for the Area 3 adversarial review (6 MEDIUM + 3 LOW)."""

    def test_short_archive_entry_removed_not_grown(self):
        """MEDIUM: a short archive_to_note entry whose pointer would be LONGER is
        archived + removed, never replaced by a longer pointer."""
        text = "Curator note (2026-06-18): brief."
        m = [mk_entry("memory", 0, text, "archive_to_note", kind="status_update")]
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        p = proposal_for(plan, "memory#0")
        self.assertEqual(p["rewrite_action"], "remove")
        self.assertIsNone(p["new_text"])
        self.assertIsNotNone(p["archive"])  # still preserved

    def test_rewrite_that_would_grow_becomes_review(self):
        """MEDIUM: rewrite_to_pointer that wouldn't be shorter degrades to review."""
        text = f"X: short. {self.realdoc}"  # tiny entry, pointer would be longer
        m = [mk_entry("memory", 0, text, "rewrite_to_pointer", kind="content_dump",
                      paths=[self.realdoc], has_target=True)]
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        p = proposal_for(plan, "memory#0")
        self.assertEqual(p["rewrite_action"], "review")
        self.assertEqual(p["new_text"], text)

    def test_no_growth_invariant(self):
        """MEDIUM: proposed chars never exceed original (no growth advertised)."""
        m = [mk_entry("memory", 0, "Header: notes.", "keep"),
             mk_entry("memory", 1, "Tiny status fixed 2026-01-01.", "archive_to_note", kind="status_update"),
             mk_entry("memory", 2, "Short.", "rewrite_to_pointer", paths=[self.realdoc], has_target=True)]
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        s = plan["summary"]
        self.assertGreaterEqual(s["original_chars"], s["proposed_chars"])
        self.assertFalse(s["grew"])

    def test_archive_pointer_uses_report_home_not_hardcoded(self):
        """MEDIUM: archive pointer path derives from the audit's home (exportable)
        and includes the session_search breadcrumb — never a hardcoded ~/.hermes."""
        text = ("Provider failover (2026-06-21): xiaomi default, then openrouter free models, "
                "then grok, then claude cli, then local phi, then anthropic api, with detailed "
                "fallback sequencing described at length inline in this entry for routing, plus "
                "per-lane overrides, credit guards, and a watchdog that pauses the chain when a "
                "provider repeatedly fails so the gateway never wedges on a dead upstream model.")
        m = [mk_entry("memory", 0, text, "archive_to_note", kind="status_update")]
        rep = mk_report(m, [], mem_path=self.mem, usr_path=self.usr)
        rep["home"] = "/tmp/otheruser/.hermes"  # arbitrary foreign home
        plan = MR.build_plan(rep, user_home=self.tmp)
        p = proposal_for(plan, "memory#0")
        self.assertEqual(p["rewrite_action"], "archive_pointer")
        self.assertIn("session_search(", p["new_text"])
        self.assertIn("/tmp/otheruser/.hermes/memories/_archive/curator/", p["new_text"])
        self.assertNotIn("~/.hermes", p["new_text"])

    def test_foreign_audit_user_home_from_params(self):
        """MEDIUM: user_home comes from the audit params (foreign-audit safe)."""
        m = [mk_entry("memory", 0, "Header: notes.", "keep")]
        rep = mk_report(m, [], mem_path=self.mem, usr_path=self.usr)
        rep["params"] = {"user_home": "/tmp/some/other/home"}
        self.assertEqual(MR.report_user_home(rep), "/tmp/some/other/home")
        self.assertEqual(MR.report_user_home(rep, override="/tmp/x"), "/tmp/x")

    def test_apply_refuses_stale_audit(self):
        """MEDIUM: apply refuses if the live file changed since the audit (SHA drift)."""
        m = [mk_entry("memory", 0, "Header: notes.", "keep"),
             mk_entry("memory", 1, "Old status fixed 2026-01-01: works now, build passes.",
                      "remove_after_archive", kind="status_update")]
        self.write_live(m, [])
        plan = MR.build_plan(self.report(m, []), user_home=self.tmp)
        # external edit AFTER the audit
        with open(self.mem, "a") as fh:
            fh.write(DELIM + "Sneaky external edit not seen by the audit.")
        before = sha(self.mem)
        res = MR.apply(plan, confirm=True, archive_dir=os.path.join(self.tmp, "arch"))
        self.assertFalse(res["applied"])
        self.assertTrue(any("drift" in e.lower() or "stale" in e.lower() for e in res["errors"]))
        self.assertEqual(sha(self.mem), before, "live must be untouched on stale audit")

    def test_apply_archive_no_clobber_same_day(self):
        """MEDIUM: two same-timestamp applies don't overwrite the first pristine archive."""
        m = [mk_entry("memory", 0, "Header: notes live in ~/.hermes/notes/.", "keep"),
             mk_entry("memory", 1, "Status fixed 2026-01-01: works, build passes.",
                      "remove_after_archive", kind="status_update")]
        self.write_live(m, [])
        original = read(self.mem)
        arch = os.path.join(self.tmp, "arch")
        import datetime as dt
        fixed = dt.datetime(2026, 6, 23, 10, 0, 0)
        # first apply
        plan1 = MR.build_plan(self.report(m, []), user_home=self.tmp)
        r1 = MR.apply(plan1, confirm=True, archive_dir=arch, now=fixed)
        self.assertTrue(r1["applied"])
        # second apply at the SAME timestamp (re-audit current live)
        rep2 = MA.run_audit(self.mem, self.usr, self.tmp, user_home=self.tmp)
        plan2 = MR.build_plan(rep2, user_home=self.tmp)
        MR.apply(plan2, confirm=True, archive_dir=arch, now=fixed)
        # the FIRST pristine original must still be recoverable (no clobber)
        archived = [read(os.path.join(arch, f)) for f in os.listdir(arch)
                    if f.startswith("MEMORY.md.pre-rewrite")]
        self.assertTrue(any(a == original for a in archived),
                        "the pristine pre-rewrite original must survive a same-day re-apply")

    def test_compact_summary_no_path_corruption(self):
        """LOW: removing the destination must not corrupt other path-like tokens."""
        dest = "/tmp/a.md"
        text = f"Topic: see /tmp/abc.md and {dest} for context."
        out = MR.compact_summary(text, "Topic", dest)
        self.assertIn("/tmp/abc.md", out)  # the other path is intact


if __name__ == "__main__":
    unittest.main(verbosity=2)
