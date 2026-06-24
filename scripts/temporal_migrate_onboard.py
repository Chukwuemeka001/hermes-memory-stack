#!/usr/bin/env python3
"""Hermes temporal migration onboarding — Area 4 of the remediation pipeline.

Wires hot memory into the bi-temporal layer (``temporal_memory.py``) so future
audits/rewrites can diff, roll back, and trace provenance. Three commands:

  * ``verify``        — reconstruct the current MEMORY.md / USER.md from the
                        temporal DB (replay events → current facts in original
                        order) and confirm it matches the live files exactly.
                        (Onboarding-spec rule #6.) Read-only.
  * ``sync``          — compare live files vs the temporal DB; on an empty store
                        do the first migration, otherwise detect DRIFT (entries
                        added/changed/removed outside the temporal layer). Dry-run
                        by default; ``--confirm-apply`` records the deltas as events.
  * ``record-rewrite``— take an Area 3 render manifest and record the rewrite as
                        temporal events (baseline snapshot → update/merge/delete),
                        preserving the full provenance chain. Dry-run by default;
                        ``--confirm-apply`` writes the events.

SAFETY:
  * NEVER writes MEMORY.md / USER.md. Area 4 only ever appends to the temporal
    layer (history.jsonl + the rebuildable memory_versions.db), and only with
    ``--confirm-apply``. ``verify`` is fully read-only.
  * Reconstruction orders current facts by each fact's first-seen ``seq`` (its
    create-event = original file position), so a faithful migration round-trips
    byte-for-byte.
  * Exportable: works against any ``--home`` (Atlas or any new user).

stdlib only; builds on ``temporal_memory.TemporalMemory`` (no LLM/network).

Usage:
    python3 temporal_migrate_onboard.py verify --home ~/.hermes [--json]
    python3 temporal_migrate_onboard.py sync   --home ~/.hermes [--confirm-apply]
    python3 temporal_migrate_onboard.py record-rewrite --home ~/.hermes \\
        --manifest /tmp/proposed/manifest.json [--confirm-apply]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import temporal_memory as TM  # noqa: E402

TOOL_VERSION = "1.0.0"
DELIM = TM.ENTRY_DELIMITER
STORES = ("MEMORY.md", "USER.md")
# Area 3 manifest store names -> temporal store filenames.
STORE_MAP = {"memory": "MEMORY.md", "user": "USER.md"}


# --------------------------------------------------------------------------- #
# Reconstruction                                                              #
# --------------------------------------------------------------------------- #
def _first_seen_order(tm: TM.TemporalMemory, store: str) -> dict:
    """Each current fact's create-event seq = its original file position."""
    return {r["fact_key"]: r["m"] for r in tm.conn.execute(
        "SELECT fact_key, MIN(seq) AS m FROM versions WHERE store=? GROUP BY fact_key",
        (store,))}


def reconstruct(tm: TM.TemporalMemory, store: str) -> str:
    """Rebuild a store's file text from the temporal DB: current (live,
    non-tombstoned) facts joined by the delimiter in original creation order."""
    order = _first_seen_order(tm, store)
    cur = tm.current(store=store)
    cur.sort(key=lambda r: (order.get(r["fact_key"], 1 << 60), r["fact_key"]))
    return DELIM.join(r["content"] for r in cur)


def _live_entries(text: str) -> list[str]:
    return [e.strip() for e in text.split(DELIM) if e.strip()]


# --------------------------------------------------------------------------- #
# verify                                                                       #
# --------------------------------------------------------------------------- #
def verify(tm: TM.TemporalMemory, live_files: dict) -> dict:
    """Read-only: reconstruct each store and compare to its live file."""
    stats = tm.stats()
    out = {"tool": "temporal_migrate_onboard", "tool_version": TOOL_VERSION,
           "command": "verify", "home": str(tm.home),
           "facts": stats.get("facts"), "versions": stats.get("total_versions"),
           "current_facts": stats.get("current_facts"),
           "facts_with_history": stats.get("facts_with_history"),
           "tombstoned": stats.get("tombstoned"), "stores": {}, "all_match": True}
    for store, path in live_files.items():
        rec = {"store": store, "live_path": path, "live_exists": bool(path and os.path.exists(path))}
        recon = reconstruct(tm, store)
        live = TM.Path(path).read_text(encoding="utf-8") if rec["live_exists"] else ""
        rec["reconstruct_chars"] = len(recon)
        rec["live_chars"] = len(live)
        rec["exact_match"] = (recon == live)
        # Honest diagnostics when not byte-exact. Use MULTISETS so a dropped
        # duplicate entry surfaces as content loss, not a cosmetic reorder.
        recon_entries, live_entries = _live_entries(recon), _live_entries(live)
        recon_ct, live_ct = Counter(recon_entries), Counter(live_entries)
        rec["content_multiset_match"] = (recon_ct == live_ct)
        rec["entries_only_in_live"] = sum((live_ct - recon_ct).values())      # not captured in temporal
        rec["entries_only_in_temporal"] = sum((recon_ct - live_ct).values())  # removed from live, still current
        # whitespace/trailing-newline only: content faithful, bytes differ
        rec["whitespace_only_diff"] = (not rec["exact_match"]) and (DELIM.join(live_entries) == recon)
        # genuine reorder: same entries+counts, different order, not just whitespace
        rec["order_differs"] = (rec["content_multiset_match"] and not rec["exact_match"]
                                and not rec["whitespace_only_diff"])
        rec["content_drift"] = not rec["content_multiset_match"]
        if not rec["exact_match"]:
            out["all_match"] = False
        out["stores"][store] = rec
    return out


# --------------------------------------------------------------------------- #
# sync (drift detection + first migration)                                    #
# --------------------------------------------------------------------------- #
def diff_live_vs_temporal(tm: TM.TemporalMemory, store: str, entries: list[str]) -> dict:
    """DRY classification of live entries vs the temporal current set (no writes).
    Mirrors ingest_files' reconciliation so the preview matches what apply does."""
    cur_rows = tm.conn.execute(
        "SELECT fact_key, content, content_hash FROM versions WHERE is_current=1 AND store=?",
        (store,)).fetchall()
    cur_keys = {r["fact_key"] for r in cur_rows}
    new, updated, unchanged = [], [], []
    assigned: dict[str, str] = {}
    touched: set[str] = set()
    for text in entries:
        h = TM.content_hash(text)
        m = tm.match(text, store=store)
        key, action = m["fact_key"], m["action"]
        if key in assigned and assigned[key] != h:
            n = 2
            while f"{key}-{n}" in assigned:
                n += 1
            key, action = f"{key}-{n}", "NEW"
        # byte-identical repeat already seen this pass: apply would dedup to one
        # fact (create once, the rest no-op) — mirror that, don't recount as NEW.
        if key in assigned and assigned[key] == h:
            unchanged.append(key)
            continue
        assigned[key] = h
        touched.add(key)
        if action == "NEW":
            new.append(key)
        elif action == "DUPLICATE":
            unchanged.append(key)
        else:
            updated.append(key)
    removed = sorted(cur_keys - touched)
    first_migration = len(cur_keys) == 0
    return {"store": store, "first_migration": first_migration,
            "new": new, "updated": updated, "unchanged": unchanged, "removed": removed,
            "drift": bool(new or updated or removed) and not first_migration}


def sync(tm: TM.TemporalMemory, live_files: dict, *, confirm: bool, source: str = "sync") -> dict:
    out = {"tool": "temporal_migrate_onboard", "tool_version": TOOL_VERSION,
           "command": "sync", "home": str(tm.home), "applied": False,
           "dry_run": not confirm, "stores": {}, "drift_detected": False}
    for store, path in live_files.items():
        entries = tm._read_file_entries(TM.Path(path)) if (path and os.path.exists(path)) else []
        d = diff_live_vs_temporal(tm, store, entries)
        d["live_entries"] = len(entries)
        out["stores"][store] = d
        if d["drift"]:
            out["drift_detected"] = True
    if confirm:
        present = [s for s, p in live_files.items() if p and os.path.exists(p)]
        summary = tm.ingest_files(present, source=source, actor="temporal_migrate_onboard")
        out["applied"] = True
        out["ingest_summary"] = summary
    return out


# --------------------------------------------------------------------------- #
# record-rewrite (Area 3 → temporal events with provenance)                   #
# --------------------------------------------------------------------------- #
def _resolve_fact_key(tm: TM.TemporalMemory, store: str, old_text: str,
                      old_hash: str, assigned: dict) -> str:
    """Map a proposal's original text to the SAME fact_key the temporal layer uses
    (reusing ingest's `-2` disambiguation), by content-hash. Falls back to
    derive_key with within-manifest collision suffixing. Never reuses a key
    already claimed by another proposal in this run."""
    row = tm.conn.execute(
        "SELECT fact_key FROM versions WHERE store=? AND is_current=1 AND content_hash=?",
        (store, old_hash)).fetchone()
    if row and row["fact_key"] not in assigned:
        return row["fact_key"]
    row = tm.conn.execute(
        "SELECT fact_key FROM versions WHERE store=? AND content_hash=? ORDER BY version LIMIT 1",
        (store, old_hash)).fetchone()
    if row and row["fact_key"] not in assigned:
        return row["fact_key"]
    key = TM.derive_key(old_text)
    if key in assigned and assigned[key] != old_hash:
        n = 2
        while f"{key}-{n}" in assigned:
            n += 1
        key = f"{key}-{n}"
    return key


def plan_rewrite_events(tm: TM.TemporalMemory, manifest: dict) -> list[dict]:
    """Compute the temporal events that record an Area 3 rewrite. Pure (no write).

    Records under the ORIGINAL fact's key so the chain is old→new (provenance).
    A pre-rewrite baseline is recorded iff that exact text isn't already in the
    fact's history. IDEMPOTENT: a proposal already at its target end-state
    (pointer already current, or already tombstoned) produces NO events, so
    re-running the same manifest never fabricates A→B→A history."""
    events = []
    assigned: dict[str, str] = {}
    for p in manifest.get("proposals", []):
        ra = p.get("rewrite_action")
        if ra in (None, "keep", "review"):
            continue
        store = STORE_MAP.get(p.get("store"), "MEMORY.md")
        old_text = p.get("old_text") or ""
        old_hash = TM.content_hash(old_text)
        key = _resolve_fact_key(tm, store, old_text, old_hash, assigned)
        assigned[key] = old_hash

        hist = tm.conn.execute(
            "SELECT content_hash, is_current, op FROM versions WHERE fact_key=? AND store=? "
            "ORDER BY version", (key, store)).fetchall()
        cur = next((h for h in hist if h["is_current"]), None)
        hist_hashes = {h["content_hash"] for h in hist}
        tombstoned = (cur is None) and bool(hist) and hist[-1]["op"] == "delete"
        baseline_op = "update" if hist else "create"

        def baseline():
            # preserve the pre-rewrite snapshot only if not already in history
            if old_hash not in hist_hashes:
                events.append({"fact_key": key, "store": store, "op": baseline_op,
                               "content": old_text, "source": "pre-rewrite-baseline",
                               "reason": "snapshot before rewrite"})

        if ra in ("rewrite_to_pointer", "archive_pointer"):
            new_hash = TM.content_hash(p.get("new_text") or "")
            if cur is not None and cur["content_hash"] == new_hash:
                continue  # already applied — idempotent no-op
            baseline()
            events.append({"fact_key": key, "store": store, "op": "update", "content": p["new_text"],
                           "source": "area3-rewrite", "reason": ra,
                           "archived_path": (p.get("archive") or {}).get("destination")})
        elif ra == "remove":
            if tombstoned:
                continue  # already removed — idempotent no-op
            baseline()
            events.append({"fact_key": key, "store": store, "op": "delete", "content": old_text,
                           "source": "area3-rewrite", "reason": "archived then removed from hot memory",
                           "archived_path": (p.get("archive") or {}).get("destination")})
        elif ra == "merge_absorb":
            if tombstoned:
                continue
            baseline()
            events.append({"fact_key": key, "store": store, "op": "delete", "content": old_text,
                           "source": "area3-rewrite",
                           "reason": f"absorbed into {p.get('merge_into')} (near-duplicate)",
                           "tags": [f"merged_into:{p.get('merge_into')}"]})
    return events


def record_rewrite(tm: TM.TemporalMemory, manifest: dict, *, confirm: bool) -> dict:
    planned = plan_rewrite_events(tm, manifest)
    out = {"tool": "temporal_migrate_onboard", "tool_version": TOOL_VERSION,
           "command": "record-rewrite", "home": str(tm.home), "dry_run": not confirm,
           "events_planned": len(planned),
           "by_op": _count(planned, "op"), "by_source": _count(planned, "source"),
           "applied": False, "events_recorded": 0}
    if confirm:
        n = 0
        for ev in planned:
            res = tm.record(fact_key=ev["fact_key"], content=ev["content"], store=ev["store"],
                            op=ev["op"], source=ev["source"], reason=ev.get("reason"),
                            tags=ev.get("tags"), archived_path=ev.get("archived_path"),
                            actor="temporal_migrate_onboard",
                            allow_duplicate=(ev["op"] == "delete"))
            if res is not None:
                n += 1
        out["applied"] = True
        out["events_recorded"] = n
    return out


def _count(items: list[dict], field: str) -> dict:
    c = {}
    for it in items:
        c[it.get(field)] = c.get(it.get(field), 0) + 1
    return c


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _resolve_live_files(args) -> dict:
    home = os.path.abspath(os.path.expanduser(
        args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    mem_dir = os.path.join(home, "memories")
    files = {"MEMORY.md": os.path.join(mem_dir, "MEMORY.md"),
             "USER.md": os.path.join(mem_dir, "USER.md")}
    if getattr(args, "memory", None):
        files["MEMORY.md"] = os.path.expanduser(args.memory)
    if getattr(args, "user", None):
        files["USER.md"] = os.path.expanduser(args.user)
    # verify --against-dir: compare reconstruction to Area 3 proposed files
    if getattr(args, "against_dir", None):
        d = os.path.expanduser(args.against_dir)
        for store, name in (("MEMORY.md", "MEMORY.proposed.md"), ("USER.md", "USER.proposed.md")):
            cand = os.path.join(d, name)
            if os.path.exists(cand):
                files[store] = cand
    return files


def _tm(args) -> TM.TemporalMemory:
    return TM.TemporalMemory(home=args.home, db_path=getattr(args, "db_path", None),
                             jsonl_path=getattr(args, "jsonl_path", None))


def _emit(obj, args, human_lines):
    if getattr(args, "json", False):
        print(json.dumps(obj, indent=2, default=str))
    else:
        print("\n".join(human_lines))


def cmd_verify(args) -> int:
    tm = _tm(args)
    try:
        res = verify(tm, _resolve_live_files(args))
    finally:
        tm.conn.close()
    lines = [f"temporal verify — facts={res['facts']} versions={res['versions']} "
             f"current={res['current_facts']}"]
    for store, r in res["stores"].items():
        if r["exact_match"]:
            status = "EXACT MATCH"
        elif r.get("whitespace_only_diff"):
            status = "whitespace/formatting differs (content faithful)"
        elif r["order_differs"]:
            status = "content matches, ORDER differs"
        else:
            status = "CONTENT DRIFT"
        lines.append(f"  {store}: {status}  (reconstruct {r['reconstruct_chars']} chars vs "
                     f"live {r['live_chars']})")
        if r.get("content_drift"):
            lines.append(f"      entries only in live (not in temporal): {r['entries_only_in_live']}  ·  "
                         f"only in temporal (removed from live): {r['entries_only_in_temporal']}")
    lines.append(f"ALL MATCH: {res['all_match']}")
    _emit(res, args, lines)
    return 0 if res["all_match"] else 1


def cmd_sync(args) -> int:
    tm = _tm(args)
    try:
        res = sync(tm, _resolve_live_files(args), confirm=args.confirm_apply)
    finally:
        tm.conn.close()
    lines = [f"temporal sync — {'APPLIED' if res['applied'] else 'DRY-RUN (use --confirm-apply to record)'}"]
    for store, d in res["stores"].items():
        tag = "FIRST MIGRATION" if d["first_migration"] else ("DRIFT" if d["drift"] else "in sync")
        lines.append(f"  {store}: {tag} — live_entries={d['live_entries']} "
                     f"new={len(d['new'])} updated={len(d['updated'])} "
                     f"removed={len(d['removed'])} unchanged={len(d['unchanged'])}")
    if res.get("ingest_summary"):
        s = res["ingest_summary"]
        lines.append(f"  recorded: created={s['created']} updated={s['updated']} "
                     f"archived={s['archived']} deleted={s['deleted']}")
    lines.append(f"DRIFT DETECTED: {res['drift_detected']}")
    _emit(res, args, lines)
    return 0


def cmd_record_rewrite(args) -> int:
    if not args.manifest:
        print("record-rewrite requires --manifest <Area 3 manifest.json>", file=sys.stderr)
        return 2
    try:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as e:
        # UX-3: actionable message instead of a raw JSONDecodeError traceback.
        print(f"error: --manifest {args.manifest} is not valid JSON ({e}).\n"
              f"       Expected the manifest.json produced by "
              f"`memory_rewrite.py render --out-dir <dir>`.", file=sys.stderr)
        return 2
    if manifest.get("schema") != "hermes-memory-rewrite-manifest/1":
        print("warning: --manifest is not a recognized Area 3 manifest", file=sys.stderr)
    tm = _tm(args)
    try:
        res = record_rewrite(tm, manifest, confirm=args.confirm_apply)
    finally:
        tm.conn.close()
    lines = [f"temporal record-rewrite — {'APPLIED' if res['applied'] else 'DRY-RUN (use --confirm-apply)'}",
             f"  events planned: {res['events_planned']}  by_op={res['by_op']}  by_source={res['by_source']}"]
    if res["applied"]:
        lines.append(f"  events recorded: {res['events_recorded']}")
    _emit(res, args, lines)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="temporal_migrate_onboard.py",
        description="Area 4 — temporal migration onboarding (verify / sync / record-rewrite).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="NEVER writes MEMORY.md/USER.md. sync/record-rewrite are dry-run unless --confirm-apply; "
               "verify is read-only.\n\n"
               "PIPELINE (Area 4 -> Area 5): steps 8-10 of RUNBOOK.md.\n"
               "  in:    --manifest /tmp/proposed/manifest.json   (from memory_rewrite.py render)\n"
               "  this:  record-rewrite --manifest … --confirm-apply ; sync --confirm-apply ; verify\n"
               "  next:  memory_maintenance.py --home ~/.hermes   (Area 5 health pass)")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
        sp.add_argument("--memory", help="MEMORY.md path override")
        sp.add_argument("--user", help="USER.md path override")
        sp.add_argument("--db-path", help="temporal index DB path override (tests)")
        sp.add_argument("--jsonl-path", help="temporal history.jsonl path override (tests)")

    v = sub.add_parser("verify", help="reconstruct current files from temporal DB and compare (read-only)")
    common(v)
    v.add_argument("--against-dir", help="compare reconstruction to Area 3 proposed files in this dir")
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("sync", help="detect drift / first-migrate live files into the temporal layer")
    common(s)
    s.add_argument("--confirm-apply", action="store_true", help="record deltas as events (else dry-run)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sync)

    r = sub.add_parser("record-rewrite", help="record an Area 3 rewrite as temporal events")
    common(r)
    r.add_argument("--manifest", help="Area 3 render manifest.json (required)")
    r.add_argument("--confirm-apply", action="store_true", help="write the events (else dry-run)")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_record_rewrite)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
