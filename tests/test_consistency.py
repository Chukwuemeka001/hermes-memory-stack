#!/usr/bin/env python3
"""Cross-file consistency quality gate (the anti-drift test).

This is the regression guard for the P2 trust pass: it fails if the authoritative
code thresholds, the config, and the docs ever disagree again, or if a doc grows a
command that does not match the real CLIs. Pure stdlib, no live data.

What it pins:
  * capacity / state.db thresholds: memory_audit + config + docs == memory_health
    (the authoritative source).
  * the shared signal module (INTEG-8) really IS shared (same objects everywhere).
  * the maintenance pass is 6 steps including state_db_remediate.
  * SKILL.md / skills have no broken commands (bare `ping`, `--min-confidence`,
    phantom curator scripts, wrong install path) and RUNBOOK.md exists.
  * no hardcoded home path leaked into the scripts.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_consistency -v
"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
SCRIPTS = os.path.join(PKG, "scripts")

# Use NORMAL imports (not importlib-from-file) so memory_signals is a single
# cached module — that's the whole point of the INTEG-8 identity assertions: the
# modules must share the SAME signal objects, which only holds under real imports.
sys.path.insert(0, SCRIPTS)
import memory_signals as S            # noqa: E402
import memory_health as H             # noqa: E402
import memory_audit as A              # noqa: E402
import hermes_memory_intake_gate as G  # noqa: E402
import temporal_memory as T           # noqa: E402
import memory_maintenance as MM       # noqa: E402


def _read(*parts):
    with open(os.path.join(PKG, *parts), "r", encoding="utf-8") as fh:
        return fh.read()


class TestThresholdConsistency(unittest.TestCase):
    """memory_health.py is authoritative; everything else must match it."""

    def test_audit_capacity_matches_health(self):
        self.assertEqual(A.CAPACITY_WARN_PCT, H.WARN_PCT,
                         "memory_audit capacity WARN must equal memory_health WARN_PCT")
        self.assertEqual(A.CAPACITY_CRIT_PCT, H.CRIT_PCT,
                         "memory_audit capacity CRIT must equal memory_health CRIT_PCT")

    def test_authoritative_values_are_the_chosen_ones(self):
        # The P2 pass fixed these exact numbers; pin them so a silent change trips.
        self.assertEqual(H.WARN_PCT, 80)
        self.assertEqual(H.CRIT_PCT, 90)
        self.assertEqual(H.STATE_DB_WARN_MB, 50)
        self.assertEqual(H.STATE_DB_CRIT_MB, 200)

    def test_config_yaml_matches_code(self):
        cfg = _read("config", "memory-defaults.yaml")

        def vals(key):
            return [int(m) for m in re.findall(rf"{key}:\s*(\d+)", cfg)]

        for v in vals("capacity_warning_percent"):
            self.assertEqual(v, H.WARN_PCT, "config capacity_warning_percent != memory_health.WARN_PCT")
        for v in vals("capacity_critical_percent"):
            self.assertEqual(v, H.CRIT_PCT, "config capacity_critical_percent != memory_health.CRIT_PCT")
        self.assertIn(H.STATE_DB_WARN_MB, vals("state_db_warning_mb"), "config missing/!= state_db warn MB")
        self.assertIn(H.STATE_DB_CRIT_MB, vals("state_db_critical_mb"), "config missing/!= state_db crit MB")
        # the stale value must be gone
        self.assertNotIn(95, vals("capacity_critical_percent"), "stale capacity_critical 95 still in config")

    def test_no_stale_250mb_threshold_in_docs(self):
        for rel in ("skills/memory-maintenance.md", "skills/state-db-remediation.md", "README.md"):
            txt = _read(*rel.split("/"))
            self.assertNotIn("250 MB", txt, f"{rel} still cites the stale 250 MB state.db threshold")
            self.assertNotIn("≥250", txt, f"{rel} still cites the stale ≥250 threshold")


class TestMaintenanceSteps(unittest.TestCase):
    def test_step_order_is_six_with_state_db(self):
        self.assertEqual(len(MM.STEP_ORDER), 6, "maintenance must be 6 steps")
        self.assertIn("state_db_remediate", MM.STEP_ORDER, "state_db_remediate must be a maintenance step")

    def test_docs_say_six_steps(self):
        for rel in ("README.md", "skills/memory-maintenance.md"):
            txt = _read(*rel.split("/"))
            self.assertIn("state_db_remediate", txt,
                          f"{rel} omits state_db_remediate from the maintenance step list")


class TestSharedSignals(unittest.TestCase):
    """INTEG-8: the signals are defined once and shared by identity (not copies)."""

    def test_format_constants_shared(self):
        for mod in (A, G, T):
            self.assertIs(mod.ENTRY_DELIMITER, S.ENTRY_DELIMITER)
        for mod in (A, T):
            self.assertIs(mod.POINTER_SIGIL, S.POINTER_SIGIL)
            self.assertIs(mod.HEADER_SENTINEL, S.HEADER_SENTINEL)

    def test_durability_regexes_shared(self):
        for name in ("TEMPORAL_RE", "COMPLETION_RE", "METRIC_RE", "LEADING_DATE_RE",
                     "PREF_RE", "POINTER_RE", "REFLECTION_RE"):
            self.assertIs(getattr(A, name), getattr(S, name),
                          f"memory_audit.{name} is not the shared memory_signals object")
            self.assertIs(getattr(G, name), getattr(S, name),
                          f"intake_gate.{name} is not the shared memory_signals object")


class TestDocsNoBrokenCommands(unittest.TestCase):
    def test_skill_md_uses_ping_flag_not_positional(self):
        skill = _read("SKILL.md")
        self.assertNotRegex(skill, r"semantic_query\.py\s+ping\b",
                            "SKILL.md uses `semantic_query.py ping` (runs a search) instead of `--ping`")

    def test_no_min_confidence_flag_anywhere(self):
        # --min-confidence is not a real flag of memory_auto_extract.py.
        for root in ("SKILL.md",):
            self.assertNotIn("--min-confidence", _read(root), f"{root} references nonexistent --min-confidence")
        skills_dir = os.path.join(PKG, "skills")
        for fn in os.listdir(skills_dir):
            if fn.endswith(".md"):
                with open(os.path.join(skills_dir, fn), encoding="utf-8") as fh:
                    self.assertNotIn("--min-confidence", fh.read(),
                                     f"skills/{fn} references nonexistent --min-confidence")

    def test_skill_md_no_phantom_curator_scripts(self):
        skill = _read("SKILL.md")
        for phantom in ("memory_curator_daily.py", "memory_curator_monitor.py",
                        "memory_curator_weekly.py"):
            self.assertNotIn(phantom, skill,
                             f"SKILL.md references {phantom}, which is not bundled in this package")

    def test_skill_md_install_path_correct(self):
        skill = _read("SKILL.md")
        self.assertNotIn("skills/hermes-memory-stack/install.sh", skill,
                         "SKILL.md uses the wrong install path (skills/ not packages/)")
        self.assertIn("packages/hermes-memory-stack", skill)

    def test_runbook_exists_with_steps(self):
        rb = _read("RUNBOOK.md")
        self.assertGreaterEqual(rb.count("```bash"), 10, "RUNBOOK.md must chain 10+ command steps")
        for script in ("state_db_remediate.py", "memory_audit.py", "memory_rewrite.py",
                       "temporal_migrate_onboard.py", "memory_maintenance.py"):
            self.assertIn(script, rb, f"RUNBOOK.md does not reference {script}")

    def test_no_hardcoded_home_in_scripts(self):
        for fn in os.listdir(SCRIPTS):
            if fn.endswith((".py", ".sh")):
                with open(os.path.join(SCRIPTS, fn), encoding="utf-8") as fh:
                    self.assertNotIn("/Users/emeka", fh.read(),
                                     f"scripts/{fn} contains a hardcoded /Users/emeka path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
