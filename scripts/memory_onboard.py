#!/usr/bin/env python3
"""Hermes memory onboarding — the single command that drives Areas 1→5.

ONE command takes a memory-overloaded Hermes home from messy → clean → handed off
to continuous maintenance. It is the shippable form of RUNBOOK.md: every step that
the runbook asks an operator to copy-paste in order, this driver runs in order,
passing each step's artifact to the next, gating every mutation behind an explicit
confirmation, and stopping cleanly on the first failure so nothing is left half-done.

WHAT IT RUNS (the same golden path as RUNBOOK.md / tests/test_e2e_pipeline.py):

  Area 1 — state.db cleanup
    1. audit state.db                         (read-only)
    2. plan + simulate a cleanup policy       (dry-run, on a COPY)
    3. apply the cleanup                       [DESTRUCTIVE — stop the gateway first]
  Area 2 — hot-memory audit
    4. audit MEMORY.md / USER.md               (read-only -> mem-audit.json)
  Area 3 — pointer rewrite & consolidation
    5. render rewrite proposals                (dry-run -> proposed/ + manifest.json)
    6. apply the rewrite                       [DESTRUCTIVE — archives originals first]
  Area 4 — temporal migration
    7. record the rewrite in the temporal layer (append-only)
    8. sync any remaining drift into temporal   (append-only)
    9. verify temporal reconstructs live exactly (read-only)
  Area 5 — maintenance handoff
   10. health + maintenance pass               (read-only)

SAFETY MODEL (the whole point — never surprise the operator's live memory):
  * DRY-RUN IS THE DEFAULT. With no apply flag the driver runs only the read-only
    and on-a-COPY steps (audit / plan / simulate / render / verify / maintenance)
    and PRINTS what each mutation WOULD do. Live MEMORY.md / USER.md / state.db are
    never touched. ``--dry-run`` against any profile is safe.
  * Mutations require an explicit ``--apply`` (or ``--auto``). Even then, each
    mutating step asks for confirmation; ``--auto`` auto-confirms the SAFE
    (append-only temporal) steps and still asks before the DESTRUCTIVE ones
    (state.db rewrite, hot-memory rewrite). ``--yes`` answers yes to all prompts
    (non-interactive — for automation/tests). A non-interactive shell with no
    ``--yes`` skips (never hangs) any step that would need a prompt.
  * Artifacts live under a stable ``--workdir`` (default ``<home>/.onboard``) so a
    partial run is resumable with ``--from-step N`` — steps 1..N-1's outputs are
    preserved on disk and re-consumed, not recomputed.
  * Every child CLI is passed ``--home`` AND inherits ``HERMES_HOME`` pointed at the
    same resolved home, so a dropped flag degrades to the right home, never a stray one.

USAGE:
    # Preview only (default) — never modifies anything; writes reviewable proposals.
    python3 memory_onboard.py --home ~/.hermes
    python3 memory_onboard.py --home ~/.hermes --dry-run

    # Apply, interactive — asks before each mutation.
    python3 memory_onboard.py --home ~/.hermes --apply

    # Apply, semi-automatic — auto-confirms safe steps, asks before destructive ones.
    python3 memory_onboard.py --home ~/.hermes --auto

    # Apply, fully non-interactive (automation / tests).
    python3 memory_onboard.py --home ~/.hermes --apply --yes

    # Resume a partially-completed run from a given step.
    python3 memory_onboard.py --home ~/.hermes --apply --from-step 4

stdlib only; drives the shipped CLIs via subprocess (the exact entrypoints an
operator runs), so this driver tests what it ships. No LLM, no network.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_VERSION = "1.0.0"

# Step kinds — drive the dry-run/confirmation policy.
READ = "read"                 # read-only; always runs (even in dry-run)
DRY = "dry"                   # writes only to the workdir / a COPY; always runs
SAFE = "mutate-safe"          # append-only, reversible (temporal); apply-mode only
DESTRUCTIVE = "mutate-hard"   # rewrites live state.db / hot memory; apply-mode + confirm

_KIND_LABEL = {READ: "read-only", DRY: "dry-run (copy/workdir)",
               SAFE: "append-only", DESTRUCTIVE: "DESTRUCTIVE"}


# --------------------------------------------------------------------------- #
# Small terminal helpers                                                      #
# --------------------------------------------------------------------------- #
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("TERM", "") not in ("", "dumb")


_C = _supports_color()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _C else s


def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def red(s): return _c("31", s)
def cyan(s): return _c("36", s)


def hr(char="─", width=72):
    print(dim(char * width))


# --------------------------------------------------------------------------- #
# Context                                                                     #
# --------------------------------------------------------------------------- #
class Ctx:
    """Resolved paths + run mode shared by every step."""

    def __init__(self, args):
        self.home = os.path.abspath(os.path.expanduser(
            args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
        self.user_home = os.path.abspath(os.path.expanduser(args.user_home)) if args.user_home else None
        self.workdir = os.path.abspath(os.path.expanduser(args.workdir)) if args.workdir \
            else os.path.join(self.home, ".onboard")
        self.db = args.db or os.path.join(self.home, "state.db")
        self.memories = os.path.join(self.home, "memories")

        # workdir artifacts (the file hand-offs between steps)
        self.state_audit = os.path.join(self.workdir, "state-audit.json")
        self.policy = os.path.join(self.workdir, "policy.json")
        self.sim_dir = os.path.join(self.workdir, "sim")
        self.mem_audit = os.path.join(self.workdir, "mem-audit.json")
        self.proposed_dir = os.path.join(self.workdir, "proposed")
        self.manifest = os.path.join(self.proposed_dir, "manifest.json")

        # live archive destinations (the rollback record)
        self.remediation_arch = os.path.join(self.home, "archives", "remediation")
        self.rewrite_arch = os.path.join(self.home, "archives", "rewrite")

        # mode
        self.apply = bool(args.apply or args.auto or args.yes) and not args.dry_run
        self.dry_run = not self.apply
        self.auto = bool(args.auto)
        self.assume_yes = bool(args.yes)
        self.from_step = args.from_step
        self.to_step = args.to_step

        # projection (Phase 1): after onboarding, show what a budgeted memory
        # projection would save. Read-only; safe in both dry-run and apply.
        self.project = bool(getattr(args, "project", False))
        self.project_budget = int(getattr(args, "project_budget", 2000))

        # state.db policy knobs (conservative defaults == RUNBOOK Step 2)
        self.retention_days = args.retention_days
        self.prune_closed = args.prune_closed
        self.prune_unclosed = args.prune_unclosed
        self.drop_trigram = args.drop_trigram
        self.delete_compression_parents = args.delete_compression_parents
        self.vacuum = args.vacuum

    def child_env(self) -> dict:
        # Point HERMES_HOME at the SAME resolved home so a (hypothetically) dropped
        # --home flag degrades to the right home, never a stray ~/.hermes.
        env = {**os.environ, "HERMES_HOME": self.home}
        if self.user_home:
            env["HOME"] = self.user_home
        return env

    def user_home_args(self) -> list:
        return ["--user-home", self.user_home] if self.user_home else []


# --------------------------------------------------------------------------- #
# Subprocess runner                                                           #
# --------------------------------------------------------------------------- #
def script(name: str) -> str:
    return os.path.join(_HERE, name)


def run_child(ctx: Ctx, argv: list, *, capture_json=False, echo=True):
    """Run one shipped CLI. Returns (returncode, parsed_or_text, raw_stdout, raw_stderr)."""
    full = [sys.executable, *argv]
    if echo:
        print(dim("    $ " + " ".join(_short(a, ctx) for a in argv)))
    proc = subprocess.run(full, capture_output=True, text=True, timeout=1800, env=ctx.child_env())
    parsed = None
    if capture_json and proc.returncode == 0:
        try:
            parsed = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            parsed = None
    return proc.returncode, parsed, proc.stdout, proc.stderr


def _short(arg: str, ctx: Ctx) -> str:
    """Compact long absolute paths in the echoed command for readability."""
    s = str(arg)
    s = s.replace(ctx.workdir, "$WORK").replace(ctx.home, "$HOME")
    return s


# --------------------------------------------------------------------------- #
# Steps                                                                       #
# --------------------------------------------------------------------------- #
# A step is a dict: n, title, area, kind, needs (artifacts that must already
# exist), and run(ctx) -> StepResult. run() does the real work and returns a
# short human summary; it must NOT mutate live data when ctx.dry_run is True
# (the runner enforces this for SAFE/DESTRUCTIVE kinds by skipping them).
class StepResult:
    def __init__(self, ok: bool, summary: str, detail: str = "", skipped: bool = False):
        self.ok = ok
        self.summary = summary
        self.detail = detail
        self.skipped = skipped


def _fail(rc, out, err, what) -> StepResult:
    tail = (err or out or "").strip().splitlines()[-6:]
    return StepResult(False, f"{what} failed (rc={rc})", "\n".join("      " + l for l in tail))


# -- Area 1 ----------------------------------------------------------------- #
def step1_state_audit(ctx: Ctx) -> StepResult:
    if not os.path.exists(ctx.db):
        return StepResult(True, f"no state.db at {ctx.db} — skipping Area 1 (steps 1-3)", skipped=True)
    rc, j, out, err = run_child(ctx, [script("state_db_remediate.py"), "audit",
                                      "--home", ctx.home, "--json"], capture_json=True)
    if rc != 0:
        return _fail(rc, out, err, "state.db audit")
    _ensure_dir(ctx.workdir)
    with open(ctx.state_audit, "w", encoding="utf-8") as fh:
        fh.write(out)
    dbs = (j or {}).get("databases", [])
    # Headline the db steps 2/3 actually operate on (--db ctx.db). An audit of --home can
    # ALSO surface snapshots and the workdir's own simulate COPY — never confuse those for
    # the live target.
    target = os.path.realpath(ctx.db)
    main = next((d for d in dbs if os.path.realpath(d.get("path", "")) == target), None)
    others = len([d for d in dbs if d is not main and d.get("is_session_db")])
    if main is None:
        main = next((d for d in dbs if d.get("path", "").endswith("state.db")), dbs[0] if dbs else {})
        others = max(0, len(dbs) - 1)
    mb = (main.get("file_bytes") or 0) / (1024 * 1024)
    fts = main.get("fts_footprint") or {}
    tri = fts.get("trigram_bytes") or fts.get("trigram_index_bytes") or 0
    extra = f", trigram FTS ~{tri/1024/1024:.1f}MB (--drop-trigram reclaims it)" if tri else ""
    more = f"; {others} other db(s) on disk (snapshots/copies) left untouched" if others else ""
    return StepResult(True, f"state.db ≈ {mb:.1f}MB · {main.get('sessions_count', '?')} sessions "
                      f"({main.get('unclosed_sessions', 0)} unclosed, "
                      f"{main.get('ended_sessions', 0)} closed){extra}{more}")


def step2_state_plan_sim(ctx: Ctx) -> StepResult:
    if not os.path.exists(ctx.db):
        return StepResult(True, "no state.db — skipped", skipped=True)
    _ensure_dir(ctx.workdir)
    rc, _, out, err = run_child(ctx, [
        script("state_db_remediate.py"), "plan", "--db", ctx.db,
        "--retention-days", str(ctx.retention_days),
        "--prune-closed", ctx.prune_closed, "--prune-unclosed", ctx.prune_unclosed,
        "--drop-trigram", ctx.drop_trigram,
        "--delete-compression-parents", ctx.delete_compression_parents,
        "--vacuum", ctx.vacuum, "--out", ctx.policy])
    if rc != 0:
        return _fail(rc, out, err, "state.db plan")
    rc, j, out, err = run_child(ctx, [
        script("state_db_remediate.py"), "simulate", "--db", ctx.db,
        "--policy", ctx.policy, "--workdir", ctx.sim_dir, "--json"], capture_json=True)
    if rc != 0:
        return _fail(rc, out, err, "state.db simulate")
    j = j or {}
    before = j.get("before_bytes") or 0
    after = j.get("after_bytes") or 0
    red = (1 - after / before) * 100 if before else 0
    ok_integrity = (j.get("integrity_after") or {}).get("ok")
    s = (f"policy → {ctx.policy}; simulated on a COPY: "
         f"{before/1024/1024:.1f}MB → {after/1024/1024:.1f}MB ({red:.0f}% smaller), "
         f"integrity {'OK' if ok_integrity else 'FAILED'}")
    if not ok_integrity:
        return StepResult(False, "simulated cleanup FAILED integrity — refusing to apply", s)
    return StepResult(True, s)


def step3_state_apply(ctx: Ctx) -> StepResult:
    if not os.path.exists(ctx.db):
        return StepResult(True, "no state.db — skipped", skipped=True)
    if not os.path.exists(ctx.policy):
        return StepResult(False, f"missing {ctx.policy} — run step 2 first (or --from-step 2)")
    rc, j, out, err = run_child(ctx, [
        script("state_db_remediate.py"), "apply", "--db", ctx.db, "--policy", ctx.policy,
        "--archive-dir", ctx.remediation_arch, "--confirm-apply", "--json"], capture_json=True)
    if rc != 0:
        return _fail(rc, out, err, "state.db apply")
    j = j or {}
    integ = (j.get("post_swap_integrity") or {}).get("ok")
    arch = (j.get("archive") or {}).get("dir", ctx.remediation_arch)
    after = os.path.getsize(ctx.db) / 1024 / 1024 if os.path.exists(ctx.db) else 0
    if not j.get("applied"):
        return StepResult(False, "apply did not complete", json.dumps(j.get("errors", [])))
    return StepResult(True, f"state.db cleaned → {after:.1f}MB, post-swap integrity "
                      f"{'OK' if integ else 'FAILED'}; rollback archive: {arch}")


# -- Area 2 ----------------------------------------------------------------- #
def step4_hot_audit(ctx: Ctx) -> StepResult:
    _ensure_dir(ctx.workdir)
    rc, _, out, err = run_child(ctx, [
        script("memory_audit.py"), "--home", ctx.home, *ctx.user_home_args(),
        "--json", "--out", ctx.mem_audit])
    if rc != 0:
        return _fail(rc, out, err, "hot-memory audit")
    try:
        rep = json.load(open(ctx.mem_audit, encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return StepResult(False, f"audit wrote an unreadable report: {e}")
    s = rep.get("summary", {})
    caps = {f["store"]: f.get("capacity_pct") for f in rep.get("files", [])}
    cap_str = ", ".join(f"{k} {v}%" for k, v in caps.items() if v is not None)
    return StepResult(True, f"audit → {ctx.mem_audit}; capacity [{cap_str}]; "
                      f"dups={s.get('duplicate_pairs', 0)} "
                      f"contradictions={s.get('contradiction_pairs', 0)} "
                      f"broken_pointers={len(s.get('broken_pointers', []))}")


# -- Area 4a — seed the temporal baseline BEFORE the rewrite ---------------- #
# ORDERING (INTEG-3): the temporal layer must hold the FULL pre-rewrite memory
# before Area 3 runs, for two reasons:
#   1. record-rewrite only records the CHANGED entries; on an un-seeded layer the
#      kept entries are missing, so a later sync invents orphan facts and the layer
#      no longer reconstructs live (byte-exact verify fails).
#   2. With the baseline present, memory_rewrite.apply() auto-records the rewrite as
#      old→new events on the SAME fact keys (provenance), and the follow-up sync sees
#      no drift. This is why the seed precedes render/apply, unlike the naive RUNBOOK
#      order (which this driver supersedes).
def step5_temporal_seed(ctx: Ctx) -> StepResult:
    rc, j, out, err = run_child(ctx, [
        script("temporal_migrate_onboard.py"), "sync", "--home", ctx.home,
        "--confirm-apply", "--json"], capture_json=True)
    if rc != 0:
        return _fail(rc, out, err, "temporal seed (sync)")
    j = j or {}
    ing = j.get("ingest_summary", {})
    stores = j.get("stores", {})
    first = any(d.get("first_migration") for d in stores.values())
    tag = "first migration" if first else "already seeded (drift reconciled)"
    return StepResult(True, f"temporal baseline {tag}; created={ing.get('created', 0)} "
                      f"updated={ing.get('updated', 0)} — the rewrite will record provenance here")


def _rewrite_review_note(manifest_path: str) -> str:
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            review = (json.load(fh).get("review") or {})
    except Exception:
        return ""
    count = review.get("risky_count") or 0
    if not count:
        return ""
    by_action = review.get("by_rewrite_action") or {}
    return f"; REVIEW.md flags {count} high-impact change(s) {by_action} before apply"


# -- Area 3 ----------------------------------------------------------------- #
def step6_rewrite_render(ctx: Ctx) -> StepResult:
    if not os.path.exists(ctx.mem_audit):
        return StepResult(False, f"missing {ctx.mem_audit} — run step 4 first (or --from-step 4)")
    rc, _, out, err = run_child(ctx, [
        script("memory_rewrite.py"), "render", "--audit", ctx.mem_audit,
        "--out-dir", ctx.proposed_dir])
    if rc != 0:
        return _fail(rc, out, err, "rewrite render")
    # surface the render's own one-line summary
    line = next((l for l in out.splitlines() if l.startswith("[render] chars")), "")
    review_note = _rewrite_review_note(ctx.manifest)
    return StepResult(True, f"proposals → {ctx.proposed_dir} "
                      f"(manifest.json, REVIEW.md). "
                      + line.replace("[render] ", "") + review_note)


def step7_rewrite_apply(ctx: Ctx) -> StepResult:
    if not os.path.exists(ctx.mem_audit):
        return StepResult(False, f"missing {ctx.mem_audit} — run step 4 first (or --from-step 4)")
    rc, _, out, err = run_child(ctx, [
        script("memory_rewrite.py"), "apply", "--audit", ctx.mem_audit,
        "--archive-dir", ctx.rewrite_arch, "--confirm-apply"])
    if rc != 0:
        return _fail(rc, out, err, "rewrite apply")
    # apply auto-records the rewrite's provenance into the seeded temporal layer
    # (P3-2 / INTEG-3) and prints the outcome to stderr — surface it.
    prov = next((l.strip() for l in err.splitlines() if "[temporal]" in l), "")
    note = f" · {prov}" if prov else ""
    return StepResult(True, f"hot memory rewritten; originals archived → {ctx.rewrite_arch}{note}")


# -- Area 4b — reconcile any residual drift into the temporal layer --------- #
def step8_temporal_sync(ctx: Ctx) -> StepResult:
    rc, j, out, err = run_child(ctx, [
        script("temporal_migrate_onboard.py"), "sync", "--home", ctx.home,
        "--confirm-apply", "--json"], capture_json=True)
    if rc != 0:
        return _fail(rc, out, err, "temporal sync")
    j = j or {}
    ing = j.get("ingest_summary", {})
    return StepResult(True, f"temporal reconciled (drift_detected={j.get('drift_detected')}); "
                      f"created={ing.get('created', 0)} updated={ing.get('updated', 0)} "
                      f"deleted={ing.get('deleted', 0)}")


def step9_temporal_verify(ctx: Ctx) -> StepResult:
    # verify exits 1 when not all_match — that's a logical signal, and it STILL emits
    # JSON on stdout, so parse stdout regardless of the return code.
    rc, j, out, err = run_child(ctx, [
        script("temporal_migrate_onboard.py"), "verify", "--home", ctx.home, "--json"],
        capture_json=True)
    if j is None:
        try:
            j = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            if ctx.dry_run:
                return StepResult(True, "temporal verify not available in dry-run", skipped=True)
            return _fail(rc, out, err, "temporal verify")
    am = j.get("all_match")
    facts = j.get("facts") or 0
    summ = f"reconstruct == live: {am} (facts={facts}, versions={j.get('versions')})"
    if am:
        return StepResult(True, summ)
    ok_fact, reason = temporal_content_acceptance(j)
    if ok_fact:
        return StepResult(True, f"{summ} — {reason}")
    # not all_match — in dry-run the Area-4 populating steps (7-8) were skipped, so an
    # empty / drifted temporal layer is EXPECTED here, not a failure.
    if ctx.dry_run:
        if not facts:
            return StepResult(True, "no temporal layer yet — Area 4 (steps 7-8) is apply-only; "
                              "would verify after --apply", skipped=True)
        return StepResult(True, f"[dry-run] {summ} — apply would reconcile drift via sync")
    return StepResult(False, f"temporal content drift — {reason}; investigate before continuing", summ)


def temporal_content_acceptance(report: dict) -> tuple[bool, str]:
    """True when temporal has no fact drift even if byte/order exactness differs.

    `all_match` is too strict for live hot memory because the temporal replay order
    can differ while preserving the same entry multiset. Acceptance is fact-level:
    no content drift and no entries only in live/temporal for every store.
    """
    stores = report.get("stores") or {}
    if not stores:
        return False, "no store verification details"
    for name, st in stores.items():
        if st.get("content_drift"):
            return False, f"{name} has content_drift=true"
        if st.get("entries_only_in_live") or st.get("entries_only_in_temporal"):
            return False, f"{name} has unmatched live/temporal entries"
    return True, "content faithful (order/whitespace-only drift accepted)"


# -- Area 5 ----------------------------------------------------------------- #
def step10_maintenance(ctx: Ctx) -> StepResult:
    rc, j, out, err = run_child(ctx, [
        script("memory_maintenance.py"), "--home", ctx.home, *ctx.user_home_args(),
        "--dry-run", "--json"], capture_json=True)
    if rc != 0:
        return _fail(rc, out, err, "maintenance")
    j = j or {}
    overall = j.get("overall", "?")
    hrc, hj, hout, herr = run_child(ctx, [
        script("memory_health.py"), "--home", ctx.home, "--json"], capture_json=True)
    health = (hj or {}).get("status") or (hj or {}).get("overall") or ("ok" if hrc == 0 else "?")
    color = green if overall in ("green",) else (yellow if overall == "yellow" else red)
    return StepResult(True, f"maintenance overall: {color(overall)}; health: {health} "
                      f"(both read-only; exit 0 even when alerting)")


STEPS = [
    {"n": 1,  "area": "1",  "kind": READ,        "title": "Audit state.db",                 "run": step1_state_audit},
    {"n": 2,  "area": "1",  "kind": DRY,         "title": "Plan + simulate cleanup policy", "run": step2_state_plan_sim},
    {"n": 3,  "area": "1",  "kind": DESTRUCTIVE, "title": "Apply state.db cleanup",         "run": step3_state_apply},
    {"n": 4,  "area": "2",  "kind": READ,        "title": "Audit hot memory",               "run": step4_hot_audit},
    {"n": 5,  "area": "4a", "kind": SAFE,        "title": "Seed temporal baseline",         "run": step5_temporal_seed},
    {"n": 6,  "area": "3",  "kind": DRY,         "title": "Render rewrite proposals",       "run": step6_rewrite_render},
    {"n": 7,  "area": "3",  "kind": DESTRUCTIVE, "title": "Apply rewrite (+ record provenance)", "run": step7_rewrite_apply},
    {"n": 8,  "area": "4b", "kind": SAFE,        "title": "Reconcile temporal layer",       "run": step8_temporal_sync},
    {"n": 9,  "area": "4",  "kind": READ,        "title": "Verify temporal",                "run": step9_temporal_verify},
    {"n": 10, "area": "5",  "kind": READ,        "title": "Maintenance health check",       "run": step10_maintenance},
]
N_STEPS = len(STEPS)


# --------------------------------------------------------------------------- #
# Confirmation gates                                                          #
# --------------------------------------------------------------------------- #
def _confirm(ctx: Ctx, step) -> bool:
    """Decide whether a mutating step may run. READ/DRY never reach here."""
    if ctx.assume_yes:
        return True
    if ctx.auto and step["kind"] == SAFE:
        return True  # append-only temporal writes are auto-confirmed under --auto
    # A prompt is required (interactive --apply, or a destructive step under --auto).
    if not sys.stdin.isatty():
        print(yellow(f"    ↷ non-interactive and no --yes — skipping this mutation "
                     f"(re-run with --yes or --auto to apply)."))
        return False
    if step["kind"] == DESTRUCTIVE:
        print(red(f"    ⚠ This step rewrites LIVE data. "
                  f"{'Stop the gateway first. ' if step['n'] == 3 else ''}"
                  f"Originals are archived for rollback."))
    try:
        ans = input(bold(f"    Proceed with step {step['n']} ({step['title']})? [y/N] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


# --------------------------------------------------------------------------- #
# Driver                                                                      #
# --------------------------------------------------------------------------- #
def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def _preflight(ctx: Ctx) -> int:
    if not os.path.isdir(ctx.home):
        print(red(f"error: home not found: {ctx.home}"), file=sys.stderr)
        return 2
    if ctx.from_step < 1 or ctx.from_step > N_STEPS:
        print(red(f"error: --from-step must be 1..{N_STEPS}"), file=sys.stderr)
        return 2
    if ctx.to_step < ctx.from_step or ctx.to_step > N_STEPS:
        print(red(f"error: --to-step must be {ctx.from_step}..{N_STEPS}"), file=sys.stderr)
        return 2
    return 0


def _banner(ctx: Ctx):
    hr("═")
    print(bold("  Hermes memory onboarding") + dim(f"  · v{TOOL_VERSION}"))
    hr("═")
    mode = green("APPLY") if ctx.apply else cyan("DRY-RUN (default — nothing live is modified)")
    conf = "yes-to-all" if ctx.assume_yes else ("auto (ask only destructive)" if ctx.auto
                                                else "interactive (ask each mutation)")
    print(f"  home      : {ctx.home}")
    print(f"  workdir   : {ctx.workdir}")
    print(f"  state.db  : {ctx.db}" + (dim("  (not present — Area 1 skipped)")
                                       if not os.path.exists(ctx.db) else ""))
    print(f"  mode      : {mode}")
    if ctx.apply:
        print(f"  confirm   : {conf}")
    span = f"{ctx.from_step}..{ctx.to_step}" if (ctx.from_step != 1 or ctx.to_step != N_STEPS) else f"1..{N_STEPS}"
    print(f"  steps     : {span}")
    print()


def _temporal_paths(ctx: Ctx):
    return (os.path.join(ctx.home, "memories", "_versions", "history.jsonl"),
            os.path.join(ctx.home, "memory_versions.db"))


def _temporal_preexisted(ctx: Ctx) -> bool:
    return any(os.path.exists(p) for p in _temporal_paths(ctx))


def _cleanup_stray_temporal(ctx: Ctx) -> None:
    """Keep dry-run's footprint pristine: a read-only temporal verify/maintenance
    pass instantiates TemporalMemory, whose constructor creates an EMPTY (rebuildable,
    zero-event) index db. If no real layer pre-existed and none was recorded (no
    history.jsonl), remove that stray empty db so a dry-run truly modifies nothing."""
    hist, db = _temporal_paths(ctx)
    if os.path.exists(hist):
        return  # real events exist (would not happen in dry-run) — never touch it
    for p in (db, db + "-wal", db + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    vd = os.path.join(ctx.home, "memories", "_versions")
    try:
        if os.path.isdir(vd) and not os.listdir(vd):
            os.rmdir(vd)
    except OSError:
        pass


def run_pipeline(ctx: Ctx) -> int:
    _banner(ctx)
    temporal_pre = _temporal_preexisted(ctx)
    try:
        return _run_steps(ctx)
    finally:
        # dry-run must leave nothing behind beyond its review artifacts in --workdir
        if ctx.dry_run and not temporal_pre:
            _cleanup_stray_temporal(ctx)


def _run_steps(ctx: Ctx) -> int:
    results = []
    for step in STEPS:
        n = step["n"]
        if n < ctx.from_step or n > ctx.to_step:
            continue
        kind = step["kind"]
        hr()
        flag = {READ: cyan, DRY: cyan, SAFE: yellow, DESTRUCTIVE: red}[kind](_KIND_LABEL[kind])
        print(bold(f"  Step {n}/{N_STEPS} · Area {step['area']} · {step['title']}") + f"   [{flag}]")

        # In dry-run, mutations are previewed, never executed.
        if ctx.dry_run and kind in (SAFE, DESTRUCTIVE):
            print(dim(f"    ⤳ DRY-RUN: would run this {('DESTRUCTIVE ' if kind == DESTRUCTIVE else '')}"
                      f"step under --apply. Skipped (no live change)."))
            results.append((n, "skipped (dry-run)", True))
            continue

        # In apply mode, gate mutations behind confirmation.
        if ctx.apply and kind in (SAFE, DESTRUCTIVE):
            if not _confirm(ctx, step):
                results.append((n, "skipped (declined)", True))
                print(yellow(f"    ↷ step {n} skipped."))
                continue

        try:
            res = step["run"](ctx)
        except Exception as e:  # a step's own bug must not leave a half-run silent
            print(red(f"    ✗ step {n} crashed: {e}"))
            _print_resume_hint(ctx, n)
            return 1

        if res.skipped:
            print(dim(f"    ⤳ {res.summary}"))
            results.append((n, res.summary, True))
            continue
        if not res.ok:
            print(red(f"    ✗ {res.summary}"))
            if res.detail:
                print(res.detail)
            _print_resume_hint(ctx, n)
            _final_summary(ctx, results, failed_at=n)
            return 1
        print(green(f"    ✓ {res.summary}"))
        if res.detail:
            print(dim(res.detail))
        results.append((n, res.summary, True))

    _final_summary(ctx, results, failed_at=None)
    if ctx.project:
        _project_summary(ctx)
    return 0


def _print_resume_hint(ctx: Ctx, failed_n: int):
    print()
    print(yellow(f"  Pipeline stopped at step {failed_n}. Earlier steps' artifacts are preserved in:"))
    print(yellow(f"    {ctx.workdir}"))
    apply_flags = "" if not ctx.apply else (" --apply" + (" --yes" if ctx.assume_yes else
                                                          (" --auto" if ctx.auto else "")))
    print(yellow(f"  Fix the cause, then resume with:"))
    print(bold(f"    python3 {os.path.basename(__file__)} --home {ctx.home}{apply_flags} --from-step {failed_n}"))


def _final_summary(ctx: Ctx, results, failed_at):
    hr("═")
    done = [n for n, _, ok in results if ok]
    if failed_at is not None:
        print(red(f"  ✗ Onboarding incomplete — failed at step {failed_at}. "
                  f"{len(done)}/{N_STEPS} steps OK before that."))
    elif ctx.dry_run:
        print(cyan("  ✓ Dry-run complete. Nothing live was modified."))
        print("  Reviewable proposals written under:")
        print(bold(f"    {ctx.proposed_dir}/  (MEMORY.proposed.md, USER.proposed.md, manifest.json, REVIEW.md)"))
        review_note = _rewrite_review_note(ctx.manifest)
        if review_note:
            print(yellow("  Review gate:" + review_note.replace(";", "")))
        print("  When the proposals look right, apply for real with:")
        print(bold(f"    python3 {os.path.basename(__file__)} --home {ctx.home} --apply"))
    else:
        print(green(f"  ✓ Onboarding complete — {len(done)}/{N_STEPS} steps. "
                    f"Messy → clean → maintenance enabled."))
        print("  Next: register the maintenance crons (see skills/memory-maintenance.md):")
        print(dim("    crons/memory-health-daily.json · crons/memory-temporal-sync.json"))
    hr("═")


def _project_summary(ctx: Ctx):
    """Phase 1 projection footer: show how much a budgeted memory projection
    saves over brute-force injection. Read-only; never fails the onboard run."""
    try:
        import memory_project as MP  # noqa: WPS433
        rep = MP.project(home=ctx.home, budget=ctx.project_budget,
                         user_home=ctx.user_home)
    except Exception as e:
        print(dim(f"    (memory projection skipped: {e})"))
        return
    hr()
    o, p, s = rep["original_tokens"], rep["projected_tokens"], rep["savings_pct"]
    print(bold(f"  Memory projection (budget {rep['budget_tokens']} tokens):"))
    print(green(f"    Your memory was {o} tokens. Projected to {p} tokens ({s}% savings)."))
    print(dim(f"    {rep['entries_selected']}/{rep['entries_total']} entries kept · "
              f"{rep['always_inject_count']} always-inject · "
              f"{rep['projected_memory_chars']} chars"))
    print(dim("    Inject the projected block with: "
              f"memory_project.py --home {ctx.home} --budget {ctx.project_budget}"))


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_onboard.py",
        description="Drive the full Hermes memory onboarding (Areas 1→5) as ONE command. "
                    "Dry-run by default — never modifies live data without --apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__[__doc__.index("USAGE:"):])
    p.add_argument("--home", help="Hermes home to onboard (default $HERMES_HOME or ~/.hermes)")
    p.add_argument("--user-home", help="OS home for resolving ~/ paths in entries "
                   "(default real $HOME; set to the profile root for self-contained profiles)")
    p.add_argument("--db", help="state.db path (default <home>/state.db)")
    p.add_argument("--workdir", help="where step artifacts live, for review + resume "
                   "(default <home>/.onboard)")

    mode = p.add_argument_group("mode (default: dry-run — nothing live changes)")
    mode.add_argument("--dry-run", action="store_true",
                      help="explicit preview; runs read-only/copy steps, skips every mutation (default)")
    mode.add_argument("--apply", action="store_true",
                      help="perform mutations; asks before each one unless --auto/--yes")
    mode.add_argument("--auto", action="store_true",
                      help="apply, auto-confirming SAFE (append-only) steps; still asks before DESTRUCTIVE ones")
    mode.add_argument("--yes", "-y", action="store_true",
                      help="answer yes to all confirmations (non-interactive; for automation/tests)")

    flow = p.add_argument_group("flow control")
    flow.add_argument("--from-step", type=int, default=1, metavar="N",
                      help=f"resume from step N (1..{N_STEPS}); reuses earlier artifacts in --workdir")
    flow.add_argument("--to-step", type=int, default=N_STEPS, metavar="N",
                      help=f"stop after step N (1..{N_STEPS})")

    sdb = p.add_argument_group("state.db cleanup policy (Area 1; conservative defaults)")
    sdb.add_argument("--retention-days", type=int, default=90, help="prune sessions older than N days (default 90)")
    sdb.add_argument("--prune-closed", default="yes", choices=["yes", "no"], help="prune CLOSED old sessions (default yes)")
    sdb.add_argument("--prune-unclosed", default="no", choices=["yes", "no"], help="prune UNCLOSED old sessions (default no)")
    sdb.add_argument("--drop-trigram", default="no", choices=["yes", "no"],
                     help="drop the trigram FTS index — reclaims the MOST space, but rebuild needed for fuzzy search (default no)")
    sdb.add_argument("--delete-compression-parents", default="no", choices=["yes", "no"],
                     help="delete superseded compression parents (default no)")
    sdb.add_argument("--vacuum", default="yes", choices=["yes", "no"], help="VACUUM after cleanup (default yes)")

    proj = p.add_argument_group("projection (Phase 1 — budget-aware memory projection)")
    proj.add_argument("--project", action="store_true",
                      help="after onboarding, report what a budgeted memory projection saves")
    proj.add_argument("--project-budget", type=int, default=2000, metavar="TOKENS",
                      help="token budget for the --project report (default 2000)")

    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    ctx = Ctx(args)
    pf = _preflight(ctx)
    if pf:
        return pf
    try:
        return run_pipeline(ctx)
    except KeyboardInterrupt:
        print(yellow("\n  interrupted — no further steps run; completed steps are preserved."))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
