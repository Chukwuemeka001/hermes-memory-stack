#!/usr/bin/env python3
"""Hermes Memory Auto-Extraction (Mem0-style) — nightly fact harvester.

Automatically extracts durable, personal facts/preferences/corrections from
recent Hermes conversations *without* the user or agent saying "remember this".

Pipeline (precision-first; see ~/.hermes/plans/memory-auto-extraction-plan.md):

    state.db sessions (last N days, human-origin sources)
      -> PRE-FILTER     role=user, drop automation-injected pseudo-user msgs,
                        require >= min_turns real user turns, require a signal word
      -> LLM EXTRACT    local Phi-4 (forced JSON schema) -> {"facts": [str, ...]}
      -> INTAKE GATE    reuse hermes_memory_intake_gate.gate() for durability
                        classification + token-Jaccard dedup vs MEMORY.md
      -> CAPS           N facts/session, M facts/night
      -> OUTPUT         JSON candidates (+ human summary). DRY-RUN by default.

Two-layer precision: the small local model proposes; the deterministic intake
gate disposes. Even if Phi-4 hallucinates a transient "fact", the gate's
temporal/completion/metric rules REJECT it and its Jaccard check drops dups.

DRY-RUN is the default and writes NOTHING to MEMORY.md. `--write` appends only
gate-ALLOW, non-duplicate facts, and logs provenance to a sidecar (never
pollutes the hot pointer file with status text).

Usage:
    python3 memory_auto_extract.py --dry-run            # default; today's sessions
    python3 memory_auto_extract.py --days 7             # last 7 days
    python3 memory_auto_extract.py --fixtures golden.jsonl   # eval mode
    python3 memory_auto_extract.py --write              # append accepted facts
    python3 memory_auto_extract.py --json               # machine-readable to stdout

This script never writes to state.db. It only reads sessions/messages.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

try:  # advisory locking (POSIX); degrade gracefully if unavailable
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore

# Reuse the blessed, deterministic write-time classifier (durability + dedup).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hermes_memory_intake_gate as gate  # noqa: E402

# ---------------------------------------------------------------------------
# CONFIG — the knobs the review loop tunes. Edit here; everything reads CONFIG.
#
# Filesystem PATHS are deliberately NOT stored here. They are resolved at call
# time from --home / $HERMES_HOME / ~ via resolve_paths(), so the script never
# binds to an import-time home (INTEG-2 / EXPORT-10). Reading a relocated path
# key off CONFIG raises loudly to prevent a regression back to import-time
# path-baking.
# ---------------------------------------------------------------------------
_RUNTIME_PATH_KEYS = {"state_db", "memory_file", "out_dir", "dedup_extra_files"}


class _Config(dict):
    """Tunables dict that REFUSES to serve filesystem paths: those move to
    resolve_paths(home) and must never be read at import time again."""

    def __getitem__(self, key):
        if key in _RUNTIME_PATH_KEYS:
            raise KeyError(
                f"{key!r} is resolved at runtime via resolve_paths(home), not CONFIG "
                "(INTEG-2/EXPORT-10)")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in _RUNTIME_PATH_KEYS:
            raise KeyError(
                f"{key!r} is resolved at runtime via resolve_paths(home), not CONFIG "
                "(INTEG-2/EXPORT-10)")
        return super().get(key, default)


CONFIG = _Config({
    # LLM (local Phi-4 via llama-server, OpenAI-compatible).
    "llm_endpoint": "http://localhost:8080/v1/chat/completions",
    "llm_model": "Phi-4-mini-instruct-Q4_K_M.gguf",
    "llm_timeout_s": 120,
    "llm_max_tokens": 320,
    "llm_temperature": 0.0,

    # Which session sources count as "the human talking". cron/subagent/dream
    # sessions are machine-driven prompts, not Emeka's durable facts.
    "include_sources": ["telegram", "cli", "tui", "interactive", "discord", "web"],
    "exclude_sources": ["cron", "subagent", "dream", "system"],

    # Pre-filter thresholds.
    "min_session_turns": 2,        # real (post-clean) user turns required
    "min_user_chars": 12,          # ignore trivially short user turns ("ok", "ty")
    "require_signal_word": True,   # session must contain >=1 signal word to call LLM
    "max_excerpt_chars": 2600,     # keep prompt within Phi-4's 4096-tok context
    "max_user_turns_per_call": 12, # chunk long sessions into LLM calls of this many turns

    # Caps (anti-flooding).
    "max_facts_per_session": 5,
    "max_facts_per_night": 10,

    # Acceptance: which adjudicated verdicts auto-accept. ALLOW only is strict.
    "accept_verdicts": ["ALLOW"],

    # Adjudication model:
    #   "veto_dedup" — LLM judges durability; the intake gate only VETOES
    #                  transient/status/metric facts (its REJECT categories)
    #                  and dedup drops near-duplicates. Keeps identity facts.
    #   "allow_only" — strict: accept ONLY gate-ALLOW (pointer/preference) facts.
    "gate_mode": "veto_dedup",

    # Verification second pass (Mem0-style): after extraction, ask the model a
    # focused yes/no per candidate ("did the user literally state this durable
    # fact?"). A narrow binary judgment is far more reliable on a small model
    # than open-ended extraction — it kills jokes/status/hallucinations that slip
    # through pass 1. Off => trust pass-1 extraction + deterministic gates only.
    "verify_pass": True,
    # Fail-CLOSED: if the verify call errors (llama-server hiccup at cron time),
    # DROP the candidate rather than auto-accept it. The deterministic gate is
    # permissive, so a fail-open verify would let context-bound / third-party
    # facts through unscrutinised. Required before --write. (adversarial review P0)
    "verify_fail_open": False,

    # Grounding: every substantive token of an extracted fact (minus scaffolding
    # words like "prefers"/"wants") must appear in the source transcript at this
    # ratio, else it is a hallucination / few-shot bleed and is rejected. This is
    # the deterministic guard against the model echoing its own prompt examples.
    "min_grounding": 0.34,

    # Dedup: a fact is a near-duplicate of MEMORY.md if EITHER
    #   token-Jaccard >= gate.DUP_JACCARD (0.6, symmetric)  OR
    #   containment overlap |fact ∩ entry| / |fact| >= dup_overlap
    # The containment test catches a short fact already covered by a long entry
    # (e.g. "Xiaomi is my default provider" vs the verbose routing pointer).
    # 0.7 is the safe ceiling: lower values over-dedup genuine short facts against
    # the large CLAUDE.md prose corpus (a 2-3 token fact trivially shares half its
    # tokens with some paragraph). Looser paraphrases of injected facts (e.g. the
    # "RPN -> trading" identity) need SEMANTIC dedup via the existing chroma index
    # — a documented Phase-2 follow-up, not token containment.
    "dup_overlap": 0.7,
    # Within-session: collapse facts whose mutual containment >= this.
    "intra_dup_overlap": 0.7,
    # NOTE: extra dedup reference files (CLAUDE.md / USER.md) are resolved at run
    # time in resolve_paths(home), NOT baked here — see _RUNTIME_PATH_KEYS.

    # Signal words — presence in cleaned user text qualifies a session for the
    # (relatively expensive) LLM pass. Tuned in the review loop.
    "signal_words": [
        # corrections
        "actually", "correction", "not ", "wrong", "no, i", "i meant",
        "instead of", "rather than",
        # preferences / standing rules
        "i prefer", "i like", "i want", "my preference", "i hate", "i don't",
        "i do not", "i never", "i always", "always", "never", "by default",
        "from now on", "going forward", "make sure", "remember", "keep ",
        # tooling / workflow
        "i use", "i'm using", "switch to", "switched to", "my setup", "my workflow",
        # identity / context
        "my name", "i work", "i live", "i'm a", "i am a", "my manager",
        "my goal", "i'm based", "i am based",
    ],

    # Automation-injected pseudo-user messages to DROP (not Emeka talking).
    # These land in the DB with role='user' but are system/tooling text.
    "automation_patterns": [
        r"^\s*\[IMPORTANT:",
        r"^\s*\[ASYNC",
        r"^\s*\[Replying to:",
        r"^\s*\[SILENT",
        r"^\s*\[NOTE:",
        r"Background process proc_",
        r"completed normally \(exit code",
        r"DELEGATION BATCH",
        r"You are running as a scheduled cron job",
        r"The following skill\(s\) were listed for this job",
        r"--dangerously-skip-permissions",
        r"^\s*\{?\"?(role|messages|tool_calls)\"?\s*[:=]",  # raw payload leakage
    ],
})


# ---------------------------------------------------------------------------
# Runtime path resolution (INTEG-2 / EXPORT-10) — honor --home / $HERMES_HOME
# at CALL time, never at import. Set by run()/main(); read by _existing().
# ---------------------------------------------------------------------------
_PATHS: dict | None = None


def resolve_paths(home: str | None = None) -> dict:
    """All filesystem paths the extractor touches, under the resolved home
    (--home || $HERMES_HOME || ~/.hermes). CLAUDE.md is the Claude-Code global
    config (outside the Hermes home), so it stays ~/.claude-relative."""
    h = gate.resolve_home(home)
    return {
        "home": str(h),
        "state_db": str(h / "state.db"),
        "memory_file": str(h / "memories" / "MEMORY.md"),
        "out_dir": str(h / "memories" / "_auto_extract"),
        "dedup_extra_files": [
            os.path.expanduser("~/.claude/CLAUDE.md"),
            str(h / "memories" / "USER.md"),
        ],
    }


def _set_paths(home: str | None) -> dict:
    """Resolve + cache paths for this run and INVALIDATE the dedup cache, so a
    new home (across tests / repeated run() calls) can't reuse MEMORY.md entries
    read under a previous home."""
    global _PATHS, _EXISTING_ENTRIES, _EXISTING_TOKENS
    _PATHS = resolve_paths(home)
    _EXISTING_ENTRIES = _EXISTING_TOKENS = None
    return _PATHS


def _paths() -> dict:
    """Resolved paths for the current run; lazily default to env/~ if a caller
    (e.g. a direct unit test) never went through run()/main()."""
    global _PATHS
    if _PATHS is None:
        _PATHS = resolve_paths(None)
    return _PATHS


@contextmanager
def _file_lock(target: Path):
    """Advisory flock on '<target>.lock' — the same curator/memory-tool/temporal
    convention every other MEMORY.md writer uses (see temporal_memory._lockpath).
    Degrades to a no-op if fcntl is unavailable (non-POSIX)."""
    lock_path = Path(str(target) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# ---------------------------------------------------------------------------
# Extraction prompt (v1). Refined across the review loop.
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM = """You are a precise memory fact-extractor for a personal AI assistant.
You read what a user literally said and output ONLY durable personal facts.

Extract a fact ONLY if ALL are true:
1. The USER literally asserted it (never infer; never use the assistant's words).
2. It is durable — still true in 30+ days (identity, health, a standing preference,
   a standing rule for how you must always behave, a tool/workflow choice, a correction).
3. It is personal to this user.

Do NOT manufacture a "preference" out of a complaint, a joke, a status update, a
question, or a task request. A sentence is a fact ONLY if the user is asserting a
stable truth about themselves or a standing instruction. When in doubt, leave it out.

NEVER extract: task instructions ("do X", "switch to opus", "go ahead", "run this");
session status ("it works now", "that's done", "I fixed you", "the gateway is up");
questions; jokes; hypotheticals ("what if we..."); one-off events, dates, metrics,
versions; anything the assistant said.

Write each fact as one plain declarative sentence starting with "User ".
Output strict JSON: {"facts": [...]}. Empty list if none. Maximum 5 facts.
CRITICAL: only output facts the user actually stated in THIS conversation. The
examples below are formatting illustrations — never copy their content unless the
user truly said it. If the conversation has no durable facts, output {"facts": []}.

Examples:
IN: "I actually prefer dark roast coffee, not light roast."
OUT: {"facts": ["User prefers dark roast coffee over light roast."]}
IN: "from now on never place a live trade without my explicit approval. ask me first."
OUT: {"facts": ["User requires explicit approval before any live trade is placed."]}
IN: "by the way remember I'm allergic to penicillin."
OUT: {"facts": ["User is allergic to penicillin."]}
IN: "just so you know, I'm colour-blind, keep that in mind for chart colours."
OUT: {"facts": ["User is colour-blind."]}
IN: "noting that I ride a motorcycle to work most days."
OUT: {"facts": ["User rides a motorcycle to work."]}
IN: "I always have to fix you lol. the telegram gateway is working now after the restart."
OUT: {"facts": []}
IN: "haha it would be hilarious if you took my job and I never had to work again lmao"
OUT: {"facts": []}
IN: "I prefer not to think about it but what if we just deleted state.db and started fresh?"
OUT: {"facts": []}
IN: "actually, go ahead and switch to the opus model for this run and delegate the review."
OUT: {"facts": []}
IN: "thanks dude that works perfectly now, exactly what I wanted."
OUT: {"facts": []}"""

EXTRACTION_USER_TMPL = """Conversation (user turns only):
{transcript}

Output the durable personal facts as JSON."""

# JSON schema forced on the model (llama-server response_format=json_schema).
FACTS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 5,
        }
    },
    "required": ["facts"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Pre-filter helpers
# ---------------------------------------------------------------------------
def _compile(patterns):
    return [re.compile(p, re.I) for p in patterns]


_AUTOMATION_RE = _compile(CONFIG["automation_patterns"])


def is_automation(text: str) -> bool:
    """True if a role='user' message is actually system/tooling-injected."""
    if not text:
        return True
    return any(r.search(text) for r in _AUTOMATION_RE)


def has_signal_word(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in CONFIG["signal_words"])


def clean_user_turns(messages: list[dict]) -> list[str]:
    """Keep only genuine user turns: role=user, not automation, not trivial."""
    out = []
    for m in messages:
        if m["role"] != "user":
            continue
        content = (m["content"] or "").strip()
        if len(content) < CONFIG["min_user_chars"]:
            continue
        if is_automation(content):
            continue
        out.append(content)
    return out


def build_transcript(user_turns: list[str], limit: int) -> str:
    """Join user turns into a compact transcript, truncated to `limit` chars."""
    lines, total = [], 0
    for t in user_turns:
        t = re.sub(r"\s+", " ", t).strip()
        line = f"User: {t}"
        if total + len(line) > limit:
            line = line[: max(0, limit - total)]
            if line:
                lines.append(line)
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call (local Phi-4, forced JSON)
# ---------------------------------------------------------------------------
def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _coerce_facts(obj) -> list[str]:
    """Normalize whatever the model returned into a list of fact strings."""
    facts = []
    if isinstance(obj, dict):
        raw = obj.get("facts", [])
    elif isinstance(obj, list):
        raw = obj
    else:
        raw = []
    for item in raw:
        if isinstance(item, str):
            f = item.strip()
        elif isinstance(item, dict):
            # Phi-4 sometimes returns {"preference": "..."} objects.
            f = "; ".join(str(v).strip() for v in item.values() if v)
        else:
            f = str(item).strip()
        if f:
            facts.append(f)
    return facts


def call_llm(transcript: str, debug: bool = False) -> tuple[list[str], str]:
    """Call local Phi-4 with forced JSON schema. Returns (facts, error)."""
    payload = {
        "model": CONFIG["llm_model"],
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": EXTRACTION_USER_TMPL.format(transcript=transcript)},
        ],
        "temperature": CONFIG["llm_temperature"],
        "max_tokens": CONFIG["llm_max_tokens"],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "facts", "strict": True, "schema": FACTS_SCHEMA},
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CONFIG["llm_endpoint"], data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CONFIG["llm_timeout_s"]) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return [], f"llm_error: {e}"
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        return [], f"bad_response: {e}"
    if debug:
        print(f"  [llm raw] {content!r}", file=sys.stderr)
    try:
        obj = json.loads(_strip_fences(content))
    except json.JSONDecodeError:
        # Last resort: pull the first {...} blob out of the text.
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            return [], "unparseable_json"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [], "unparseable_json"
    return _coerce_facts(obj), ""


# ---------------------------------------------------------------------------
# Verification second pass (Mem0-style) — focused per-candidate yes/no
# ---------------------------------------------------------------------------
VERIFY_SYSTEM = """You verify proposed long-term memories about a user (called "User").
You get a candidate fact and the EXACT user text it was drawn from.

Reply STRICT JSON: {"keep": true or false, "reason": "<short>"}.

keep=true ONLY if the user LITERALLY asserted this as a DURABLE, PERSONAL fact
about THEMSELVES: their identity/health, a standing preference, a standing rule
for how the assistant must always behave, a tool/workflow choice, or a correction.
Durable tool and scheduling preferences DO count (e.g. "I prefer Neovim over VS
Code" -> keep=true; "I'm a night owl, never schedule before noon" -> keep=true).

keep=false if ANY of these apply:
- TASK/REQUEST scoped to this work ("for this PR", "for this run", "just for
  today's rollout", "right now") — a session rule, not a standing rule.
- TEMPORARY / time-bound ("for now", "until X is done", "while X is broken",
  "for the time being", "this sprint", "temporarily", "in the meantime").
- THIRD PARTY: a fact about someone OTHER than the user (my coworker/wife/client/
  manager/friend/patient ...). Only facts about the USER themselves qualify.
- a status update / progress report / "now works"; a question; a joke, sarcasm,
  or exaggeration; a hypothetical or "what if"; roleplay / "pretend you are ...";
  a quoted or DENIED statement ("my mentor says ...", "that's not even true",
  "I'm NOT allergic to ..."); indifference ("either is fine", "I don't care");
  an assistant statement; or anything the user did not actually assert.
When unsure, keep=false."""

VERIFY_USER_TMPL = """Candidate memory: "{fact}"

What the user actually said:
{source}

Should this be saved as a durable memory? JSON only."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "boolean"}, "reason": {"type": "string"}},
    "required": ["keep", "reason"],
    "additionalProperties": False,
}


def verify_fact(fact: str, source_text: str, debug: bool = False) -> tuple[bool, str]:
    """Focused binary verification. Fail-open (keep) on transport error."""
    payload = {
        "model": CONFIG["llm_model"],
        "messages": [
            {"role": "system", "content": VERIFY_SYSTEM},
            {"role": "user", "content": VERIFY_USER_TMPL.format(
                fact=fact, source=source_text[:1500])},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "verdict", "strict": True, "schema": VERIFY_SCHEMA},
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        CONFIG["llm_endpoint"], data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=CONFIG["llm_timeout_s"]) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        if debug:
            print(f"  [verify raw] {fact[:40]!r} -> {content!r}", file=sys.stderr)
        obj = json.loads(_strip_fences(content))
        return bool(obj.get("keep", False)), str(obj.get("reason", ""))[:120]
    except Exception as e:  # noqa: BLE001 — surface in report
        # Fail CLOSED by default: a verify outage must not auto-accept facts.
        return bool(CONFIG["verify_fail_open"]), f"verify_error: {e}"


# ---------------------------------------------------------------------------
# Adjudication — durability veto (reuse intake gate) + dedup vs MEMORY.md
# ---------------------------------------------------------------------------
# Meta-NARRATION in the FACT TEXT itself — the extractor described the user's
# tone instead of asserting a fact ("User jokingly compares..."). Narrowed to a
# narration *shape* so legit facts that merely mention humour are not nuked
# (e.g. "User dislikes humour in code comments"). (adversarial review P2)
META_RE = re.compile(
    r"\b(jok(ing|ingly)|humorously|humourously|sarcastically|figuratively|"
    r"metaphorically|is\s+(joking|kidding|being\s+sarcastic|exaggerating)|"
    r"\b(lol|lmao|rofl)\b|compares?\s+\w+\s+to)\b", re.I)

# --- Source-side disqualifiers (adversarial-review hardening) -----------------
# The extractor strips scope/attribution qualifiers when phrasing a fact, so the
# disqualifier survives ONLY in the originating user text. These scan the SOURCE.
_REL = (r"(?:co-?workers?|colleagues?|wife|husband|spouse|partner|girlfriend|"
        r"boyfriend|clients?|customers?|managers?|bosses|boss|supervisors?|"
        r"employees?|friends?|mentors?|mentees?|mom|mum|dad|mother|father|"
        r"parents?|sisters?|brothers?|siblings?|sons?|daughters?|kids?|child|"
        r"children|roommates?|flatmates?|teammates?|patients?|neighbou?rs?|"
        r"landlords?|tenants?|doctors?|nurses?|coach|therapists?)")
# Third party as the SUBJECT of the candidate fact itself (allow an adjective:
# "User's old mentor", "their main client").
THIRD_PARTY_FACT_RE = re.compile(
    r"\b(?:user'?s|their|his|her|the)\s+(?:\w+\s+){0,2}" + _REL + r"\b", re.I)
# "my coworker / my old mentor ..." in the source = user is talking ABOUT someone.
MY_RELATION_RE = re.compile(r"\bmy\s+(?:\w+\s+){0,2}" + _REL + r"\b", re.I)
# Scope / expiry markers that make a "preference" non-durable.
EXPIRY_RE = re.compile(
    r"\b(for now|for the time being|in the meantime|for the moment|at the moment|"
    r"right now|temporarily|just for today|for today|today'?s\s+\w+|this rollout|"
    r"this sprint|this week|this month|for the next|one[- ]off|"
    r"for this (?:pr|run|task|session|migration|rollout|sprint|build|ticket|fix)|"
    r"until\b|while\b|pending\b|drop back|switch (?:me )?back|revert (?:it )?back)\b",
    re.I)
# Permanence overrides — if present, an expiry token is NOT disqualifying.
# (Must be SPECIFIC: an earlier `always.*ever` clause wrongly matched
# "always ... never" because "never" ends in "ever". adversarial review P0.)
PERMANENCE_RE = re.compile(
    r"\b(permanent(ly)?|forever|going forward|from now on|for good\b|in general|"
    r"as a (?:standing |general )?rule|standing rule|by default|this is permanent|"
    r"life-?long|all my life|my whole life|always have|always had|"
    r"not (?:just )?(?:for now|for today|for testing|temporary|temporarily|a one[- ]?off))\b",
    re.I)
# A negated/correction fact ("User is not allergic ...") is auto-routed to REVIEW,
# not ALLOW: negations are subtle corrections that deserve a human glance and the
# hot file should be built from positive standing assertions. (review: adv-neg-1)
NEGATED_FACT_RE = re.compile(
    r"^\s*user\s+(is\s+not|isn'?t|was\s+not|wasn'?t|does\s+not|doesn'?t|do\s+not|"
    r"don'?t|did\s+not|didn'?t|has\s+no\b|have\s+no\b|is\s+no\s+longer|no\s+longer)\b",
    re.I)
HYPOTHETICAL_RE = re.compile(
    r"\b(what if|what would happen|would that|suppose\b|imagine if|hypothetical|"
    r"someday|some day|one day|i might (?:move|start|switch|try|get|buy|ride)|"
    r"thinking out loud|just thinking|just musing|playing with the idea)\b", re.I)
QUOTE_RE = re.compile(
    r"\b(my\s+(?:\w+\s+){0,3}(?:said|says|always said|used to say|told me|reckons?)|"
    r"they say|people say|as the saying goes)\b", re.I)
ROLEPLAY_RE = re.compile(
    r"\b(role-?play|let'?s pretend|pretend (?:you|to be|that)|in character|"
    r"stay in character|act as if|you'?re playing|persona)\b", re.I)


def _sentences(text: str):
    return [s.strip() for s in re.split(r"[.!?\n]+", text) if s.strip()]


def grounding_sentences(fact: str, source_text: str, thresh: float = 0.5) -> list[str]:
    """Sentence(s) of the source the fact actually derives from (>= thresh
    grounded). Used to SCOPE the content disqualifiers so an unrelated clause
    elsewhere in a mixed session can't taint a genuine fact. (review round 2)"""
    return [s for s in _sentences(source_text) if grounding_ratio(fact, s) >= thresh]


def grounding_context(fact: str, source_text: str, limit: int = 700) -> str:
    """Focused context (the grounding sentences) for the verify pass, so a long
    task-heavy transcript can't bias the judge against a genuine buried fact."""
    gs = grounding_sentences(fact, source_text)
    return (". ".join(gs))[:limit] if gs else source_text[:limit]


def source_disqualifier(fact: str, source_text: str):
    """Return (verdict, category, reason) if the SOURCE disqualifies the fact,
    else None. Catches the false-positive classes the adversarial review found:
    third-party subjects, session/task-scoped or temporary rules, hypotheticals,
    quoted/attributed statements, and roleplay.

    Content disqualifiers (expiry/hypothetical/quote) are scoped to the fact's
    GROUNDING sentences, mirroring the third-party path — so a "for this run"
    clause in an unrelated turn no longer downgrades a genuine durable fact
    elsewhere in the same session. (review round 2)"""
    low = source_text.lower()
    # 1. Third party — fact subject is someone else, or fact derives from a
    #    "my <relation> ..." clause in the source.
    if THIRD_PARTY_FACT_RE.search(fact):
        return ("REJECT", "third_party", "fact subject is a third party, not the user")
    for m in MY_RELATION_RE.finditer(source_text):
        for sent in _sentences(source_text):
            if m.group(0).lower() in sent.lower() and grounding_ratio(fact, sent) >= 0.5:
                return ("REJECT", "third_party",
                        f'fact derives from third-party clause "{m.group(0)}"')
    # Scope the remaining content checks to the sentence(s) the fact came from.
    gs = grounding_sentences(fact, source_text)
    gtext = " ".join(gs) if gs else source_text
    glow = gtext.lower()
    # 2. Scope / expiry without a permanence override -> not durable.
    if EXPIRY_RE.search(glow) and not PERMANENCE_RE.search(glow):
        return ("REVIEW", "scoped_or_temporary",
                "source scopes the preference to a task/session or marks it temporary")
    # 3. Hypothetical / what-if.
    if HYPOTHETICAL_RE.search(glow):
        return ("REJECT", "hypothetical", "source is hypothetical / 'what if'")
    # 4. Quoted / attributed to someone else.
    if QUOTE_RE.search(glow):
        return ("REJECT", "quoted", "preference attributed to / quoted from someone else")
    # 5. Roleplay / persona framing — checked over the whole session (the framing
    #    spans turns, so a fact stated mid-roleplay is still in-character).
    if ROLEPLAY_RE.search(low):
        return ("REJECT", "roleplay", "stated inside roleplay / persona framing")
    return None


_EXISTING_ENTRIES = None
_EXISTING_TOKENS = None


def _existing():
    """Cache (entry, token-set) pairs from MEMORY.md + extra dedup files."""
    global _EXISTING_ENTRIES, _EXISTING_TOKENS
    if _EXISTING_ENTRIES is None:
        p = _paths()
        _EXISTING_ENTRIES = gate.read_existing_entries(p["home"])
        _EXISTING_TOKENS = [gate.tokens(e) for e in _EXISTING_ENTRIES]
        # Also dedup against statically-injected context (CLAUDE.md / USER.md):
        # the agent already knows these, so don't re-mine them. Each file is one
        # big reference token-set (paragraph granularity for prose).
        for fp in p["dedup_extra_files"]:
            try:
                raw = Path(fp).read_text(encoding="utf-8")
            except OSError:
                continue
            for para in re.split(r"\n\s*\n", raw):
                para = para.strip()
                if len(para) > 20:
                    _EXISTING_ENTRIES.append(f"[{Path(fp).name}] " + para[:120])
                    _EXISTING_TOKENS.append(gate.tokens(para))
    return _EXISTING_ENTRIES, _EXISTING_TOKENS


def dup_score(fact: str) -> tuple[float, float]:
    """Return (best containment-overlap, best Jaccard) of fact vs MEMORY.md.

    Containment = |fact ∩ entry| / |fact| — catches a short fact already
    covered by a long existing entry, which symmetric Jaccard misses.
    """
    cand = gate.tokens(fact)
    if not cand:
        return 0.0, 0.0
    entries, toks = _existing()
    best_ov = best_jac = 0.0
    for et in toks:
        if not et:
            continue
        inter = len(cand & et)
        ov = inter / len(cand)
        jac = inter / len(cand | et)
        best_ov = max(best_ov, ov)
        best_jac = max(best_jac, jac)
    return best_ov, best_jac


# Scaffolding words the model adds when phrasing a fact — excluded from the
# grounding test so we measure whether the SUBSTANTIVE content is in the source.
FRAME_WORDS = {
    "prefers", "prefer", "wants", "want", "requires", "require", "uses", "use",
    "using", "likes", "dislikes", "needs", "need", "always", "never", "over",
    "than", "instead", "rather", "default", "standing", "stated", "mentioned",
}


def grounding_ratio(fact: str, source_text: str) -> float:
    """Fraction of the fact's substantive tokens present in the source text."""
    ft = gate.tokens(fact) - FRAME_WORDS
    if not ft:
        return 0.0
    st = gate.tokens(source_text)
    return len(ft & st) / len(ft)


def hyphen_grounded(fact: str, source_text: str) -> bool:
    """Each hyphenated compound in the fact must appear CONTIGUOUSLY in the
    source (normalised). gate.tokens() splits "colour-blind" into two unigrams
    that unrelated words ("colour scheme", "blind spots") can satisfy
    independently — this closes that bleed hole. (adversarial review P0)"""
    def norm(s):
        return re.sub(r"[\s-]+", "", s.lower())
    src = norm(source_text)
    for comp in re.findall(r"\b\w+(?:-\w+)+\b", fact):
        if norm(comp) not in src:
            return False
    return True


def adjudicate(fact: str, source_text: str = "") -> dict:
    """Decide ALLOW / REVIEW / REJECT for one extracted fact.

    REJECT  — ungrounded (hallucination/bleed), or gate flags it transient.
    REVIEW  — near-duplicate of MEMORY.md, or (allow_only mode) not gate-ALLOW.
    ALLOW   — grounded, durable, novel, personal: safe to add.
    """
    # 0. Grounding veto — the fact must actually come from the conversation
    #    (token ratio AND every hyphenated compound present contiguously).
    if source_text:
        gr = grounding_ratio(fact, source_text)
        if gr < CONFIG["min_grounding"] or not hyphen_grounded(fact, source_text):
            ov0, jac0 = dup_score(fact)
            why = (f"grounding={gr:.2f} < {CONFIG['min_grounding']}"
                   if gr < CONFIG["min_grounding"] else "hyphenated compound absent from source")
            return {"max_jaccard_existing": round(max(ov0, jac0), 2),
                    "overlap_existing": round(ov0, 2), "grounding": round(gr, 2),
                    "verdict": "REJECT", "category": "ungrounded",
                    "reason": f"not grounded in transcript ({why}) — hallucination/few-shot bleed"}
    # 0a. Source disqualifier — third-party / scoped-temporary / hypothetical /
    #     quoted / roleplay. The disqualifier lives only in the source text.
    if source_text:
        sd = source_disqualifier(fact, source_text)
        if sd:
            verdict, category, reason = sd
            ov0, jac0 = dup_score(fact)
            return {"max_jaccard_existing": round(max(ov0, jac0), 2),
                    "overlap_existing": round(ov0, 2),
                    "grounding": round(grounding_ratio(fact, source_text), 2),
                    "verdict": verdict, "category": category, "reason": reason}
    # 0b. Meta/joke veto — the extractor described noise instead of a fact.
    if META_RE.search(fact):
        ov0, jac0 = dup_score(fact)
        return {"max_jaccard_existing": round(max(ov0, jac0), 2),
                "overlap_existing": round(ov0, 2),
                "grounding": round(grounding_ratio(fact, source_text), 2) if source_text else None,
                "verdict": "REJECT", "category": "meta_joke",
                "reason": "fact text contains joke/sarcasm/meta marker"}
    # 0c. Negated/correction fact -> REVIEW (surface for a human; don't auto-add).
    if NEGATED_FACT_RE.search(fact):
        ov0, jac0 = dup_score(fact)
        return {"max_jaccard_existing": round(max(ov0, jac0), 2),
                "overlap_existing": round(ov0, 2),
                "grounding": round(grounding_ratio(fact, source_text), 2) if source_text else None,
                "verdict": "REVIEW", "category": "negated_correction",
                "reason": "negated/correction assertion — surface for human review, not auto-add"}
    g = gate.classify(fact)
    ov, jac = dup_score(fact)
    dup = round(max(ov, jac), 2)
    base = {"max_jaccard_existing": dup, "overlap_existing": round(ov, 2),
            "grounding": round(grounding_ratio(fact, source_text), 2) if source_text else None}

    if ov >= CONFIG["dup_overlap"] or jac >= gate.DUP_JACCARD:
        return {**base, "verdict": "REVIEW", "category": "near_duplicate",
                "reason": f"already in MEMORY.md (overlap={ov:.2f}, jaccard={jac:.2f})"}

    if CONFIG["gate_mode"] == "allow_only":
        return {**base, "verdict": g["verdict"], "category": g["category"],
                "reason": g["reason"]}

    # veto_dedup: the LLM judged durability; the gate only vetoes transient facts.
    if g["verdict"] == "REJECT":
        return {**base, "verdict": "REJECT", "category": g["category"],
                "reason": g["reason"]}
    return {**base, "verdict": "ALLOW",
            "category": g["category"] if g["verdict"] == "ALLOW" else "durable_personal",
            "reason": g["reason"]}


def dedupe_intra(facts: list[str]) -> list[str]:
    """Collapse near-identical facts from the SAME session (keep first seen)."""
    kept, kept_tok = [], []
    for f in facts:
        ft = gate.tokens(f)
        if ft and any(len(ft & kt) / len(ft) >= CONFIG["intra_dup_overlap"]
                      for kt in kept_tok if kt):
            continue
        kept.append(f)
        kept_tok.append(ft)
    return kept


# ---------------------------------------------------------------------------
# Session loading
# ---------------------------------------------------------------------------
def load_sessions(db_path: str, since_ts: float, include, exclude):
    """Yield (session_meta, messages) for candidate sessions since `since_ts`."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        srows = con.execute(
            "SELECT id, source, title, started_at, message_count "
            "FROM sessions WHERE started_at >= ? ORDER BY started_at",
            (since_ts,),
        ).fetchall()
        for s in srows:
            src = (s["source"] or "").lower()
            if include and src not in include:
                continue
            if src in exclude:
                continue
            mrows = con.execute(
                "SELECT role, content, timestamp FROM messages "
                "WHERE session_id = ? AND active = 1 ORDER BY timestamp",
                (s["id"],),
            ).fetchall()
            yield dict(s), [dict(m) for m in mrows]
    finally:
        con.close()


def load_fixtures(path: str):
    """Yield (session_meta, messages) from a golden JSONL eval file.

    Each line: {"session_id","source","messages":[{role,content}], "_expected_*"}.
    Fields starting with '_' are eval labels; the extractor ignores them.
    """
    for lineno, ln in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError as e:
            # UX-3: a malformed --fixtures line gets an actionable message, not a
            # raw JSONDecodeError traceback (same guard as the Area-1/3/4 loaders).
            print(f"error: --fixtures {path} line {lineno} is not valid JSON ({e}).\n"
                  f'       Each non-comment line must be one JSON object: '
                  f'{{"session_id": ..., "messages": [...]}}.', file=sys.stderr)
            raise SystemExit(2)
        meta = {
            "id": obj.get("session_id", "fixture"),
            "source": obj.get("source", "telegram"),
            "title": obj.get("title", ""),
            "started_at": 0,
            "message_count": len(obj.get("messages", [])),
            "_labels": {k: v for k, v in obj.items() if k.startswith("_")},
        }
        yield meta, obj.get("messages", [])


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------
def extract_from_session(meta, messages, debug=False):
    """Run the full pipeline on one session. Returns a result dict."""
    user_turns = clean_user_turns(messages)
    res = {
        "session_id": meta["id"],
        "source": meta["source"],
        "title": meta.get("title") or "",
        "n_user_turns": len(user_turns),
        "skipped": None,
        "candidates": [],   # [{fact, verdict, category, reason, dup, jaccard}]
    }
    if "_labels" in meta:
        res["_labels"] = meta["_labels"]

    if len(user_turns) < CONFIG["min_session_turns"]:
        res["skipped"] = f"too_few_user_turns ({len(user_turns)} < {CONFIG['min_session_turns']})"
        return res

    joined = " ".join(user_turns)
    if CONFIG["require_signal_word"] and not has_signal_word(joined):
        res["skipped"] = "no_signal_word"
        return res

    # Chunk long sessions so each LLM call stays inside Phi-4's context window.
    chunk = CONFIG["max_user_turns_per_call"]
    raw_facts = []
    for i in range(0, len(user_turns), chunk):
        transcript = build_transcript(user_turns[i:i + chunk], CONFIG["max_excerpt_chars"])
        facts, err = call_llm(transcript, debug=debug)
        if err:
            res.setdefault("errors", []).append(err)
            continue
        raw_facts.extend(facts)

    # Drop exact + near-identical repeats from this session, then adjudicate
    # each surviving fact (durability veto + dedup vs MEMORY.md).
    uniq, seen = [], set()
    for f in raw_facts:
        key = f.lower().strip()
        if key and key not in seen:
            seen.add(key)
            uniq.append(f)
    for f in dedupe_intra(uniq):
        a = adjudicate(f, source_text=joined)
        # Verification second pass — only on facts we'd otherwise accept.
        # Feed it the focused grounding context (not the whole task-heavy
        # transcript) so a buried genuine fact isn't judged against unrelated
        # task chatter. (review round 2)
        if CONFIG["verify_pass"] and a["verdict"] == "ALLOW":
            keep, why = verify_fact(f, grounding_context(f, joined), debug=debug)
            a["verify"] = {"keep": keep, "reason": why}
            if not keep:
                a["verdict"], a["category"] = "REJECT", "verify_rejected"
                a["reason"] = f"verification pass rejected: {why}"
        res["candidates"].append({"fact": f, **a})

    # Per-session cap on ACCEPTED facts (keep accepted first, then the rest).
    accepted = [c for c in res["candidates"] if c["verdict"] in CONFIG["accept_verdicts"]]
    if len(accepted) > CONFIG["max_facts_per_session"]:
        keep = {id(c) for c in accepted[: CONFIG["max_facts_per_session"]]}
        for c in accepted[CONFIG["max_facts_per_session"]:]:
            c["verdict"], c["category"] = "REVIEW", "over_session_cap"
    return res


def run(args):
    paths = _set_paths(getattr(args, "home", None))
    if args.fixtures:
        sessions = list(load_fixtures(args.fixtures))
        scope = f"fixtures:{args.fixtures}"
    else:
        since = time.time() - args.days * 86400
        sessions = list(load_sessions(
            paths["state_db"], since,
            set(CONFIG["include_sources"]), set(CONFIG["exclude_sources"])))
        scope = f"last {args.days}d (since {datetime.fromtimestamp(since):%Y-%m-%d %H:%M})"

    results = [extract_from_session(m, msgs, debug=args.debug) for m, msgs in sessions]

    # Global night cap on accepted facts.
    all_accepted = []
    for r in results:
        for c in r["candidates"]:
            if c["verdict"] in CONFIG["accept_verdicts"]:
                all_accepted.append((r["session_id"], c))
    capped = all_accepted[: CONFIG["max_facts_per_night"]]
    capped_ids = {(sid, c["fact"]) for sid, c in capped}
    for sid, c in all_accepted:
        if (sid, c["fact"]) not in capped_ids:
            c["verdict"], c["category"] = "REVIEW", "over_night_cap"

    accepted = [c for _, c in capped]
    review = [c for r in results for c in r["candidates"]
              if c["verdict"] == "REVIEW"]
    rejected = [c for r in results for c in r["candidates"]
                if c["verdict"] == "REJECT"]

    # UX-2: surface model reachability so a dead LLM never masquerades as a clean
    # "nothing to extract" run. A session that called the model and failed
    # (endpoint down / timeout / bad response) records it in r["errors"]; a
    # SKIPPED session (too few turns / no signal word) never calls the model.
    attempted = [r for r in results if not r.get("skipped")]
    errored = [r for r in results if r.get("errors")]
    error_kinds: dict[str, int] = {}
    for r in errored:
        for e in r["errors"]:
            kind = e.split(":", 1)[0].strip() or "error"
            error_kinds[kind] = error_kinds.get(kind, 0) + 1
    total_candidates = sum(len(r["candidates"]) for r in results)
    # "Could not reach the model": ≥1 session attempted the model, every attempt
    # errored, and nothing came back — categorically different from a clean run
    # that simply found no durable facts.
    model_unreachable = bool(attempted) and total_candidates == 0 and len(errored) == len(attempted)

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "scope": scope,
        "config_digest": {k: CONFIG[k] for k in (
            "min_session_turns", "require_signal_word", "max_facts_per_session",
            "max_facts_per_night", "accept_verdicts")},
        "n_sessions_scanned": len(sessions),
        "n_sessions_attempted": len(attempted),
        "n_sessions_with_candidates": sum(1 for r in results if r["candidates"]),
        "n_sessions_errored": len(errored),
        "error_kinds": error_kinds,
        "errors": [e for r in errored for e in r["errors"]][:20],
        "model_unreachable": model_unreachable,
        "counts": {"accepted": len(accepted), "review": len(review), "rejected": len(rejected)},
        "accepted": accepted,
        "review": review,
        "rejected": rejected,
        "per_session": results,
    }
    return report


# ---------------------------------------------------------------------------
# Output / write
# ---------------------------------------------------------------------------
def write_candidates(report, out_dir: str) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stamp = report["generated_at"].replace(":", "").replace("-", "")
    path = Path(out_dir) / f"candidates-{stamp}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def append_to_memory(report, memory_file: str, out_dir: str) -> int:
    """WRITE mode: append accepted facts to MEMORY.md + provenance sidecar.

    Crash-safe + concurrency-safe, matching every other MEMORY.md writer in the
    package (temporal_memory.restore): take the MEMORY.md.lock flock on the
    LOGICAL path (so it serializes against the curator/gateway even when MEMORY.md
    is a symlink), RE-READ the file under the lock, archive a .bak, then write via
    mkstemp + fsync + os.replace on the resolved real target. So a crash /
    disk-full can't truncate the hot file, and a concurrent curator/gateway write
    can't be silently clobbered. (SAFETY-1)

    The old read-then-truncate-and-overwrite (`mf.write_text(...)`, no lock, no
    atomic rename, no fsync, no archive) is replaced wholesale.
    """
    accepted = report["accepted"]
    if not accepted:
        return 0
    # Defense-in-depth (mirrors temporal_memory.restore's guard): never write a
    # fact that embeds the §/\n§\n entry delimiter — it would fragment MEMORY.md
    # into phantom entries that pass the strip-based drift check unnoticed.
    accepted = [c for c in accepted
                if gate.ENTRY_DELIMITER not in c["fact"]
                and not any(ln.strip() == "§" for ln in c["fact"].splitlines())]
    if not accepted:
        return 0
    add_entries = [c["fact"] for c in accepted]
    target = Path(memory_file).expanduser()       # LOGICAL path — lock on this
    real = Path(os.path.realpath(target))         # resolved real file — write to this
    real.parent.mkdir(parents=True, exist_ok=True)

    with _file_lock(target):
        # Re-read UNDER the lock so a concurrent writer's entries are preserved,
        # and re-join on the canonical delimiter (no leading empty entry on an
        # empty file — fixes SAFETY-9).
        raw = real.read_text(encoding="utf-8") if real.exists() else ""
        entries = ([e.strip() for e in raw.split(gate.ENTRY_DELIMITER) if e.strip()]
                   if raw.strip() else [])
        entries.extend(add_entries)
        content = gate.ENTRY_DELIMITER.join(entries)   # NO trailing newline (matches curator)
        # New files default to 0o600 (personal memory) — matches every peer writer.
        mode = (real.stat().st_mode & 0o777) if real.exists() else 0o600
        if real.exists():
            bak = real.with_name(f"{real.name}.bak.{int(time.time())}")
            bak.write_bytes(real.read_bytes())
        fd, tmp = tempfile.mkstemp(dir=str(real.parent), suffix=".tmp", prefix=".mem_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, real)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # Provenance log (keeps source/date OUT of the hot pointer file).
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    log = Path(out_dir) / "append-log.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        for c in accepted:
            fh.write(json.dumps({"at": report["generated_at"], **c}, ensure_ascii=False) + "\n")
    return len(accepted)


def render_summary(report) -> str:
    L = [
        f"🧠 Memory auto-extraction — {report['scope']}",
        f"   sessions scanned: {report['n_sessions_scanned']}  "
        f"(with candidates: {report['n_sessions_with_candidates']})",
        f"   accepted: {report['counts']['accepted']}  "
        f"review: {report['counts']['review']}  rejected: {report['counts']['rejected']}",
        "",
    ]
    # UX-2: a dead/erroring model must be loud and distinct from "found nothing".
    n_err = report.get("n_sessions_errored", 0)
    kinds = ", ".join(f"{k}×{v}" for k, v in sorted(report.get("error_kinds", {}).items()))
    if report.get("model_unreachable"):
        n_att = report.get("n_sessions_attempted", n_err)
        L += [f"❌ COULD NOT REACH THE MODEL — all {n_att} attempted session(s) errored "
              f"({kinds or 'llm_error'}).",
              "   Results are INCOMPLETE — this is NOT a clean 'nothing to extract' run.",
              "   Check the LLM endpoint (CONFIG['llm_endpoint'] / llama-server) and re-run.",
              ""]
    elif n_err:
        L += [f"⚠️  {n_err} session(s) errored ({kinds}) — extraction is PARTIAL; "
              "some sessions could not be processed.", ""]
    if report["accepted"]:
        L.append("✅ ACCEPTED (gate=ALLOW, not duplicate):")
        for c in report["accepted"]:
            L.append(f"   • {c['fact']}")
    elif report.get("model_unreachable"):
        L.append("⚠️  ACCEPTED: none — run was INCOMPLETE (see model error above).")
    else:
        L.append("✅ ACCEPTED: none")
    if report["review"]:
        L.append("")
        L.append("🟡 REVIEW (borderline / near-duplicate / over cap):")
        for c in report["review"]:
            j = c.get("max_jaccard_existing", 0)
            L.append(f"   • {c['fact']}  [{c['category']}, jaccard={j}]")
    if report["rejected"]:
        L.append("")
        L.append("⛔ REJECTED (transient/task/status — gate filtered):")
        for c in report["rejected"]:
            L.append(f"   • {c['fact']}  [{c['category']}]")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Hermes memory auto-extraction (Mem0-style).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="NEEDS A LOCAL LLM (CONFIG['llm_endpoint'], default localhost:8080). If the model\n"
               "is unreachable the run exits non-zero and says so — it never reports a silent\n"
               "'0 facts, all ok'.\n\n"
               "GOLDEN PATH:\n"
               "  1. python3 memory_auto_extract.py --dry-run --days 1     # review candidates (safe)\n"
               "  2. inspect the report; run for ~a week before trusting precision\n"
               "  3. only then: python3 memory_auto_extract.py --write     # appends accepted facts")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="default; extract & report, write nothing to MEMORY.md")
    mode.add_argument("--write", action="store_true",
                      help="append accepted facts to MEMORY.md (TRUST-GATED: only after a week "
                           "of reviewed dry-runs; overrides dry-run)")
    ap.add_argument("--days", type=int, default=1, help="lookback window in days (default 1)")
    ap.add_argument("--fixtures", help="JSONL eval file (bypasses state.db)")
    ap.add_argument("--json", action="store_true", help="emit full JSON report to stdout")
    ap.add_argument("--out", help="candidates JSON output dir (default <home>/memories/_auto_extract)")
    ap.add_argument("--home", default=None,
                    help="HERMES_HOME override (default: $HERMES_HOME or ~/.hermes)")
    ap.add_argument("--debug", action="store_true", help="print raw LLM output to stderr")
    args = ap.parse_args(argv)

    report = run(args)          # resolves + caches paths for the chosen home
    paths = _paths()
    out_dir = args.out or paths["out_dir"]
    cand_path = write_candidates(report, out_dir)
    report["candidates_file"] = cand_path

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_summary(report))
        print(f"\n📄 candidates: {cand_path}")

    if args.write:
        n = append_to_memory(report, paths["memory_file"], out_dir)
        print(f"\n✍️  WROTE {n} fact(s) to {paths['memory_file']}")
    # UX-2: a model we could not reach is a FAILURE, not a clean run. Exit
    # non-zero so a cron / operator notices instead of trusting "0 facts, all ok".
    if report.get("model_unreachable"):
        print("⚠️  exit 2: model unreachable — no sessions could be processed; "
              "results are incomplete.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
