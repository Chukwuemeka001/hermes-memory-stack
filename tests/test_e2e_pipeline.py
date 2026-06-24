#!/usr/bin/env python3
"""End-to-end pipeline test — the SHIPPING GATE for the Hermes memory stack.

Builds a synthetic *messy* Hermes home (tests/synthetic_profile.py) and drives the
complete Area 1→5 pipeline through the REAL CLIs (subprocess, not imports),
asserting expectations at every step plus rollback, temporal reconstruction, and
failure tolerance.

Everything happens in a temp dir; live ~/.hermes is never touched. The profile is
cleaned up on success and KEPT on failure for debugging.

Run:
    python3 -m unittest tests.test_e2e_pipeline -v
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
# Single source of truth for the maintenance step order (no triple-hardcoding/drift).
from memory_maintenance import STEP_ORDER as MM_STEP_ORDER  # noqa: E402

DELIM = "\n§\n"

# Structural live-data backstop: every subprocess gets HERMES_HOME (and HOME, the
# secondary `~/.hermes` fallback) pointed at an empty sentinel dir. Every CLI call
# below ALSO passes an explicit --home/--db, so this is belt-and-suspenders — but it
# means a future dropped flag degrades to a harmless no-op on an empty dir instead of
# ever reaching the real ~/.hermes. Isolation becomes structural, not convention-only.
_SENTINEL_HOME = tempfile.mkdtemp(prefix="e2e_sentinel_home_")
atexit.register(shutil.rmtree, _SENTINEL_HOME, ignore_errors=True)


def script(name):
    return os.path.join(SCRIPTS, name)


def run(*args, check=False):
    """Run a real CLI via subprocess (tests the shipped entrypoints)."""
    env = {**os.environ, "HERMES_HOME": _SENTINEL_HOME, "HOME": _SENTINEL_HOME}
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True,
                       timeout=600, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"command failed rc={r.returncode}: {' '.join(map(str, args))}\n"
                             f"STDOUT:{r.stdout[-800:]}\nSTDERR:{r.stderr[-800:]}")
    return r


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def entries(text):
    return [e.strip() for e in text.split(DELIM) if e.strip()]


# --------------------------------------------------------------------------- #
# Full pipeline (ordered steps, shared profile)                               #
# --------------------------------------------------------------------------- #
class TestE2EPipeline(unittest.TestCase):
    """Steps run in name order on ONE messy profile (a stateful pipeline)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="e2e_pipeline_")
        cls.info = synthetic_profile.build_profile(cls.root, seed=42)
        cls.mem = os.path.join(cls.root, "memories", "MEMORY.md")
        cls.usr = os.path.join(cls.root, "memories", "USER.md")
        cls.db = os.path.join(cls.root, "state.db")
        cls.ctx = {}
        cls._done_names = set()

    @classmethod
    def tearDownClass(cls):
        expected = {m for m in dir(cls) if m.startswith("test_")}
        if cls._done_names >= expected:
            shutil.rmtree(cls.root, ignore_errors=True)
        else:
            print(f"\n[E2E] kept profile for debugging: {cls.root} "
                  f"(incomplete: {sorted(expected - cls._done_names)})")

    def setUp(self):
        # The steps are an ordered, stateful pipeline (unittest runs methods in name
        # order, sharing cls.ctx). Fail loudly + clearly if a single late step is run
        # in isolation or a test randomizer reorders them, instead of a cryptic KeyError.
        if self._testMethodName != "test_00_initial_state" and "init_db_bytes" not in type(self).ctx:
            self.skipTest("E2E steps are an ordered pipeline; run the whole TestE2EPipeline "
                          "class together (incompatible with pytest-randomly / -k single-step).")

    def _done(self):
        type(self)._done_names.add(self._testMethodName)

    def audit_json(self, mem=None, usr=None):
        r = run(script("memory_audit.py"), "--memory", mem or self.mem,
                "--user", usr or self.usr, "--home", self.root, "--user-home", self.root,
                "--json", check=True)
        return json.loads(r.stdout)

    # -- Step 0: initial snapshot ---------------------------------------- #
    def test_00_initial_state(self):
        a = self.audit_json()
        mem = next(f for f in a["files"] if f["store"] == "memory")
        usr = next(f for f in a["files"] if f["store"] == "user")
        self.ctx["init_mem_chars"] = mem["char_count"]
        self.ctx["init_mem_entries"] = mem["entry_count"]
        self.ctx["init_mem_cap"] = mem["capacity_pct"]
        self.ctx["init_db_bytes"] = os.path.getsize(self.db)
        # planted to be messy
        self.assertGreaterEqual(mem["char_count"], 13500, "MEMORY should be ~full")
        self.assertGreaterEqual(mem["entry_count"], 40)
        self.assertGreaterEqual(mem["capacity_pct"], 85)
        self.assertGreaterEqual(usr["capacity_pct"], 90)
        self.assertGreaterEqual(os.path.getsize(self.db) / (1024 * 1024), 5.0, "state.db >=5MB")
        self._done()

    # -- Step 1: Area 1 state.db audit -> simulate -> apply -------------- #
    def test_01_area1_state_db(self):
        # audit discovers the DB
        a = run(script("state_db_remediate.py"), "audit", "--home", self.root, "--json", check=True)
        adb = json.loads(a.stdout)
        self.assertTrue(any(d["path"].endswith("state.db") and d.get("is_session_db")
                            for d in adb["databases"]))
        # plan: drop trigram + prune closed/unclosed + delete compression parents + vacuum
        policy = os.path.join(self.root, "policy.json")
        run(script("state_db_remediate.py"), "plan", "--db", self.db, "--retention-days", "90",
            "--prune-closed", "yes", "--prune-unclosed", "yes",
            "--delete-compression-parents", "yes", "--drop-trigram", "yes",
            "--vacuum", "yes", "--out", policy, check=True)
        self.assertTrue(os.path.exists(policy))
        # simulate on a copy: integrity OK + >40% reduction
        sim = run(script("state_db_remediate.py"), "simulate", "--db", self.db,
                  "--policy", policy, "--workdir", os.path.join(self.root, "sim"), "--json", check=True)
        s = json.loads(sim.stdout)
        self.assertTrue(s["integrity_after"]["ok"], "post-clean integrity must pass")
        reduction = 1 - s["after_bytes"] / s["before_bytes"]
        # Spec floor is >40%; the trigram index alone is ~half the file, so a healthy
        # full clean reclaims ~85%. Gate at 0.60 to catch a regression that silently
        # drops a major reclaimer (trigram / compression-parent delete) yet still
        # clears 40% on vacuum+prune alone.
        self.assertGreater(reduction, 0.60, f"state.db should shrink substantially (got {reduction:.1%})")
        self.ctx["db_sim_reduction"] = reduction
        # apply (synthetic DB is quiescent): archive + atomic swap + post integrity
        ap = run(script("state_db_remediate.py"), "apply", "--db", self.db, "--policy", policy,
                 "--archive-dir", os.path.join(self.root, "db-archive"), "--confirm-apply",
                 "--json", check=True)
        res = json.loads(ap.stdout)
        self.assertTrue(res["applied"], f"apply must succeed: {res.get('errors')}")
        self.assertTrue(res["post_swap_integrity"]["ok"])
        after = os.path.getsize(self.db)
        self.assertGreater(1 - after / self.ctx["init_db_bytes"], 0.60, "live state.db shrank substantially")
        # archive exists with a manifest + hashed members
        self.assertTrue(os.path.isdir(res["archive"]["dir"]))
        self.assertTrue(any(f["sha256"] and len(f["sha256"]) == 64 for f in res["archive"]["files"]))
        self._done()

    # -- Step 2: Area 2 audit detects the planted problems -------------- #
    def test_02_area2_audit_detects(self):
        a = self.audit_json()
        s = a["summary"]
        from collections import Counter
        kinds = Counter(e["kind"] for f in a["files"] for e in f["entries"])
        self.assertGreaterEqual(kinds["content_dump"], 5, "detect >=5 content dumps")
        self.assertGreaterEqual(s["duplicate_pairs"], 2, "detect >=2 duplicate pairs")
        self.assertGreaterEqual(s["contradiction_pairs"], 2, "detect >=2 contradictions")
        self.assertGreaterEqual(kinds["status_update"], 3, "detect >=3 status updates")
        self.assertGreaterEqual(len(s["broken_pointers"]), 1, "detect >=1 broken pointer")
        self.assertGreaterEqual(kinds["pointer"], 5, "already-curated ↪ pointers classified as pointers")
        self.ctx["area2"] = {"dumps": kinds["content_dump"], "dups": s["duplicate_pairs"],
                             "contradictions": s["contradiction_pairs"],
                             "status": kinds["status_update"], "broken": len(s["broken_pointers"])}
        self._done()

    # -- Step 3: Area 3 render -> proposed smaller + re-audits clean ----- #
    def test_03_area3_render(self):
        prop = os.path.join(self.root, "proposed")
        run(script("memory_rewrite.py"), "render", "--home", self.root, "--user-home", self.root,
            "--out-dir", prop, check=True)
        pm = os.path.join(prop, "MEMORY.proposed.md")
        pu = os.path.join(prop, "USER.proposed.md")
        self.assertTrue(os.path.exists(pm) and os.path.exists(pu))
        self.assertTrue(os.path.exists(os.path.join(prop, "manifest.json")))
        # proposed re-audits cleanly + under capacity targets
        a = self.audit_json(mem=pm, usr=pu)
        self.assertFalse(any(f.get("errors") for f in a["files"]), "proposed must parse cleanly")
        pmcap = next(f for f in a["files"] if f["store"] == "memory")["capacity_pct"]
        pucap = next(f for f in a["files"] if f["store"] == "user")["capacity_pct"]
        self.assertLess(pmcap, 70, f"proposed MEMORY <70% (got {pmcap})")
        self.assertLess(pucap, 85, f"proposed USER <85% (got {pucap})")
        # ...and the drop must be MATERIAL, not a token nibble — guards against an
        # Area-3 regression that under-compresses yet still squeaks under 70%.
        self.assertLess(pmcap, self.ctx["init_mem_cap"] - 15,
                        f"Area 3 must materially compress MEMORY "
                        f"(init {self.ctx['init_mem_cap']}% -> proposed {pmcap}%)")
        self.ctx["proposed_cap"] = {"memory": pmcap, "user": pucap}
        self._done()

    # -- Step 4: Area 3 apply -> live changed + recoverable archive ------ #
    def test_04_area3_apply(self):
        before = (sha(self.mem), sha(self.usr))
        self.ctx["pre_apply_mem"] = read(self.mem)
        arch = os.path.join(self.root, "mem-archive")
        run(script("memory_rewrite.py"), "apply", "--home", self.root, "--user-home", self.root,
            "--archive-dir", arch, "--confirm-apply", check=True)
        after = (sha(self.mem), sha(self.usr))
        self.assertNotEqual(before, after, "live hot files must change after apply")
        # archive: pre-rewrite originals + a manifest exist
        files = os.listdir(arch)
        pre = [f for f in files if "MEMORY.md.pre-rewrite" in f]
        man = [f for f in files if f.startswith("rewrite-manifest")]
        self.assertTrue(pre, "pre-rewrite MEMORY archive must exist")
        self.assertTrue(man, "rewrite manifest must exist")
        self.ctx["mem_archive_dir"] = arch
        # Area 3 ITSELF must collapse the genuine duplicate pairs (the paraphrase pair A
        # and the near-exact pair B) — NOT the later human step. Asserting here pins the
        # dedup result to the pipeline so test_08's "0 duplicates" can't be a side effect
        # of the human contradiction edit.
        post = self.audit_json()
        self.assertLessEqual(post["summary"]["duplicate_pairs"], self.ctx["area2"]["dups"] - 2,
                             f"Area 3 apply must merge the true duplicate pairs "
                             f"(was {self.ctx['area2']['dups']}, now {post['summary']['duplicate_pairs']})")
        self._done()

    # -- Step 5: human review resolves the FLAGGED contradictions -------- #
    def test_05_human_resolution(self):
        # Area 3 flags contradictions `user_review` and NEVER auto-resolves them, so the
        # cleaned final state requires a human gate. Simulate it deterministically over
        # the SET of entries involved in any contradiction (the audit emits cross-product
        # pairs, so per-pair logic double-counts): keep every entry that asserts the
        # CURRENT state ("now"/"currently"), drop the stale remainder.
        a = self.audit_json()
        memf = next(f for f in a["files"] if f["store"] == "memory")
        txt = {e["ref"]: e["text"] for e in memf["entries"]}
        involved = {ref for c in a["contradiction_pairs"] for ref in (c["a"], c["b"])
                    if ref.startswith("memory#")}
        self.assertTrue(involved, "there must be flagged contradictions to resolve")
        RECENT = ("now", "currently", "current")
        drop = {txt[r].strip() for r in involved
                if not any(w in txt[r].lower() for w in RECENT)}
        kept = [seg for seg in entries(read(self.mem)) if seg.strip() not in drop]
        with open(self.mem, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(kept))
        # Correctness, not just count: the human KEEPS the current values and DROPS the
        # stale ones (the old test could drop the wrong/current side and still pass).
        live = read(self.mem)
        for current in ("Bar-9000 is now the default coding model.",
                        "Beta is now the default trading provider.",
                        "E2 is now the default embedding backend."):
            self.assertIn(current, live, "must keep the CURRENT side of each contradiction")
        for stale in ("Default coding model is Foo-7B.",
                      "Default trading provider is Alpha.",
                      "Default embedding backend is E1."):
            self.assertNotIn(stale, live, "must drop the STALE side of each contradiction")
        # ...and the resolution actually clears the contradictions (verified, not assumed)
        self.assertEqual(self.audit_json()["summary"]["contradiction_pairs"], 0,
                         "human resolution must leave 0 contradictions")
        self.ctx["human_removed"] = len(drop)
        self._done()

    # -- Step 6: Area 4 temporal sync -> verify ALL MATCH ---------------- #
    def test_06_area4_temporal(self):
        # fresh user (empty _versions): first migration captures the cleaned state
        sy = run(script("temporal_migrate_onboard.py"), "sync", "--home", self.root,
                 "--confirm-apply", "--json", check=True)
        self.assertTrue(json.loads(sy.stdout)["applied"])
        vr = run(script("temporal_migrate_onboard.py"), "verify", "--home", self.root, "--json")
        v = json.loads(vr.stdout)
        self.assertTrue(v["all_match"], f"temporal must reconstruct live exactly: "
                        f"{[(k, s['exact_match']) for k, s in v['stores'].items()]}")
        self.assertGreater(v["facts"], 0)
        self.assertGreater(v["versions"], 0)
        self.assertEqual(vr.returncode, 0, "verify exit 0 when ALL MATCH")
        self.ctx["temporal"] = {"facts": v["facts"], "versions": v["versions"]}
        self._done()

    # -- Step 7: Area 5 maintenance pass -------------------------------- #
    def test_07_area5_maintenance(self):
        m = run(script("memory_maintenance.py"), "--home", self.root, "--user-home", self.root,
                "--dry-run", "--json", check=True)
        rep = json.loads(m.stdout)
        self.assertTrue(rep["hot_files_untouched"], "maintenance must not touch hot files")
        self.assertEqual([s["step"] for s in rep["steps"]],
                         ["temporal_sync", "temporal_verify", "auto_extract", "audit",
                          "state_db_remediate", "capacity"])
        self.assertIn(rep["overall"], ("green", "yellow"), "should not be red after cleanup")
        # the state_db step ran and, since step 1 already cleaned the DB, reports ok
        sdb = next(s for s in rep["steps"] if s["step"] == "state_db_remediate")
        self.assertEqual(sdb["status"], "ok", "cleaned state.db should be within thresholds")
        # health exits 0 even when alerting (the exit-convention guarantee)
        h = run(script("memory_health.py"), "--home", self.root, "--json", check=True)
        self.assertEqual(h.returncode, 0, "health exits 0 even when alerting")
        self.ctx["maint_overall"] = rep["overall"]
        self._done()

    # -- Step 8: final state matches the clean targets ------------------ #
    def test_08_final_state(self):
        a = self.audit_json()
        s = a["summary"]
        mem = next(f for f in a["files"] if f["store"] == "memory")
        usr = next(f for f in a["files"] if f["store"] == "user")
        self.assertLessEqual(mem["capacity_pct"], 70, f"final MEMORY <=70% (got {mem['capacity_pct']})")
        self.assertLessEqual(usr["capacity_pct"], 85, f"final USER <=85% (got {usr['capacity_pct']})")
        self.assertEqual(s["duplicate_pairs"], 0, "final 0 duplicates")
        self.assertEqual(s["contradiction_pairs"], 0, "final 0 contradictions")
        self.assertLessEqual(mem["entry_count"], 35, f"final entries <=35 (got {mem['entry_count']})")
        # the already-curated ↪ pointers survived the whole pipeline untouched
        self.assertGreaterEqual(read(self.mem).count("↪"), 5,
                                "curated ↪ pointers must be preserved through the pipeline")
        self.ctx["final"] = {"mem_cap": mem["capacity_pct"], "user_cap": usr["capacity_pct"],
                             "entries": mem["entry_count"]}
        self._done()

    # -- Step 9: rollback — archived originals are recoverable ---------- #
    def test_09_rollback(self):
        arch = self.ctx["mem_archive_dir"]
        pre = [os.path.join(arch, f) for f in os.listdir(arch) if "MEMORY.md.pre-rewrite" in f]
        self.assertTrue(pre)
        archived = read(pre[0])
        # spot-check a known original entry survives in the archive (it was removed live)
        self.assertIn("Default coding model is Foo-7B", archived,
                      "a known original entry must be recoverable from the archive")
        # SHA in the manifest matches the archived original bytes
        man = [os.path.join(arch, f) for f in os.listdir(arch) if f.startswith("rewrite-manifest")][0]
        manifest = json.loads(read(man))
        src_sha = manifest["source"]["memory"]["sha256"]
        self.assertEqual(src_sha, sha(pre[0]), "manifest SHA-256 must match the archived original")
        # and it matches the pre-apply content we captured live
        self.assertEqual(sha(pre[0]), hashlib.sha256(self.ctx["pre_apply_mem"].encode()).hexdigest())
        self._done()

    # -- Step 10: temporal reconstruction holds + history accumulates --- #
    def test_10_temporal_reconstruction(self):
        # (a) reconstruction of the current cleaned state is byte-exact
        v = json.loads(run(script("temporal_migrate_onboard.py"), "verify",
                           "--home", self.root, "--json").stdout)
        self.assertTrue(v["all_match"], "temporal reconstruct == live for both files")
        for store, st in v["stores"].items():
            self.assertTrue(st["exact_match"], f"{store} must reconstruct byte-exact")
        self.assertGreaterEqual(v["facts"], 20)
        versions0 = v["versions"]
        # (b) mutate one entry, re-sync, and prove the layer is LOAD-BEARING, not a
        # trivial first-migration snapshot: history must grow (append-only) AND the new
        # state must still reconstruct byte-exact. (`versions >= facts` on a first
        # migration is vacuous — this drives an actual second version.)
        segs = entries(read(self.mem))
        segs[-1] = segs[-1] + " (revised in a later session)"
        with open(self.mem, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(segs))
        run(script("temporal_migrate_onboard.py"), "sync", "--home", self.root,
            "--confirm-apply", "--json", check=True)
        v2 = json.loads(run(script("temporal_migrate_onboard.py"), "verify",
                            "--home", self.root, "--json").stdout)
        self.assertTrue(v2["all_match"], "reconstruction must hold after an edit + re-sync")
        self.assertGreater(v2["versions"], versions0,
                           "a re-sync after a content change must append a new version")
        self._done()


# --------------------------------------------------------------------------- #
# Failure tolerance / graceful degradation (independent, lightweight)         #
# --------------------------------------------------------------------------- #
class TestFailureTolerance(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="e2e_fail_")
        os.makedirs(os.path.join(self.root, "memories"), exist_ok=True)
        with open(os.path.join(self.root, "memories", "MEMORY.md"), "w") as fh:
            fh.write(DELIM.join(["Header notes live in ~/.hermes/notes/.",
                                 "User prefers blunt correction, always.",
                                 "Gateway fixed on 2026-01-01: now works, build passes."]))
        with open(os.path.join(self.root, "memories", "USER.md"), "w") as fh:
            fh.write("Emeka is money-minded; ships with guardrails.")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_maintenance_skip_step_others_complete(self):
        m = run(script("memory_maintenance.py"), "--home", self.root, "--user-home", self.root,
                "--skip", "audit", "--json", check=True)
        rep = json.loads(m.stdout)
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertEqual(by["audit"], "skipped")
        self.assertIn(by["capacity"], ("ok", "alert"))  # others still ran
        self.assertEqual(m.returncode, 0)

    def test_maintenance_no_temporal_layer_degrades(self):
        # no _versions/ and no memory_versions.db -> temporal steps degrade, pass still runs
        m = run(script("memory_maintenance.py"), "--home", self.root, "--user-home", self.root,
                "--json")
        self.assertEqual(m.returncode, 0, "maintenance exits 0 even with no temporal layer")
        rep = json.loads(m.stdout)
        self.assertTrue(rep["hot_files_untouched"])
        by = {s["step"]: s["status"] for s in rep["steps"]}
        self.assertIn(by["temporal_sync"], ("skipped", "ok", "alert"))
        self.assertEqual(len(rep["steps"]), len(MM_STEP_ORDER))

    def test_health_on_minimal_home_exits_zero(self):
        h = run(script("memory_health.py"), "--home", self.root, "--json")
        self.assertEqual(h.returncode, 0)
        self.assertIn(json.loads(h.stdout)["overall"], ("green", "yellow", "red"))


# --------------------------------------------------------------------------- #
# Stress pipeline — the break-a-naive-system profile (--level stress)          #
# --------------------------------------------------------------------------- #
class TestStressPipeline(unittest.TestCase):
    """Drive the full Area 1→5 pipeline against a DRAMATICALLY worse profile.
    Floors (asserted): 200+ MEMORY entries / 40k+ chars, USER 12k+ chars, a
    50 MB+ / 100+ session / 5 000+ message state.db. A seed-42 build lands ~211
    entries / ~67k chars (~4x over the 15k budget), USER ~3x over budget. Same
    gates as the normal E2E, harder inputs. KEY assertions: MEMORY comes down from
    40k+ chars to under the 15k budget, duplicates -> 0, contradictions -> 0.

    Built under a SHORT temp path on purpose: Area 3 archives each dump/debugging
    finding into a ~280-char findable breadcrumb that embeds the absolute note path,
    so a long /var/folders/... root would inflate the budget measurement by ~2 KB and
    make the "under budget in one pass" assertion path-length-dependent. A short root
    keeps the assertion about the pipeline, not the temp dir.
    """

    @classmethod
    def setUpClass(cls):
        base = "/tmp" if (os.path.isdir("/tmp") and os.access("/tmp", os.W_OK)) else None
        cls.root = tempfile.mkdtemp(prefix="hms_s", dir=base)
        cls.info = synthetic_profile.build_profile(cls.root, seed=42, level="stress")
        cls.mem = os.path.join(cls.root, "memories", "MEMORY.md")
        cls.usr = os.path.join(cls.root, "memories", "USER.md")
        cls.db = os.path.join(cls.root, "state.db")
        cls.ok = False

    @classmethod
    def tearDownClass(cls):
        if cls.ok:
            shutil.rmtree(cls.root, ignore_errors=True)
        else:
            print(f"\n[E2E-stress] kept profile for debugging: {cls.root}")

    def audit(self, mem=None, usr=None):
        r = run(script("memory_audit.py"), "--memory", mem or self.mem, "--user", usr or self.usr,
                "--home", self.root, "--user-home", self.root, "--json", check=True)
        return json.loads(r.stdout)

    def budget(self, file_path):
        """(raw_chars, path_normalized_chars). Normalized subtracts the temp-root
        portion of every embedded absolute note path, so the budget check measures
        the PIPELINE's compression rather than the (arbitrary) temp-dir length — the
        on-disk size in a real ~/.hermes (short root) tracks the normalized value."""
        text = read(file_path)
        return len(text), len(text) - text.count(self.root) * len(self.root)

    def test_stress_pipeline(self):
        from collections import Counter
        info = self.info
        # -- Step 0: the input really is a break-a-naive-system mess ------------ #
        self.assertGreaterEqual(info["memory_entries"], 200, "200+ MEMORY entries")
        self.assertGreaterEqual(info["memory_chars"], 40000, "MEMORY 40k+ chars (>>15k budget)")
        self.assertGreaterEqual(info["user_chars"], 12000, "USER 12k+ chars (>>6k budget)")
        self.assertGreaterEqual(info["state_db"]["sessions"], 100, "100+ sessions")
        self.assertGreaterEqual(info["state_db"]["messages"], 5000, "5000+ messages")
        self.assertGreaterEqual(info["state_db_mb"], 50.0, "state.db 50MB+")
        a0 = self.audit()
        s0 = a0["summary"]
        self.assertGreaterEqual(s0["duplicate_pairs"], 15, "15+ duplicate pairs detected")
        self.assertGreaterEqual(s0["contradiction_pairs"], 10, "10+ contradictions detected")
        # Per-CATEGORY floors — catch a builder shortfall or a classifier drift that the
        # coarse aggregates would miss (e.g. debugging findings collapsing into prefs, or
        # projects collapsing into a dup cluster). Floors sit below the planted counts.
        kinds = Counter(e["kind"] for f in a0["files"] for e in f["entries"])
        self.assertGreaterEqual(kinds["content_dump"], 18, "content dumps present in force")
        self.assertGreaterEqual(kinds["status_update"], 25, "status updates present in force")
        self.assertGreaterEqual(kinds["debugging_finding"], 10, "debugging findings classified correctly")
        self.assertGreaterEqual(kinds["project_progress"], 8, "project-progress entries (not dup-collapsed)")
        self.assertGreaterEqual(kinds["pointer"], 12, "curated pointers present")
        self.assertGreaterEqual(kinds["todo_temporary"], 5, "todos present")
        self.assertGreaterEqual(len(s0["broken_pointers"]), 3, "broken pointers present")
        init_mem_chars = next(f for f in a0["files"] if f["store"] == "memory")["char_count"]

        # -- Step 1: Area 1 state.db -> integrity-checked big shrink ------------ #
        policy = os.path.join(self.root, "policy.json")
        run(script("state_db_remediate.py"), "plan", "--db", self.db, "--retention-days", "90",
            "--prune-closed", "yes", "--prune-unclosed", "yes",
            "--delete-compression-parents", "yes", "--drop-trigram", "yes",
            "--vacuum", "yes", "--out", policy, check=True)
        sim = json.loads(run(script("state_db_remediate.py"), "simulate", "--db", self.db,
                             "--policy", policy, "--workdir", os.path.join(self.root, "sim"),
                             "--json", check=True).stdout)
        self.assertTrue(sim["integrity_after"]["ok"])
        self.assertGreater(1 - sim["after_bytes"] / sim["before_bytes"], 0.40,
                           "bloated state.db should shrink >40%")
        before_db = os.path.getsize(self.db)
        ap = json.loads(run(script("state_db_remediate.py"), "apply", "--db", self.db,
                            "--policy", policy, "--archive-dir", os.path.join(self.root, "dbarch"),
                            "--confirm-apply", "--json", check=True).stdout)
        self.assertTrue(ap["applied"] and ap["post_swap_integrity"]["ok"])
        self.assertGreater(1 - os.path.getsize(self.db) / before_db, 0.40, "live state.db shrank >40%")

        # -- Step 2: Area 3 render -> proposed UNDER the 15k budget (KEY) ------- #
        prop = os.path.join(self.root, "proposed")
        run(script("memory_rewrite.py"), "render", "--home", self.root, "--user-home", self.root,
            "--out-dir", prop, check=True)
        pa = self.audit(mem=os.path.join(prop, "MEMORY.proposed.md"),
                        usr=os.path.join(prop, "USER.proposed.md"))
        self.assertFalse(any(f.get("errors") for f in pa["files"]), "proposed must parse cleanly")
        _praw, pnorm = self.budget(os.path.join(prop, "MEMORY.proposed.md"))
        self.assertLess(pnorm, 15000,
                        f"KEY: proposed MEMORY must come under the 15k budget (got {pnorm})")

        # -- Step 3: Area 3 apply + human resolution of flagged contradictions -- #
        run(script("memory_rewrite.py"), "apply", "--home", self.root, "--user-home", self.root,
            "--archive-dir", os.path.join(self.root, "memarch"), "--confirm-apply", check=True)
        a = self.audit()
        # Area 3 ITSELF must merge the bulk of the genuine duplicates — pin this BEFORE the
        # human edit so "dups -> 0" can't be credited to the human step (the residual after
        # apply is the contradiction-derived dups, which the human resolution then clears).
        self.assertLessEqual(a["summary"]["duplicate_pairs"], s0["duplicate_pairs"] - 15,
                             f"Area 3 apply must merge the bulk of duplicates "
                             f"(input {s0['duplicate_pairs']} -> post-apply {a['summary']['duplicate_pairs']})")
        txt = {e["ref"]: e["text"] for e in next(f for f in a["files"] if f["store"] == "memory")["entries"]}
        involved = {ref for c in a["contradiction_pairs"] for ref in (c["a"], c["b"])
                    if ref.startswith("memory#")}
        RECENT = ("now", "currently", "current")
        drop = {txt[r].strip() for r in involved if not any(w in txt[r].lower() for w in RECENT)}
        kept = [seg for seg in entries(read(self.mem)) if seg.strip() not in drop]
        with open(self.mem, "w", encoding="utf-8") as fh:
            fh.write(DELIM.join(kept))

        # -- Step 4: FINAL gates (the user's KEY assertions) ------------------- #
        fa = self.audit()
        fs = fa["summary"]
        fm = next(f for f in fa["files"] if f["store"] == "memory")
        fu = next(f for f in fa["files"] if f["store"] == "user")
        fraw, fnorm = self.budget(self.mem)
        self.assertLess(fnorm, 15000, f"KEY: final MEMORY under the 15k budget (got {fnorm})")
        if len(self.root) < 30:  # short root -> the literal on-disk size is also a faithful check
            self.assertLess(fraw, 15000, f"final MEMORY under 15k on disk (got {fraw})")
        self.assertLess(fu["char_count"], 6000, f"final USER under the 6k budget (got {fu['char_count']})")
        self.assertEqual(fs["duplicate_pairs"], 0, "KEY: duplicates resolved to 0")
        self.assertEqual(fs["contradiction_pairs"], 0, "KEY: contradictions resolved to 0")
        self.assertLess(fnorm, init_mem_chars * 0.5, "MEMORY at least halved")
        self.assertGreaterEqual(read(self.mem).count("↪"), 5, "curated ↪ pointers preserved")
        # Entry count: a SINGLE pass legitimately cannot reach the 35-entry ceiling on a
        # stress home — every survivor is kept content (prefs/pointers/projects/breadcrumbs),
        # 0 removals. We pin the achievable band (catches entry-splitting / under-compression
        # the char gate would miss) and acknowledge the over-ceiling state as the honest
        # single-pass signal (the stack's own health tool rates this home 'red' on entries).
        self.assertGreater(fm["entry_count"], 35, "single pass stays over the 35-entry ceiling (by design)")
        self.assertLess(fm["entry_count"], 130, f"final entry count bounded (got {fm['entry_count']})")

        # -- Step 5: Area 4 temporal reconstruction byte-exact on the big file -- #
        self.assertTrue(json.loads(run(script("temporal_migrate_onboard.py"), "sync", "--home",
                        self.root, "--confirm-apply", "--json", check=True).stdout)["applied"])
        v = json.loads(run(script("temporal_migrate_onboard.py"), "verify", "--home", self.root,
                           "--json").stdout)
        self.assertTrue(v["all_match"], "temporal must reconstruct the cleaned stress files byte-exact")

        # -- Step 6: Area 5 maintenance survives the cleaned stress home -------- #
        rep = json.loads(run(script("memory_maintenance.py"), "--home", self.root, "--user-home",
                             self.root, "--dry-run", "--json", check=True).stdout)
        self.assertTrue(rep["hot_files_untouched"])
        # Real outcome assertions (not just rc/untouched): all 6 steps ran in order, the
        # cleaned state.db is within thresholds, and the pass is not red. (capacity/audit
        # still alert on the over-ceiling entry count — that's the honest single-pass state.)
        self.assertEqual([s["step"] for s in rep["steps"]], MM_STEP_ORDER)
        self.assertIn(rep["overall"], ("green", "yellow"), "cleaned home must not be red to maintenance")
        sdb = next(s for s in rep["steps"] if s["step"] == "state_db_remediate")
        self.assertEqual(sdb["status"], "ok", "state.db cleaned in Step 1 -> within thresholds")
        # health exits 0 even though it rates the home red on entry count (the exit convention)
        self.assertEqual(run(script("memory_health.py"), "--home", self.root, "--json").returncode, 0,
                         "health exits 0 even when alerting")

        print(f"\n[stress] MEMORY {init_mem_chars}->{fm['char_count']}ch (norm {fnorm}) "
              f"({info['memory_entries']}->{fm['entry_count']}e), USER ->{fu['char_count']}ch, "
              f"dups {s0['duplicate_pairs']}->0, contra {s0['contradiction_pairs']}->0, "
              f"state.db {info['state_db_mb']}MB->{round(os.path.getsize(self.db) / 1048576, 1)}MB")
        type(self).ok = True


# --------------------------------------------------------------------------- #
# Cross-user isolation: $HERMES_HOME ALONE must route (INTEG-2 / EXPORT-10)    #
# --------------------------------------------------------------------------- #
class TestHermesHomeAloneRouting(unittest.TestCase):
    """Setting $HERMES_HOME ALONE — with $HOME pointed at a DECOY 'real home' and
    NO --home flag — must route the auto-extract tier's reads to $HERMES_HOME, never
    to the $HOME-based ~/.hermes. Exercises the REAL intake-gate CLI so cross-user
    isolation is guarded end-to-end.

    This test deliberately does NOT use the shared run() helper, which forces
    HOME==HERMES_HOME as a structural backstop; here we set them to DIFFERENT dirs
    so the routing is actually proven, not masked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e_homeres_")
        self.hh = os.path.join(self.tmp, "hermes_home")          # $HERMES_HOME target
        self.decoy = os.path.join(self.tmp, "decoy_real_home")   # $HOME (the "real" home)
        os.makedirs(os.path.join(self.hh, "memories"))
        os.makedirs(os.path.join(self.decoy, ".hermes", "memories"))
        os.makedirs(os.path.join(self.tmp, "empty", "memories"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gate(self, text, hermes_home):
        env = {**os.environ, "HERMES_HOME": hermes_home, "HOME": self.decoy}
        return subprocess.run(
            [sys.executable, script("hermes_memory_intake_gate.py"), "--json", text],
            capture_output=True, text=True, timeout=120, env=env)

    def test_intake_gate_routes_to_hermes_home_not_home(self):
        pref = "User prefers blunt ROI-focused correction over reassurance, always."
        # The near-duplicate lives ONLY in $HERMES_HOME; the decoy ~/.hermes differs.
        with open(os.path.join(self.hh, "memories", "MEMORY.md"), "w") as fh:
            fh.write(pref)
        # The DECOY ~/.hermes ALSO holds a near-duplicate, so if the gate ever wrongly
        # fell back to the $HOME-based ~/.hermes the control case would (wrongly) flag
        # a duplicate too — making the control assertion actually discriminating.
        with open(os.path.join(self.decoy, ".hermes", "memories", "MEMORY.md"), "w") as fh:
            fh.write(pref)
        # HERMES_HOME has the dup -> gate must SEE it -> REVIEW/near_duplicate (exit 2).
        r = self._gate(pref, self.hh)
        self.assertEqual(r.returncode, 2, f"expected REVIEW (read HERMES_HOME); stderr={r.stderr}")
        self.assertEqual(json.loads(r.stdout)["category"], "near_duplicate")
        # Control: HERMES_HOME points at an EMPTY home -> ALLOW (exit 0). Because the
        # decoy $HOME ~/.hermes DOES contain the near-dup, ALLOW here proves the gate
        # did NOT fall back to it (a fallback would have returned exit 2).
        r2 = self._gate(pref, os.path.join(self.tmp, "empty"))
        self.assertEqual(r2.returncode, 0, f"expected ALLOW (empty HERMES_HOME); stderr={r2.stderr}")
        self.assertNotEqual(json.loads(r2.stdout)["category"], "near_duplicate")


# --------------------------------------------------------------------------- #
# INTEG-3 / P3-2 — rewrite→temporal provenance bridge                         #
# --------------------------------------------------------------------------- #
class TestRewriteTemporalProvenance(unittest.TestCase):
    """memory_rewrite.apply() records the rewrite as `area3-rewrite` provenance into
    an EXISTING temporal layer (the wired INTEG-3 bridge), and is a safe no-op — never
    fabricating a layer — when none exists."""

    def _profile(self, seed):
        root = tempfile.mkdtemp(prefix="e2e_prov_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        synthetic_profile.build_profile(root, seed=seed)
        return root

    def test_apply_records_area3_provenance_into_seeded_layer(self):
        root = self._profile(seed=7)
        hist = os.path.join(root, "memories", "_versions", "history.jsonl")
        # 1) seed the temporal baseline from the ORIGINAL (pre-rewrite) files
        run(script("temporal_migrate_onboard.py"), "sync", "--home", root,
            "--confirm-apply", "--json", check=True)
        self.assertTrue(os.path.exists(hist), "seed must create the temporal history")
        self.assertNotIn("area3-rewrite", read(hist), "no rewrite events before apply")
        # 2) apply the Area 3 rewrite — the bridge must auto-record provenance
        arch = os.path.join(root, "rw-arch")
        r = run(script("memory_rewrite.py"), "apply", "--home", root, "--user-home", root,
                "--archive-dir", arch, "--confirm-apply", check=True)
        self.assertIn("temporal provenance:", r.stdout, "apply must report the recording")
        # 3) the temporal layer now carries `area3-rewrite` source events...
        ev = [json.loads(l) for l in read(hist).splitlines() if l.strip()]
        a3 = [e for e in ev if e.get("source") == "area3-rewrite"]
        self.assertTrue(a3, "apply must record area3-rewrite provenance events after the rewrite")
        # ...recorded as update/delete on the EXISTING baseline keys (the old→new chain)
        self.assertTrue(all(e["op"] in ("update", "delete") for e in a3),
                        "provenance events must update/tombstone the seeded baseline facts")
        # 4) and the layer still reconstructs live byte-exact after a reconcile sync
        run(script("temporal_migrate_onboard.py"), "sync", "--home", root,
            "--confirm-apply", "--json", check=True)
        v = json.loads(run(script("temporal_migrate_onboard.py"), "verify",
                           "--home", root, "--json").stdout)
        self.assertTrue(v["all_match"], f"temporal must reconstruct live exactly after apply+record: "
                        f"{[(k, s['exact_match']) for k, s in v['stores'].items()]}")

    def test_apply_without_layer_is_graceful_noop(self):
        root = self._profile(seed=9)
        arch = os.path.join(root, "rw-arch")
        # no temporal layer present -> apply must SUCCEED and skip provenance (not fabricate one)
        r = run(script("memory_rewrite.py"), "apply", "--home", root, "--user-home", root,
                "--archive-dir", arch, "--confirm-apply")
        self.assertEqual(r.returncode, 0, f"rewrite must succeed without a temporal layer: {r.stderr}")
        self.assertFalse(os.path.exists(os.path.join(root, "memory_versions.db")),
                         "apply must NOT create a temporal layer when none existed")
        self.assertIn("no temporal layer", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
