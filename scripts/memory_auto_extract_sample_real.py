#!/usr/bin/env python3
"""Sampler: build a fixtures-format corpus from REAL Claude Code transcripts.

These are genuine, unseen, instruction-heavy sessions. They contain almost no
durable personal facts, so they are a strong PRECISION stress-test: a clean
extractor should emit close to nothing across dozens of them. Output is the
same JSONL shape memory_auto_extract.py --fixtures reads (no _expected labels).

    python3 memory_auto_extract_sample_real.py --max-sessions 50 --out /tmp/real_corpus.jsonl
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

import memory_auto_extract as mod  # reuse is_automation

PROJECT_DIRS = [
    Path.home() / ".claude" / "projects" / "-Users-emeka",
    Path.home() / ".claude" / "projects" / "-Users-emeka--hermes",
]

# Harness / non-human fingerprints to drop (these are not Emeka typing).
SKIP_RE = re.compile(
    r"^\s*<(role|system|command|local-command|user-prompt|task)"
    r"|<system-reminder>|tool_result|tool_use|This session is being continued"
    r"|Caveat:|\[Request interrupted|Your task is to|You are |```", re.I)


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return ""


def human_turns(path: str) -> list[str]:
    turns, seen = [], set()
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if o.get("type") != "user":
            continue
        m = o.get("message") or {}
        txt = extract_text(m.get("content")).strip()
        # Keep only plausibly human-typed messages.
        if not (12 <= len(txt) <= 1500):
            continue
        if txt.startswith("<") or SKIP_RE.search(txt):
            continue
        if mod.is_automation(txt):
            continue
        key = txt[:80].lower()
        if key in seen:
            continue
        seen.add(key)
        turns.append(re.sub(r"\s+", " ", txt))
    return turns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-sessions", type=int, default=50)
    ap.add_argument("--min-turns", type=int, default=2)
    ap.add_argument("--out", default="/tmp/real_corpus.jsonl")
    a = ap.parse_args()

    files = []
    for d in PROJECT_DIRS:
        files += glob.glob(str(d / "*.jsonl"))
    files.sort(key=os.path.getmtime, reverse=True)

    written = 0
    with open(a.out, "w", encoding="utf-8") as fh:
        for f in files:
            if written >= a.max_sessions:
                break
            turns = human_turns(f)
            if len(turns) < a.min_turns:
                continue
            msgs = [{"role": "user", "content": t} for t in turns[:20]]
            sid = "real-" + os.path.basename(f)[:8]
            fh.write(json.dumps({"session_id": sid, "source": "telegram",
                                 "_kind": "real_unlabeled", "messages": msgs},
                                ensure_ascii=False) + "\n")
            written += 1
    print(f"wrote {written} real sessions -> {a.out}")


if __name__ == "__main__":
    main()
