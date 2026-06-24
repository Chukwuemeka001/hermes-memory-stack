#!/usr/bin/env python3
"""Labeled eval harness for memory_auto_extract.py.

Runs the extractor over the golden fixtures (known durable facts vs known
noise) and reports PRECISION (genuine / accepted) and RECALL (known facts
caught). Used to score each iteration of the review loop objectively.

    python3 memory_auto_extract_eval.py                  # golden fixtures
    python3 memory_auto_extract_eval.py --real --days 7  # real state.db (precision)

On real data there are no labels, so it just lists accepted facts for manual /
agent scoring (genuine durable NEW facts on Emeka's recent sessions are ~0,
so any accept is a likely false positive to confirm).
"""
from __future__ import annotations

import argparse
import types

import memory_auto_extract as mod
import hermes_memory_intake_gate as gate

FIXTURES = str(mod.Path(__file__).resolve().parent / "memory_auto_extract_fixtures.jsonl")


def overlap(expected: str, got: str) -> float:
    """Overlap coefficient of content tokens, with light 5-char-prefix stemming
    so morphological rephrasings count (schedule/scheduled, prefer/prefers)."""
    def stem(s):
        return {t[:5] for t in gate.tokens(s)}
    a, b = stem(expected), stem(got)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def matches_expected(got: str, expected_list: list[str], thresh=0.5) -> bool:
    return any(overlap(e, got) >= thresh for e in expected_list)


def accepted_of(session) -> list[dict]:
    return [c for c in session["candidates"] if c["verdict"] in mod.CONFIG["accept_verdicts"]]


def run_eval(real=False, days=7, fixtures_file=None):
    fx = None if real else (fixtures_file or FIXTURES)
    args = types.SimpleNamespace(fixtures=fx, days=days, debug=False)
    report = mod.run(args)

    if real:
        print(f"\n=== REAL DATA eval — {report['scope']} ===")
        print(f"sessions scanned: {report['n_sessions_scanned']}, "
              f"accepted: {report['counts']['accepted']}, "
              f"review: {report['counts']['review']}, rejected: {report['counts']['rejected']}")
        print("\nACCEPTED (each is a likely false positive unless genuinely durable+new):")
        for c in report["accepted"]:
            print(f"  ⚠️  {c['fact']}  [{c['category']}, jac={c.get('max_jaccard_existing',0)}]")
        if not report["accepted"]:
            print("  (none) ✅ — no false positives on real recent sessions")
        print("\nREVIEW (surfaced, not written):")
        for c in report["review"]:
            print(f"  🟡 {c['fact']}  [{c['category']}]")
        return report, None

    # ---- labeled scoring ----
    tp = fp = 0                      # accepted: genuine vs false-positive
    recall_hit = recall_total = 0
    rows = []
    for s in report["per_session"]:
        lab = s.get("_labels", {})
        kind = lab.get("_kind", "?")
        expected = lab.get("_expected_facts", [])
        acc = accepted_of(s)

        if kind == "recall":
            recall_total += 1
            hit = any(matches_expected(c["fact"], expected) for c in acc)
            recall_hit += 1 if hit else 0
            for c in acc:
                genuine = matches_expected(c["fact"], expected)
                tp += 1 if genuine else 0
                fp += 0 if genuine else 1
                rows.append((kind, "✅TP" if genuine else "❓extra", c["fact"], s["session_id"]))
            if not acc:
                rows.append((kind, "❌MISS", f"(expected: {expected[0] if expected else ''})",
                             s["session_id"]))
        else:  # noise or dedup — nothing should be accepted
            for c in acc:
                fp += 1
                rows.append((kind, "❌FP", c["fact"], s["session_id"]))
            if not acc:
                rows.append((kind, "✅ok", "(correctly nothing accepted)", s["session_id"]))

    total_acc = tp + fp
    precision = tp / total_acc if total_acc else None
    recall = recall_hit / recall_total if recall_total else None

    print("\n=== GOLDEN FIXTURE eval ===")
    print(f"{'kind':<8} {'result':<8} {'session':<22} fact")
    print("-" * 90)
    for kind, res, fact, sid in rows:
        print(f"{kind:<8} {res:<8} {sid:<22} {fact[:60]}")
    print("-" * 90)
    pstr = f"{precision:.0%}" if precision is not None else "n/a (0 accepted)"
    rstr = f"{recall:.0%}" if recall is not None else "n/a"
    print(f"PRECISION (genuine/accepted): {tp}/{total_acc} = {pstr}")
    print(f"RECALL    (known facts caught): {recall_hit}/{recall_total} = {rstr}")
    print(f"False positives (noise/dedup accepted): {fp}")
    return report, {"precision": precision, "recall": recall, "tp": tp, "fp": fp,
                    "recall_hit": recall_hit, "recall_total": recall_total}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="run on real state.db instead of fixtures")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--fixtures-file", help="path to an alternate fixtures JSONL (e.g. holdout)")
    a = ap.parse_args()
    run_eval(real=a.real, days=a.days, fixtures_file=a.fixtures_file)
