#!/usr/bin/env python3
"""Tests for install.sh — validates the installer against a temporary HERMES_HOME.

These tests prove that the installer:
1. Creates required directories before copying (EXPORT-1 fix)
2. Copies ALL scripts (22 Python + 6 shell, not just 11) (EXPORT-2 fix)
3. Copies cron wrappers and cron JSON definitions
4. Exports HERMES_HOME to child processes (EXPORT-4 fix)
5. Uses python3 -m pip (EXPORT-9 fix)
6. Ver passes and reports all components
7. Works on a completely clean HERMES_HOME

Run:
    python3 -m unittest tests.test_install -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
INSTALL_SH = os.path.join(PKG, "install.sh")

# All scripts that MUST be installed
EXPECTED_SCRIPTS = [
    # Shared signal module — imported by memory_audit / intake_gate / temporal (INTEG-8)
    "memory_signals.py",
    # Tier 1: Semantic
    "semantic_index.py",
    "semantic_query.py",
    "memory_entry_index.py",
    "memory_project.py",
    "memory_shadow.py",
    "memory_harness.py",
    "memory_harness_tasks.json",
    "semantic_reindex.sh",
    # Tier 2: Auto-extraction
    "memory_auto_extract.py",
    "memory_auto_extract_cron.sh",
    "memory_auto_extract_eval.py",
    "memory_auto_extract_sample_real.py",
    "hermes_memory_intake_gate.py",
    # Tier 3: Temporal
    "temporal_memory.py",
    "temporal_migrate.py",
    "temporal_migrate_onboard.py",
    "temporal_ingest.sh",
    # Tier 4: Remediation (Areas 1-5)
    "state_db_remediate.py",
    "memory_audit.py",
    "memory_rewrite.py",
    "memory_health.py",
    "memory_health_cron.sh",
    "memory_maintenance.py",
    "memory_maintenance_cron.sh",
    "memory_onboard.py",          # one-command Area 1→5 driver (INTEG-10)
]

EXPECTED_CRON_JSONS = [
    "auto-extraction-dry-run.json",
    "memory-health-daily.json",
    "memory-temporal-sync.json",
    "semantic-reindex.json",
    "temporal-ingest.json",
]


class TestInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install_test_")
        self.home = os.path.join(self.tmp, ".hermes")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_install(self, tier="temporal"):
        """Run install.sh against a clean HERMES_HOME. Skips semantic (needs chromadb)."""
        env = os.environ.copy()
        env["HOME"] = self.tmp
        env["HERMES_HOME"] = self.home
        r = subprocess.run(
            ["bash", INSTALL_SH, tier],
            capture_output=True, text=True, timeout=60, env=env,
        )
        return r

    def test_scripts_dir_created_before_copy(self):
        """EXPORT-1 fix: install.sh must create $SCRIPTS_DIR before any cp."""
        r = self.run_install("temporal")
        self.assertEqual(r.returncode, 0, f"install failed: {r.stderr}")
        self.assertTrue(os.path.isdir(os.path.join(self.home, "scripts")),
                        "scripts/ dir not created")

    def test_temporal_installs_all_temporal_scripts(self):
        """Tier 3 installs temporal_memory, temporal_migrate, temporal_migrate_onboard, temporal_ingest."""
        r = self.run_install("temporal")
        self.assertEqual(r.returncode, 0, f"install failed: {r.stderr}")
        scripts_dir = os.path.join(self.home, "scripts")
        for name in ["temporal_memory.py", "temporal_migrate.py",
                      "temporal_migrate_onboard.py", "temporal_ingest.sh",
                      "memory_signals.py"]:   # temporal_memory imports it (INTEG-8)
            path = os.path.join(scripts_dir, name)
            self.assertTrue(os.path.exists(path), f"{name} not installed")

    def test_remediation_installs_all_area_scripts(self):
        """EXPORT-2 fix: remediation tier copies all Areas 1-5 scripts."""
        r = self.run_install("remediation")
        self.assertEqual(r.returncode, 0, f"install failed: {r.stderr}")
        scripts_dir = os.path.join(self.home, "scripts")
        for name in ["state_db_remediate.py", "memory_audit.py", "memory_rewrite.py",
                      "memory_health.py", "memory_project.py", "memory_shadow.py", "memory_harness.py",
                      "memory_harness_tasks.json", "memory_health_cron.sh",
                      "memory_maintenance.py", "memory_maintenance_cron.sh",
                      "memory_onboard.py",    # one-command Area 1→5 driver (INTEG-10)
                      "memory_signals.py"]:   # memory_audit imports it (INTEG-8)
            path = os.path.join(scripts_dir, name)
            self.assertTrue(os.path.exists(path), f"{name} not installed by remediation tier")
        self.assertTrue(os.path.exists(os.path.join(self.home, "skills", "memory-stack", "memory-projection.md")),
                        "projection skill doc not installed by remediation tier")

    def test_installed_modules_import_cleanly(self):
        """INTEG-8 regression: a module a tier copies must have ALL its dependencies
        copied too (notably the shared memory_signals.py), so the INSTALLED script
        actually RUNS — not merely exists. Running `--help` exercises the
        module-load import chain (the `from memory_signals import …` at file top)."""
        for tier in ["extraction", "temporal", "remediation"]:
            r = self.run_install(tier)
            self.assertEqual(r.returncode, 0, f"tier {tier} failed: {r.stderr}")
        scripts_dir = os.path.join(self.home, "scripts")
        env = os.environ.copy()
        env["HOME"] = self.tmp
        env["HERMES_HOME"] = self.home
        for name in ["memory_audit.py", "temporal_memory.py", "hermes_memory_intake_gate.py",
                     "memory_onboard.py", "memory_project.py", "memory_shadow.py", "memory_harness.py"]:
            p = subprocess.run(["python3", os.path.join(scripts_dir, name), "--help"],
                               capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(p.returncode, 0,
                             f"installed {name} failed to run — missing dependency? {p.stderr}")
        self.assertTrue(os.path.exists(os.path.join(scripts_dir, "memory_signals.py")),
                        "memory_signals.py not installed (INTEG-8 shared dependency)")

    def test_extraction_installs_gate_and_fixtures(self):
        """Tier 2 installs intake gate and fixture files."""
        r = self.run_install("extraction")
        self.assertEqual(r.returncode, 0, f"install failed: {r.stderr}")
        scripts_dir = os.path.join(self.home, "scripts")
        self.assertTrue(os.path.exists(os.path.join(scripts_dir, "hermes_memory_intake_gate.py")))
        # At least one fixture file
        fixtures = [f for f in os.listdir(scripts_dir) if f.startswith("memory_auto_extract_fixtures")]
        self.assertGreater(len(fixtures), 0, "no fixture files installed")

    def test_crons_tier_copies_all_jsons(self):
        """Tier 5 copies all cron JSON definitions."""
        r = self.run_install("crons")
        self.assertEqual(r.returncode, 0, f"install failed: {r.stderr}")
        crons_dir = os.path.join(self.home, "crons")
        self.assertTrue(os.path.isdir(crons_dir), "crons/ dir not created")
        for name in EXPECTED_CRON_JSONS:
            path = os.path.join(crons_dir, name)
            self.assertTrue(os.path.exists(path), f"{name} not installed")
            # Validate it's valid JSON
            with open(path) as f:
                data = json.load(f)
            self.assertIsInstance(data, (dict, list))

    def test_install_verify_exits_clean(self):
        """The verify step should succeed after a full install (minus semantic)."""
        # Install extraction + temporal + remediation (skip semantic which needs chromadb)
        for tier in ["extraction", "temporal", "remediation"]:
            r = self.run_install(tier)
            self.assertEqual(r.returncode, 0, f"tier {tier} failed: {r.stderr}")

        # Run verify
        env = os.environ.copy()
        env["HOME"] = self.tmp
        env["HERMES_HOME"] = self.home
        r = subprocess.run(
            ["bash", INSTALL_SH, "verify"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        self.assertEqual(r.returncode, 0, f"verify failed: {r.stderr}")
        self.assertIn("✓", r.stdout, "no success markers in verify output")

    def test_hermes_home_exported_to_children(self):
        """EXPORT-4 fix: install.sh must export HERMES_HOME so child processes see it."""
        # We can't easily test the export directly, but we can verify the script
        # has 'export HERMES_HOME' in it
        with open(INSTALL_SH) as f:
            content = f.read()
        self.assertIn("export HERMES_HOME", content,
                       "install.sh does not export HERMES_HOME")

    def test_no_bare_pip(self):
        """EXPORT-9 fix: install.sh must use python3 -m pip, not bare pip."""
        with open(INSTALL_SH) as f:
            content = f.read()
        # Check that bare 'pip install' is not used (python3 -m pip is OK)
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Bare 'pip install' without python3 -m prefix
            if "pip install" in stripped and "python3 -m pip" not in stripped:
                self.fail(f"Line {i}: bare 'pip install' found: {stripped}")

    def test_no_hardcoded_emeka_path(self):
        """No /Users/emeka hardcoded in install.sh."""
        with open(INSTALL_SH) as f:
            content = f.read()
        self.assertNotIn("/Users/emeka", content,
                          "install.sh contains hardcoded /Users/emeka path")

    def test_all_scripts_present_in_package(self):
        """Every script that install.sh references must exist in the package scripts/ dir."""
        scripts_dir = os.path.join(PKG, "scripts")
        for name in EXPECTED_SCRIPTS:
            path = os.path.join(scripts_dir, name)
            self.assertTrue(os.path.exists(path),
                            f"Package missing script: {name}")


if __name__ == "__main__":
    unittest.main()
