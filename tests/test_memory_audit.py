#!/usr/bin/env python3
"""Tests for memory_audit.py (Area 2) — stdlib unittest, synthetic files only.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_memory_audit -v
"""
from __future__ import annotations

import datetime as _dt
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


def _load():
    path = os.path.join(SCRIPTS, "memory_audit.py")
    spec = importlib.util.spec_from_file_location("memory_audit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ma = _load()
DELIM = ma.ENTRY_DELIMITER
TODAY = _dt.date(2026, 6, 23)


def write_file(path: str, entries: list[str]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(DELIM.join(entries))
    return path


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def find(report: dict, substr: str) -> dict:
    for f in report["files"]:
        for e in f["entries"]:
            if substr in e["text"]:
                return e
    raise AssertionError(f"no entry contains {substr!r}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memaudit_test_")
        self.home = self.tmp
        self.mem = os.path.join(self.tmp, "memories", "MEMORY.md")
        self.usr = os.path.join(self.tmp, "memories", "USER.md")
        # a real file some entries can point to
        self.real_doc = os.path.join(self.tmp, "real_doc.md")
        with open(self.real_doc, "w") as fh:
            fh.write("# real\n")
        self.missing_doc = os.path.join(self.tmp, "missing_doc.md")  # never created

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def audit(self, **kw):
        kw.setdefault("today", TODAY)
        kw.setdefault("user_home", self.tmp)
        return ma.run_audit(self.mem, self.usr, self.home, **kw)


# --------------------------------------------------------------------------- #
class TestParsing(Base):
    def test_parses_section_delimiter(self):
        write_file(self.mem, [
            "Long-form notes live in `~/.hermes/notes/`. Read INDEX.md first.",
            "User prefers blunt correction over reassurance, always.",
            "Trading: full context ~/.hermes/notes/trading/.",
        ])
        write_file(self.usr, ["Emeka is a trader-engineer; money-minded."])
        rep = self.audit()
        mem = next(f for f in rep["files"] if f["store"] == "memory")
        self.assertEqual(mem["entry_count"], 3)
        self.assertEqual(mem["entries"][0]["kind"], "header")
        # blank/extra delimiters don't create empty entries
        write_file(self.mem, ["A real entry here that is long enough.", "", "  ", "Another real entry here."])
        rep2 = self.audit()
        mem2 = next(f for f in rep2["files"] if f["store"] == "memory")
        self.assertEqual(mem2["entry_count"], 2)


class TestPathChecks(Base):
    def test_existing_path_not_broken(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            f"Trading spec: canonical doc at {self.real_doc} — full context there.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        e = find(rep, "Trading spec")
        self.assertFalse(e["flags"]["broken_pointer"])
        self.assertEqual(e["recommended_action"], "keep")

    def test_missing_path_flagged_broken(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            f"Legacy plan: see {self.missing_doc} for the old approach.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        e = find(rep, "Legacy plan")
        self.assertTrue(e["flags"]["broken_pointer"])
        self.assertEqual(e["recommended_action"], "verify_current")

    def test_directory_and_extensionless_not_broken(self):
        # dirs / model-name-like tokens must NOT be existence-checked (no false broken)
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "Routing: prefer xai-oauth Grok/grok-build-0.1; context ~/.hermes/notes/personal/.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        e = find(rep, "Routing:")
        self.assertFalse(e["flags"]["broken_pointer"])

    def test_tilde_resolves_against_user_home(self):
        # create ~/sub/real.md under a fake user-home; entry references ~/sub/real.md
        uh = os.path.join(self.tmp, "fakehome")
        os.makedirs(os.path.join(uh, "sub"))
        with open(os.path.join(uh, "sub", "real.md"), "w") as fh:
            fh.write("x")
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "Doc: canonical spec at ~/sub/real.md for full context.",
            "Gone: see ~/sub/nope.md for details.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit(user_home=uh)
        self.assertFalse(find(rep, "canonical spec")["flags"]["broken_pointer"])
        self.assertTrue(find(rep, "Gone:")["flags"]["broken_pointer"])


class TestClassification(Base):
    def _std(self, extra_mem, extra_usr=None):
        write_file(self.mem, ["Header: Long-form notes live in `~/.hermes/notes/`."] + extra_mem)
        write_file(self.usr, extra_usr or ["Emeka prefers blunt correction over fluff."])
        return self.audit()

    def test_content_dump_with_pointer_rewrites(self):
        dump = ("Trading architecture details: " + "the system uses order blocks and liquidity "
                "inducement across many timeframes with detailed POI logic " * 4 +
                f" canonical doc {self.real_doc}.")
        rep = self._std([dump])
        e = find(rep, "Trading architecture details")
        self.assertEqual(e["kind"], "content_dump")
        self.assertEqual(e["recommended_action"], "rewrite_to_pointer")

    def test_content_dump_howto_moves_to_skill(self):
        howto = ("How to deploy the gateway: first stop the process, then pull latest, "
                 "then restart the service, then verify the logs, then check telegram, "
                 "then confirm discord, then re-run the watchdog, then validate the cron " * 3)
        rep = self._std([howto])
        e = find(rep, "How to deploy")
        self.assertEqual(e["kind"], "content_dump")
        self.assertEqual(e["recommended_action"], "move_to_skill")

    def test_status_update_detected(self):
        rep = self._std(["Gateway fixed on 2026-01-01: restarted it, now works, build passes."])
        e = find(rep, "Gateway fixed")
        self.assertEqual(e["kind"], "status_update")
        self.assertIn(e["recommended_action"], ("remove_after_archive", "archive_to_note"))

    def test_project_progress_detected(self):
        rep = self._std([
            f"NCLEX Phase 3 complete (2026-01-01): 132 cards shipped, build passes. Status: {self.real_doc}"])
        e = find(rep, "NCLEX Phase 3")
        self.assertEqual(e["kind"], "project_progress")
        self.assertEqual(e["recommended_action"], "verify_current")

    def test_todo_detected(self):
        rep = self._std(["TODO: wire up the watchdog cron and document it next session."])
        e = find(rep, "TODO:")
        self.assertEqual(e["kind"], "todo_temporary")
        self.assertEqual(e["recommended_action"], "user_review")

    def test_durable_preference_kept(self):
        rep = self._std(["User prefers blunt, ROI-focused correction and always wants brief plans."])
        e = find(rep, "ROI-focused")
        self.assertEqual(e["kind"], "preference_fact")
        self.assertEqual(e["recommended_action"], "keep")

    def test_malformed_flagged(self):
        rep = self._std(["Trading system needs work."])
        e = find(rep, "needs work")
        self.assertEqual(e["kind"], "malformed")
        self.assertEqual(e["recommended_action"], "user_review")

    def test_archived_pointer_kept(self):
        rep = self._std([
            "↪ Local LLM: Phi-4 on port… → archived 2026-06-23. Find: session_search(\"phi-4\")."])
        e = find(rep, "Local LLM")
        self.assertEqual(e["kind"], "pointer")
        self.assertEqual(e["recommended_action"], "keep")


class TestDuplicates(Base):
    def test_paraphrase_duplicate_detected_cross_file(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "User prefers concise plain terminal output, no fluff, no option trees.",
        ])
        write_file(self.usr, [
            "Emeka likes concise plain terminal output and hates fluff and option trees.",
        ])
        rep = self.audit()
        pairs = rep["duplicate_pairs"]
        self.assertTrue(pairs, "expected a near-duplicate pair")
        refs = {tuple(sorted((p["a"], p["b"]))) for p in pairs}
        self.assertIn(("memory#1", "user#0"), refs)
        # the lower-quality side gets a merge recommendation
        actions = {find(rep, "concise plain terminal output, no fluff")["recommended_action"],
                   find(rep, "hates fluff")["recommended_action"]}
        self.assertIn("merge", actions)

    def test_distinct_pointers_not_duplicates(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "↪ Local LLM setup → archived 2026-06-23. Find: session_search(\"llm\") or spine search.",
            "↪ Telegram gateway setup → archived 2026-06-21. Find: session_search(\"telegram\") or spine search.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        self.assertEqual(rep["duplicate_pairs"], [],
                         "two distinct archived pointers must not be flagged as duplicates")


class TestContradictions(Base):
    def test_default_conflict_detected(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "Default coding model is Foo-7B.",
            "Bar-9000 is now the default coding model.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        self.assertTrue(rep["contradiction_pairs"], "expected a possible contradiction")
        e = find(rep, "Foo-7B")
        self.assertIsNotNone(e["flags"]["possible_contradiction"])
        self.assertEqual(e["recommended_action"], "user_review")

    def test_enabled_vs_paused_detected(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "Provider failover automation is enabled and active.",
            "Provider failover automation is paused.",
        ])
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        self.assertTrue(any("enabled/active vs paused" in c["reason"]
                            for c in rep["contradiction_pairs"]))

    def test_unrelated_entries_not_contradictions(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "User prefers blunt correction over reassurance.",
            "Trading uses order blocks and liquidity inducement.",
        ])
        write_file(self.usr, ["Emeka is money-minded and ships with guardrails."])
        rep = self.audit()
        self.assertEqual(rep["contradiction_pairs"], [])


class TestSafetyAndOutput(Base):
    def _fixture(self):
        write_file(self.mem, [
            "Header: Long-form notes live in `~/.hermes/notes/`.",
            "User prefers blunt correction, always.",
            "Gateway fixed on 2026-01-01: now works, build passes.",
            f"Trading spec: canonical doc {self.real_doc}.",
        ])
        write_file(self.usr, ["Emeka is a trader-engineer; money-minded; ships with guardrails."])

    def test_no_input_mutation(self):
        self._fixture()
        before = (sha(self.mem), sha(self.usr))
        self.audit()
        # also exercise the CLI path with --out
        out = os.path.join(self.tmp, "report.md")
        ma.main(["--home", self.home, "--memory", self.mem, "--user", self.usr,
                 "--user-home", self.tmp, "--out", out])
        after = (sha(self.mem), sha(self.usr))
        self.assertEqual(before, after, "audit must not modify input files")
        self.assertTrue(os.path.exists(out))

    def test_json_output_valid(self):
        self._fixture()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ma.main(["--home", self.home, "--memory", self.mem, "--user", self.usr,
                          "--user-home", self.tmp, "--json"])
        self.assertEqual(rc, 0)
        obj = json.loads(buf.getvalue())
        self.assertEqual(obj["tool"], "memory_audit")
        self.assertIn("summary", obj)
        self.assertIn("by_recommended_action", obj["summary"])

    def test_markdown_output(self):
        self._fixture()
        rep = self.audit()
        md = ma.render_markdown(rep)
        self.assertIn("# Hermes Hot-Memory Audit", md)
        self.assertIn("| Ref |", md)
        self.assertIn("## Capacity", md)

    def test_capacity_flags(self):
        # tiny limit forces a CRITICAL flag deterministically via a big USER.md
        write_file(self.mem, ["Header: Long-form notes live in `~/.hermes/notes/`."])
        write_file(self.usr, ["x" * 5900])  # 5900/6000 = 98% -> CRITICAL
        rep = self.audit()
        self.assertEqual(rep["summary"]["capacity"]["user"]["flag"], "CRITICAL")

    def test_entry_pressure_over_ceiling(self):
        entries = ["Header: Long-form notes live in `~/.hermes/notes/`."]
        entries += [f"Durable preference number {i}: user always wants thing {i}." for i in range(40)]
        write_file(self.mem, entries)
        write_file(self.usr, ["Emeka prefers blunt correction."])
        rep = self.audit()
        self.assertTrue(rep["summary"]["entry_pressure"]["over_ceiling"])


class TestReviewRegressions(Base):
    """Regressions for the Area 2 adversarial review (1 HIGH + 12 MEDIUM + 1 LOW)."""

    def _std(self, mem_entries, usr_entries=None, **kw):
        write_file(self.mem, ["Header: Long-form notes live in `~/.hermes/notes/`."] + mem_entries)
        write_file(self.usr, usr_entries or ["Emeka prefers blunt correction over fluff."])
        return self.audit(**kw)

    def test_R1_metric_in_preference_not_deleted(self):
        """HIGH: a durable preference containing %/fraction/semver must stay a
        preference_fact and be KEPT (not status_update -> remove_after_archive)."""
        rep = self._std([
            "User prefers risk capped at 2% of account per trade, always.",
            "Always pin deps to v2.0.1 style exact versions.",
            "User wants 5/10 fresh diagnostics before implementing the next phase.",
        ])
        for sub in ("risk capped", "pin deps", "fresh diagnostics"):
            e = find(rep, sub)
            self.assertEqual(e["kind"], "preference_fact", sub)
            self.assertEqual(e["recommended_action"], "keep", sub)

    def test_R1_sanity_real_status_still_detected(self):
        rep = self._std(["Coverage hit 80% on 2026-01-01, merged."])
        self.assertEqual(find(rep, "Coverage hit")["kind"], "status_update")

    def test_R2_confirmed_durable_fact_not_dropped(self):
        rep = self._std([
            "User confirmed the exam date is 2026-05-02.",
            "Deployed the gateway on 2026-05-02.",
        ])
        durable = find(rep, "exam date")
        self.assertNotEqual(durable["recommended_action"], "remove_after_archive")
        self.assertEqual(find(rep, "Deployed the gateway")["kind"], "status_update")

    def test_R2b_expectation_and_frustration_are_durable_preferences(self):
        rep = self._std([], usr_entries=[
            "Memory OS expectation: tools should ACT, not just observe/report. User frustrated that all built memory tools were dry-run/read-only while live memory sat at 83%/92%. Apply fixes when safe — don't build observer tools and leave memory bloated."
        ])
        e = find(rep, "tools should ACT")
        self.assertEqual(e["kind"], "preference_fact")
        self.assertEqual(e["recommended_action"], "keep")

    def test_R3_complementary_versions_not_contradiction(self):
        rep = self._std([
            "Origin Candidate V3 model is active and live.",
            "Origin Candidate V2 model is deprecated.",
        ])
        self.assertEqual(rep["contradiction_pairs"], [],
                         "different versions are complementary, not contradictory")

    def test_R4_dated_activity_log_detected(self):
        rep = self._std(["2026-01-15: rewrote the memory parser and tuned thresholds."])
        e = find(rep, "rewrote the memory parser")
        self.assertEqual(e["kind"], "status_update")
        self.assertGreater(e["scores"]["staleness_risk"], 0.0)

    def test_R5_phase_complete_without_date(self):
        rep = self._std(["Phase 3 of the trainer is complete."])
        e = find(rep, "Phase 3")
        self.assertEqual(e["kind"], "project_progress")
        self.assertEqual(e["recommended_action"], "verify_current")

    def test_R6_midsize_pathless_dump_not_kept(self):
        prose = ("The trading pipeline processes market data through ingestion, "
                 "normalization, feature extraction, and signal generation before "
                 "routing decisions to the execution layer that handles order "
                 "management and risk controls across multiple venues and timeframes "
                 "at once, with reconciliation and logging throughout the run cycle, "
                 "plus a monitoring loop that emits metrics and reconciles fills "
                 "against the journal so discrepancies surface early in the session.")
        self.assertGreater(len(prose), 350)
        rep = self._std([prose])
        e = find(rep, "trading pipeline processes")
        self.assertNotEqual(e["recommended_action"], "keep")
        self.assertIn(e["recommended_action"], ("move_to_note", "rewrite_to_pointer"))

    def test_R7_default_change_phrasings_detected(self):
        rep = self._std([
            "Default coding model is Opus.",
            "We switched the default coding model to Sonnet on 2026-06-20.",
        ])
        self.assertTrue(rep["contradiction_pairs"], "switched-to default change should conflict")

    def test_R8_owner_name_symmetry(self):
        # distinct facts that share only the owner name must not become a dup
        write_file(self.mem, ["Header: Long-form notes live in `~/.hermes/notes/`."])
        write_file(self.usr, ["Jdoe wants morning summaries.", "Jdoe wants evening summaries."])
        # bug shape: name NOT stripped -> inflated similarity -> flagged dup
        bug = ma.run_audit(self.mem, self.usr, self.home, today=TODAY, user_home="/tmp/nobody")
        self.assertTrue(bug["duplicate_pairs"], "control: shared name inflates similarity")
        # fix: owner_name strips the name symmetrically -> not flagged
        fixed = ma.run_audit(self.mem, self.usr, self.home, today=TODAY,
                             user_home="/tmp/nobody", owner_name="jdoe")
        self.assertEqual(fixed["duplicate_pairs"], [])

    def test_R9_non_utf8_does_not_crash(self):
        os.makedirs(os.path.dirname(self.mem), exist_ok=True)
        with open(self.mem, "wb") as fh:
            fh.write("Header: notes live in ~/.hermes/notes/\n§\nUser prefers blunt".encode())
            fh.write(b" \x80 ")  # invalid UTF-8 byte
            fh.write(" correction.".encode())
        write_file(self.usr, ["Emeka is money-minded."])
        rep = self.audit()  # must not raise
        self.assertEqual(rep["tool"], "memory_audit")
        self.assertGreaterEqual(rep["summary"]["total_entries"], 2)

    def test_R10_out_refuses_to_clobber_input(self):
        self._std(["User prefers blunt correction."])
        before = sha(self.mem)
        rc = ma.main(["--home", self.home, "--memory", self.mem, "--user", self.usr,
                      "--user-home", self.tmp, "--out", self.mem])
        self.assertEqual(rc, 2)
        self.assertEqual(sha(self.mem), before, "input must be untouched")
        # a distinct --out still works
        out = os.path.join(self.tmp, "rep.md")
        rc2 = ma.main(["--home", self.home, "--memory", self.mem, "--user", self.usr,
                       "--user-home", self.tmp, "--out", out])
        self.assertEqual(rc2, 0)
        self.assertTrue(os.path.exists(out))

    def test_R11_summary_has_shrink_prioritization(self):
        big = "Routing details: " + ("xiaomi then openrouter then grok then claude " * 12)
        rep = self._std([big])
        s = rep["summary"]
        self.assertIn("top_shrink_targets", s)
        self.assertIn("estimated_savings", s)
        self.assertTrue(s["top_shrink_targets"])
        self.assertGreater(s["estimated_savings"]["memory"]["est_recoverable_chars"], 0)

    def test_R12_long_preference_no_target_is_user_review(self):
        pref = "User always prefers " + ("a very specific detailed standing workflow rule " * 9)
        self.assertGreater(len(pref), 350)
        rep = self._std([pref])
        e = find(rep, "always prefers")
        self.assertEqual(e["kind"], "preference_fact")
        self.assertEqual(e["recommended_action"], "user_review")  # not rewrite_to_pointer

    def test_R13_content_dump_false_path_token_moves_to_note(self):
        dump = ("Reviewer packet flow: send each item to telegram for "
                "accept/reject/revise before any source edits, and keep the audit "
                "trail intact across the whole multi stage pipeline that spans "
                "several reviewers and many days of asynchronous back and forth work, "
                "with each decision logged and each revision tracked so the final "
                "promotion record reflects exactly which reviewer touched which item.")
        self.assertGreater(len(dump), 350)
        rep = self._std([dump])
        e = find(rep, "Reviewer packet flow")
        self.assertEqual(e["kind"], "content_dump")
        self.assertEqual(e["recommended_action"], "move_to_note")  # no real target

    def test_R14_mixed_polarity_agreeing_not_contradiction(self):
        rep = self._std([
            "POIWatcher execution is live but the bridge was briefly broken.",
            "POIWatcher execution enabled and running fine.",
        ])
        self.assertEqual(rep["contradiction_pairs"], [],
                         "agreeing entries with mixed polarity words must not conflict")


# --------------------------------------------------------------------------- #
# INTEG-9 / P3-3 — semantic near-duplicate detection                          #
# --------------------------------------------------------------------------- #
class TestSemanticDedup(unittest.TestCase):
    """Embedding-backed near-duplicate detection layered on the token audit."""

    def _entry(self, ref, kind, tokens, text):
        # minimal entry shape that find_semantic_duplicates reads
        return {"ref": ref, "store": "memory", "kind": kind,
                "_tokens": set(tokens), "text": text}

    def test_vec_cosine(self):
        self.assertAlmostEqual(ma._vec_cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(ma._vec_cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(ma._vec_cosine([1, 1], [1, 1]), 1.0)   # non-unit input normalises
        self.assertEqual(ma._vec_cosine([1, 0], []), 0.0)             # length mismatch
        self.assertEqual(ma._vec_cosine([0, 0], [1, 1]), 0.0)         # zero vector

    def test_finds_dups_including_beyond_token(self):
        # A/B: identical tokens (token-flagged). C/D: only ~0.33 token Jaccard (the token
        # pass at 0.45 MISSES them) but same embedding topic -> the semantic value-add.
        entries = [
            self._entry("memory#0", "fact", {"a", "b", "c", "d"}, "alpha topic about X"),
            self._entry("memory#1", "fact", {"a", "b", "c", "d"}, "alpha restated about X"),
            self._entry("memory#2", "fact", {"a", "b", "e", "f"}, "beta concerning Y"),
            self._entry("memory#3", "fact", {"a", "b", "g", "h"}, "beta about Y also"),
            self._entry("memory#4", "fact", {"z1", "z2"}, "gamma unrelated"),
        ]

        def stub(texts):
            return [([1.0, 0.0] if "X" in t else ([0.0, 1.0] if "Y" in t else [0.5, 0.5]))
                    for t in texts]

        res = ma.find_semantic_duplicates(entries, stub, prefilter_jaccard=0.30,
                                          near_cosine=0.85, token_near=0.45)
        self.assertTrue(res["available"])
        self.assertEqual(res["embedded_entries"], 4, "entry #4 has no lexical neighbour — not embedded")
        flagged = {(p["a"], p["b"]) for p in res["pairs"]}
        self.assertEqual(flagged, {("memory#0", "memory#1"), ("memory#2", "memory#3")})
        self.assertEqual(res["added_over_token"], 1, "C/D is the pair token Jaccard would miss")
        cd = next(p for p in res["pairs"] if p["a"] == "memory#2")
        self.assertFalse(cd["token_flagged"])
        ab = next(p for p in res["pairs"] if p["a"] == "memory#0")
        self.assertTrue(ab["token_flagged"])

    def test_none_when_embedder_unavailable(self):
        entries = [self._entry("memory#0", "fact", {"a", "b", "c", "d"}, "alpha X"),
                   self._entry("memory#1", "fact", {"a", "b", "c", "d"}, "alpha X too")]
        self.assertIsNone(ma.find_semantic_duplicates(entries, lambda texts: None),
                          "a None embedding result must propagate as 'fall back to token Jaccard'")

    def test_no_candidates_does_not_embed(self):
        entries = [self._entry("memory#0", "fact", {"a", "b"}, "one"),
                   self._entry("memory#1", "fact", {"c", "d"}, "two")]

        def boom(texts):
            raise AssertionError("must not embed when there are no lexical candidates")

        res = ma.find_semantic_duplicates(entries, boom, prefilter_jaccard=0.30)
        self.assertTrue(res["available"])
        self.assertEqual(res["checked_pairs"], 0)
        self.assertEqual(res["pairs"], [])

    def test_pointer_pairs_excluded(self):
        entries = [self._entry("memory#0", "pointer", {"a", "b", "c", "d"}, "↪ notes X"),
                   self._entry("memory#1", "pointer", {"a", "b", "c", "d"}, "↪ notes X")]

        def boom(texts):
            raise AssertionError("pointer/pointer pairs share a template — must be excluded")

        self.assertEqual(ma.find_semantic_duplicates(entries, boom)["checked_pairs"], 0)

    def test_run_audit_adds_field_and_unavailable_marker(self):
        import shutil
        tmp = tempfile.mkdtemp(prefix="sem_audit_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        mem = os.path.join(tmp, "memories", "MEMORY.md")
        usr = os.path.join(tmp, "memories", "USER.md")
        write_file(mem, [
            "Trading brain V3 uses the liquidity inducement origin candidate model now.",
            "The trading brain V3 currently relies on liquidity inducement origin candidates.",
            "Emeka prefers blunt ROI-focused correction over reassurance.",
        ])
        write_file(usr, ["Owner timezone is Ontario Canada."])
        # available path: a real embedder stub -> report carries a populated field
        stub = (lambda texts: [[1.0, 0.0] if "trading brain" in t.lower() else [0.0, 1.0]
                               for t in texts])
        rep = ma.run_audit(mem, usr, tmp, today=TODAY, user_home=tmp, semantic_embed=stub)
        self.assertIn("semantic_duplicates", rep)
        self.assertTrue(rep["semantic_duplicates"]["available"])
        # unavailable path: embedder returns None -> explicit fallback marker, audit still works
        rep2 = ma.run_audit(mem, usr, tmp, today=TODAY, user_home=tmp,
                            semantic_embed=lambda texts: None)
        self.assertIn("semantic_duplicates", rep2)
        self.assertFalse(rep2["semantic_duplicates"]["available"])
        # token duplicate detection is unaffected either way
        self.assertEqual(rep["duplicate_pairs"], rep2["duplicate_pairs"])
        # and with NO semantic_embed the field is absent (schema unchanged by default)
        rep3 = ma.run_audit(mem, usr, tmp, today=TODAY, user_home=tmp)
        self.assertNotIn("semantic_duplicates", rep3)


class TestSemanticDaemonEmbed(unittest.TestCase):
    """The semantic daemon's `embed` mode + its pure-stdlib client (no chromadb needed)."""

    def setUp(self):
        import semantic_query as SQ
        self.SQ = SQ
        self._saved_model = SQ._model

    def tearDown(self):
        self.SQ._model = self._saved_model

    def test_embed_texts_returns_none_without_daemon(self):
        SQ = self.SQ
        self.assertIsNone(SQ.embed_texts(["hello"], sock_path="/tmp/no_such_onboard.sock", timeout=1.0))
        self.assertEqual(SQ.embed_texts([], sock_path="/tmp/whatever.sock"), [],
                         "empty input short-circuits to [] without touching the socket")

    def test_handle_request_embed_branch(self):
        SQ = self.SQ

        class _Arr(list):
            def tolist(self):
                return [list(v) for v in self]

        class FakeModel:
            def encode(self, texts, show_progress_bar=False, normalize_embeddings=True):
                return _Arr([[1.0, 0.0, 0.0] for _ in texts])

        SQ._model = FakeModel()   # _get_model() returns this; no sentence-transformers import
        resp = SQ._handle_request({"mode": "embed", "texts": ["a", "b"]})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["count"], 2)
        self.assertEqual(resp["dim"], 3)
        self.assertEqual(len(resp["embeddings"]), 2)
        # empty texts: ok with no vectors, model untouched
        self.assertEqual(SQ._handle_request({"mode": "embed", "texts": []})["embeddings"], [])
        # invalid texts: rejected, not crashed
        self.assertFalse(SQ._handle_request({"mode": "embed", "texts": "notalist"})["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
