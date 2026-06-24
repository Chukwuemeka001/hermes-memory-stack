#!/usr/bin/env python3
"""Tests for memory_auto_extract.py — the auto-extract WRITE path + home routing.

Covers the CRITICAL paths the UltraReview found untested:
  * SAFETY-1 — append_to_memory must be atomic/locked/archived (crash-safe,
               concurrency-safe), like every other MEMORY.md writer.
  * SAFETY-9 — no leading empty entry on an empty MEMORY.md.
  * INTEG-2/EXPORT-10 — $HERMES_HOME alone must route reads/writes, not ~/.hermes.

Run:
    cd ~/.hermes/packages/hermes-memory-stack
    python3 -m unittest tests.test_memory_auto_extract -v
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)


def _load(name):
    path = os.path.join(SCRIPTS, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MA = _load("memory_auto_extract")
gate = MA.gate           # the exact gate instance the extractor uses
DELIM = gate.ENTRY_DELIMITER


def _report(facts, generated_at="2026-06-24T00:00:00"):
    return {"generated_at": generated_at,
            "accepted": [{"fact": f, "verdict": "ALLOW", "category": "durable_personal",
                          "reason": "test"} for f in facts]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="autoex_test_")
        self.home = os.path.join(self.tmp, "home")
        self.memdir = os.path.join(self.home, "memories")
        os.makedirs(self.memdir, exist_ok=True)
        self.mem = os.path.join(self.memdir, "MEMORY.md")
        self.out = os.path.join(self.memdir, "_auto_extract")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entries(self, path=None):
        raw = Path(path or self.mem).read_text(encoding="utf-8")
        return [e.strip() for e in raw.split(DELIM) if e.strip()]


class TestAppendAtomic(Base):
    def test_append_to_memory_atomic(self):
        Path(self.mem).write_text("Existing pointer: see ~/.hermes/notes/.", encoding="utf-8")
        n = MA.append_to_memory(
            _report(["User prefers vim.", "User writes Python."]), self.mem, self.out)
        self.assertEqual(n, 2)
        ents = self.entries()
        self.assertIn("User prefers vim.", ents)
        self.assertIn("User writes Python.", ents)
        self.assertIn("Existing pointer: see ~/.hermes/notes/.", ents)
        self.assertTrue(all(e.strip() for e in ents))   # no blank entries
        log = os.path.join(self.out, "append-log.jsonl")
        self.assertTrue(os.path.exists(log))
        with open(log, encoding="utf-8") as fh:
            self.assertEqual(sum(1 for _ in fh), 2)     # provenance for both

    def test_append_empty_file_no_leading_delimiter(self):
        # SAFETY-9: empty MEMORY.md must not yield a leading "\n§\n..." blank entry.
        Path(self.mem).write_text("", encoding="utf-8")
        MA.append_to_memory(_report(["User is allergic to penicillin."]), self.mem, self.out)
        raw = Path(self.mem).read_text(encoding="utf-8")
        self.assertFalse(raw.startswith(DELIM), "leading empty entry on empty file (SAFETY-9)")
        self.assertEqual(self.entries(), ["User is allergic to penicillin."])

    def test_append_creates_archive(self):
        original = "Existing pointer entry."
        Path(self.mem).write_text(original, encoding="utf-8")
        MA.append_to_memory(_report(["User uses Neovim."]), self.mem, self.out)
        baks = [p for p in os.listdir(self.memdir) if p.startswith("MEMORY.md.bak.")]
        self.assertTrue(baks, "append did not archive a .bak before writing (SAFETY-1)")
        self.assertEqual(
            Path(os.path.join(self.memdir, baks[0])).read_text(encoding="utf-8"), original)

    def test_new_file_is_0600(self):
        # Fresh personal-memory file must be private (0o600), matching every peer
        # writer — not world/group-readable. (adversarial review: LOW)
        newmem = os.path.join(self.memdir, "fresh_MEMORY.md")   # does not exist yet
        MA.append_to_memory(_report(["User prefers vim."]), newmem, self.out)
        mode = stat.S_IMODE(os.stat(newmem).st_mode)
        self.assertEqual(mode, 0o600, f"fresh memory file mode {oct(mode)} != 0o600")

    def test_embedded_delimiter_fact_dropped(self):
        # A fact embedding the §/\n§\n delimiter must NOT be written (it would
        # fragment MEMORY.md into phantom entries). (adversarial review: LOW)
        Path(self.mem).write_text("Seed.", encoding="utf-8")
        bad = "line one" + DELIM + "line two"
        n = MA.append_to_memory(_report(["User prefers vim.", bad]), self.mem, self.out)
        self.assertEqual(n, 1)                       # only the safe fact written
        self.assertEqual(set(self.entries()), {"Seed.", "User prefers vim."})

    def test_replace_failure_cleans_tmp(self):
        # The except-branch must unlink the tmp on a failed swap and leave the
        # original intact. (covers the non-crash failure path) (adversarial review: NIT)
        original = "Seed entry."
        Path(self.mem).write_text(original, encoding="utf-8")
        with mock.patch.object(MA.os, "replace", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                MA.append_to_memory(_report(["User prefers vim."]), self.mem, self.out)
        self.assertEqual(Path(self.mem).read_text(encoding="utf-8"), original)
        leftovers = [p for p in os.listdir(self.memdir) if p.startswith(".mem_")]
        self.assertEqual(leftovers, [], f"tmp not cleaned on failed swap: {leftovers}")


class TestAppendCrash(Base):
    """SAFETY-1 regression — a crash mid-write must leave MEMORY.md intact."""

    def test_append_crash_preserves_original(self):
        if not hasattr(os, "fork"):
            self.skipTest("requires os.fork (POSIX)")
        original = "Durable pointer: trading context ~/.hermes/notes/trading/."
        Path(self.mem).write_text(original, encoding="utf-8")
        orig_bytes = Path(self.mem).read_bytes()
        pid = os.fork()
        if pid == 0:  # child: crash exactly at the atomic swap, before it commits
            try:
                def _boom(*a, **k):
                    os._exit(137)
                MA.os.replace = _boom
                MA.append_to_memory(_report(["User prefers dark roast."]), self.mem, self.out)
            except BaseException:
                pass
            os._exit(0)   # unreachable (boom exits first) — guards against a no-op
        _pid, status = os.waitpid(pid, 0)
        # The boom (==> os.replace ==> the crash window) MUST have been reached,
        # else byte-equality would pass vacuously. (adversarial review: LOW)
        self.assertTrue(os.WIFEXITED(status) and os.WEXITSTATUS(status) == 137,
                        "child did not crash at os.replace — test would be vacuous")
        # MEMORY.md must be byte-identical to the original: no truncation/partial.
        self.assertEqual(Path(self.mem).read_bytes(), orig_bytes,
                         "MEMORY.md corrupted/truncated by a crash mid-write (SAFETY-1)")
        self.assertEqual(self.entries(), [original])


class TestAppendLock(Base):
    """SAFETY-1 regression — concurrent appends must not lose entries."""

    def test_append_respects_lock(self):
        if not hasattr(os, "fork"):
            self.skipTest("requires os.fork (POSIX)")
        Path(self.mem).write_text("Seed entry.", encoding="utf-8")
        n_proc, n_each = 4, 5
        pids = []
        for p in range(n_proc):
            pid = os.fork()
            if pid == 0:
                try:
                    for i in range(n_each):
                        MA.append_to_memory(
                            _report([f"Fact proc {p} item {i} unique."]), self.mem, self.out)
                    os._exit(0)
                except BaseException:
                    os._exit(1)
            pids.append(pid)
        rcs = [os.waitpid(pid, 0)[1] for pid in pids]
        self.assertTrue(all(os.WIFEXITED(s) and os.WEXITSTATUS(s) == 0 for s in rcs), rcs)
        ents = self.entries()
        self.assertIn("Seed entry.", ents)
        for p in range(n_proc):
            for i in range(n_each):
                self.assertIn(f"Fact proc {p} item {i} unique.", ents,
                              "a concurrent append was lost (lock/atomicity broke)")
        self.assertEqual(len(ents), 1 + n_proc * n_each)


class TestAppendSymlink(Base):
    """SAFETY-1 (HIGH, adversarial review) — under a symlinked MEMORY.md the flock
    must be on the LOGICAL path so it serializes vs the curator/gateway, and the
    write must reach the real target through the link."""

    def test_append_locks_logical_path_under_symlink(self):
        realdir = os.path.join(self.tmp, "realstore")
        os.makedirs(realdir)
        realfile = os.path.join(realdir, "MEMORY_real.md")
        Path(realfile).write_text("Seed.", encoding="utf-8")
        os.symlink(realfile, self.mem)            # MEMORY.md -> realstore/MEMORY_real.md
        MA.append_to_memory(_report(["User prefers vim."]), self.mem, self.out)
        self.assertTrue(os.path.exists(self.mem + ".lock"),
                        "did not lock the LOGICAL MEMORY.md.lock (won't serialize vs peers)")
        self.assertFalse(os.path.exists(realfile + ".lock"),
                         "locked the realpath -> different inode than peers -> no mutual exclusion")
        self.assertTrue(os.path.islink(self.mem), "symlink must be preserved")
        self.assertIn("User prefers vim.", Path(realfile).read_text(encoding="utf-8"))


class TestHomeResolution(Base):
    """INTEG-2 / EXPORT-10 regression — $HERMES_HOME alone, no HOME override."""

    def test_home_resolution_ignores_real_home(self):
        fake = self.home
        Path(self.mem).write_text("Sentinel entry only in HERMES_HOME.", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HERMES_HOME": fake}):
            self.assertEqual(gate.resolve_home(), Path(fake))
            self.assertEqual(gate.memory_file(), Path(fake) / "memories" / "MEMORY.md")
            self.assertIn("Sentinel entry only in HERMES_HOME.", gate.read_existing_entries())
            p = MA.resolve_paths()
            self.assertEqual(p["memory_file"], str(Path(fake) / "memories" / "MEMORY.md"))
            self.assertEqual(p["state_db"], str(Path(fake) / "state.db"))
            self.assertEqual(p["out_dir"], str(Path(fake) / "memories" / "_auto_extract"))
        # genuinely NOT the real ~/.hermes
        self.assertNotEqual(os.path.realpath(fake),
                            os.path.realpath(os.path.expanduser("~/.hermes")))

    def test_cli_home_flag_overrides_env(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/tmp/should-not-win"}):
            p = MA.resolve_paths(self.home)
            self.assertEqual(p["memory_file"], str(Path(self.home) / "memories" / "MEMORY.md"))


class TestModelReachability(Base):
    """UX-2 — a model we cannot reach must be LOUD and non-zero, never a silent
    '0 facts, all ok'. A healthy run that simply finds nothing stays exit 0."""

    FIXTURES = os.path.join(SCRIPTS, "memory_auto_extract_fixtures.jsonl")

    def _run_main(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = MA.main(["--fixtures", self.FIXTURES, "--home", self.home, "--out", self.out])
        return rc, buf.getvalue()

    def test_model_unreachable_exits_2_and_is_loud(self):
        self.addCleanup(setattr, MA, "call_llm", MA.call_llm)
        self.addCleanup(MA.CONFIG.__setitem__, "require_signal_word", MA.CONFIG["require_signal_word"])
        MA.CONFIG["require_signal_word"] = False
        MA.call_llm = lambda t, debug=False: ([], "llm_error: simulated connection refused")
        rc, out = self._run_main()
        self.assertEqual(rc, 2, "model-unreachable run must exit non-zero")
        self.assertIn("COULD NOT REACH THE MODEL", out)
        self.assertNotIn("✅ ACCEPTED: none", out, "must not show the clean 'found nothing' marker")

    def test_healthy_empty_run_exits_0(self):
        self.addCleanup(setattr, MA, "call_llm", MA.call_llm)
        self.addCleanup(MA.CONFIG.__setitem__, "require_signal_word", MA.CONFIG["require_signal_word"])
        MA.CONFIG["require_signal_word"] = False
        MA.call_llm = lambda t, debug=False: ([], "")   # healthy: reachable, just no facts
        rc, out = self._run_main()
        self.assertEqual(rc, 0, "a healthy run that finds nothing must exit 0")
        self.assertNotIn("COULD NOT REACH", out)
        self.assertIn("✅ ACCEPTED: none", out)

    def test_malformed_fixtures_clean_error(self):
        # UX-3 (load_fixtures): a bad --fixtures line must exit 2 with a message, not traceback.
        import contextlib
        import io
        bad = os.path.join(self.tmp, "bad_fixtures.jsonl")
        Path(bad).write_text('{"session_id":"ok","messages":[]}\n{ this is not json\n', encoding="utf-8")
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(buf):
                MA.main(["--fixtures", bad, "--home", self.home, "--out", self.out])
        self.assertEqual(cm.exception.code, 2, "malformed --fixtures must exit 2 (not a traceback)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
