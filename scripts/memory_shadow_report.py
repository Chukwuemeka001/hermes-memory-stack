#!/usr/bin/env python3
"""Summarize memory_shadow.py JSONL telemetry and gate projection rollout.

The report is deliberately cheap: it reads append-only JSONL shadow events, does
stdlib aggregation, and emits a small JSON/Markdown decision artifact. It never
loads ChromaDB, never calls an LLM, and never reads raw memory blocks unless they
were already logged by shadow mode.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import Any

TOOL_VERSION = "0.1.0"
CRITICAL_PIN_CLASSES = {"safety", "identity", "operational"}
SEMANTIC_SOURCE_MARKERS = ("memories-index:", "semantic", "subprocess:", "direct")


def _pct(v: float | int | None) -> float:
    try:
        if v is None or math.isnan(float(v)):
            return 0.0
        return round(float(v), 2)
    except Exception:
        return 0.0


def _mean(values: list[float | int]) -> float:
    return round(statistics.mean(values), 2) if values else 0.0


def _p95(values: list[float | int]) -> float:
    if not values:
        return 0.0
    vals = sorted(float(v) for v in values)
    idx = max(0, min(len(vals) - 1, math.ceil(len(vals) * 0.95) - 1))
    return round(vals[idx], 2)


def load_events(paths: list[str]) -> tuple[list[dict], list[dict]]:
    events: list[dict] = []
    errors: list[dict] = []
    for raw_path in paths:
        path = Path(os.path.expanduser(raw_path))
        if not path.exists():
            errors.append({"path": str(path), "line": 0, "error": "missing-file"})
            continue
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    event.setdefault("_source_path", str(path))
                    event.setdefault("_source_line", lineno)
                    events.append(event)
                except Exception as e:
                    errors.append({"path": str(path), "line": lineno, "error": str(e)})
    return events, errors


def _event_has_raw_blocks(event: dict) -> bool:
    return "block" in (event.get("full") or {}) or "block" in (event.get("projected") or {})


def _source_is_semantic(source: str) -> bool:
    low = (source or "").lower()
    return any(marker in low for marker in SEMANTIC_SOURCE_MARKERS) and not low.startswith("disabled:")


def _collect_used_missing(event: dict) -> list[dict]:
    usage = event.get("answer_usage") or {}
    missing = usage.get("used_missing_from_projection") or []
    if not isinstance(missing, list):
        return []
    out = []
    for item in missing:
        if not isinstance(item, dict):
            continue
        out.append({
            "turn_id": event.get("turn_id"),
            "entry_ref": item.get("entry_ref"),
            "pin_class": item.get("pin_class", "none"),
            "overlap": item.get("overlap"),
            "reason": item.get("reason"),
            "preview": item.get("preview", ""),
        })
    return out


def summarize(events: list[dict], errors: list[dict], *, min_avg_savings: float,
              max_missing_rate: float, require_semantic: bool = True,
              min_answer_turns: int = 5, min_daemon_rate: float = 0.95,
              max_subprocess_rate: float = 0.0, max_p95_retrieval_latency_ms: float = 500.0) -> dict:
    raw_shadow_events = [e for e in events if e.get("tool") == "memory_shadow" or e.get("mode") == "shadow"]
    duplicates = 0
    by_turn: dict[str, dict] = {}
    shadow_events: list[dict] = []
    for e in raw_shadow_events:
        tid = str(e.get("turn_id") or f"_line:{e.get('_source_path')}:{e.get('_source_line')}")
        if tid in by_turn:
            duplicates += 1
            old = by_turn[tid]
            if str(e.get("generated_at") or "") >= str(old.get("generated_at") or ""):
                by_turn[tid] = e
        else:
            by_turn[tid] = e
    shadow_events = list(by_turn.values())
    total = len(shadow_events)
    savings = [_pct((e.get("projected") or {}).get("savings_pct")) for e in shadow_events]
    projected_tokens = [int((e.get("projected") or {}).get("tokens") or 0) for e in shadow_events]
    full_tokens = [int((e.get("full") or {}).get("tokens") or 0) for e in shadow_events]
    selected_counts = [int((e.get("projected") or {}).get("entries_selected") or 0) for e in shadow_events]
    skipped_counts = [int((e.get("diff") or {}).get("skipped_count") or 0) for e in shadow_events]
    over_budget = [e.get("turn_id") for e in shadow_events if (e.get("projected") or {}).get("over_budget")]
    raw_block_events = [e.get("turn_id") for e in shadow_events if _event_has_raw_blocks(e)]
    active_not_full = [e.get("turn_id") for e in shadow_events if e.get("active_block") != "full"]

    source_counts = collections.Counter((e.get("projected") or {}).get("relevance_source") or "unknown" for e in shadow_events)
    semantic_count = sum(1 for e in shadow_events if _source_is_semantic((e.get("projected") or {}).get("relevance_source") or ""))
    retrieval_events = []
    retrieval_path_counts: collections.Counter[str] = collections.Counter()
    retrieval_latencies: list[float] = []
    retrieval_hits: list[int] = []
    retrieval_requested: list[int] = []
    retrieval_candidate_pool: list[int] = []
    for e in shadow_events:
        tel = (e.get("projected") or {}).get("retrieval_telemetry") or {}
        if not isinstance(tel, dict) or not tel:
            continue
        retrieval_events.append(tel)
        path = str(tel.get("path") or "unknown")
        retrieval_path_counts[path] += 1
        for key, bucket in (("retrieval_latency_ms", retrieval_latencies),
                            ("hits_returned", retrieval_hits),
                            ("n_requested", retrieval_requested),
                            ("candidate_pool_size", retrieval_candidate_pool)):
            val = tel.get(key)
            if val is None:
                continue
            try:
                bucket.append(float(val))
            except (TypeError, ValueError):
                pass

    skipped_ref_counts: collections.Counter[str] = collections.Counter()
    selected_ref_counts: collections.Counter[str] = collections.Counter()
    missing_ref_counts: collections.Counter[str] = collections.Counter()
    skipped_pin_counts: collections.Counter[str] = collections.Counter()
    missing_pin_counts: collections.Counter[str] = collections.Counter()
    missing_items: list[dict] = []
    answer_usage_events = 0
    safety_pin_drops: list[dict] = []
    determinism_groups: dict[tuple, set] = collections.defaultdict(set)

    for e in shadow_events:
        route_pkt = (e.get("projected") or {}).get("route_packet") or {}
        route_fp = route_pkt.get("feedback_fingerprint") if isinstance(route_pkt, dict) else None
        group_key = (e.get("query_sha256"), (e.get("full") or {}).get("sha256"), e.get("budget_tokens"), route_fp)
        determinism_groups[group_key].add((e.get("projected") or {}).get("sha256"))
        diff = e.get("diff") or {}
        skipped_ref_counts.update(str(x) for x in diff.get("skipped_refs") or [])
        selected_ref_counts.update(str(x) for x in diff.get("selected_refs") or [])
        for pe in e.get("per_entry") or []:
            if not isinstance(pe, dict) or pe.get("selected"):
                continue
            pin = pe.get("pin_class") or "none"
            if pin != "none":
                skipped_pin_counts[pin] += 1
            if pin == "safety":
                safety_pin_drops.append({"turn_id": e.get("turn_id"), "entry_ref": pe.get("entry_ref"), "pin_class": pin})
        usage = e.get("answer_usage")
        if usage:
            answer_usage_events += 1
            for item in _collect_used_missing(e):
                missing_items.append(item)
                if item.get("entry_ref"):
                    missing_ref_counts[str(item["entry_ref"])] += 1
                missing_pin_counts[item.get("pin_class") or "none"] += 1

    missing_rate = (len(missing_items) / answer_usage_events) if answer_usage_events else 0.0
    avg_savings = _mean(savings)
    semantic_rate = (semantic_count / total) if total else 0.0
    telemetry_rate = (len(retrieval_events) / total) if total else 0.0
    daemon_rate = (retrieval_path_counts.get("daemon", 0) / len(retrieval_events)) if retrieval_events else 0.0
    subprocess_rate = (retrieval_path_counts.get("subprocess", 0) / len(retrieval_events)) if retrieval_events else 0.0
    p95_latency_ms = _p95(retrieval_latencies)

    failures: list[str] = []
    warnings: list[str] = []
    if errors:
        failures.append(f"{len(errors)} malformed/missing input row(s)")
    if not total:
        failures.append("no shadow events found")
    if active_not_full:
        failures.append(f"{len(active_not_full)} event(s) did not keep active_block=full")
    if raw_block_events:
        failures.append(f"{len(raw_block_events)} event(s) logged raw memory blocks")
    if over_budget:
        failures.append(f"{len(over_budget)} projected event(s) exceeded budget")
    if safety_pin_drops:
        failures.append(f"{len(safety_pin_drops)} safety pinned entry drop(s)")
    determinism_violations = sum(1 for hashes in determinism_groups.values() if len({h for h in hashes if h}) > 1)
    if determinism_violations:
        failures.append(f"{determinism_violations} deterministic replay group(s) produced different projected hashes")
    critical_missing = sum(v for k, v in missing_pin_counts.items() if k in CRITICAL_PIN_CLASSES)
    if critical_missing:
        failures.append(f"{critical_missing} used missing safety/identity/operational pinned item(s)")

    if total and avg_savings < min_avg_savings:
        warnings.append(f"average savings {avg_savings}% below threshold {min_avg_savings}%")
    if require_semantic and total and semantic_rate < 1.0:
        warnings.append(f"semantic relevance source used for {semantic_rate:.0%} of turns")
    if total and telemetry_rate < 1.0:
        failures.append(f"retrieval telemetry present for {telemetry_rate:.0%} of turns")
    if retrieval_events and daemon_rate < min_daemon_rate:
        failures.append(f"daemon retrieval path rate {daemon_rate:.0%} below threshold {min_daemon_rate:.0%}")
    if retrieval_events and subprocess_rate > max_subprocess_rate:
        failures.append(f"subprocess fallback rate {subprocess_rate:.0%} exceeds threshold {max_subprocess_rate:.0%}")
    if retrieval_events and p95_latency_ms > max_p95_retrieval_latency_ms:
        failures.append(f"p95 retrieval latency {p95_latency_ms}ms exceeds threshold {max_p95_retrieval_latency_ms}ms")
    if answer_usage_events == 0:
        warnings.append("no answer_usage telemetry; cannot verify used-but-skipped context")
    elif answer_usage_events < min_answer_turns:
        warnings.append(f"only {answer_usage_events} answer_usage event(s); need >= {min_answer_turns} for rollout confidence")
    elif missing_rate > max_missing_rate:
        warnings.append(f"used-missing rate {missing_rate:.2f} exceeds threshold {max_missing_rate}")
    if skipped_pin_counts:
        # Skipped non-answer pins are not necessarily a failure, but they are rollout-sensitive.
        warnings.append(f"{sum(skipped_pin_counts.values())} skipped pinned entries need inspection")

    status = "FAIL" if failures else ("WARN" if warnings else "PASS")
    return {
        "tool": "memory_shadow_report",
        "tool_version": TOOL_VERSION,
        "status": status,
        "failures": failures,
        "warnings": warnings,
        "inputs": {"events": len(events), "shadow_events": total, "errors": errors},
        "metrics": {
            "avg_savings_pct": avg_savings,
            "min_savings_pct": min(savings) if savings else 0.0,
            "p95_projected_tokens": _p95(projected_tokens),
            "avg_projected_tokens": _mean(projected_tokens),
            "avg_full_tokens": _mean(full_tokens),
            "avg_selected_entries": _mean(selected_counts),
            "avg_skipped_entries": _mean(skipped_counts),
            "semantic_source_rate": round(semantic_rate, 4),
            "retrieval_telemetry_events": len(retrieval_events),
            "retrieval_telemetry_rate": round(telemetry_rate, 4),
            "daemon_path_rate": round(daemon_rate, 4),
            "subprocess_fallback_rate": round(subprocess_rate, 4),
            "p95_retrieval_latency_ms": p95_latency_ms,
            "avg_retrieval_latency_ms": _mean(retrieval_latencies),
            "avg_hits_returned": _mean(retrieval_hits),
            "avg_n_requested": _mean(retrieval_requested),
            "avg_candidate_pool_size": _mean(retrieval_candidate_pool),
            "answer_usage_events": answer_usage_events,
            "min_answer_turns": min_answer_turns,
            "used_missing_count": len(missing_items),
            "used_missing_rate": round(missing_rate, 4),
            "raw_block_events": len(raw_block_events),
            "over_budget_events": len(over_budget),
            "duplicate_turns": duplicates,
            "safety_pin_drops": len(safety_pin_drops),
            "determinism_violations": determinism_violations,
        },
        "sources": dict(source_counts.most_common()),
        "retrieval_paths": dict(retrieval_path_counts.most_common()),
        "top_skipped_refs": skipped_ref_counts.most_common(20),
        "top_selected_refs": selected_ref_counts.most_common(20),
        "top_used_missing_refs": missing_ref_counts.most_common(20),
        "pin_counts": {
            "skipped": dict(skipped_pin_counts),
            "used_missing": dict(missing_pin_counts),
        },
        "used_missing_examples": missing_items[:20],
        "rollout_decision": rollout_decision(status, warnings, failures),
    }


def rollout_decision(status: str, warnings: list[str], failures: list[str]) -> dict:
    if status == "PASS":
        return {
            "decision": "PASS",
            "message": "Projection can be trialed in low-risk lanes; keep shadow logging on.",
            "next_actions": [
                "Enable projected mode only for low-risk summaries/status checks.",
                "Continue shadow logs with answer_usage enabled for serious work.",
            ],
        }
    if status == "WARN":
        return {
            "decision": "WARN",
            "message": "Do not flip live projection globally; tune and collect more evidence.",
            "next_actions": [
                "Inspect top_skipped_refs and top_used_missing_refs.",
                "Raise budget or relevance reserve if required context is consistently skipped.",
                "Add pin rules for any skipped safety/identity/operational items.",
            ],
        }
    return {
        "decision": "FAIL",
        "message": "Keep full memory active. Projection failed a safety/telemetry gate.",
        "next_actions": [
            "Fix failures before another rollout attempt.",
            "Regenerate shadow logs after the fix.",
            "Do not use projected memory as active context.",
        ],
    }


def render_markdown(report: dict) -> str:
    m = report["metrics"]
    lines = [
        "# Memory Shadow Report",
        "",
        f"**Status:** {report['status']}",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Shadow events | {report['inputs']['shadow_events']} |",
        f"| Avg savings | {m['avg_savings_pct']}% |",
        f"| Min savings | {m['min_savings_pct']}% |",
        f"| Avg full tokens | {m['avg_full_tokens']} |",
        f"| Avg projected tokens | {m['avg_projected_tokens']} |",
        f"| P95 projected tokens | {m['p95_projected_tokens']} |",
        f"| Semantic source rate | {round(m['semantic_source_rate'] * 100, 2)}% |",
        f"| Retrieval telemetry events | {m['retrieval_telemetry_events']} |",
        f"| Daemon path rate | {round(m['daemon_path_rate'] * 100, 2)}% |",
        f"| Subprocess fallback rate | {round(m['subprocess_fallback_rate'] * 100, 2)}% |",
        f"| P95 retrieval latency | {m['p95_retrieval_latency_ms']} ms |",
        f"| Avg hits returned | {m['avg_hits_returned']} |",
        f"| Avg candidate pool | {m['avg_candidate_pool_size']} |",
        f"| Answer-usage events | {m['answer_usage_events']} |",
        f"| Used-missing count | {m['used_missing_count']} |",
        f"| Raw block events | {m['raw_block_events']} |",
        f"| Over-budget events | {m['over_budget_events']} |",
        f"| Safety pin drops | {m['safety_pin_drops']} |",
        f"| Determinism violations | {m['determinism_violations']} |",
        f"| Duplicate turns deduped | {m['duplicate_turns']} |",
        "",
    ]
    if report["failures"]:
        lines += ["## Failures", ""] + [f"- {x}" for x in report["failures"]] + [""]
    if report["warnings"]:
        lines += ["## Warnings", ""] + [f"- {x}" for x in report["warnings"]] + [""]
    lines += ["## Relevance sources", "", "| Source | Count |", "|---|---:|"]
    for src, count in report["sources"].items():
        lines.append(f"| `{src}` | {count} |")
    lines += ["", "## Top skipped refs", "", "| Ref | Count |", "|---|---:|"]
    for ref, count in report["top_skipped_refs"][:10]:
        lines.append(f"| `{ref}` | {count} |")
    lines += ["", "## Top used-but-missing refs", "", "| Ref | Count |", "|---|---:|"]
    for ref, count in report["top_used_missing_refs"][:10]:
        lines.append(f"| `{ref}` | {count} |")
    if not report["top_used_missing_refs"]:
        lines.append("| — | 0 |")
    decision = report["rollout_decision"]
    lines += ["", "## Rollout decision", "", f"**{decision['decision']}** — {decision['message']}", "", "### Next actions", ""]
    lines += [f"- {x}" for x in decision["next_actions"]]
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Summarize memory_shadow.py JSONL logs and decide projection rollout readiness.")
    p.add_argument("paths", nargs="+", help="shadow JSONL file(s)")
    p.add_argument("--json", action="store_true", help="emit JSON report instead of Markdown")
    p.add_argument("--out", help="write report to this file")
    p.add_argument("--min-avg-savings", type=float, default=40.0)
    p.add_argument("--max-missing-rate", type=float, default=0.10)
    p.add_argument("--min-answer-turns", type=int, default=5, help="answer-aware events required before PASS (default 5)")
    p.add_argument("--allow-nonsemantic", action="store_true", help="do not warn when relevance source is static/non-semantic")
    p.add_argument("--min-daemon-rate", type=float, default=0.95, help="minimum daemon retrieval path rate before PASS (default 0.95)")
    p.add_argument("--max-subprocess-rate", type=float, default=0.0, help="maximum subprocess fallback rate before PASS (default 0.0)")
    p.add_argument("--max-p95-retrieval-latency-ms", type=float, default=500.0, help="maximum P95 retrieval latency before PASS (default 500ms)")
    p.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as FAIL")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    events, errors = load_events(args.paths)
    report = summarize(events, errors, min_avg_savings=args.min_avg_savings,
                       max_missing_rate=args.max_missing_rate,
                       require_semantic=not args.allow_nonsemantic,
                       min_answer_turns=args.min_answer_turns,
                       min_daemon_rate=args.min_daemon_rate,
                       max_subprocess_rate=args.max_subprocess_rate,
                       max_p95_retrieval_latency_ms=args.max_p95_retrieval_latency_ms)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json else render_markdown(report)
    if args.out:
        out = Path(os.path.expanduser(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    if report["status"] == "FAIL" or (args.strict and report["status"] == "WARN"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
