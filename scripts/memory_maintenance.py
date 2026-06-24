#!/usr/bin/env python3
"""Hermes memory-stack maintenance orchestrator — Area 5.

Runs ONE coordinated maintenance pass over the whole memory stack and emits a
single consolidated report (JSON + markdown), so a single weekly cron covers the
read-only checks instead of several. It REPORTS on the semantic daemon and the
last auto-extraction candidates (it does NOT reindex ChromaDB or re-run the
extractor by default), and it SUBSUMES the temporal-ingest cron (capturing drift
into the temporal layer weekly via --apply-temporal-sync). Steps, in order:

  1. temporal sync       — detect (and optionally record) drift of live hot
                           memory vs the temporal layer.  DRY by default.
  2. temporal verify     — confirm the temporal DB reconstructs the live files.
  3. auto-extraction     — report the latest dry-run candidates (optionally
                           re-run the extractor with --run-extract; off by
                           default to keep this a provider-free, no_agent-safe
                           pass). A dead/erroring extractor reports as an ALERT.
  4. memory audit        — Area 2 quality assessment (read-only).
  5. state.db remediate  — Area 1: flag oversized state.db files and the
                           remediation available (read-only recommendations).
  6. capacity            — hot-file char/entry pressure (read-only).
  then a consolidated report (the 6 functional steps above are STEP_ORDER).

NON-NEGOTIABLE SAFETY:
  * NEVER writes MEMORY.md / USER.md — this is a read-only maintenance REPORTER.
    The actual hot-memory writes happen elsewhere, each explicitly gated:
      - Memory Curator daily sweep (already a cron)
      - memory_auto_extract.py --write (not enabled; dry-run only)
      - temporal_migrate_onboard.py sync --confirm-apply (temporal layer only)
      - memory_rewrite.py apply --confirm-apply (Area 3)
  * The only optional WRITE this orchestrator can make is to the TEMPORAL layer
    (``--apply-temporal-sync`` → records drift events; never touches hot files)
    or the semantic index (``--reindex-semantic``); both are off by default.
  * NEVER restarts/▸stops the gateway, touches Telegram, or interferes with any
    cron. No subprocess in the default path makes a provider/network call.
  * EXIT 0 when the pass RAN (alerts/failures are in the content + per-step
    status + overall score); EXIT 1 only if the orchestrator itself could not run.

Each step is individually skippable and partial-failure tolerant: one step
erroring never aborts the others. Exportable: everything derives from ``--home``.

stdlib only; reuses sibling memory-stack modules (degrades gracefully if absent).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

TOOL_VERSION = "1.0.0"
_RANK = {"green": 0, "ok": 0, "skipped": 0, "unknown": 1, "yellow": 1, "alert": 1,
         "warning": 1, "red": 2, "critical": 2, "error": 2}

STEP_ORDER = ["temporal_sync", "temporal_verify", "auto_extract", "audit", "state_db_remediate", "capacity"]


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _worse(a: str, b: str) -> str:
    return b if _RANK.get(b, 1) > _RANK.get(a, 1) else a


# --------------------------------------------------------------------------- #
# Temporal handle: a COPY in dry mode (live never re-indexed), live on apply   #
# --------------------------------------------------------------------------- #
def _temporal_handle(home: str, apply_sync: bool):
    """Return (tm_or_None, cleanup_fn, note). Dry mode operates on a COPY so the
    live memory_versions.db is never written; apply mode uses the live layer."""
    try:
        import temporal_memory as TM  # noqa: WPS433
    except Exception as e:
        return None, (lambda: None), f"temporal_memory unavailable: {e}"
    db = os.path.join(home, "memory_versions.db")
    jsonl = os.path.join(home, "memories", "_versions", "history.jsonl")
    if apply_sync:
        try:
            tm = TM.TemporalMemory(home=home)
            return tm, (lambda: tm.conn.close()), "live temporal layer (apply mode)"
        except Exception as e:
            return None, (lambda: None), f"cannot open live temporal layer: {e}"
    # dry: copy
    try:
        tmpdir = tempfile.mkdtemp(prefix="memmaint_tmpl_")
        cdb = os.path.join(tmpdir, "memory_versions.db")
        cjsonl = os.path.join(tmpdir, "history.jsonl")
        if os.path.exists(db):
            shutil.copy2(db, cdb)
        if os.path.exists(jsonl):
            shutil.copy2(jsonl, cjsonl)
        tm = TM.TemporalMemory(home=home, db_path=cdb, jsonl_path=cjsonl)

        def cleanup():
            try:
                tm.conn.close()
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return tm, cleanup, "temporal DB copy (read-only dry mode)"
    except Exception as e:
        return None, (lambda: None), f"cannot prepare temporal copy: {e}"


# --------------------------------------------------------------------------- #
# Steps — each returns {step, status, summary, details}; never raises          #
# --------------------------------------------------------------------------- #
def step_temporal_sync(ctx) -> dict:
    tm = ctx.get("tm")
    if tm is None:
        return _step("temporal_sync", "skipped", ctx.get("tm_note", "no temporal layer"))
    try:
        import temporal_migrate_onboard as O  # noqa: WPS433
        res = O.sync(tm, ctx["live_files"], confirm=ctx["apply_sync"])
        ms = res["stores"].get("MEMORY.md", {})
        us = res["stores"].get("USER.md", {})
        drift = res.get("drift_detected")
        status = "ok" if (ctx["apply_sync"] or not drift) else "alert"
        summary = (f"{'APPLIED' if res.get('applied') else 'dry-run'}: drift={drift} "
                   f"(MEMORY new={len(ms.get('new', []))}/upd={len(ms.get('updated', []))}/"
                   f"rem={len(ms.get('removed', []))}; USER new={len(us.get('new', []))}/"
                   f"upd={len(us.get('updated', []))}/rem={len(us.get('removed', []))})")
        return _step("temporal_sync", status, summary, res)
    except Exception as e:
        return _step("temporal_sync", "error", f"temporal sync failed: {e}")


def step_temporal_verify(ctx) -> dict:
    tm = ctx.get("tm")
    if tm is None:
        return _step("temporal_verify", "skipped", ctx.get("tm_note", "no temporal layer"))
    try:
        import temporal_migrate_onboard as O  # noqa: WPS433
        res = O.verify(tm, ctx["live_files"])
        drift = any(s.get("content_drift") for s in res["stores"].values())
        # Only CONTENT drift is an alert; a whitespace/order-only mismatch is benign
        # (otherwise the weekly cron would alert forever on a trailing newline).
        status = "ok" if (res["all_match"] or not drift) else "alert"
        note = "" if (res["all_match"] or drift) else " (whitespace/order only — benign)"
        summary = (f"all_match={res['all_match']} facts={res['facts']} versions={res['versions']} "
                   f"content_drift={drift}{note}")
        return _step("temporal_verify", status, summary, res)
    except Exception as e:
        return _step("temporal_verify", "error", f"temporal verify failed: {e}")


def step_auto_extract(ctx) -> dict:
    """Default: report the latest dry-run candidates (no LLM). With --run-extract:
    invoke the extractor in --dry-run (may need a provider; failure is non-fatal)."""
    home = ctx["home"]
    if ctx.get("run_extract"):
        script = os.path.join(_HERE, "memory_auto_extract.py")
        if not os.path.exists(script):
            return _step("auto_extract", "skipped", "memory_auto_extract.py not present")
        try:
            # Propagate the orchestrator's resolved home so the extractor mines the
            # SAME profile (env belt-and-suspenders for older copies w/o --home).
            r = subprocess.run([sys.executable, script, "--dry-run", "--json", "--home", home],
                               capture_output=True, text=True, timeout=300,
                               env={**os.environ, "HERMES_HOME": home})
            accepted, errored, unreachable = None, 0, False
            try:
                obj = json.loads(r.stdout or "{}")
                # Read the extractor's ACTUAL report keys (counts.accepted /
                # n_sessions_errored / model_unreachable). The old code looked for
                # 'candidates'/'facts'/'extracted', which the extractor never emits,
                # so it always reported candidates=None AND status ok — masking a
                # dead model. (C2)
                accepted = (obj.get("counts") or {}).get("accepted")
                errored = obj.get("n_sessions_errored", 0) or 0
                unreachable = bool(obj.get("model_unreachable"))
            except (json.JSONDecodeError, AttributeError):
                pass
            # A model we couldn't reach (extractor exit 2 / model_unreachable) or any
            # per-session error is an ALERT, never green — so the consolidated pass
            # can't quietly hide a dead extractor.
            if unreachable or r.returncode != 0:
                status, note = "alert", (f"extractor could not reach the model "
                                         f"(rc={r.returncode}, errored={errored}) — extraction INCOMPLETE")
            elif errored:
                status, note = "alert", f"extractor ran with {errored} session error(s); accepted={accepted}"
            else:
                status, note = "ok", f"extractor dry-run accepted={accepted}"
            return _step("auto_extract", status, note,
                         {"returncode": r.returncode, "accepted": accepted,
                          "n_sessions_errored": errored, "model_unreachable": unreachable})
        except Exception as e:  # the explicitly-requested run could not happen → alert
            return _step("auto_extract", "alert", f"extractor run failed ({e}) — extraction INCOMPLETE")
    # report-only (provider-free)
    try:
        import memory_health as H  # noqa: WPS433
        info = H.check_auto_extract(home)
        return _step("auto_extract", "ok",
                     f"last candidates={info.get('candidate_count')} "
                     f"({info.get('note', info.get('latest_file', ''))})", info)
    except Exception as e:
        return _step("auto_extract", "skipped", f"no auto-extraction info ({e})")


def step_audit(ctx) -> dict:
    try:
        import memory_audit as MA  # noqa: WPS433
        rep = MA.run_audit(ctx["live_files"]["MEMORY.md"], ctx["live_files"]["USER.md"],
                           ctx["home"], user_home=ctx.get("user_home"))
        s = rep["summary"]
        status = "alert" if s["actionable_entries"] > 0 else "ok"
        summary = (f"{s['total_entries']} entries · actionable={s['actionable_entries']} · "
                   f"dups={s['duplicate_pairs']} · contradictions={s['contradiction_pairs']} · "
                   f"broken={len(s['broken_pointers'])}")
        # keep the report light (drop per-entry text/big arrays)
        light = {"summary": s, "duplicate_pairs": rep["duplicate_pairs"],
                 "contradiction_pairs": rep["contradiction_pairs"]}
        return _step("audit", status, summary, light)
    except Exception as e:
        return _step("audit", "error", f"audit failed: {e}")


def step_capacity(ctx) -> dict:
    try:
        import memory_health as H  # noqa: WPS433
        cap = H.check_capacity(ctx["home"], ctx["live_files"]["MEMORY.md"], ctx["live_files"]["USER.md"])
        files = cap.get("files", {})
        status = {"ok": "ok", "warning": "alert", "critical": "alert",
                  "unknown": "skipped"}.get(cap["status"], "ok")
        summary = "; ".join(
            f"{k}={v['pct']}%[{v['status']}]" for k, v in files.items() if v.get("exists"))
        return _step("capacity", status, summary or "no hot files", cap)
    except Exception as e:
        return _step("capacity", "error", f"capacity check failed: {e}")


def step_state_db_remediate(ctx) -> dict:
    """Audit state.db files and generate remediation recommendations for oversized ones. Read-only."""
    try:
        import memory_health as H  # noqa: WPS433
        result = H.check_state_db(ctx["home"])
        dbs = result.get("dbs", [])
        oversized = [d for d in dbs if d.get("remediation")]
        if not oversized:
            summary = f"{len(dbs)} state.db(s) checked, all within thresholds"
            return _step("state_db_remediate", "ok", summary, result)
        # Build summary of what needs remediation
        parts = []
        for d in oversized:
            rem = d["remediation"]
            savings_parts = []
            if rem.get("drop_trigram_savings_mb", 0) > 0:
                savings_parts.append(f"trigram ~{rem['drop_trigram_savings_mb']}MB")
            if rem.get("compression_parent_savings_mb", 0) > 0:
                savings_parts.append(f"comp-parents ~{rem['compression_parent_savings_mb']}MB")
            savings_str = ", ".join(savings_parts) if savings_parts else "manual review"
            parts.append(f"{d['path']}: {d['mb']}MB ({rem['severity']}, reclaim: {savings_str})")
        summary = f"{len(oversized)} state.db(s) need remediation: " + "; ".join(parts)
        status = "critical" if any(d["remediation"].get("severity") == "critical" for d in oversized) else "warning"
        return _step("state_db_remediate", status, summary, result)
    except Exception as e:
        return _step("state_db_remediate", "error", f"state.db remediation check failed: {e}")


_STEP_FNS = {"temporal_sync": step_temporal_sync, "temporal_verify": step_temporal_verify,
             "auto_extract": step_auto_extract, "audit": step_audit,
             "state_db_remediate": step_state_db_remediate, "capacity": step_capacity}


def _step(name, status, summary, details=None):
    return {"step": name, "status": status, "summary": summary, "details": details}


# --------------------------------------------------------------------------- #
# Orchestrate                                                                  #
# --------------------------------------------------------------------------- #
def run_maintenance(home: str, *, apply_sync: bool = False, run_extract: bool = False,
                    skips: set | None = None, user_home: str | None = None) -> dict:
    home = os.path.abspath(os.path.expanduser(home))
    skips = skips or set()
    live_files = {"MEMORY.md": os.path.join(home, "memories", "MEMORY.md"),
                  "USER.md": os.path.join(home, "memories", "USER.md")}
    # Guard: snapshot hot-file hashes to PROVE we never wrote them.
    pre = {k: _sha(v) for k, v in live_files.items()}

    needs_tm = ("temporal_sync" not in skips) or ("temporal_verify" not in skips)
    tm, cleanup, tm_note = (None, (lambda: None), "skipped")
    if needs_tm:
        tm, cleanup, tm_note = _temporal_handle(home, apply_sync)

    ctx = {"home": home, "live_files": live_files, "apply_sync": apply_sync,
           "run_extract": run_extract, "tm": tm, "tm_note": tm_note, "user_home": user_home}

    steps = []
    try:
        for name in STEP_ORDER:
            if name in skips:
                steps.append(_step(name, "skipped", "skipped by flag"))
                continue
            try:  # partial-failure tolerance: a crashing step never aborts the rest
                steps.append(_STEP_FNS[name](ctx))
            except Exception as e:
                steps.append(_step(name, "error", f"step crashed: {e}"))
    finally:
        cleanup()

    post = {k: _sha(v) for k, v in live_files.items()}
    untouched = (pre == post)

    overall = "green"
    for st in steps:
        color = {"ok": "green", "skipped": "green", "alert": "yellow", "warning": "yellow",
                 "error": "red", "critical": "red"}.get(st["status"], "yellow")
        if _RANK.get(color, 0) > _RANK.get(overall, 0):
            overall = color
    alerts = [f"{s['step']}: {s['summary']}" for s in steps if s["status"] in ("alert", "warning", "critical", "error")]

    return {"tool": "memory_maintenance", "tool_version": TOOL_VERSION, "generated_at": _now(),
            "home": home, "mode": "apply-temporal-sync" if apply_sync else "dry-run",
            "overall": overall, "hot_files_untouched": untouched,
            "steps": steps, "alerts": alerts}


# --------------------------------------------------------------------------- #
# Render + CLI                                                                 #
# --------------------------------------------------------------------------- #
_BADGE = {"green": "🟢 GREEN", "yellow": "🟡 YELLOW", "red": "🔴 RED"}


def render_markdown(rep: dict) -> str:
    L = [f"# Memory Stack Maintenance — {_BADGE.get(rep['overall'], rep['overall'])}",
         f"_{rep['generated_at']} · {rep['home']} · mode={rep['mode']}_",
         f"_hot files (MEMORY.md/USER.md) untouched: {rep['hot_files_untouched']}_", ""]
    if rep["alerts"]:
        L.append("## Alerts")
        L += [f"- ⚠️ {a}" for a in rep["alerts"]]
        L.append("")
    L.append("## Steps (in order)")
    for s in rep["steps"]:
        icon = {"ok": "✅", "alert": "🟡", "skipped": "⏭️", "error": "❌"}.get(s["status"], "•")
        L.append(f"- {icon} **{s['step']}** [{s['status']}] — {s['summary']}")
    L += ["", "---",
          "_Read-only maintenance reporter. Hot files are never modified. Writes (curator sweep, "
          "auto-extract --write, temporal sync --confirm-apply, Area 3 apply) are separate, gated steps._"]
    return "\n".join(L)


def render_summary(rep: dict) -> str:
    n = len(rep["alerts"])
    return f"Memory maintenance ({rep['mode']}): {_BADGE.get(rep['overall'], rep['overall'])} · {n} alert(s)"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_maintenance.py",
        description="Read-only memory-stack maintenance orchestrator (one consolidated pass). "
                    "Never writes MEMORY.md/USER.md. Exit 0 on success even with alerts.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    p.add_argument("--user-home", help="OS home for resolving ~/ paths (audit existence checks)")
    p.add_argument("--dry-run", action="store_true", default=True,
                   help="read-only (default). Present for clarity; maintenance never writes hot files.")
    p.add_argument("--apply-temporal-sync", action="store_true",
                   help="record detected drift into the TEMPORAL layer (safe; never hot files)")
    p.add_argument("--run-extract", action="store_true",
                   help="invoke the auto-extractor dry-run (may need a provider; off by default)")
    p.add_argument("--skip", action="append", default=[], choices=STEP_ORDER,
                   help="skip a step (repeatable)")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--summary", action="store_true", help="one-line summary only")
    p.add_argument("--out", help="also write the report to this path")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    home = args.home or os.environ.get("HERMES_HOME") or "~/.hermes"
    try:
        rep = run_maintenance(home, apply_sync=args.apply_temporal_sync,
                              run_extract=args.run_extract, skips=set(args.skip),
                              user_home=os.path.expanduser(args.user_home) if args.user_home else None)
    except Exception as e:  # catastrophic: the orchestrator itself failed
        print(f"memory_maintenance failed: {e}", file=sys.stderr)
        return 1
    text = (json.dumps(rep, indent=2, default=str) if args.json
            else render_summary(rep) if args.summary else render_markdown(rep))
    print(text)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            print(f"(could not write --out: {e})", file=sys.stderr)
    # defensive: if somehow a hot file changed, that's a real failure
    if not rep["hot_files_untouched"]:
        print("FATAL: hot files changed during maintenance (should be impossible)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
