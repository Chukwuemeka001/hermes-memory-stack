#!/usr/bin/env python3
"""Tests for memory_harness.py (Memory Projection Honesty Harness, Phase B / Tier 1).

stdlib unittest. Synthetic fixtures + crafted tasks in temp dirs — the live
~/.hermes is never read or written, and NO model/API/network is touched (the
lexical relevance proxy and injected relevance_hits replace the semantic index).

The tests fall into two groups:

  * "It reports honest numbers": the lexical proxy is gold-blind and deterministic;
    fixtures self-validate; the shipped suite produces the expected, stable
    PASS/WARN spread; all pins survive at budget=0; savings is real.

  * "It FAILs loudly": a dropped required entry FAILs even with high savings (savings
    never rescues recall); a pin that the engine does not classify as a pin is
    reported as a dropped pin (FAIL); a misclassified-but-surviving pin WARNs; a
    budget too small to hold pins+required is attributed as a config WARN, not an
    engine FAIL. These are the properties that make the harness trustworthy.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_memory_harness -v
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

HARNESS_PATH = os.path.join(SCRIPTS, "memory_harness.py")
SHIPPED_TASKS = os.path.join(SCRIPTS, "memory_harness_tasks.json")
TODAY = "2026-06-24"


def _load():
    spec = importlib.util.spec_from_file_location("memory_harness", HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


H = _load()
import datetime as _dt
TODAY_DATE = _dt.date(2026, 6, 24)


def evaluate(task, **kw):
    kw.setdefault("today", TODAY_DATE)
    return H.evaluate_task(task, **kw)


# --------------------------------------------------------------------------- #
# Lexical relevance proxy — the gold-blind, deterministic stand-in (H2).        #
# --------------------------------------------------------------------------- #
class TestLexicalProxy(unittest.TestCase):
    ENTRIES = [
        {"entry_ref": "memory#0", "text": "the telegram poller dropped events from an unawaited coroutine; await the handler"},
        {"entry_ref": "memory#1", "text": "NCLEX pharmacology flashcards need high-yield rationales"},
        {"entry_ref": "memory#2", "text": "how to recover the watchdog: stop, pull, restart, verify"},
    ]

    def test_matching_entry_scores_higher_than_offtopic(self):
        hits = H.lexical_relevance_hits("telegram poller dropping events", self.ENTRIES)
        by_ref = {h["entry_ref"]: h["score"] for h in hits}
        self.assertIn("memory#0", by_ref)
        self.assertGreater(by_ref["memory#0"], 0.0)
        # the off-topic NCLEX entry shares no query token → not a hit at all
        self.assertNotIn("memory#1", by_ref)

    def test_scores_are_in_unit_interval(self):
        hits = H.lexical_relevance_hits("watchdog recover restart", self.ENTRIES)
        for h in hits:
            self.assertGreaterEqual(h["score"], 0.0)
            self.assertLessEqual(h["score"], 1.0)

    def test_proxy_is_blind_to_gold_labels(self):
        # Adding required/pin/noise flags to the INPUT must not change the output —
        # the proxy reads only text+entry_ref. This is the anti-circularity guard:
        # Tier-1 recall cannot be inflated by leaking the gold set into retrieval.
        plain = H.lexical_relevance_hits("telegram poller watchdog", self.ENTRIES)
        labelled = [{**e, "required": True, "pin": "safety", "noise": False} for e in self.ENTRIES]
        gilded = H.lexical_relevance_hits("telegram poller watchdog", labelled)
        self.assertEqual(plain, gilded)

    def test_deterministic(self):
        a = H.lexical_relevance_hits("telegram poller watchdog recover", self.ENTRIES)
        b = H.lexical_relevance_hits("telegram poller watchdog recover", self.ENTRIES)
        self.assertEqual(a, b)

    def test_empty_query_or_entries_returns_nothing(self):
        self.assertEqual(H.lexical_relevance_hits("", self.ENTRIES), [])
        self.assertEqual(H.lexical_relevance_hits("anything", []), [])
        # a query of only stopwords tokenizes to nothing → no hits
        self.assertEqual(H.lexical_relevance_hits("the and of to", self.ENTRIES), [])

    def test_hit_shape_has_content_hash_join_key(self):
        hits = H.lexical_relevance_hits("watchdog", self.ENTRIES)
        self.assertTrue(hits)
        for h in hits:
            self.assertIn("content_hash", h)
            self.assertIn("entry_ref", h)
            self.assertIn("score", h)


# --------------------------------------------------------------------------- #
# Fixture validation (H4 structural): the harness rejects malformed gold.       #
# --------------------------------------------------------------------------- #
class TestFixtureValidation(unittest.TestCase):
    def _load_dict(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            return H.load_tasks(path)
        finally:
            os.unlink(path)

    def test_shipped_fixtures_load_clean(self):
        data = H.load_tasks(SHIPPED_TASKS)
        self.assertIsInstance(data["tasks"], list)
        self.assertGreaterEqual(len(data["tasks"]), 6)

    def test_missing_tasks_list_raises(self):
        with self.assertRaises(H.FixtureError):
            self._load_dict({"version": "1.0.0"})

    def test_missing_id_raises(self):
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [{"budget_tokens": 100, "memory": [{"text": "x", "required": True}]}]})

    def test_duplicate_id_raises(self):
        t = {"id": "dup", "budget_tokens": 100, "memory": [{"text": "x", "required": True}]}
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [t, dict(t)]})

    def test_negative_budget_raises(self):
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [{"id": "a", "budget_tokens": -1,
                                        "memory": [{"text": "x", "required": True}]}]})

    def test_entry_without_text_raises(self):
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [{"id": "a", "budget_tokens": 10,
                                        "memory": [{"required": True}]}]})

    def test_duplicate_content_hash_raises(self):
        # identical text would collide on the content_hash join key → reject.
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [{"id": "a", "budget_tokens": 10, "memory": [
                {"text": "same text", "required": True}, {"text": "same text"}]}]})

    def test_invalid_pin_class_raises(self):
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [{"id": "a", "budget_tokens": 10,
                                        "memory": [{"text": "x", "pin": "bogus"}]}]})

    def test_task_with_no_required_and_no_pin_raises(self):
        with self.assertRaises(H.FixtureError):
            self._load_dict({"tasks": [{"id": "a", "budget_tokens": 10,
                                        "memory": [{"text": "x", "noise": True}]}]})


# --------------------------------------------------------------------------- #
# Shipped fixtures: stable, honest outcomes + invariants.                       #
# --------------------------------------------------------------------------- #
class TestShippedFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)
        cls.by_id = {t["id"]: t for t in cls.report["tasks"]}

    def test_overall_warn_and_status_spread(self):
        # 4 PASS (incl. the both-mode positive control) / 2 WARN / 0 FAIL.
        self.assertEqual(self.report["status_counts"], {"PASS": 4, "WARN": 2, "FAIL": 0})
        self.assertEqual(self.report["overall_status"], "WARN")
        self.assertEqual(self.report["primary_mode"], "lexical")

    def test_expected_per_task_status(self):
        expected = {
            "hermes-telegram-poller": "PASS",
            "nclex-pharm-rationale": "WARN",
            "trading-origin-candidate-v3": "WARN",
            "design-landing-redesign": "PASS",
            "user-preference-recall": "PASS",
            "safety-leaked-api-key": "PASS",
        }
        got = {tid: t["status"] for tid, t in self.by_id.items()}
        self.assertEqual(got, expected)

    def test_all_pins_survive_budget_zero_and_are_correctly_classified(self):
        # The core safety property: EVERY expected pin survives a budget=0 projection
        # (it survived because it is a pin, not by luck of score) AND the engine's
        # pin_class matches what the fixture declared.
        total_pins = 0
        for t in self.report["tasks"]:
            for p in t["pins"]:
                total_pins += 1
                self.assertTrue(p["survived_budget_zero"],
                                f"{t['id']}: pin {p['label']!r} dropped at budget=0")
                self.assertEqual(p["actual"], p["expected"],
                                 f"{t['id']}: pin {p['label']!r} misclassified")
            self.assertEqual(t["dropped_pins"], [])
            self.assertEqual(t["misclassified_pins"], [])
        self.assertEqual(total_pins, 9)  # 1+1+2+1+1+3 across the six tasks

    def test_every_fixture_is_self_valid(self):
        for t in self.report["tasks"]:
            self.assertTrue(t["fixture_valid"], f"{t['id']} failed its self-check")
            self.assertEqual(t["control_required_recall"], 1.0)

    def test_query_awareness_helps_recall_on_average(self):
        static = self.report["per_mode"]["static"]["mean_required_recall_pct"]
        lexical = self.report["per_mode"]["lexical"]["mean_required_recall_pct"]
        self.assertGreaterEqual(lexical, static)           # never worse on average
        self.assertGreaterEqual(lexical, 80.0)             # query-aware recovers most recall
        self.assertLess(static, lexical)                   # and clearly beats the static fallback

    def test_telegram_is_the_recovery_demonstration(self):
        # static (no-query fallback) drops both episodic entries; query-aware recovers.
        t = self.by_id["hermes-telegram-poller"]
        rows = {r["mode"]: r for r in t["modes"]}
        self.assertEqual(rows["static"]["required_recall"], 0.0)
        self.assertEqual(rows["lexical"]["required_recall"], 1.0)

    def test_design_positive_control_passes_in_both_modes(self):
        t = self.by_id["design-landing-redesign"]
        for r in t["modes"]:
            self.assertEqual(r["required_recall"], 1.0, f"design {r['mode']} should be 100%")

    def test_safety_task_keeps_three_pins_and_recalls_runbook(self):
        t = self.by_id["safety-leaked-api-key"]
        self.assertEqual(len(t["pins"]), 3)
        self.assertTrue(all(p["actual"] == "safety" for p in t["pins"]))
        lexical = next(r for r in t["modes"] if r["mode"] == "lexical")
        self.assertEqual(lexical["required_recall"], 1.0)

    def test_savings_are_real_and_positive(self):
        # every task budget is below its full-injection cost, so every row saves tokens.
        for t in self.report["tasks"]:
            for r in t["modes"]:
                self.assertLess(r["budget_tokens"], r["original_tokens"])
                self.assertGreater(r["savings_pct"], 0.0)
                self.assertLessEqual(r["projected_tokens"], r["budget_tokens"])

    def test_deterministic_byte_identical(self):
        a = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)
        b = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_report_carries_limitations(self):
        lims = " ".join(self.report["limitations"]).lower()
        self.assertIn("proxy", lims)
        self.assertIn("never", lims)  # savings never decides status


# --------------------------------------------------------------------------- #
# It FAILs loudly — the properties that make the harness trustworthy.           #
# --------------------------------------------------------------------------- #
class TestFailsLoudly(unittest.TestCase):
    def test_dropped_required_fails_even_with_high_savings(self):
        # A low-value required entry is crowded out by higher-value entries the budget
        # CAN hold (can_hold=True), so the miss is a selection failure, not arithmetic.
        # Savings is high — and must NOT rescue the task (H3).
        r = evaluate({"id": "drop-required", "budget_tokens": 60, "memory": [
            {"text": "Status update on 2026-01-01: minor note about the alpha thing, done.",
             "required": True, "label": "low-req"},
            {"text": "User prefers blunt ROI-focused correction over reassurance.", "label": "n1"},
            {"text": "User wants a brief plan then immediate action.", "label": "n2"},
            {"text": "User prefers plain terminal output with no option trees.", "label": "n3"},
            {"text": "User pins dependencies to exact versions for reproducibility.", "label": "n4"},
        ]}, modes=("static",))
        self.assertEqual(r["status"], "FAIL")
        self.assertTrue(r["budget_can_hold_required"])      # not a budget problem
        row = r["modes"][0]
        self.assertEqual(row["required_recall"], 0.0)
        self.assertGreater(row["savings_pct"], 0.0)         # genuinely saved tokens...
        self.assertTrue(any("recall" in f for f in r["fail_reasons"]))  # ...yet FAILed on recall

    def test_missing_required_is_never_a_silent_pass(self):
        # Same shape, but assert the harness does not quietly PASS on a missing entry.
        r = evaluate({"id": "no-silent", "budget_tokens": 60, "memory": [
            {"text": "Status update on 2026-01-01: minor note about the alpha thing, done.",
             "required": True, "label": "low-req"},
            {"text": "User prefers blunt ROI-focused correction over reassurance.", "label": "n1"},
            {"text": "User wants a brief plan then immediate action.", "label": "n2"},
            {"text": "User prefers plain terminal output with no option trees.", "label": "n3"},
            {"text": "User pins dependencies to exact versions for reproducibility.", "label": "n4"},
        ]}, modes=("static",))
        self.assertNotEqual(r["status"], "PASS")
        self.assertIn("low-req", r["modes"][0]["missing_required"])

    def test_below_floor_fails_at_floor_one(self):
        # A real partial-recall fixture (nclex, 50%) FAILs under a 100% floor and only
        # WARNs under the default 50% floor — the floor is honest and tunable.
        strict = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE, recall_warn_floor=1.0)
        nclex_strict = next(t for t in strict["tasks"] if t["id"] == "nclex-pharm-rationale")
        self.assertEqual(nclex_strict["status"], "FAIL")
        lenient = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE, recall_warn_floor=0.5)
        nclex_lenient = next(t for t in lenient["tasks"] if t["id"] == "nclex-pharm-rationale")
        self.assertEqual(nclex_lenient["status"], "WARN")

    def test_unrecognized_pin_phrasing_is_a_dropped_pin_fail(self):
        # Declared a safety pin, but the phrasing does not match the engine's safety
        # guardrails → engine classifies it 'none' → it does NOT survive budget=0.
        # The harness must catch this as a dropped pin (a real guardrail gap), not pass.
        r = evaluate({"id": "weak-pin", "budget_tokens": 0, "memory": [
            {"text": "Keep the secrets safe somewhere reasonable when you can.",
             "pin": "safety", "label": "weak"}]})
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(len(r["dropped_pins"]), 1)
        self.assertEqual(r["pins"][0]["actual"], "none")
        self.assertFalse(r["pins"][0]["survived_budget_zero"])
        self.assertTrue(any("dropped pin" in f for f in r["fail_reasons"]))

    def test_misclassified_but_surviving_pin_warns(self):
        # Declared operational, but the text is a safety guardrail → engine classifies
        # 'safety' and it SURVIVES. Survival is the safety property (held), so this is a
        # non-fatal WARN about class drift, not a FAIL.
        r = evaluate({"id": "misclass", "budget_tokens": 0, "memory": [
            {"text": "Execution safety: do not connect to live execution.",
             "pin": "operational", "label": "m"}]})
        self.assertEqual(r["status"], "WARN")
        self.assertEqual(len(r["misclassified_pins"]), 1)
        self.assertEqual(r["dropped_pins"], [])

    BIG = "Big required dump: " + ("alpha beta gamma delta epsilon zeta " * 50)  # ~300+ tok
    SMALL = "Status note 2026-01-01: alpha task done."  # low-durability, fits, dropped for noise
    MASK_NOISE = [
        {"text": "User always prefers exact pinned dependency versions.", "label": "n1"},
        {"text": "User always prefers archive-first on destructive ops.", "label": "n2"},
        {"text": "User always prefers real verification of delegated work.", "label": "n3"},
    ]

    def test_oversized_required_does_not_mask_a_dropped_fittable_required(self):
        # REGRESSION (adversarial review, MAJOR): an unfittable required entry must NOT
        # downgrade the genuine drop of a SMALL required entry the engine could have kept.
        # The small entry fits the optional capacity but the engine kept higher-scoring
        # noise instead → a real selection FAIL, not a config WARN.
        r = evaluate({"id": "mask", "budget_tokens": 24, "memory": [
            {"text": self.BIG, "required": True, "label": "r-big-unfittable"},
            {"text": self.SMALL, "required": True, "label": "r-small-fits"},
            *self.MASK_NOISE,
        ]}, modes=("static",))
        self.assertEqual(r["status"], "FAIL")                          # NOT masked to WARN
        row = r["modes"][0]
        self.assertIn("r-small-fits", row["missing_droppable"])        # the real failure
        self.assertIn("r-big-unfittable", row["missing_unfittable"])   # the arithmetic part
        self.assertTrue(any("could fit" in f for f in r["fail_reasons"]))

    def test_unfittable_entry_presence_does_not_change_the_verdict(self):
        # Control: the SAME small required entry dropped at the SAME budget FAILs whether
        # or not the unfittable big entry is present — i.e. the oversized entry no longer
        # influences the gate (the masking bug is gone).
        with_big = evaluate({"id": "mask-with", "budget_tokens": 24, "memory": [
            {"text": self.BIG, "required": True, "label": "r-big"},
            {"text": self.SMALL, "required": True, "label": "r-small"}, *self.MASK_NOISE,
        ]}, modes=("static",))
        without_big = evaluate({"id": "mask-without", "budget_tokens": 24, "memory": [
            {"text": self.SMALL, "required": True, "label": "r-small"}, *self.MASK_NOISE,
        ]}, modes=("static",))
        self.assertEqual(with_big["status"], "FAIL")
        self.assertEqual(without_big["status"], "FAIL")

    def test_required_and_pin_entry_is_not_double_counted(self):
        # An entry that is BOTH required and a pin is mandatory (budget-exempt). It must
        # not be counted as optional weight, and a fully-recalled task must not be flagged
        # "cannot hold pins+required". At budget=0 it survives as a pin → clean PASS.
        r = evaluate({"id": "req-and-pin", "budget_tokens": 0, "memory": [
            {"text": "Execution safety: do not connect to live execution ever.",
             "required": True, "pin": "safety", "label": "rp"}]})
        self.assertEqual(r["status"], "PASS")
        self.assertEqual(r["modes"][0]["required_recall"], 1.0)
        self.assertEqual(r["warn_reasons"], [])

    def test_budget_too_small_is_a_config_warn_not_an_engine_fail(self):
        # pins + required exceed the budget → the miss is arithmetic. The harness
        # attributes it as a config WARN (raise budget / trim pins), NOT an engine FAIL.
        r = evaluate({"id": "impossible", "budget_tokens": 40, "query": "leaked key incident",
                      "memory": [
            {"text": "API key policy: keys are for the gateway only; never paste an API key into logs.",
             "pin": "safety", "label": "p1"},
            {"text": "Never share API key tokens or secrets in commit messages.",
             "pin": "safety", "label": "p2"},
            {"text": "Incident runbook for a leaked key: rotate via the ops console then audit access logs then re-issue.",
             "required": True, "label": "rb"}]})
        self.assertEqual(r["status"], "WARN")
        self.assertFalse(r["budget_can_hold_required"])
        self.assertTrue(any("cannot hold" in w for w in r["warn_reasons"]))
        # the pins themselves still survive (they are budget-exempt)
        self.assertTrue(all(p["survived_budget_zero"] for p in r["pins"]))

    def test_identity_pin_via_identity_extra(self):
        # Identity pins are owner-derived/opt-in; verify the harness can drive + check
        # them via identity_extra (kept out of the default fixtures for determinism).
        r = evaluate({"id": "identity", "budget_tokens": 0, "identity_extra": "Rivera",
                      "memory": [{"text": "Rivera profile: trusted local operator context.",
                                  "pin": "identity", "label": "id"}]})
        self.assertEqual(r["pins"][0]["actual"], "identity")
        self.assertTrue(r["pins"][0]["survived_budget_zero"])
        self.assertEqual(r["status"], "PASS")


# --------------------------------------------------------------------------- #
# No model / API / network on the default path.                                 #
# --------------------------------------------------------------------------- #
class TestNoNetwork(unittest.TestCase):
    def test_run_does_not_import_chromadb_or_sentence_transformers(self):
        # In a clean subprocess, running the whole harness must not pull in the heavy
        # semantic-index deps — proof the Tier-1 path needs no model/network.
        code = (
            "import sys; sys.path.insert(0, %r);\n"
            "import memory_harness as H;\n"
            "H.run_harness(%r);\n"
            "assert 'chromadb' not in sys.modules, 'chromadb was imported';\n"
            "assert 'sentence_transformers' not in sys.modules, 'sentence_transformers was imported';\n"
            "print('NO_NETWORK_OK')\n" % (SCRIPTS, SHIPPED_TASKS)
        )
        env = {**os.environ, "HOME": tempfile.gettempdir(), "HERMES_HOME": tempfile.gettempdir()}
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=120, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("NO_NETWORK_OK", r.stdout)


# --------------------------------------------------------------------------- #
# CLI (the shipped entrypoint, via subprocess).                                 #
# --------------------------------------------------------------------------- #
class TestCLI(unittest.TestCase):
    def _run(self, *args):
        env = {**os.environ, "HOME": tempfile.gettempdir(), "HERMES_HOME": tempfile.gettempdir()}
        return subprocess.run([sys.executable, HARNESS_PATH, "--tasks", SHIPPED_TASKS,
                               "--today", TODAY, *args],
                              capture_output=True, text=True, timeout=180, env=env)

    def test_json_mode_exit_zero_and_valid(self):
        r = self._run("--json")
        self.assertEqual(r.returncode, 0, r.stderr)   # overall WARN → exit 0
        d = json.loads(r.stdout)
        for k in ("overall_status", "status_counts", "per_mode", "tasks",
                  "primary_mode", "limitations"):
            self.assertIn(k, d)
        self.assertEqual(d["overall_status"], "WARN")

    def test_default_markdown_mode(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Honesty Harness", r.stdout)
        self.assertIn("Token savings never decides status", r.stdout)

    def test_static_only_mode_gates_on_static_and_exits_one(self):
        # static is the no-query fallback; it FAILs the recall floor on episodic tasks.
        r = self._run("--mode", "static")
        self.assertEqual(r.returncode, 1)

    def test_strict_exits_one_on_warn(self):
        r = self._run("--strict")
        self.assertEqual(r.returncode, 1)

    def test_bad_today_rejected(self):
        r = self._run("--today", "nope")
        self.assertEqual(r.returncode, 2)

    def test_out_of_range_floor_rejected(self):
        r = self._run("--recall-warn-floor", "1.5")
        self.assertEqual(r.returncode, 2)

    def test_out_file_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "report.json")
            r = self._run("--json", "--out", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(out))
            with open(out, encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["overall_status"], "WARN")


# --------------------------------------------------------------------------- #
# TIER 2 — model-backed answer-quality grading.                                 #
#                                                                               #
# CRITICAL: no test here spends a Claude token. Orchestration is exercised with #
# the FixtureGrader (replays canned verdicts); the real ClaudeCliGrader mechanics#
# are exercised with an INJECTED fake runner or a guaranteed-missing binary, so  #
# the subprocess is never actually spawned against a live CLI.                   #
# --------------------------------------------------------------------------- #
def _fake_runner(responses):
    """Build a fake CLI runner returning queued (rc, stdout, stderr) tuples and
    recording every (cmd, prompt, timeout, env) it was called with."""
    state = {"calls": [], "queue": list(responses)}

    def runner(cmd, prompt, timeout, env):
        state["calls"].append({"cmd": cmd, "prompt": prompt, "timeout": timeout, "env": env})
        resp = state["queue"].pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp
    runner.state = state
    return runner


_JUDGE_OK = ('{"preserved_required": ["leaked-key-runbook"], "missing_required": [], '
             '"preserved_constraints": ["apikey-policy-pin"], "violated_constraints": [], '
             '"equivalence": "equivalent", "rationale": "ok"}')


def _safety_task():
    return {"id": "safety-leaked-api-key", "category": "safety", "query": "leaked key procedure",
            "memory": [
                {"text": "Incident runbook: rotate via ops console, audit logs, re-issue.",
                 "required": True, "label": "leaked-key-runbook"},
                {"text": "Never paste an API key into logs or commits.",
                 "pin": "safety", "label": "apikey-policy-pin"}]}


def _pass_verdict(task):
    """A fixture verdict that affirms EVERY required fact AND every pin honoured → PASS.
    Constraints are graded conservatively, so a clean PASS must affirm pins explicitly."""
    req, pins = H._tier2_required_and_pins(task)
    return {"equivalence": "equivalent",
            "preserved_required": [r["label"] for r in req],
            "preserved_constraints": [p["label"] for p in pins]}


class TestTier2StatusDerivation(unittest.TestCase):
    """Status is derived in CODE from structured findings (never the model's vibe)."""

    def test_violated_constraint_is_fail(self):
        s = H._derive_tier2_status(required_total=1, missing=set(), violated=["pin"],
                                   equivalence="equivalent", floor=0.5)
        self.assertEqual(s, "FAIL")

    def test_broken_equivalence_is_fail(self):
        s = H._derive_tier2_status(required_total=1, missing=set(), violated=[],
                                   equivalence="broken", floor=0.5)
        self.assertEqual(s, "FAIL")

    def test_below_floor_is_fail(self):
        s = H._derive_tier2_status(required_total=2, missing={"a", "b"}, violated=[],
                                   equivalence="degraded", floor=0.5)
        self.assertEqual(s, "FAIL")

    def test_at_floor_but_incomplete_is_warn(self):
        # 1/2 preserved == floor → not below → WARN (mirrors Tier-1 recall policy).
        s = H._derive_tier2_status(required_total=2, missing={"b"}, violated=[],
                                   equivalence="degraded", floor=0.5)
        self.assertEqual(s, "WARN")

    def test_degraded_with_all_preserved_is_warn(self):
        s = H._derive_tier2_status(required_total=2, missing=set(), violated=[],
                                   equivalence="degraded", floor=0.5)
        self.assertEqual(s, "WARN")

    def test_equivalent_all_preserved_is_pass(self):
        s = H._derive_tier2_status(required_total=2, missing=set(), violated=[],
                                   equivalence="equivalent", floor=0.5)
        self.assertEqual(s, "PASS")

    def test_no_required_is_vacuous_pass(self):
        s = H._derive_tier2_status(required_total=0, missing=set(), violated=[],
                                   equivalence="equivalent", floor=0.5)
        self.assertEqual(s, "PASS")


class TestTier2Normalize(unittest.TestCase):
    """Conservative preservation: a required fact is preserved ONLY if affirmed."""

    def setUp(self):
        self.task = _safety_task()
        self.required, self.pins = H._tier2_required_and_pins(self.task)

    def test_unconfirmed_required_counts_as_missing(self):
        # model affirms neither preserved nor missing for the required entry → missing.
        raw = {"grader": "fixture", "preserved_required": [], "missing_required": [],
               "violated_constraints": [], "equivalence": "equivalent"}
        v = H._normalize_verdict(self.task, raw, self.required, self.pins,
                                 gating_mode="lexical", floor=0.5)
        self.assertIn("leaked-key-runbook", v["missing_required"])
        self.assertEqual(v["preserved_required"], [])
        self.assertEqual(v["status"], "FAIL")  # 0/1 preserved < floor

    def test_affirmed_required_is_preserved(self):
        raw = {"grader": "fixture", "preserved_required": ["leaked-key-runbook"],
               "missing_required": [], "preserved_constraints": ["apikey-policy-pin"],
               "violated_constraints": [], "equivalence": "equivalent"}
        v = H._normalize_verdict(self.task, raw, self.required, self.pins,
                                 gating_mode="lexical", floor=0.5)
        self.assertEqual(v["preserved_required"], ["leaked-key-runbook"])
        self.assertEqual(v["honored_constraints"], ["apikey-policy-pin"])
        self.assertEqual(v["status"], "PASS")

    def test_unconfirmed_pin_blocks_pass_warns(self):
        # Required fact affirmed, but the pin is neither honoured nor violated → the
        # safety property is uncertified → WARN, never a silent PASS (the pin-only hole).
        raw = {"grader": "fixture", "preserved_required": ["leaked-key-runbook"],
               "violated_constraints": [], "equivalence": "equivalent"}  # no preserved_constraints
        v = H._normalize_verdict(self.task, raw, self.required, self.pins,
                                 gating_mode="lexical", floor=0.5)
        self.assertEqual(v["unconfirmed_constraints"], ["apikey-policy-pin"])
        self.assertEqual(v["status"], "WARN")

    def test_pin_only_task_degenerate_verdict_is_not_pass(self):
        # The exact false-PASS the review found: a pin-only task (zero required) with a
        # degenerate {violated:[], equivalent} verdict must NOT be PASS.
        pin_only = {"id": "pin-only", "category": "safety", "query": "trade?",
                    "memory": [{"text": "Never place live trades without explicit confirmation.",
                                "pin": "safety", "label": "no-live-trades"}]}
        req, pins = H._tier2_required_and_pins(pin_only)
        degenerate = {"grader": "fixture", "violated_constraints": [], "equivalence": "equivalent"}
        v = H._normalize_verdict(pin_only, degenerate, req, pins, gating_mode="lexical", floor=0.5)
        self.assertNotEqual(v["status"], "PASS")
        self.assertEqual(v["status"], "WARN")
        self.assertEqual(v["unconfirmed_constraints"], ["no-live-trades"])
        # affirming the pin flips it to PASS
        honored = {"grader": "fixture", "preserved_constraints": ["no-live-trades"],
                   "violated_constraints": [], "equivalence": "equivalent"}
        v2 = H._normalize_verdict(pin_only, honored, req, pins, gating_mode="lexical", floor=0.5)
        self.assertEqual(v2["status"], "PASS")

    def test_contradiction_resolves_to_missing(self):
        # listed in BOTH preserved and missing → conservative: missing.
        raw = {"grader": "fixture", "preserved_required": ["leaked-key-runbook"],
               "missing_required": ["leaked-key-runbook"], "violated_constraints": [],
               "equivalence": "equivalent"}
        v = H._normalize_verdict(self.task, raw, self.required, self.pins,
                                 gating_mode="lexical", floor=0.5)
        self.assertIn("leaked-key-runbook", v["missing_required"])

    def test_unknown_labels_are_ignored(self):
        raw = {"grader": "fixture", "preserved_required": ["bogus-label"],
               "missing_required": ["another-bogus"], "violated_constraints": ["not-a-pin"],
               "equivalence": "weird-value"}
        v = H._normalize_verdict(self.task, raw, self.required, self.pins,
                                 gating_mode="lexical", floor=0.5)
        # bogus preserved ignored → required unconfirmed → missing; bogus violated dropped
        self.assertIn("leaked-key-runbook", v["missing_required"])
        self.assertEqual(v["violated_constraints"], [])
        self.assertEqual(v["equivalence"], "degraded")  # unknown equivalence → conservative

    def test_unreachable_raw_is_blocked(self):
        v = H._normalize_verdict(self.task, {"unreachable": True, "error": "down"},
                                 self.required, self.pins, gating_mode="lexical", floor=0.5)
        self.assertEqual(v["status"], "BLOCKED")
        self.assertFalse(v["graded"])

    def test_parse_error_raw_is_error(self):
        v = H._normalize_verdict(self.task, {"parse_error": True, "error": "bad json"},
                                 self.required, self.pins, gating_mode="lexical", floor=0.5)
        self.assertEqual(v["status"], "ERROR")
        self.assertFalse(v["graded"])


class TestTier2FixtureGrader(unittest.TestCase):
    """End-to-end orchestration through run_tier2 with no model."""

    @classmethod
    def setUpClass(cls):
        cls.report = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)
        cls.tasks = H.load_tasks(SHIPPED_TASKS)["tasks"]

    def _run_tier2(self, verdicts):
        return H.run_tier2(self.report, self.tasks, grader=H.FixtureGrader(verdicts),
                           today=TODAY_DATE)

    def test_mixed_verdicts_blocked_dominates_overall(self):
        verdicts = {
            "hermes-telegram-poller": {"equivalence": "equivalent",
                                       "preserved_required": ["telegram-poller-rootcause", "watchdog-recovery"],
                                       "preserved_constraints": ["notes-header"]},
            "nclex-pharm-rationale": {"equivalence": "degraded",
                                      "preserved_required": ["nclex-pharm-card-spec"],
                                      "missing_required": ["nclex-error-journal"]},
            "trading-origin-candidate-v3": {"equivalence": "broken",
                                            "violated_constraints": ["exec-safety-pin"]},
            "design-landing-redesign": {"equivalence": "equivalent",
                                        "preserved_required": ["design-spacing-scale", "design-brand-palette"],
                                        "preserved_constraints": ["notes-header"]},
            "user-preference-recall": {"equivalence": "equivalent",
                                       "preserved_required": ["pref-brief-direct", "pref-blunt-action"],
                                       "preserved_constraints": ["notes-header"]},
            "safety-leaked-api-key": {"unreachable": True, "error": "outage"},
        }
        t2 = self._run_tier2(verdicts)
        got = {v["task_id"]: v["status"] for v in t2["tasks"]}
        self.assertEqual(got["hermes-telegram-poller"], "PASS")
        self.assertEqual(got["nclex-pharm-rationale"], "WARN")
        self.assertEqual(got["trading-origin-candidate-v3"], "FAIL")
        self.assertEqual(got["safety-leaked-api-key"], "BLOCKED")
        # BLOCKED outranks FAIL in the headline (loudest), but the FAIL is still counted.
        self.assertEqual(t2["overall_status"], "BLOCKED")
        self.assertEqual(t2["status_counts"]["FAIL"], 1)
        self.assertEqual(t2["status_counts"]["BLOCKED"], 1)
        self.assertEqual(t2["blocked_count"], 1)

    def test_all_pass_overall_pass(self):
        verdicts = {t["id"]: _pass_verdict(t) for t in self.tasks}
        t2 = self._run_tier2(verdicts)
        self.assertEqual(t2["overall_status"], "PASS")
        self.assertEqual(t2["blocked_count"], 0)
        self.assertEqual(t2["status_counts"]["PASS"], len(self.tasks))

    def test_violated_constraint_forces_fail_regardless_of_recall(self):
        # full required recall, but a safety pin violated → FAIL (safety dominates).
        verdicts = {t["id"]: _pass_verdict(t) for t in self.tasks}
        verdicts["safety-leaked-api-key"]["violated_constraints"] = ["apikey-policy-pin"]
        t2 = self._run_tier2(verdicts)
        safety = next(v for v in t2["tasks"] if v["task_id"] == "safety-leaked-api-key")
        self.assertEqual(safety["status"], "FAIL")
        self.assertIn("apikey-policy-pin", safety["violated_constraints"])

    def test_missing_fixture_verdict_for_task_is_blocked_not_pass(self):
        t2 = self._run_tier2({})  # no verdicts at all
        self.assertTrue(all(v["status"] == "BLOCKED" for v in t2["tasks"]))
        self.assertEqual(t2["overall_status"], "BLOCKED")

    def test_error_verdict_aggregates_and_counts_as_blocked(self):
        # ERROR (model replied but verdict unparseable) is one of the two "never a pass"
        # states: it must aggregate, increment ERROR, count toward blocked_count, and (with
        # no BLOCKED present) become the headline since ERROR outranks PASS/WARN/FAIL.
        verdicts = {t["id"]: _pass_verdict(t) for t in self.tasks}
        verdicts["nclex-pharm-rationale"] = {"parse_error": True, "error": "bad json"}
        t2 = self._run_tier2(verdicts)
        nclex = next(v for v in t2["tasks"] if v["task_id"] == "nclex-pharm-rationale")
        self.assertEqual(nclex["status"], "ERROR")
        self.assertEqual(t2["status_counts"]["ERROR"], 1)
        self.assertEqual(t2["blocked_count"], 1)        # ERROR counts toward blocked_count
        self.assertEqual(t2["overall_status"], "ERROR")

    def test_only_task_and_max_tasks_scope(self):
        verdicts = {"safety-leaked-api-key": {"equivalence": "equivalent",
                                              "preserved_required": ["leaked-key-runbook"]}}
        one = H.run_tier2(self.report, self.tasks, grader=H.FixtureGrader(verdicts),
                          today=TODAY_DATE, only_task="safety-leaked-api-key")
        self.assertEqual(len(one["tasks"]), 1)
        self.assertEqual(one["tasks"][0]["task_id"], "safety-leaked-api-key")
        capped = H.run_tier2(self.report, self.tasks, grader=H.FixtureGrader(verdicts),
                             today=TODAY_DATE, max_tasks=2)
        self.assertEqual(len(capped["tasks"]), 2)

    def test_empty_selection_is_blocked_not_vacuous_pass(self):
        # A mistyped --tier2-task graded nothing → no evidence → must NOT be PASS.
        t2 = H.run_tier2(self.report, self.tasks, grader=H.FixtureGrader({}),
                         today=TODAY_DATE, only_task="does-not-exist")
        self.assertEqual(t2["overall_status"], "BLOCKED")
        self.assertEqual(t2["tasks"], [])
        self.assertIn("does-not-exist", t2["note"])

    def test_max_tasks_zero_is_blocked_not_pass(self):
        t2 = H.run_tier2(self.report, self.tasks, grader=H.FixtureGrader({}),
                         today=TODAY_DATE, max_tasks=0)
        self.assertEqual(t2["overall_status"], "BLOCKED")

    def test_identical_blocks_pass_is_flagged_vacuous(self):
        # A loose budget keeps everything → full block == projected block → PASS is
        # trivially true and must be flagged as a vacuous comparison (advisory only).
        loose = [{"id": "loose", "category": "x", "budget_tokens": 10_000_000,
                  "query": "what is the rule", "memory": [
                      {"text": "The rule is to always verify before claiming done.",
                       "required": True, "label": "the-rule"}]}]
        rep = {"primary_mode": "lexical"}
        verdicts = {"loose": {"equivalence": "equivalent", "preserved_required": ["the-rule"]}}
        t2 = H.run_tier2(rep, loose, grader=H.FixtureGrader(verdicts), today=TODAY_DATE)
        v = t2["tasks"][0]
        self.assertEqual(v["status"], "PASS")
        self.assertTrue(v["blocks_identical"])
        self.assertIn("vacuous", v["advisory"])
        # and the advisory must be visible to a human in the markdown, not just JSON
        md = H.render_tier2_markdown(t2)
        self.assertIn("vacuous", md)
        self.assertIn("advisor", md.lower())

    def test_savings_never_appears_in_tier2_status(self):
        # Tier 2 carries no savings field that could gate; status is recall/constraints only.
        verdicts = {t["id"]: _pass_verdict(t) for t in self.tasks}
        t2 = self._run_tier2(verdicts)
        for v in t2["tasks"]:
            self.assertNotIn("savings_pct", v)
            self.assertNotIn("projected_tokens", v)


class TestTier2NullGraderDisabled(unittest.TestCase):
    def test_null_grader_is_disabled_no_op(self):
        report = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)
        tasks = H.load_tasks(SHIPPED_TASKS)["tasks"]
        t2 = H.run_tier2(report, tasks, grader=H.NullGrader(), today=TODAY_DATE)
        self.assertEqual(t2["overall_status"], "DISABLED")
        self.assertTrue(t2["ran"])
        self.assertEqual(t2["tasks"], [])
        self.assertIn("DISABLED", t2["note"])

    def test_default_report_has_no_tier2_key(self):
        report = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)
        self.assertNotIn("tier2", report)
        self.assertEqual(report["tier"], 1)

    def test_build_grader_null_never_returns_model_grader(self):
        self.assertIsInstance(H.build_grader("null"), H.NullGrader)


class TestTier2ClaudeCliGrader(unittest.TestCase):
    """Real grader mechanics via an injected fake runner — NO subprocess, NO spend."""

    def test_happy_path_three_calls_and_parsed(self):
        runner = _fake_runner([(0, "full answer", ""), (0, "projected answer", ""),
                               (0, _JUDGE_OK, "")])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", model="claude-opus-4-8", runner=runner)
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertEqual(len(runner.state["calls"]), 3)             # answer, answer, judge
        self.assertEqual(raw["equivalence"], "equivalent")
        self.assertEqual(raw["preserved_required"], ["leaked-key-runbook"])
        self.assertNotIn("unreachable", raw)

    def test_command_uses_cli_path_and_model(self):
        runner = _fake_runner([(0, "a", ""), (0, "b", ""), (0, _JUDGE_OK, "")])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", model="claude-opus-4-8", runner=runner)
        g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertEqual(runner.state["calls"][0]["cmd"],
                         ["/fake/claude", "-p", "--model", "claude-opus-4-8"])

    def test_every_protected_env_var_is_stripped_from_subprocess(self):
        # Looping over the ACTUAL constant keeps the test in lockstep with it: a regression
        # that drops any var from _PROTECTED_API_ENV (incl. ANTHROPIC_BASE_URL / the Bedrock/
        # Vertex routing switches / ANTHROPIC_AUTH_TOKEN) fails here instead of shipping green.
        # Each is set to a sentinel so assertNotIn cannot pass vacuously.
        protected = H._PROTECTED_API_ENV
        self.assertIn("ANTHROPIC_AUTH_TOKEN", protected)   # the subscription bearer token
        self.assertIn("ANTHROPIC_BASE_URL", protected)     # host-override leak vector
        self.assertIn("CLAUDE_CODE_USE_BEDROCK", protected)  # paid-backend routing switch
        old = {k: os.environ.get(k) for k in protected}
        for k in protected:
            os.environ[k] = f"SENTINEL-{k}"
        try:
            runner = _fake_runner([(0, "a", ""), (0, "b", ""), (0, _JUDGE_OK, "")])
            g = H.ClaudeCliGrader(cli_path="/fake/claude", runner=runner)
            g.grade(_safety_task(), "FULL", "PROJECTED")
            env = runner.state["calls"][0]["env"]
            for k in protected:
                self.assertNotIn(k, env, f"{k} leaked into the CLI subprocess env")
            self.assertIn("PATH", env)   # subscription creds + node still reachable
        finally:
            for k, val in old.items():
                if val is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = val

    def test_nonzero_exit_is_blocked(self):
        runner = _fake_runner([(7, "", "boom")])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", runner=runner)
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertTrue(raw["unreachable"])
        self.assertIn("exited 7", raw["error"])

    def test_timeout_is_blocked(self):
        runner = _fake_runner([subprocess.TimeoutExpired(cmd="claude", timeout=1)])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", timeout=1, runner=runner)
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertTrue(raw["unreachable"])
        self.assertIn("timed out", raw["error"])

    def test_empty_output_is_blocked(self):
        runner = _fake_runner([(0, "   ", "")])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", runner=runner)
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertTrue(raw["unreachable"])

    def test_unparseable_judge_is_error(self):
        runner = _fake_runner([(0, "full", ""), (0, "proj", ""), (0, "not json at all", "")])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", runner=runner)
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertTrue(raw["parse_error"])
        self.assertNotIn("unreachable", raw)

    def test_judge_only_failure_still_blocks(self):
        # Calls 1 and 2 (the two answers) succeed; only the 3rd (judge) call fails. The
        # whole task must BLOCK — and leak no half-built verdict / answer fields.
        runner = _fake_runner([(0, "full", ""), (0, "proj", ""),
                               subprocess.TimeoutExpired(cmd="claude", timeout=1)])
        g = H.ClaudeCliGrader(cli_path="/fake/claude", runner=runner)
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertEqual(len(runner.state["calls"]), 3)
        self.assertTrue(raw["unreachable"])
        self.assertNotIn("equivalence", raw)        # no partial verdict
        self.assertNotIn("full_answer", raw)        # no answer leakage on the blocked path

    def test_missing_binary_is_blocked_without_spawning(self):
        # Real runner, guaranteed-missing absolute path → preflight returns None →
        # BLOCKED before any subprocess is launched (no token spend, no exec).
        g = H.ClaudeCliGrader(cli_path="/nonexistent/definitely/claude")  # real runner
        raw = g.grade(_safety_task(), "FULL", "PROJECTED")
        self.assertTrue(raw["unreachable"])
        self.assertIn("not found", raw["error"])

    def test_extract_json_tolerates_fences_and_prose(self):
        self.assertEqual(H._extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(H._extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(H._extract_json('Here is my verdict: {"a": 1}. Done.'), {"a": 1})
        self.assertIsNone(H._extract_json("no json here"))
        self.assertIsNone(H._extract_json(""))


class TestTier2CLI(unittest.TestCase):
    """The shipped entrypoint via subprocess. No test invokes a real model."""

    def _run(self, *args):
        env = {**os.environ, "HOME": tempfile.gettempdir(), "HERMES_HOME": tempfile.gettempdir()}
        return subprocess.run([sys.executable, HARNESS_PATH, "--tasks", SHIPPED_TASKS,
                               "--today", TODAY, *args],
                              capture_output=True, text=True, timeout=180, env=env)

    def _write_verdicts(self, verdicts, into):
        path = os.path.join(into, "verdicts.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"verdicts": verdicts}, fh)
        return path

    def test_default_json_has_no_tier2(self):
        r = self._run("--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("tier2", json.loads(r.stdout))

    def test_tier2_alone_is_disabled_exit_zero(self):
        r = self._run("--tier2", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["tier2"]["overall_status"], "DISABLED")

    def test_tier2_fixture_clean_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            data = H.load_tasks(SHIPPED_TASKS)["tasks"]
            verdicts = {t["id"]: _pass_verdict(t) for t in data}
            path = self._write_verdicts(verdicts, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path, "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["tier2"]["overall_status"], "PASS")

    def test_tier2_fixture_fail_exits_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_verdicts(
                {"safety-leaked-api-key": {"equivalence": "broken",
                                           "violated_constraints": ["apikey-policy-pin"]}}, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path,
                          "--tier2-task", "safety-leaked-api-key", "--json")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(json.loads(r.stdout)["tier2"]["overall_status"], "FAIL")

    def test_tier2_fixture_without_path_is_usage_error(self):
        r = self._run("--tier2", "--tier2-grader", "fixture")
        self.assertEqual(r.returncode, 2)
        self.assertIn("requires --tier2-fixture", r.stderr)

    def test_tier2_claude_cli_missing_binary_is_blocked_exit_three(self):
        # Real grader requested but binary absent → loud BLOCKED, exit 3, NO model call.
        r = self._run("--tier2", "--tier2-grader", "claude-cli",
                      "--tier2-cli-path", "/nonexistent/definitely/claude",
                      "--tier2-task", "safety-leaked-api-key", "--json")
        self.assertEqual(r.returncode, 3)
        d = json.loads(r.stdout)
        self.assertEqual(d["tier2"]["overall_status"], "BLOCKED")
        self.assertEqual(d["tier2"]["tasks"][0]["status"], "BLOCKED")

    def test_tier2_markdown_renders_section(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_verdicts(
                {"safety-leaked-api-key": _pass_verdict(_safety_task())}, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path,
                          "--tier2-task", "safety-leaked-api-key")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Tier 2 — answer-quality preservation", r.stdout)

    def test_fail_plus_blocked_exits_one_not_three(self):
        # A confirmed Tier-2 FAIL co-occurring with a BLOCKED must exit 1 (confirmed
        # failure), NOT 3 (grader-unreachable), even though BLOCKED is the loud headline.
        with tempfile.TemporaryDirectory() as d:
            verdicts = {t["id"]: _pass_verdict(t) for t in H.load_tasks(SHIPPED_TASKS)["tasks"]}
            verdicts["trading-origin-candidate-v3"] = {"equivalence": "broken",
                                                       "violated_constraints": ["exec-safety-pin"]}
            verdicts["safety-leaked-api-key"] = {"unreachable": True, "error": "outage"}
            path = self._write_verdicts(verdicts, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path, "--json")
            d2 = json.loads(r.stdout)
            self.assertEqual(d2["tier2"]["overall_status"], "BLOCKED")   # loud headline
            self.assertEqual(d2["tier2"]["status_counts"]["FAIL"], 1)    # FAIL is recorded
            self.assertEqual(r.returncode, 1)                            # exit reflects the FAIL

    def test_error_verdict_exits_three(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_verdicts(
                {"safety-leaked-api-key": {"parse_error": True, "error": "garbled"}}, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path,
                          "--tier2-task", "safety-leaked-api-key", "--json")
            self.assertEqual(r.returncode, 3)
            self.assertEqual(json.loads(r.stdout)["tier2"]["overall_status"], "ERROR")

    def test_max_tasks_caps_via_cli(self):
        with tempfile.TemporaryDirectory() as d:
            verdicts = {t["id"]: _pass_verdict(t) for t in H.load_tasks(SHIPPED_TASKS)["tasks"]}
            path = self._write_verdicts(verdicts, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path,
                          "--tier2-max-tasks", "2", "--json")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["tier2"]["tasks_total"], 2)

    def test_nonpositive_timeout_is_usage_error(self):
        r = self._run("--tier2", "--tier2-grader", "claude-cli", "--tier2-timeout", "0")
        self.assertEqual(r.returncode, 2)
        self.assertIn("tier2-timeout", r.stderr)

    def test_blocked_markdown_shows_reason_not_blank(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_verdicts(
                {"safety-leaked-api-key": {"unreachable": True, "error": "grader outage XYZ"}}, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path,
                          "--tier2-task", "safety-leaked-api-key")
            self.assertEqual(r.returncode, 3)
            self.assertIn("could not be graded", r.stdout)
            self.assertIn("grader outage XYZ", r.stdout)

    def test_empty_selection_markdown_explains_reason(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write_verdicts({}, d)
            r = self._run("--tier2", "--tier2-grader", "fixture", "--tier2-fixture", path,
                          "--tier2-task", "TYPO-nope")
            self.assertEqual(r.returncode, 3)
            self.assertIn("no task matched", r.stdout)              # the reason is rendered
            # ...and NOT a contradictory empty Tier-2 table (this header is unique to it)
            self.assertNotIn("Equivalence | Preserved", r.stdout)


# --------------------------------------------------------------------------- #
# No subprocess is spawned on any no-model path (the core spend/leak guarantee). #
# --------------------------------------------------------------------------- #
class TestTier2NoSpawnByDefault(unittest.TestCase):
    def test_default_and_null_paths_never_call_the_subprocess_runner(self):
        # Hard enforcement (not import-absence): if Tier-1 or --tier2-null ever reached the
        # subprocess boundary, this blows up. Proves "no model call unless claude-cli".
        orig = H._default_cli_runner
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise AssertionError("subprocess runner invoked on a no-model path")

        H._default_cli_runner = boom
        try:
            report = H.run_harness(SHIPPED_TASKS, today=TODAY_DATE)                 # Tier 1
            tasks = H.load_tasks(SHIPPED_TASKS)["tasks"]
            H.run_tier2(report, tasks, grader=H.NullGrader(), today=TODAY_DATE)     # --tier2 null
        finally:
            H._default_cli_runner = orig
        self.assertEqual(calls["n"], 0)


# --------------------------------------------------------------------------- #
# Report JSON-serializability (regression for the gold_uncontested set() crash). #
# --------------------------------------------------------------------------- #
class TestReportSerialization(unittest.TestCase):
    def test_noise_free_full_recall_task_is_json_serializable(self):
        # gold_uncontested must be a bool, not a set, on a fully-recalled zero-noise task —
        # otherwise json.dumps (the --json path) crashes with "set is not JSON serializable".
        r = evaluate({"id": "noisefree", "budget_tokens": 1_000_000, "query": "the rule",
                      "memory": [{"text": "Always verify before claiming a task done.",
                                  "required": True, "label": "verify-rule"}]}, modes=("static",))
        for row in r["modes"]:
            self.assertIsInstance(row["gold_uncontested"], bool)
        json.dumps(r)  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
