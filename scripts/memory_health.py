#!/usr/bin/env python3
"""Hermes memory-stack health check — Area 5 (self-monitoring).

A single READ-ONLY status report across the whole memory stack: hot-file
capacity, entry pressure, temporal-layer drift, memory cron statuses, state.db
sizes, semantic daemon, and auto-extraction candidates — rolled up to a
green/yellow/red score. Designed to be the cheap, script-only (no_agent) daily
check that replaces the noisy every-6h capacity monitor.

EXIT CONVENTION (matches the lesson from the broken capacity monitor):
  * exit 0 when the check RAN — capacity/drift ALERTS are conveyed in the
    CONTENT (and the green/yellow/red score), never via the exit code.
  * exit 1 ONLY on a genuine script failure (couldn't run / unhandled error).
So a "red" health report still exits 0; the cron scheduler must not mistake an
alert for a job failure.

SAFETY: pure read-only. The only write is an explicit ``--out`` report path. The
temporal check runs against a COPY of the temporal DB so the live
``memory_versions.db`` is never even re-indexed. Never touches the gateway,
Telegram, or any cron. Exportable: everything derives from ``--home``.

stdlib only (best-effort imports of sibling memory-stack modules; degrades
gracefully if any are absent).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

TOOL_VERSION = "1.0.0"

# Budgets / thresholds (aligned with memory_audit + the curator monitor).
MEMORY_CHAR_LIMIT = 15000
USER_CHAR_LIMIT = 6000
ENTRY_TARGET = 25
ENTRY_CEILING = 35
WARN_PCT = 80
CRIT_PCT = 90               # USER.md at 91% should read CRITICAL (matches curator)
STATE_DB_WARN_MB = 50       # recommend audit/remediation above this
STATE_DB_CRIT_MB = 200      # urgent — remediation strongly recommended
STATE_DB_REMEDIATE_MB = 30  # generate remediation plan above this (sub-warn)
ENTRY_DELIMITER = "\n§\n"

_RANK = {"green": 0, "ok": 0, "unknown": 1, "yellow": 1, "warning": 1, "red": 2, "critical": 2, "error": 2}
_FLAG_TO_COLOR = {"ok": "green", "warning": "yellow", "critical": "red",
                  "unknown": "unknown", "error": "yellow"}  # a check that couldn't run = investigate, not memory-critical


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _entries(text: str) -> list[str]:
    return [e.strip() for e in text.split(ENTRY_DELIMITER) if e.strip()]


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _cap_flag(pct: float) -> str:
    if pct >= CRIT_PCT:
        return "critical"
    if pct >= WARN_PCT:
        return "warning"
    return "ok"


# --------------------------------------------------------------------------- #
# Individual checks (each returns a dict with a "status" flag; never raises)   #
# --------------------------------------------------------------------------- #
def check_capacity(home: str, memory_path: str, user_path: str) -> dict:
    out = {"status": "ok", "files": {}, "entry_pressure": None}
    worst = "ok"
    for store, path, limit in (("memory", memory_path, MEMORY_CHAR_LIMIT),
                               ("user", user_path, USER_CHAR_LIMIT)):
        text = _read(path)
        if text is None:
            out["files"][store] = {"path": path, "exists": False, "status": "unknown"}
            worst = _w(worst, "unknown")
            continue
        chars = len(text)
        entries = len(_entries(text))
        pct = round(100 * chars / limit, 1) if limit else 0.0
        flag = _cap_flag(pct)
        out["files"][store] = {"path": path, "exists": True, "chars": chars,
                               "limit": limit, "pct": pct, "entries": entries, "status": flag}
        worst = _w(worst, flag)
    mem = out["files"].get("memory", {})
    if mem.get("exists"):
        n = mem["entries"]
        ep_flag = "critical" if n > ENTRY_CEILING else ("warning" if n > ENTRY_TARGET else "ok")
        out["entry_pressure"] = {"count": n, "target": ENTRY_TARGET, "ceiling": ENTRY_CEILING,
                                 "status": ep_flag}
        worst = _w(worst, ep_flag)
    out["status"] = worst
    return out


def check_temporal(home: str, memory_path: str, user_path: str) -> dict:
    """Read-only drift check via a COPY of the temporal DB (live never re-indexed)."""
    db = os.path.join(home, "memory_versions.db")
    jsonl = os.path.join(home, "memories", "_versions", "history.jsonl")
    if not os.path.exists(db) and not os.path.exists(jsonl):
        return {"status": "unknown", "note": "no temporal layer yet (run temporal sync to migrate)"}
    tmpdir = None
    try:
        import temporal_migrate_onboard as O  # noqa: WPS433
        import temporal_memory as TM  # noqa: WPS433
        tmpdir = tempfile.mkdtemp(prefix="memhealth_tmpl_")
        cdb = os.path.join(tmpdir, "memory_versions.db")
        cjsonl = os.path.join(tmpdir, "history.jsonl")
        if os.path.exists(db):
            shutil.copy2(db, cdb)
        if os.path.exists(jsonl):
            shutil.copy2(jsonl, cjsonl)
        tm = TM.TemporalMemory(home=home, db_path=cdb, jsonl_path=cjsonl)
        try:
            res = O.verify(tm, {"MEMORY.md": memory_path, "USER.md": user_path})
        finally:
            tm.conn.close()
        any_content_drift = any(s.get("content_drift") for s in res["stores"].values())
        # Only CONTENT drift is actionable. A whitespace/order-only mismatch
        # (e.g. a trailing newline) is benign and must not flip the badge yellow.
        status = "warning" if any_content_drift else "ok"
        return {"status": status, "facts": res["facts"], "versions": res["versions"],
                "current_facts": res["current_facts"], "all_match": res["all_match"],
                "content_drift": any_content_drift,
                "stores": {k: {"exact_match": v["exact_match"],
                               "entries_only_in_live": v.get("entries_only_in_live"),
                               "entries_only_in_temporal": v.get("entries_only_in_temporal"),
                               "whitespace_only_diff": v.get("whitespace_only_diff"),
                               "order_differs": v.get("order_differs"),
                               "content_drift": v.get("content_drift")}
                           for k, v in res["stores"].items()}}
    except Exception as e:  # never let the temporal check fail the whole report
        return {"status": "error", "note": f"temporal check error: {e}"}
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def check_crons(home: str) -> dict:
    jobs_path = os.path.join(home, "cron", "jobs.json")
    if not os.path.exists(jobs_path):
        return {"status": "unknown", "note": "no cron registry found"}
    try:
        with open(jobs_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        if isinstance(jobs, dict):
            jobs = list(jobs.values())
    except Exception as e:
        return {"status": "error", "note": f"cannot read cron registry: {e}"}
    if not isinstance(jobs, list):
        return {"status": "error", "note": "cron registry 'jobs' is not a list"}
    mem_jobs, errors, worst = [], [], "ok"
    for j in jobs:
        if not isinstance(j, dict):
            continue  # skip a malformed registry entry rather than crashing
        name = (j.get("name") or "").lower()
        if not any(k in name for k in ("memory", "curator", "semantic", "temporal", "extract", "spine")):
            continue
        last = j.get("last_status")
        paused = (j.get("state") == "paused") or bool(j.get("paused_at"))
        flag = "ok"
        if not paused and last == "error":
            flag, worst = "warning", _w(worst, "warning")
            errors.append(j.get("name"))
        mem_jobs.append({"id": j.get("id"), "name": j.get("name"), "last_status": last,
                         "last_run_at": j.get("last_run_at"), "paused": paused,
                         "delivery_error": bool(j.get("last_delivery_error")), "flag": flag})
    return {"status": worst, "jobs": mem_jobs, "errors": errors}


def check_state_db(home: str) -> dict:
    """Light glob of LIVE state.db files (skip snapshots/backups); sizes + remediation recommendations."""
    found, worst = [], "ok"
    for dirpath, dirnames, filenames in os.walk(home):
        low = dirpath.lower()
        if "state-snapshots" in low or "pre-update" in low or "/.remediate_work_" in low:
            dirnames[:] = []
            continue
        if "state.db" in filenames:
            p = os.path.join(dirpath, "state.db")
            try:
                mb = os.path.getsize(p) / (1024 * 1024)
            except OSError:
                continue
            flag = "critical" if mb >= STATE_DB_CRIT_MB else ("warning" if mb >= STATE_DB_WARN_MB else "ok")
            worst = _w(worst, flag)
            entry = {"path": os.path.relpath(p, home), "mb": round(mb, 1), "flag": flag}
            # Generate remediation recommendation for oversized DBs
            if mb >= STATE_DB_REMEDIATE_MB:
                entry["remediation"] = _recommend_state_db_remediation(p, mb)
            found.append(entry)
    found.sort(key=lambda d: d["mb"], reverse=True)
    return {"status": worst, "dbs": found,
            "oversized": [d["path"] for d in found if d["flag"] != "ok"],
            "remediation_needed": [d["path"] for d in found if "remediation" in d]}


def _recommend_state_db_remediation(db_path: str, mb: float) -> dict:
    """Generate a recommended cleanup policy for an oversized state.db. Read-only estimate."""
    rec = {"action": "run_state_db_remediate", "severity": "critical" if mb >= STATE_DB_CRIT_MB else "warning",
           "current_mb": round(mb, 1), "commands": []}
    try:
        import state_db_remediate as R  # noqa: WPS433
        audit = R.audit_db(db_path, os.path.dirname(db_path))
        if not audit.get("is_session_db"):
            rec["note"] = "not a Hermes session DB — manual review needed"
            return rec
        trigram = audit.get("trigram_fts_tables", [])
        reclaim = audit.get("reclaim_estimates", {})
        comp = audit.get("compression_parents", {})
        unclosed = audit.get("unclosed_sessions", 0)
        sessions = audit.get("sessions_count", 0)
        rec["sessions"] = sessions
        rec["unclosed_sessions"] = unclosed
        rec["compression_parents"] = comp.get("total", 0)
        rec["has_trigram"] = bool(trigram)
        rec["drop_trigram_savings_mb"] = round(reclaim.get("drop_trigram_bytes", 0) / 1024 / 1024, 1)
        rec["compression_parent_savings_mb"] = round(reclaim.get("delete_compression_parents_base_bytes", 0) / 1024 / 1024, 1)
        # Build recommended policy
        policy = {"drop_trigram": bool(trigram), "vacuum": True}
        if comp.get("with_child", 0) > 0:
            policy["delete_compression_parents"] = True
        if unclosed > 0 and unclosed == sessions:
            policy["prune_unclosed"] = True
            policy["retention_days"] = 90
        rec["recommended_policy"] = policy
        # Build actionable commands
        safe_dir = os.path.dirname(db_path)
        rec["commands"] = [
            f"# 1. Audit (read-only):",
            f"python3 ~/.hermes/packages/hermes-memory-stack/scripts/state_db_remediate.py audit --db {db_path}",
            f"# 2. Simulate on a copy (read-only):",
            f"python3 ~/.hermes/packages/hermes-memory-stack/scripts/state_db_remediate.py simulate --db {db_path} --policy /tmp/state-db-policy.json --workdir /tmp/state-db-sim",
            f"# 3. Apply (stop gateway first!):",
            f"python3 ~/.hermes/packages/hermes-memory-stack/scripts/state_db_remediate.py apply --db {db_path} --policy /tmp/state-db-policy.json --archive-dir {safe_dir}/../archives/remediation --confirm-apply",
        ]
    except Exception as e:
        rec["note"] = f"audit unavailable ({e}); manual review needed"
        rec["commands"] = [f"python3 ~/.hermes/packages/hermes-memory-stack/scripts/state_db_remediate.py audit --db {db_path}"]
    return rec


def check_semantic(home: str) -> dict:
    """Optional, non-fatal: is the semantic daemon socket present?"""
    sock = os.path.join(home, "chroma", "semantic.sock")
    chroma = os.path.join(home, "chroma")
    if not os.path.isdir(chroma):
        return {"status": "unknown", "running": None, "note": "semantic retrieval not installed"}
    running = os.path.exists(sock)
    return {"status": "ok", "running": running,
            "note": "daemon socket present" if running else "daemon socket absent (query starts it on demand)"}


def check_hot_audit(home: str, memory_path: str, user_path: str) -> dict:
    """Best-effort audit signals that capacity alone cannot show (broken pointers, dups)."""
    try:
        import memory_audit as MA  # noqa: WPS433
        rep = MA.run_audit(memory_path, user_path, home, user_home=os.path.expanduser("~"))
        s = rep.get("summary", {})
        broken = s.get("broken_pointers") or []
        dups = s.get("duplicate_pairs", 0)
        contra = s.get("contradiction_pairs", 0)
        status = "warning" if broken or contra else "ok"
        return {"status": status, "broken_pointers": broken, "duplicate_pairs": dups,
                "contradiction_pairs": contra, "actionable": s.get("actionable_entries", 0)}
    except Exception as e:
        return {"status": "error", "note": f"hot-memory audit unavailable: {e}"}


def check_auto_extract(home: str) -> dict:
    """Best-effort: latest dry-run candidates count from the _auto_extract dir."""
    d = os.path.join(home, "memories", "_auto_extract")
    if not os.path.isdir(d):
        return {"status": "unknown", "note": "no auto-extraction output yet"}
    latest, latest_mtime = None, -1.0
    for root, _dirs, files in os.walk(d):
        for f in files:
            if f.endswith(".json") or f.endswith(".jsonl"):
                fp = os.path.join(root, f)
                try:
                    m = os.path.getmtime(fp)
                except OSError:
                    continue
                if m > latest_mtime:
                    latest, latest_mtime = fp, m
    if not latest:
        return {"status": "unknown", "note": "no candidate files found"}
    count = None
    try:
        raw = _read(latest) or ""
        if latest.endswith(".jsonl"):
            count = sum(1 for ln in raw.splitlines() if ln.strip())
        else:
            obj = json.loads(raw)
            if isinstance(obj, list):
                count = len(obj)
            elif isinstance(obj, dict):
                for key in ("candidates", "facts", "extracted"):
                    if isinstance(obj.get(key), list):
                        count = len(obj[key])
                        break
    except Exception:
        count = None
    return {"status": "ok", "latest_file": os.path.relpath(latest, home),
            "last_modified": _dt.datetime.fromtimestamp(latest_mtime).isoformat(timespec="seconds"),
            "candidate_count": count}


def _w(cur: str, new: str) -> str:
    """Return the worse of two flags."""
    return new if _RANK.get(new, 1) > _RANK.get(cur, 1) else cur


# --------------------------------------------------------------------------- #
# Assemble                                                                     #
# --------------------------------------------------------------------------- #
def run_health(home: str, *, memory_path: str | None = None, user_path: str | None = None) -> dict:
    home = os.path.abspath(os.path.expanduser(home))
    memory_path = memory_path or os.path.join(home, "memories", "MEMORY.md")
    user_path = user_path or os.path.join(home, "memories", "USER.md")

    def _safe(label, fn, *a):
        # Per-check isolation: a single check raising must never crash the whole
        # report or leak a non-zero exit (the broken-capacity-monitor lesson).
        try:
            return fn(*a)
        except Exception as e:
            return {"status": "error", "note": f"{label} check error: {e}"}

    checks = {
        "capacity": _safe("capacity", check_capacity, home, memory_path, user_path),
        "temporal": _safe("temporal", check_temporal, home, memory_path, user_path),
        "hot_audit": _safe("hot_audit", check_hot_audit, home, memory_path, user_path),
        "crons": _safe("crons", check_crons, home),
        "state_db": _safe("state_db", check_state_db, home),
        "semantic": _safe("semantic", check_semantic, home),
        "auto_extract": _safe("auto_extract", check_auto_extract, home),
    }
    # Overall score: worst of the SAFETY-relevant checks. Semantic/auto_extract
    # are informational only (they never drive the score to red). "unknown" maps
    # to green here so a not-yet-installed component doesn't raise a false alarm.
    score = "green"
    for key in ("capacity", "temporal", "hot_audit", "crons", "state_db"):
        color = _FLAG_TO_COLOR.get(checks[key]["status"], "green")
        if color == "unknown":
            color = "green"
        if _RANK.get(color, 0) > _RANK.get(score, 0):
            score = color

    alerts = _alerts(checks)
    return {"tool": "memory_health", "tool_version": TOOL_VERSION, "generated_at": _now(),
            "home": home, "overall": score, "alerts": alerts, "checks": checks}


def _alerts(checks: dict) -> list[str]:
    a = []
    cap = checks["capacity"]
    for store, f in cap.get("files", {}).items():
        if f.get("status") == "critical":
            a.append(f"{store.upper()}.md CRITICAL: {f['pct']}% of {f['limit']} chars — cleanup needed")
        elif f.get("status") == "warning":
            a.append(f"{store.upper()}.md getting full: {f['pct']}%")
    ep = cap.get("entry_pressure")
    if ep and ep["status"] != "ok":
        a.append(f"MEMORY.md entry count {ep['count']} > {'ceiling' if ep['status']=='critical' else 'target'} "
                 f"({ep['ceiling'] if ep['status']=='critical' else ep['target']})")
    t = checks["temporal"]
    if t.get("content_drift"):
        a.append("temporal layer DRIFT: live hot memory differs from the temporal DB — run temporal sync")
    ha = checks.get("hot_audit", {})
    if ha.get("broken_pointers"):
        a.append(f"broken hot-memory pointers: {len(ha['broken_pointers'])} ({', '.join(ha['broken_pointers'][:5])})")
    if ha.get("contradiction_pairs"):
        a.append(f"possible hot-memory contradictions: {ha['contradiction_pairs']}")
    for name in checks["crons"].get("errors", []):
        a.append(f"cron in error state: {name}")
    for p in checks["state_db"].get("oversized", []):
        a.append(f"state.db oversized: {p}")
    return a


# --------------------------------------------------------------------------- #
# Render + CLI                                                                 #
# --------------------------------------------------------------------------- #
_BADGE = {"green": "🟢 GREEN", "yellow": "🟡 YELLOW", "red": "🔴 RED", "unknown": "⚪ UNKNOWN"}


def render_markdown(rep: dict) -> str:
    c = rep["checks"]
    L = [f"# Memory Stack Health — {_BADGE.get(rep['overall'], rep['overall'])}",
         f"_{rep['generated_at']} · {rep['home']}_", ""]
    if rep["alerts"]:
        L.append("## Alerts")
        L += [f"- ⚠️ {x}" for x in rep["alerts"]]
        L.append("")
    L.append("## Capacity")
    for store, f in c["capacity"].get("files", {}).items():
        if f.get("exists"):
            L.append(f"- {store}.md: {f['pct']}% ({f['chars']}/{f['limit']} chars, {f['entries']} entries) [{f['status']}]")
        else:
            L.append(f"- {store}.md: not found")
    ep = c["capacity"].get("entry_pressure")
    if ep:
        L.append(f"- MEMORY entries: {ep['count']} (target {ep['target']}, ceiling {ep['ceiling']}) [{ep['status']}]")
    t = c["temporal"]
    L += ["", "## Temporal"]
    if t["status"] == "unknown" or t["status"] == "error":
        L.append(f"- {t.get('note')}")
    else:
        L.append(f"- facts={t.get('facts')} versions={t.get('versions')} current={t.get('current_facts')} "
                 f"all_match={t.get('all_match')} drift={t.get('content_drift')} [{t['status']}]")
    ha = c.get("hot_audit", {})
    L += ["", "## Hot-memory audit"]
    if ha.get("status") == "error":
        L.append(f"- {ha.get('note')}")
    else:
        L.append(f"- broken_pointers={len(ha.get('broken_pointers') or [])} duplicates={ha.get('duplicate_pairs')} contradictions={ha.get('contradiction_pairs')} [{ha.get('status')}]")
        if ha.get("broken_pointers"):
            L.append("- broken refs: " + ", ".join(ha.get("broken_pointers")[:10]))
    L += ["", "## Memory crons"]
    for j in c["crons"].get("jobs", []):
        flag = "" if j["flag"] == "ok" else "  ⚠️"
        L.append(f"- {j['name']}: last={j['last_status']} at {j['last_run_at']} "
                 f"{'(paused)' if j['paused'] else ''}{flag}")
    if c["crons"].get("status") == "unknown":
        L.append(f"- {c['crons'].get('note')}")
    L += ["", "## state.db"]
    for d in c["state_db"].get("dbs", [])[:8]:
        L.append(f"- {d['path']}: {d['mb']}MB [{d['flag']}]")
    L += ["", "## Semantic / auto-extraction",
          f"- semantic: {c['semantic'].get('note')}",
          f"- auto-extract: candidates={c['auto_extract'].get('candidate_count')} "
          f"({c['auto_extract'].get('note', c['auto_extract'].get('latest_file',''))})"]
    L += ["", "---", "_Read-only health check. Alerts are in the content; this exits 0 on success._"]
    return "\n".join(L)


def render_summary(rep: dict) -> str:
    return (f"Memory health: {_BADGE.get(rep['overall'], rep['overall'])} · "
            f"{len(rep['alerts'])} alert(s)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_health.py",
        description="Read-only memory-stack health check (green/yellow/red). Exit 0 on success "
                    "even when red; exit 1 only on a real script failure.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    p.add_argument("--memory", help="MEMORY.md path override")
    p.add_argument("--user", help="USER.md path override")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--markdown", action="store_true", help="emit full markdown (default: markdown)")
    p.add_argument("--summary", action="store_true", help="one-line summary only")
    p.add_argument("--out", help="also write the report to this path")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    home = args.home or os.environ.get("HERMES_HOME") or "~/.hermes"
    try:
        rep = run_health(home, memory_path=os.path.expanduser(args.memory) if args.memory else None,
                         user_path=os.path.expanduser(args.user) if args.user else None)
    except Exception as e:  # genuine script failure
        print(f"memory_health failed: {e}", file=sys.stderr)
        return 1
    if args.json:
        text = json.dumps(rep, indent=2, default=str)
    elif args.summary:
        text = render_summary(rep)
    else:
        text = render_markdown(rep)
    print(text)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as e:
            print(f"(could not write --out: {e})", file=sys.stderr)
    return 0  # success: alerts are in the content + overall score, never the exit code


if __name__ == "__main__":
    raise SystemExit(main())
