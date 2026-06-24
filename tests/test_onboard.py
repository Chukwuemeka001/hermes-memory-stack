#!/usr/bin/env python3
"""Tests for memory_onboard.py — the single-command Area 1→5 onboarding driver.

Covers: --help, dry-run never mutates (the safety guarantee), the full apply
pipeline on a synthetic messy profile (the integration proof), --from-step resume,
single-step isolation, missing-artifact guards, and partial-failure preservation.

Drives the REAL CLI via subprocess (the shipped entrypoint), in a temp dir; live
~/.hermes is never touched. Run:
    python3 -m unittest tests.test_onboard -v
"""
from __future__ import annotations

import atexit
import hashlib
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
sys.path.insert(0, HERE)
sys.path.insert(0, SCRIPTS)
import synthetic_profile  # noqa: E402

ONBOARD = os.path.join(SCRIPTS, "memory_onboard.py")

# Structural live-data backstop (mirrors test_e2e_pipeline): point HERMES_HOME/HOME
# at an empty sentinel so any (hypothetical) dropped --home degrades to a harmless
# empty dir, never the real ~/.hermes. Every call below ALSO passes --home explicitly.
_SENTINEL = tempfile.mkdtemp(prefix="onboard_sentinel_")
atexit.register(shutil.rmtree, _SENTINEL, ignore_errors=True)


def run_onboard(*args, timeout=900):
    env = {**os.environ, "HERMES_HOME": _SENTINEL, "HOME": _SENTINEL}
    return subprocess.run([sys.executable, ONBOARD, *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def build(root, seed=42):
    synthetic_profile.build_profile(root, seed=seed)
    return {
        "mem": os.path.join(root, "memories", "MEMORY.md"),
        "usr": os.path.join(root, "memories", "USER.md"),
        "db": os.path.join(root, "state.db"),
        "history": os.path.join(root, "memories", "_versions", "history.jsonl"),
        "versions_db": os.path.join(root, "memory_versions.db"),
    }


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def verify_temporal(root):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "temporal_migrate_onboard.py"),
                        "verify", "--home", root, "--json"],
                       capture_output=True, text=True, timeout=120,
                       env={**os.environ, "HERMES_HOME": _SENTINEL, "HOME": _SENTINEL})
    return json.loads(r.stdout)


# --------------------------------------------------------------------------- #
# Lightweight read-only / dry-run tests on ONE shared profile                 #
# --------------------------------------------------------------------------- #
class TestOnboardDryAndHelp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="onboard_dry_")
        cls.f = build(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def _snapshot(self):
        return (sha(self.f["mem"]), sha(self.f["usr"]), os.path.getsize(self.f["db"]))

    def test_help_exits_zero(self):
        r = run_onboard("--help")
        self.assertEqual(r.returncode, 0)
        for token in ("--dry-run", "--apply", "--auto", "--from-step", "Areas 1"):
            self.assertIn(token, r.stdout, f"--help must document {token}")

    def test_dry_run_is_default_and_never_mutates(self):
        before = self._snapshot()
        wd = os.path.join(self.root, "wd_default")
        # NO mode flag at all -> must default to dry-run.
        r = run_onboard("--home", self.root, "--user-home", self.root, "--workdir", wd)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY-RUN", r.stdout)
        self.assertIn("Dry-run complete", r.stdout)
        # live data byte-identical
        self.assertEqual(before, self._snapshot(), "dry-run must not modify any live file")
        # no temporal EVENTS recorded and no stray index left behind, no live archives
        self.assertFalse(os.path.exists(self.f["history"]),
                         "dry-run must not record any temporal events")
        self.assertFalse(os.path.exists(self.f["versions_db"]),
                         "dry-run must leave no stray temporal index (cleaned up if read-only verify created one)")
        self.assertFalse(os.path.isdir(os.path.join(self.root, "archives")),
                         "dry-run must not write live archives")
        # but it DID produce reviewable artifacts under the workdir
        self.assertTrue(os.path.exists(os.path.join(wd, "mem-audit.json")))
        self.assertTrue(os.path.exists(os.path.join(wd, "proposed", "manifest.json")))
        self.assertTrue(os.path.exists(os.path.join(wd, "proposed", "MEMORY.proposed.md")))

    def test_explicit_dry_run_skips_every_mutation(self):
        wd = os.path.join(self.root, "wd_explicit")
        r = run_onboard("--home", self.root, "--user-home", self.root, "--workdir", wd, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stderr)
        # the four mutation steps must show as skipped previews: 3 (state.db apply),
        # 5 (temporal seed), 7 (rewrite apply), 8 (temporal reconcile).
        self.assertEqual(r.stdout.count("would run"), 4,
                         "dry-run must preview exactly the 4 mutation steps and execute nothing live")

    def test_single_step_isolation(self):
        wd = os.path.join(self.root, "wd_iso")
        before = self._snapshot()
        r = run_onboard("--home", self.root, "--user-home", self.root, "--workdir", wd,
                        "--from-step", "1", "--to-step", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Step 1/10", r.stdout)
        self.assertNotIn("Step 2/10", r.stdout)
        self.assertEqual(before, self._snapshot())

    def test_resume_reuses_prior_artifacts(self):
        wd = os.path.join(self.root, "wd_resume")
        # produce the audit artifact (steps 1-4; step 5 seed is skipped in dry-run)
        r1 = run_onboard("--home", self.root, "--user-home", self.root, "--workdir", wd,
                         "--dry-run", "--to-step", "4")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertTrue(os.path.exists(os.path.join(wd, "mem-audit.json")))
        self.assertFalse(os.path.exists(os.path.join(wd, "proposed", "manifest.json")),
                         "render has not run yet")
        # resume at render (step 6) — it must CONSUME the step-4 mem-audit.json, not recompute
        r2 = run_onboard("--home", self.root, "--user-home", self.root, "--workdir", wd,
                         "--dry-run", "--from-step", "6", "--to-step", "6")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("Step 6/10", r2.stdout)
        self.assertNotIn("Step 1/10", r2.stdout)
        self.assertTrue(os.path.exists(os.path.join(wd, "proposed", "manifest.json")),
                        "resumed render must produce the manifest from the reused audit")

    def test_missing_artifact_guard(self):
        wd = os.path.join(self.root, "wd_missing")  # never populated
        before = self._snapshot()
        # jump straight to render (step 6) with no mem-audit.json present
        r = run_onboard("--home", self.root, "--user-home", self.root, "--workdir", wd,
                        "--dry-run", "--from-step", "6", "--to-step", "6")
        self.assertNotEqual(r.returncode, 0, "missing required artifact must fail, not silently pass")
        self.assertIn("missing", r.stdout.lower() + r.stderr.lower())
        self.assertEqual(before, self._snapshot(), "a guard failure must not touch live data")


# --------------------------------------------------------------------------- #
# Full apply pipeline — the integration proof (own fresh profile, mutates)     #
# --------------------------------------------------------------------------- #
class TestOnboardFullApply(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="onboard_apply_")
        cls.f = build(cls.root)
        cls.mem_before = sha(cls.f["mem"])
        cls.db_before = os.path.getsize(cls.f["db"])
        cls.result = run_onboard("--home", cls.root, "--user-home", cls.root, "--apply", "--yes")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_pipeline_completes(self):
        self.assertEqual(self.result.returncode, 0,
                         f"full apply must succeed:\n{self.result.stdout[-1500:]}\n{self.result.stderr[-800:]}")
        self.assertIn("Onboarding complete", self.result.stdout)
        self.assertIn("10/10 steps", self.result.stdout)

    def test_state_db_shrank(self):
        self.assertLess(os.path.getsize(self.f["db"]), self.db_before,
                        "Area 1 must shrink the live state.db")

    def test_hot_memory_rewritten(self):
        self.assertNotEqual(self.mem_before, sha(self.f["mem"]), "Area 3 must rewrite live MEMORY.md")

    def test_provenance_recorded_by_apply(self):
        # INTEG-3 / P3-2: the rewrite apply (step 7) auto-records area3-rewrite events
        # into the seeded temporal layer.
        self.assertTrue(os.path.exists(self.f["history"]), "temporal history must exist")
        with open(self.f["history"], encoding="utf-8") as fh:
            events = [json.loads(l) for l in fh if l.strip()]
        a3 = [e for e in events if e.get("source") == "area3-rewrite"]
        self.assertTrue(a3, "apply must record area3-rewrite provenance events into temporal")
        # and the apply step surfaced the recording in its output
        self.assertIn("recorded", self.result.stdout)

    def test_temporal_reconstructs_byte_exact(self):
        v = verify_temporal(self.root)
        self.assertTrue(v["all_match"],
                        f"temporal must reconstruct live exactly after onboarding: "
                        f"{[(k, s['exact_match']) for k, s in v['stores'].items()]}")
        self.assertGreater(v["facts"], 0)

    def test_rollback_archives_exist(self):
        # both destructive steps leave a recoverable archive
        self.assertTrue(os.path.isdir(os.path.join(self.root, "archives", "remediation")))
        rw = os.path.join(self.root, "archives", "rewrite")
        self.assertTrue(os.path.isdir(rw) and os.listdir(rw), "rewrite originals must be archived")


# --------------------------------------------------------------------------- #
# Partial failure — pipeline stops, earlier artifacts preserved, live safe     #
# --------------------------------------------------------------------------- #
class TestOnboardPartialFailure(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="onboard_fail_")
        self.f = build(self.root)
        self.wd = os.path.join(self.root, ".onboard")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_failure_stops_and_preserves_prior_artifacts(self):
        # run Area 1 up to (not including) apply -> produces state-audit.json + policy.json
        r1 = run_onboard("--home", self.root, "--apply", "--yes", "--to-step", "2")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.wd, "policy.json")))
        db_before = os.path.getsize(self.f["db"])

        # corrupt the policy so the state.db apply (step 3) fails deterministically
        with open(os.path.join(self.wd, "policy.json"), "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json")

        r2 = run_onboard("--home", self.root, "--apply", "--yes", "--from-step", "3", "--to-step", "3")
        self.assertNotEqual(r2.returncode, 0, "a failed mutation step must return non-zero")
        out = r2.stdout + r2.stderr
        self.assertIn("stopped at step 3", out.lower().replace("·", ""))
        self.assertIn("--from-step 3", out, "must print a resume hint")
        # the EARLIER step's artifact is preserved (not wiped by the failure)
        self.assertTrue(os.path.exists(os.path.join(self.wd, "state-audit.json")),
                        "artifacts from steps before the failure must be preserved")
        # and the live state.db was NOT swapped (apply refused before mutating)
        self.assertEqual(db_before, os.path.getsize(self.f["db"]),
                         "a failed apply must leave the live state.db unchanged")


if __name__ == "__main__":
    unittest.main()
