#!/usr/bin/env python3
"""Shadow-mode memory projection telemetry.

Computes FULL and PROJECTED hot-memory blocks for a live/synthetic turn, records the
projection diff, but explicitly marks the active answer block as FULL. This is the
safe dogfood bridge before wiring projected memory into live Hermes prompt assembly.

READ-ONLY with respect to MEMORY.md/USER.md. The only write is an append-only JSONL
telemetry report under reports/ (or --out), unless --dry-run is set.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import memory_project as MP  # noqa: E402

TOOL_VERSION = "0.1.0"
DEFAULT_BUDGET = MP.DEFAULT_BUDGET
DEFAULT_REPORTS_DIR = "reports"
_TOKEN_RE = re.compile(r"[a-z0-9_./@+-]{3,}", re.I)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _parse_today(value: str | None) -> _dt.date:
    if not value:
        return _dt.date.today()
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as e:
        raise SystemExit(f"error: --today must be YYYY-MM-DD, got {value!r}") from e


def _default_out(today: _dt.date) -> str:
    return os.path.join(DEFAULT_REPORTS_DIR, f"shadow-projection-{today.isoformat()}.jsonl")


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _usage_against_answer(answer: str | None, entries: list[dict], *, threshold: float = 0.16) -> dict | None:
    """Best-effort deterministic usage signal for shadow dogfood.

    This is deliberately conservative and explainable. It does NOT claim semantic
    citation. It flags entries whose tokens overlap the answer enough to warrant
    inspection, plus exact path/identifier hits when present.
    """
    if not answer:
        return None
    ans_tokens = _tokens(answer)
    used = []
    for e in entries:
        text = e.get("text", "")
        etoks = _tokens(text)
        jac = _jaccard(ans_tokens, etoks)
        pathish = [tok for tok in etoks if ("/" in tok or tok.startswith("~")) and tok in ans_tokens]
        exact_label = (e.get("entry_ref") or "").lower() in answer.lower()
        if jac >= threshold or pathish or exact_label:
            used.append({
                "entry_ref": e["entry_ref"],
                "selected": bool(e.get("selected")),
                "pin_class": e.get("pin_class", "none"),
                "score": e.get("score"),
                "overlap": round(jac, 4),
                "path_hits": sorted(pathish)[:5],
                "reason": "path/label" if (pathish or exact_label) else "token-overlap",
                "preview": e.get("preview", ""),
            })
    used = sorted(used, key=lambda x: (not x["selected"], -x["overlap"], x["entry_ref"]))
    return {
        "answer_chars": len(answer),
        "answer_sha256": sha256_text(answer),
        "used_entry_count": len(used),
        "used_selected_count": sum(1 for u in used if u["selected"]),
        "used_missing_from_projection": [u for u in used if not u["selected"]],
        "used_entries": used,
        "method": "token-jaccard-plus-path-hits",
        "threshold": threshold,
    }


def _load_full_entries(home: str, *, memory_path: str | None, user_path: str | None,
                       user_home: str | None, today: _dt.date,
                       stale_days: int, max_entry_chars: int) -> list[dict]:
    if memory_path is None or user_path is None:
        d_mem, d_usr = MP.MA._default_paths(home)
        memory_path = memory_path or d_mem
        user_path = user_path or d_usr
    resolved_user_home = os.path.abspath(os.path.expanduser(user_home)) if user_home else os.path.expanduser("~")
    raw = MP._load_entries(memory_path, user_path, resolved_user_home,
                           today=today, stale_days=stale_days,
                           max_entry_chars=max_entry_chars)
    return [{
        "entry_ref": e["ref"],
        "store": e["store"],
        "index": e["index"],
        "text": e["text"],
        "tokens": MP.entry_weight(e["text"]),
        "preview": e["preview"],
    } for e in raw]


def run_shadow(*, home: str, query: str, budget: int = DEFAULT_BUDGET,
               user_home: str | None = None, today: _dt.date | None = None,
               memory_path: str | None = None, user_path: str | None = None,
               db_path: str | None = None, always_inject_extra: str | None = None,
               identity_extra: str | None = None, relevance_n: int = MP.DEFAULT_RELEVANCE_N,
               relevance_reserve_count: int = MP.DEFAULT_RELEVANCE_RESERVE_COUNT,
               relevance_reserve_threshold: float = MP.DEFAULT_RELEVANCE_RESERVE_THRESHOLD,
               recency_halflife_days: int = MP.DEFAULT_RECENCY_HALFLIFE_DAYS,
               stale_days: int = MP.DEFAULT_STALE_DAYS,
               max_entry_chars: int = MP.DEFAULT_MAX_ENTRY_CHARS,
               answer: str | None = None, include_blocks: bool = False,
               turn_id: str | None = None) -> dict:
    if not query:
        raise ValueError("query is required for shadow projection")
    if budget < 0:
        raise ValueError("budget must be >= 0")

    today = today or _dt.date.today()
    home = os.path.abspath(os.path.expanduser(home))
    full_entries = _load_full_entries(home, memory_path=memory_path, user_path=user_path,
                                      user_home=user_home, today=today,
                                      stale_days=stale_days,
                                      max_entry_chars=max_entry_chars)
    full_block = MP.ENTRY_DELIMITER.join(e["text"] for e in full_entries)
    full_by_ref = {e["entry_ref"]: e for e in full_entries}

    proj = MP.project(
        home, budget=budget, user_home=user_home, today=today,
        recency_halflife_days=recency_halflife_days, stale_days=stale_days,
        max_entry_chars=max_entry_chars, memory_path=memory_path, user_path=user_path,
        db_path=db_path, always_inject_extra=always_inject_extra,
        identity_extra=identity_extra, query=query, relevance_n=relevance_n,
        relevance_reserve_count=relevance_reserve_count,
        relevance_reserve_threshold=relevance_reserve_threshold)

    per = proj["per_entry"]
    selected_refs = {e["entry_ref"] for e in per if e["selected"]}
    skipped_refs = [e["entry_ref"] for e in per if not e["selected"]]
    enriched_entries = []
    for e in per:
        src = full_by_ref.get(e["entry_ref"], {})
        enriched_entries.append({**e, "text": src.get("text", "")})

    usage = _usage_against_answer(answer, enriched_entries)
    full_tokens = MP.est_tokens(full_block)
    projected_tokens = proj["projected_tokens"]

    event = {
        "tool": "memory_shadow",
        "tool_version": TOOL_VERSION,
        "mode": "shadow",
        "active_block": "full",
        "note": "Projected memory was computed and logged, but FULL memory remains the answer source.",
        "turn_id": turn_id or str(uuid.uuid4()),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "today": today.isoformat(),
        "home": home,
        "query": query,
        "query_sha256": sha256_text(query),
        "budget_tokens": budget,
        "full": {
            "entries_total": len(full_entries),
            "tokens": full_tokens,
            "sha256": sha256_text(full_block),
        },
        "projected": {
            "entries_selected": proj["entries_selected"],
            "entries_skipped": proj["entries_skipped"],
            "tokens": projected_tokens,
            "savings_pct": proj["savings_pct"],
            "sha256": sha256_text(proj["projected_block"]),
            "pinned_count": proj["pinned_count"],
            "pin_breakdown": proj["pin_breakdown"],
            "relevance_source": proj["relevance_source"],
            "relevance_reserved_count": proj["relevance_reserved_count"],
            "over_budget": proj["over_budget"],
        },
        "diff": {
            "selected_refs": sorted(selected_refs),
            "skipped_refs": sorted(skipped_refs),
            "skipped_count": len(skipped_refs),
            "selected_count": len(selected_refs),
        },
        "per_entry": [{k: v for k, v in e.items() if k != "text"} for e in per],
        "answer_usage": usage,
    }
    if include_blocks:
        event["full"]["block"] = full_block
        event["projected"]["block"] = proj["projected_block"]
    return event


def append_jsonl(path: str, event: dict) -> None:
    out = Path(os.path.expanduser(path))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_shadow.py",
        description="Run memory projection in SHADOW mode: log full-vs-projected telemetry while keeping FULL memory active.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="EXAMPLES:\n"
               "  memory_shadow.py --home ~/.hermes --query 'what do we know about NCLEX?' --json\n"
               "  memory_shadow.py --query \"$USER_TURN\" --out reports/shadow.jsonl\n")
    p.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    p.add_argument("--user-home", help="OS home for resolving ~/ paths in entries")
    p.add_argument("--memory", help="explicit MEMORY.md path")
    p.add_argument("--user", help="explicit USER.md path")
    p.add_argument("--db", help="temporal memory_versions.db path")
    p.add_argument("--query", required=True, help="current user turn/task")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help=f"projection token budget (default {DEFAULT_BUDGET})")
    p.add_argument("--today", help="override today's date YYYY-MM-DD")
    p.add_argument("--always-inject-extra", metavar="REGEX", help="extra operational pin topic regex")
    p.add_argument("--identity-extra", metavar="REGEX", help="extra identity pin topic regex")
    p.add_argument("--relevance-n", type=int, default=MP.DEFAULT_RELEVANCE_N)
    p.add_argument("--relevance-reserve-count", type=int, default=MP.DEFAULT_RELEVANCE_RESERVE_COUNT)
    p.add_argument("--relevance-reserve-threshold", type=float, default=MP.DEFAULT_RELEVANCE_RESERVE_THRESHOLD)
    p.add_argument("--recency-halflife-days", type=int, default=MP.DEFAULT_RECENCY_HALFLIFE_DAYS)
    p.add_argument("--stale-days", type=int, default=MP.DEFAULT_STALE_DAYS)
    p.add_argument("--max-entry-chars", type=int, default=MP.DEFAULT_MAX_ENTRY_CHARS)
    p.add_argument("--answer-file", help="optional answer text for deterministic used-memory overlap telemetry")
    p.add_argument("--answer-text", help="optional answer text (prefer --answer-file for long answers)")
    p.add_argument("--include-blocks", action="store_true", help="include raw full/projected blocks in JSON output/log (off by default)")
    p.add_argument("--out", help="append JSONL telemetry here (default reports/shadow-projection-<today>.jsonl)")
    p.add_argument("--dry-run", action="store_true", help="do not append telemetry; print only")
    p.add_argument("--json", action="store_true", help="print full event JSON (default prints compact summary)")
    p.add_argument("--turn-id", help="stable id for the turn being shadowed")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    today = _parse_today(args.today)
    home = os.path.abspath(os.path.expanduser(args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    answer = args.answer_text
    if args.answer_file:
        answer = Path(os.path.expanduser(args.answer_file)).read_text(encoding="utf-8")
    if args.budget < 0:
        print("error: --budget must be >= 0", file=sys.stderr)
        return 2

    try:
        event = run_shadow(
            home=home, query=args.query, budget=args.budget, user_home=args.user_home,
            today=today, memory_path=args.memory, user_path=args.user, db_path=args.db,
            always_inject_extra=args.always_inject_extra, identity_extra=args.identity_extra,
            relevance_n=args.relevance_n,
            relevance_reserve_count=args.relevance_reserve_count,
            relevance_reserve_threshold=args.relevance_reserve_threshold,
            recency_halflife_days=args.recency_halflife_days,
            stale_days=args.stale_days, max_entry_chars=args.max_entry_chars,
            answer=answer, include_blocks=args.include_blocks, turn_id=args.turn_id)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    out = args.out or _default_out(today)
    if not args.dry_run:
        append_jsonl(out, event)
        event["wrote"] = os.path.abspath(os.path.expanduser(out))

    if args.json:
        print(json.dumps(event, indent=2, ensure_ascii=False))
    else:
        print(
            f"shadow {event['turn_id']} active=FULL "
            f"full={event['full']['tokens']}tok projected={event['projected']['tokens']}tok "
            f"savings={event['projected']['savings_pct']}% "
            f"selected={event['projected']['entries_selected']}/{event['full']['entries_total']} "
            f"skipped={event['diff']['skipped_count']}"
        )
        if event.get("wrote"):
            print(f"wrote {event['wrote']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
