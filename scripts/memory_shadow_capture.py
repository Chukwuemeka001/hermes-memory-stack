#!/usr/bin/env python3
"""Capture one answer-aware shadow event and refresh the rollout report.

This is the operational wrapper for dogfooding memory projection after a real
turn: pass the user query and final answer, append a `memory_shadow.py` JSONL
event with `answer_usage`, then regenerate `memory_shadow_report.py` outputs.

It is still shadow-only: FULL memory remains the active answer source.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import memory_shadow as MS  # noqa: E402
import memory_shadow_report as MR  # noqa: E402

TOOL_VERSION = "0.1.0"


def _parse_today(value: str | None) -> _dt.date:
    if not value:
        return _dt.date.today()
    try:
        return _dt.date.fromisoformat(value)
    except ValueError as e:
        raise SystemExit(f"error: --today must be YYYY-MM-DD, got {value!r}") from e


def _read_answer(args: argparse.Namespace) -> str:
    answer = args.answer_text
    if args.answer_file:
        path = Path(os.path.expanduser(args.answer_file))
        answer = path.read_text(encoding="utf-8")
    if not answer or not answer.strip():
        raise ValueError("answer text is required; pass --answer-file or --answer-text")
    return answer


def _default_jsonl(home: str, today: _dt.date) -> str:
    return os.path.join(home, "notes", "memory-stack", f"shadow-projection-{today.isoformat()}.jsonl")


def _default_report_md(today: _dt.date) -> str:
    return os.path.join("reports", f"shadow-report-{today.isoformat()}.md")


def _default_report_json(today: _dt.date) -> str:
    return os.path.join("reports", f"shadow-report-{today.isoformat()}.json")


def capture_and_report(args: argparse.Namespace) -> dict:
    today = _parse_today(args.today)
    home = os.path.abspath(os.path.expanduser(args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    answer = _read_answer(args)
    out_jsonl = os.path.abspath(os.path.expanduser(args.out or _default_jsonl(home, today)))
    report_md = os.path.abspath(os.path.expanduser(args.report_md or _default_report_md(today)))
    report_json = os.path.abspath(os.path.expanduser(args.report_json or _default_report_json(today)))

    event = MS.run_shadow(
        home=home,
        query=args.query,
        budget=args.budget,
        user_home=args.user_home,
        today=today,
        memory_path=args.memory,
        user_path=args.user,
        db_path=args.db,
        always_inject_extra=args.always_inject_extra,
        identity_extra=args.identity_extra,
        relevance_n=args.relevance_n,
        relevance_reserve_count=args.relevance_reserve_count,
        relevance_reserve_threshold=args.relevance_reserve_threshold,
        recency_halflife_days=args.recency_halflife_days,
        stale_days=args.stale_days,
        max_entry_chars=args.max_entry_chars,
        answer=answer,
        include_blocks=False,
        turn_id=args.turn_id,
    )
    if event.get("active_block") != "full":
        raise RuntimeError("shadow invariant failed: active_block must remain full")
    MS.append_jsonl(out_jsonl, event)

    events, errors = MR.load_events([out_jsonl])
    report = MR.summarize(
        events,
        errors,
        min_avg_savings=args.min_avg_savings,
        max_missing_rate=args.max_missing_rate,
        require_semantic=not args.allow_nonsemantic,
        min_answer_turns=args.min_answer_turns,
        min_daemon_rate=args.min_daemon_rate,
        max_subprocess_rate=args.max_subprocess_rate,
        max_p95_retrieval_latency_ms=args.max_p95_retrieval_latency_ms,
    )
    Path(report_md).parent.mkdir(parents=True, exist_ok=True)
    Path(report_json).parent.mkdir(parents=True, exist_ok=True)
    Path(report_md).write_text(MR.render_markdown(report) + "\n", encoding="utf-8")
    Path(report_json).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    usage = event.get("answer_usage") or {}
    return {
        "tool": "memory_shadow_capture",
        "tool_version": TOOL_VERSION,
        "event_turn_id": event["turn_id"],
        "jsonl": out_jsonl,
        "report_md": report_md,
        "report_json": report_json,
        "event": {
            "full_tokens": event["full"]["tokens"],
            "projected_tokens": event["projected"]["tokens"],
            "savings_pct": event["projected"]["savings_pct"],
            "relevance_source": event["projected"].get("relevance_source"),
            "answer_used_entry_count": usage.get("used_entry_count", 0),
            "answer_used_missing_count": len(usage.get("used_missing_from_projection") or []),
            "raw_blocks_logged": False,
            "active_block": event["active_block"],
        },
        "report": {
            "status": report["status"],
            "warnings": report["warnings"],
            "failures": report["failures"],
            "metrics": report["metrics"],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_shadow_capture.py",
        description="Append one answer-aware memory shadow event and refresh shadow rollout reports.",
    )
    p.add_argument("--home", help="Hermes home (default $HERMES_HOME or ~/.hermes)")
    p.add_argument("--user-home", help="OS home for resolving ~/ paths in entries")
    p.add_argument("--memory", help="explicit MEMORY.md path")
    p.add_argument("--user", help="explicit USER.md path")
    p.add_argument("--db", help="temporal memory_versions.db path")
    p.add_argument("--query", required=True, help="current user turn/task")
    p.add_argument("--answer-file", help="final answer file for answer_usage telemetry")
    p.add_argument("--answer-text", help="final answer text; prefer --answer-file for long answers")
    p.add_argument("--budget", type=int, default=MS.DEFAULT_BUDGET)
    p.add_argument("--today", help="override today's date YYYY-MM-DD")
    p.add_argument("--out", help="shadow JSONL path (default ~/.hermes/notes/memory-stack/shadow-projection-<date>.jsonl)")
    p.add_argument("--report-md", help="Markdown report path (default reports/shadow-report-<date>.md)")
    p.add_argument("--report-json", help="JSON report path (default reports/shadow-report-<date>.json)")
    p.add_argument("--turn-id", help="stable turn id")
    p.add_argument("--always-inject-extra", metavar="REGEX")
    p.add_argument("--identity-extra", metavar="REGEX")
    p.add_argument("--relevance-n", type=int, default=MS.MP.DEFAULT_RELEVANCE_N)
    p.add_argument("--relevance-reserve-count", type=int, default=MS.MP.DEFAULT_RELEVANCE_RESERVE_COUNT)
    p.add_argument("--relevance-reserve-threshold", type=float, default=MS.MP.DEFAULT_RELEVANCE_RESERVE_THRESHOLD)
    p.add_argument("--recency-halflife-days", type=int, default=MS.MP.DEFAULT_RECENCY_HALFLIFE_DAYS)
    p.add_argument("--stale-days", type=int, default=MS.MP.DEFAULT_STALE_DAYS)
    p.add_argument("--max-entry-chars", type=int, default=MS.MP.DEFAULT_MAX_ENTRY_CHARS)
    p.add_argument("--min-avg-savings", type=float, default=40.0)
    p.add_argument("--max-missing-rate", type=float, default=0.10)
    p.add_argument("--min-answer-turns", type=int, default=5, help="answer-aware events required before PASS (default 5)")
    p.add_argument("--allow-nonsemantic", action="store_true")
    p.add_argument("--min-daemon-rate", type=float, default=0.95, help="minimum daemon retrieval path rate before PASS (default 0.95)")
    p.add_argument("--max-subprocess-rate", type=float, default=0.0, help="maximum subprocess fallback rate before PASS (default 0.0)")
    p.add_argument("--max-p95-retrieval-latency-ms", type=float, default=500.0, help="maximum P95 retrieval latency before PASS (default 500ms)")
    p.add_argument("--json", action="store_true", help="print full capture/report JSON")
    p.add_argument("--strict", action="store_true", help="exit nonzero on WARN as well as FAIL")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.budget < 0:
        print("error: --budget must be >= 0", file=sys.stderr)
        return 2
    try:
        result = capture_and_report(args)
    except (OSError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        ev = result["event"]
        rep = result["report"]
        print(
            f"captured {result['event_turn_id']} status={rep['status']} "
            f"full={ev['full_tokens']}tok projected={ev['projected_tokens']}tok "
            f"savings={ev['savings_pct']}% used_missing={ev['answer_used_missing_count']}"
        )
        print(f"jsonl {result['jsonl']}")
        print(f"report {result['report_md']}")
    if result["report"]["status"] == "FAIL" or (args.strict and result["report"]["status"] == "WARN"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
