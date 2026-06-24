#!/usr/bin/env python3
"""Hermes MEMORY.md Intake Gate — write-time classifier (heuristics, no LLM).

MEMORY.md is a HOT POINTER FILE injected into every Hermes turn. Every entry
costs context budget forever. This gate is the *write-time* enforcement layer
the deep-research doc (§2, §5.2) called for: it converts the prose intake policy
(notes/hermes/memory-intake-policy.md) into a mechanism, so junk never lands in
hot memory even when the agent is lazy.

It is INTENTIONALLY cheap and deterministic — Layer 1 of the recommended
rules→embedding→LLM cascade. It uses only regex/length/Jaccard heuristics; no
network, no model, no spend. (Layers 2/3 — embedding dedup + LLM adjudication —
are future work and are NOT implemented here.)

Decision (per intake policy decision tree):
    Standing user preference   → ALLOW  (route: USER.md)
    Routing pointer (1 line)   → ALLOW  (route: MEMORY.md)
    Active project pointer     → ALLOW  (route: MEMORY.md, replaces existing)
    Status update / event log  → REJECT (route: notes / Obsidian)
    Detailed knowledge dump    → REJECT (route: skill / Obsidian)
    Dream reflection           → REJECT (route: dream file)
    Near-duplicate of existing → REVIEW (route: consolidate/UPDATE, not ADD)
    Ambiguous, no durable sig  → REVIEW (route: pick a store; likely notes)

Usage:
    python3 hermes_memory_intake_gate.py "entry text here"
    echo "entry text" | python3 hermes_memory_intake_gate.py
    python3 hermes_memory_intake_gate.py --json "entry text"

Exit codes:  0 = ALLOW   1 = REJECT   2 = REVIEW (human/consolidation needed)

This gate ONLY classifies. It never writes to MEMORY.md. The caller decides what
to do with the verdict. Dry-run / advisory by nature.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from memory_signals import (  # noqa: E402 — shared signal source of truth (INTEG-8)
    ENTRY_DELIMITER, TEMPORAL_RE, COMPLETION_RE, METRIC_RE,
    LEADING_DATE_RE, PREF_RE, POINTER_RE, REFLECTION_RE,
)


def resolve_home(cli_home: str | None = None) -> Path:
    """Shared home resolution for the auto-extract tier (INTEG-2 / EXPORT-10):
    --home || $HERMES_HOME || ~/.hermes. Computed at CALL time, never at import,
    so a process that sets only $HERMES_HOME (a second user, CI, a test) is
    routed to that home instead of hard-binding to the real ~/.hermes."""
    if cli_home:
        return Path(cli_home).expanduser()
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(os.path.expanduser("~/.hermes"))


def memory_file(home: str | None = None) -> Path:
    """The hot pointer file (MEMORY.md) under the resolved home. Replaces the
    former import-time MEMORY_FILE constant so HERMES_HOME is honored."""
    return resolve_home(home) / "memories" / "MEMORY.md"

# An entry over this many chars is suspicious for a HOT pointer file: pointers
# are one line. Over the hard ceiling it is almost certainly a content dump.
SUSPICIOUS_LEN = 200
DUMP_LEN = 420

# Near-duplicate threshold (token Jaccard) vs existing MEMORY.md entries.
DUP_JACCARD = 0.6

# Linguistic signals (deep-research §2.1 — durable vs transient is detectable)
# are imported from memory_signals (INTEG-8): TEMPORAL_RE, COMPLETION_RE,
# METRIC_RE, LEADING_DATE_RE, PREF_RE, POINTER_RE, REFLECTION_RE. They are the
# SAME objects the read-time audit uses, so the write-time gate and the audit
# can never again disagree about what counts as a status update / pointer / pref.

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "via", "are",
    "was", "has", "have", "not", "but", "all", "any", "use", "used", "user",
    "emeka", "hermes", "when", "then", "must", "should", "after", "before",
    "current", "currently", "default", "also", "per", "set", "now", "new",
    "first", "still", "each", "every", "over", "without", "their", "they",
    "them", "than", "more", "less", "one", "two", "three",
}


def tokens(text: str) -> set[str]:
    toks = set(re.findall(r"[a-z][a-z0-9]{3,}", text.lower())) - STOPWORDS
    for p in re.findall(r"~?/[\w./-]+", text):          # keep paths
        toks.add(p.lower())
    return toks


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def read_existing_entries(home: str | None = None) -> list[str]:
    mf = memory_file(home)
    if not mf.exists():
        return []
    raw = mf.read_text(encoding="utf-8")
    return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]


def nearest_existing(text: str, home: str | None = None) -> tuple[float, str | None]:
    """Highest token-Jaccard against any existing MEMORY.md entry."""
    cand = tokens(text)
    best, best_entry = 0.0, None
    for e in read_existing_entries(home):
        j = jaccard(cand, tokens(e))
        if j > best:
            best, best_entry = j, e
    return best, best_entry


# ---------------------------------------------------------------------------
# Classifier (Layer 1 rules; first decisive signal wins)
# ---------------------------------------------------------------------------
def classify(text: str) -> dict:
    text = text.strip()
    low = text.lower()
    n = len(text)

    has_temporal = bool(TEMPORAL_RE.search(low))
    has_completion = bool(COMPLETION_RE.search(low))
    has_metric = bool(METRIC_RE.search(text))
    has_lead_date = bool(LEADING_DATE_RE.search(text))
    is_pointer = bool(POINTER_RE.search(text))
    is_pref = bool(PREF_RE.search(low))
    size_flag = "over-200" if n > SUSPICIOUS_LEN else "ok"

    def verdict(v, cat, route, reason):
        return {"verdict": v, "category": cat, "route": route, "reason": reason,
                "length": n, "size_flag": size_flag,
                "signals": {"temporal": has_temporal, "completion": has_completion,
                            "metric": has_metric, "leading_date": has_lead_date,
                            "pointer": is_pointer, "preference": is_pref}}

    if not text:
        return verdict("REJECT", "empty", "—", "empty entry")

    # 1. Dream reflection — belongs in its own dream file, never hot memory.
    if REFLECTION_RE.search(text):
        return verdict("REJECT", "dream_reflection", "dream file (~/.hermes/memories/dream-reflection-*.md)",
                       "reflection fingerprint ([M#] citations / reflection frontmatter)")

    # 2. Status update / event log — completed events & metric snapshots age out.
    if (has_temporal or has_lead_date) and (has_completion or has_metric):
        return verdict("REJECT", "status_update", "~/.hermes/notes/ or Obsidian",
                       "temporal/dated + completion/metric signal = status/event log")
    if has_lead_date and has_completion:
        return verdict("REJECT", "event_log", "~/.hermes/notes/",
                       "leading date + completion verb = dated event log")
    if has_metric and not is_pointer:
        return verdict("REJECT", "status_update", "~/.hermes/notes/",
                       "metric/version snapshot (point-in-time, not durable)")

    # 3. Detailed knowledge dump — long, non-pointer prose belongs in a store.
    if n > DUMP_LEN and not is_pointer:
        return verdict("REJECT", "detailed_knowledge", "skill file or Obsidian",
                       f"{n} chars of non-pointer detail (dump, not a hot pointer)")

    # 4. Routing / active-project pointer — the legitimate MEMORY.md content.
    if is_pointer and n <= SUSPICIOUS_LEN:
        return verdict("ALLOW", "pointer", "MEMORY.md",
                       "routing/active-project pointer, one line (points to a store)")
    if is_pointer and n <= DUMP_LEN:
        return verdict("ALLOW", "pointer", "MEMORY.md (TRIM)",
                       f"pointer but {n} chars (>200) — allowed, trim to one line")

    # 5. Standing user preference — durable, route to USER.md.
    if is_pref and not has_lead_date:
        if n <= SUSPICIOUS_LEN:
            return verdict("ALLOW", "preference", "USER.md (or MEMORY.md)",
                           "stative preference verb + no date anchor = standing preference")
        return verdict("REVIEW", "preference", "USER.md (CONDENSE)",
                       f"reads as preference but {n} chars — condense before adding")

    # 6. Long & ambiguous → store it, don't inject it.
    if n > SUSPICIOUS_LEN:
        return verdict("REVIEW", "ambiguous_long", "~/.hermes/notes/",
                       f"{n} chars, no clear pointer/preference signal — likely a note")

    # 7. Short & ambiguous → no durable signal; human picks the store.
    return verdict("REVIEW", "ambiguous", "~/.hermes/notes/ (or USER.md if a preference)",
                   "no temporal/metric/pointer/preference signal detected")


def gate(text: str, home: str | None = None) -> dict:
    result = classify(text)
    # Dedup / repetition check — only meaningful if it would otherwise land.
    if result["verdict"] == "ALLOW":
        j, match = nearest_existing(text, home)
        result["max_jaccard_existing"] = round(j, 2)
        if j >= DUP_JACCARD:
            preview = (match or "")[:70].replace("\n", " ")
            result["verdict"] = "REVIEW"
            result["category"] = "near_duplicate"
            result["route"] = "CONSOLIDATE / UPDATE existing (not ADD)"
            result["reason"] = (f"near-duplicate of existing entry "
                                f"(jaccard {j:.2f} ≥ {DUP_JACCARD}): \"{preview}…\"")
    return result


EXIT = {"ALLOW": 0, "REJECT": 1, "REVIEW": 2}


def render(result: dict) -> str:
    icon = {"ALLOW": "✅", "REJECT": "⛔", "REVIEW": "🟡"}[result["verdict"]]
    lines = [
        f"{icon} {result['verdict']}  —  {result['category']}",
        f"   route:  {result['route']}",
        f"   reason: {result['reason']}",
        f"   length: {result['length']} chars ({result['size_flag']})",
    ]
    if "max_jaccard_existing" in result:
        lines.append(f"   max similarity to existing entry: {result['max_jaccard_existing']}")
    sig = result.get("signals", {})
    on = [k for k, v in sig.items() if v]
    lines.append(f"   signals: {', '.join(on) if on else 'none'}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="MEMORY.md write-time intake gate (heuristics; ALLOW/REJECT/REVIEW).")
    ap.add_argument("text", nargs="?", help="candidate entry text (or pipe via stdin)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--home", default=None,
                    help="HERMES_HOME override (default: $HERMES_HOME or ~/.hermes)")
    args = ap.parse_args(argv)

    text = args.text if args.text is not None else sys.stdin.read()
    text = (text or "").strip()
    if not text:
        print("usage: hermes_memory_intake_gate.py \"entry text\"  (or pipe via stdin)",
              file=sys.stderr)
        return 1

    result = gate(text, home=args.home)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return EXIT[result["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
