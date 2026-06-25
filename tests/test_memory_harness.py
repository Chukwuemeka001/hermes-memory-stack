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


if __name__ == "__main__":
    unittest.main(verbosity=2)
