#!/usr/bin/env python3
"""Shared memory-stack signals — the single source of truth for the hot-memory
format markers and the durable-vs-transient linguistic signals.

Why this module exists (INTEG-8): the ENTRY/pointer/header format constants and
the durability regexes were copy-pasted into ``memory_audit.py``,
``hermes_memory_intake_gate.py`` and ``temporal_memory.py`` — and had already
DRIFTED. The read-time audit matched ``passes|broke|crashed|applied|replaced``
as completion verbs and ``KB`` as a metric; the write-time intake gate did not.
So the gate and the audit disagreed about what counts as a status update — the
exact silent inconsistency this stack is supposed to prevent.

The fix is one canonical definition per signal, imported everywhere. Edit the
signals HERE and nowhere else. The values below are the union (superset) of what
the three modules used, chosen so the read-time audit's behaviour is unchanged
and the write-time gate simply catches the same (slightly broader) set of junk.

stdlib only; import-safe (no side effects, no I/O, no network).
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Format constants — byte-identical to hermes_memory_curator.py / memory_tool. #
# These define the on-disk shape of MEMORY.md / USER.md and MUST never drift.  #
# --------------------------------------------------------------------------- #
ENTRY_DELIMITER = "\n§\n"          # separates entries within a hot-memory file
POINTER_SIGIL = "↪"                 # leads an archived/routing pointer entry
HEADER_SENTINEL = "Long-form notes live in"   # stable id for the nav header

# --------------------------------------------------------------------------- #
# Linguistic signals (deep-research §2.1 — durable vs transient is detectable).#
# Canonical superset: the read-time audit and the write-time gate classify     #
# identically against these. Broadening the gate only makes it catch MORE junk #
# (its job); the audit's behaviour is unchanged from the pre-extraction copy.  #
# --------------------------------------------------------------------------- #
# Transient: time anchors. A standing fact has no "when".
TEMPORAL_RE = re.compile(
    r"\b(now|today|currently|this week|right now|just\b|recently|yesterday|"
    r"tonight|last night|as of|so far|at the moment)\b", re.I)
# Completion / event verbs (telic past) — describe a finished event, already aging.
COMPLETION_RE = re.compile(
    r"\b(fixed|done|resolved|shipped|merged|upgraded to|repaired|wired|"
    r"completed?|installed|deployed|finished|confirmed|verified|"
    r"found that|turns out|discovered|switched to|migrated|rolled back|"
    r"passes|broke|crashed|applied|replaced)\b", re.I)
# Metric / version snapshots — point-in-time numbers.
METRIC_RE = re.compile(
    r"(\b\d+\s*/\s*\d+\b|\b\d+(\.\d+)?\s*(tok/s|tokens?|ms|GB|MB|KB|fps)\b|"
    r"\bv?\d+\.\d+\.\d+\b|\b\d{1,3}\s*%)", re.I)
# A leading date prefix = dated snapshot / log line (allows a short word/space label).
LEADING_DATE_RE = re.compile(
    r"^\s*[-*]?\s*(?:[\w ]+[:,]\s*)?(?:\()?20\d{2}-\d{2}-\d{2}\b")
# Durable preference verbs (stative / normative, generic present).
PREF_RE = re.compile(
    r"\b(prefers?|wants?|expects?|requires?|needs?|always|never|by default|"
    r"insists?|likes?|dislikes?|standing rule|hates?|rule:|policy:)\b", re.I)
# Pointer markers — say WHERE detail lives.
POINTER_RE = re.compile(
    r"(~/|/Users/|skill:|\bskill\b|notes/|\.md\b|\bcanonical\b|\bread \b|"
    r"\bsee \b|full context|located at|lives in|lives at|\brepo\b|→|↪|"
    r"session_search|spine search)", re.I)
# Dream-reflection fingerprints (citation refs / reflection frontmatter).
REFLECTION_RE = re.compile(
    r"(\[M\d+\]|\(sources?:\s*\[M|sources_reflected|type:\s*reflection|"
    r"dream reflection)", re.I)

__all__ = [
    "ENTRY_DELIMITER", "POINTER_SIGIL", "HEADER_SENTINEL",
    "TEMPORAL_RE", "COMPLETION_RE", "METRIC_RE", "LEADING_DATE_RE",
    "PREF_RE", "POINTER_RE", "REFLECTION_RE",
]
