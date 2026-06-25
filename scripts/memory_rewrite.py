#!/usr/bin/env python3
"""Hermes pointer rewrite & consolidation — Area 3 of the onboarding pipeline.

Consumes an Area 2 audit (``memory_audit.py`` JSON, or run internally read-only)
and produces **reviewable rewrite proposals** and **proposed output files**. It
turns the audit's per-entry recommendations into concrete old->new entries:
condensing content dumps to one-line pointers, consolidating duplicates,
archiving stale status, and flagging anything that needs a human — without ever
fabricating a destination, growing an entry, or losing a fact.

SAFETY (non-negotiable):
  * ``plan`` (default) and ``render`` NEVER modify live MEMORY.md / USER.md.
    ``plan`` writes nothing but its own ``--out`` report; ``render`` writes only
    under ``--out-dir`` (proposed files + archived originals + manifest).
  * Never-lose: every changed/removed entry's ORIGINAL text is recorded in the
    manifest, and (in render) written to an archive file. A removal/merge is
    only proposed when its original is preserved.
  * No hallucinated destinations: a "rewrite to pointer" only fires when the
    entry already references a real file; otherwise it degrades to user_review.
    Archive pointers retrieve via ``session_search(...)`` plus a home-derived
    archive path (never a fabricated/hardcoded path).
  * No growth: a rewrite that would not be shorter than the original is rejected
    (degraded to review, or — for a tiny archived entry — removed).
  * Durable USER.md preferences are preserved, not collapsed into pointers.
  * Proposed output preserves ``\n§\n`` and order, and re-audits cleanly.
  * Optional ``apply`` REFUSES without ``--confirm-apply``, refuses a stale audit
    (live SHA must match), and archives live originals (timestamped, no-clobber)
    BEFORE writing. (Not used in normal onboarding.)

stdlib only; reuses ``memory_audit`` for parsing/classification (no LLM/network).

Usage:
    python3 memory_rewrite.py plan   --audit /tmp/audit.json
    python3 memory_rewrite.py plan   --home ~/.hermes --out /tmp/plan.json
    python3 memory_rewrite.py render --audit /tmp/audit.json --out-dir /tmp/proposed
    python3 memory_rewrite.py apply  --audit /tmp/audit.json --archive-dir DIR --confirm-apply
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import datetime as _dt
try:
    import fcntl
except Exception:  # pragma: no cover - non-POSIX fallback
    fcntl = None
import json
import os
import re
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import memory_audit as MA  # noqa: E402

TOOL_VERSION = "1.1.0"
DELIM = MA.ENTRY_DELIMITER
POINTER_SIGIL = MA.POINTER_SIGIL
HEADER_SENTINEL = MA.HEADER_SENTINEL
PLAN_SCHEMA = "hermes-memory-rewrite-plan/1"

POINTER_MAX = 240
SUMMARY_MAX = 150

_REPLACE_WITH_POINTER = {"rewrite_to_pointer", "archive_to_note", "move_to_note", "move_to_skill"}
_REMOVE_ACTIONS = {"remove_after_archive"}
_REVIEW_ACTIONS = {"verify_current", "user_review"}


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def now_date() -> str:
    return _dt.date.today().isoformat()


def slugify(text: str, n: int = 64) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:n] or "entry")


def keywords(entry: dict, k: int = 3) -> list[str]:
    base = MA.first_line(entry["text"])
    out, seen = [], set()
    for t in re.findall(r"[a-z][a-z0-9]{3,}", base.lower()):
        if t in MA.STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= k:
            break
    return out


def derive_topic(entry: dict) -> str:
    fl = MA.first_line(entry["text"])
    if fl.startswith(POINTER_SIGIL):
        fl = fl[len(POINTER_SIGIL):].strip()
    m = re.match(r"^([A-Za-z][\w &/+.\-]{1,46}?)\s*(?:\([^)]*\))?\s*:", fl)
    if m:
        return m.group(1).strip()
    key = entry.get("key") or "entry"
    return key.replace("-", " ").strip().title() or "Note"


def best_destination(entry: dict, user_home: str) -> str | None:
    """Pick a real referenced destination — NEVER invent one."""
    paths = entry.get("paths_referenced") or []
    for p in paths:
        if MA.is_file_path(p) and os.path.exists(MA.resolve_path(p, user_home)):
            return p
    for p in paths:
        if MA.is_file_path(p):
            return p
    for p in paths:
        return p
    return None


def compact_summary(text: str, topic: str, dest: str | None) -> str:
    """First sentence of the entry, minus the topic prefix and the destination
    path. Purely extractive — no fabricated content."""
    body = " ".join(text.split())
    body = re.sub(r"^" + re.escape(topic) + r"\s*:\s*", "", body, flags=re.I).strip()
    if body.startswith(POINTER_SIGIL):
        body = body[len(POINTER_SIGIL):].strip()
    first = re.split(r"(?<=[.!?])\s+", body, maxsplit=1)[0].strip()
    if dest:
        # remove the dest only as a whole token (don't corrupt longer paths)
        first = re.sub(re.escape(dest) + r"(?![\w./\-])", "", first)
        # drop a dangling connector left where the path was
        first = re.sub(r"\b(canonical doc|full context|located at|lives in|lives at|"
                       r"see|at|in|doc|repo)\s*[:.]?\s*$", "", first, flags=re.I)
    first = re.sub(r"\s+", " ", first).strip(" .,;:`()")
    if len(first) > SUMMARY_MAX:
        first = first[:SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(" .,;:`") + "…"
    return first


def make_pointer(entry: dict, user_home: str) -> str | None:
    """`Topic: compact summary. Full context: <path>.` — only if a real
    destination exists. Returns None when there is no destination."""
    dest = best_destination(entry, user_home)
    if not dest:
        return None
    topic = derive_topic(entry)
    summary = compact_summary(entry["text"], topic, dest)
    ptr = f"{topic}: {summary} Full context: {dest}." if summary else f"{topic}: full context {dest}."
    ptr = re.sub(r"\s+", " ", ptr).strip()
    if len(ptr) > POINTER_MAX:
        head, tail = f"{topic}: ", f" Full context: {dest}."
        room = max(0, POINTER_MAX - len(head) - len(tail) - 1)
        trimmed = summary[:room].rsplit(" ", 1)[0].rstrip(" .,;:`") if room else ""
        ptr = head + (trimmed + "…" if trimmed else "") + tail
    return ptr


def archive_slug(date: str, entry: dict) -> str:
    return slugify(f"{date}-{entry['ref'].replace('#', '-')}-{entry.get('key', '')}")


def home_archive_path(home: str, slug: str) -> str:
    """The STABLE, home-derived location an archived entry will live at after the
    rewrite is accepted (exportable — never hardcodes a username/home)."""
    return os.path.join(home, "memories", "_archive", "curator", slug + ".md")


def make_archive_pointer(entry: dict, date: str, final_path: str) -> str:
    """`↪ Topic: short → archived <date>. Find: session_search("kw") or <path>` —
    matches the existing Hermes archived-pointer convention. The primary retrieval
    is session_search (location-independent); the path is the post-acceptance home
    archive location (derived, not fabricated)."""
    topic = derive_topic(entry)
    kw = " ".join(keywords(entry, 3)) or topic.lower()
    short = compact_summary(entry["text"], topic, None)
    short = (short[:80].rsplit(" ", 1)[0] + "…") if len(short) > 80 else short
    return (f"{POINTER_SIGIL} {topic}: {short} → archived {date}. "
            f'Find: session_search("{kw}") or {final_path}')


# --------------------------------------------------------------------------- #
# Load audit (from JSON or run internally, read-only)                         #
# --------------------------------------------------------------------------- #
def load_audit(args) -> dict:
    if getattr(args, "audit", None):
        try:
            with open(args.audit, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except json.JSONDecodeError as e:
            # UX-3: actionable message instead of a raw JSONDecodeError traceback.
            print(f"error: --audit file {args.audit} is not valid JSON ({e}).\n"
                  f"       Expected the report from `memory_audit.py --out {args.audit}` "
                  f"(check for a truncated copy).", file=sys.stderr)
            raise SystemExit(2)
        if report.get("tool") != "memory_audit":
            print(f"error: --audit file {args.audit} is not a memory_audit report "
                  f"(missing tool=\"memory_audit\"). Did you pass the right file?", file=sys.stderr)
            raise SystemExit(2)
        return report
    home = os.path.abspath(os.path.expanduser(
        args.home or os.environ.get("HERMES_HOME") or "~/.hermes"))
    def_mem = os.path.join(home, "memories", "MEMORY.md")
    def_usr = os.path.join(home, "memories", "USER.md")
    memory_path = os.path.expanduser(args.memory) if getattr(args, "memory", None) else def_mem
    user_path = os.path.expanduser(args.user) if getattr(args, "user", None) else def_usr
    user_home = os.path.abspath(os.path.expanduser(args.user_home)) if getattr(args, "user_home", None) \
        else os.path.expanduser("~")
    return MA.run_audit(memory_path, user_path, home, user_home=user_home)


def _entries_by_store(report: dict) -> dict:
    out = {}
    for f in report["files"]:
        if f.get("exists"):
            out[f["store"]] = {"path": f["path"], "sha256": f.get("sha256"),
                               "char_count": f.get("char_count"),
                               "entries": list(f["entries"])}
    return out


def report_user_home(report: dict, override: str | None = None) -> str:
    if override:
        return os.path.abspath(os.path.expanduser(override))
    uh = (report.get("params") or {}).get("user_home")
    return uh or os.path.expanduser("~")


# --------------------------------------------------------------------------- #
# Core: build the rewrite plan                                                #
# --------------------------------------------------------------------------- #
def build_plan(report: dict, *, user_home: str | None = None) -> dict:
    """Pure planning: decide old->new for every entry. Writes nothing."""
    user_home = user_home or report_user_home(report)
    home = report.get("home") or os.path.expanduser("~/.hermes")
    date = report.get("generated_at") or now_date()
    stores = _entries_by_store(report)
    by_ref = {e["ref"]: e for s in stores.values() for e in s["entries"]}

    proposals = {}
    order = {store: [e["ref"] for e in info["entries"]] for store, info in stores.items()}
    originals = {ref: e["text"] for ref, e in by_ref.items()}
    drop_refs = set()
    absorbs = {}

    # Pass 1: per-entry decision.
    for store, info in stores.items():
        for e in info["entries"]:
            proposals[e["ref"]] = _decide(e, user_home, date, home)

    # Pass 2: merges (loser absorbed into the higher-quality survivor).
    for ref, p in proposals.items():
        if p["rewrite_action"] != "merge_absorb":
            continue
        survivor = p.get("merge_into")
        if not survivor or survivor not in by_ref:
            p.update(rewrite_action="review", status="review_needed", new_text=p["old_text"],
                     rationale="merge target not found in audit — preserved for review")
            continue
        drop_refs.add(ref)
        absorbs.setdefault(survivor, []).append(ref)
        p["new_text"] = None

    for survivor, losers in absorbs.items():
        sp = proposals.get(survivor)
        if sp:
            sp["absorbs"] = [{"ref": r, "text": proposals[r]["old_text"]} for r in losers]

    # Pass 3: removals drop from hot memory.
    for ref, p in proposals.items():
        if p["rewrite_action"] == "remove":
            drop_refs.add(ref)

    plan = {
        "schema": PLAN_SCHEMA, "tool": "memory_rewrite", "tool_version": TOOL_VERSION,
        "generated_at": date, "home": home, "user_home": user_home,
        "_proposals": proposals, "_order": order, "_originals": originals,
        "_drop_refs": drop_refs, "_stores": {s: i["path"] for s, i in stores.items()},
        "_src_sha": {s: i["sha256"] for s, i in stores.items()},
    }
    compose_proposed(plan)
    plan["proposals"] = [proposals[r] for s in order.values() for r in s]
    plan["files"] = {s: {k: v for k, v in d.items() if k not in ("original_text", "proposed_text")}
                     for s, d in plan["_out_files_text"].items()}
    plan["summary"] = _summarize(proposals, plan["_out_files_text"])
    return plan


def compose_proposed(plan: dict) -> None:
    """(Re)build proposed file texts from current proposals + original order."""
    proposals, order, originals = plan["_proposals"], plan["_order"], plan["_originals"]
    out = {}
    for store, refs in order.items():
        kept = []
        for ref in refs:
            p = proposals[ref]
            if ref in plan["_drop_refs"]:
                continue
            kept.append(p["new_text"] if p["new_text"] is not None else originals[ref])
        original_text = DELIM.join(originals[r] for r in refs)
        proposed_text = DELIM.join(kept)
        out[store] = {"path": plan["_stores"][store], "source_sha256": plan["_src_sha"][store],
                      "original_chars": len(original_text), "proposed_chars": len(proposed_text),
                      "original_entries": len(refs), "proposed_entries": len(kept),
                      "original_text": original_text, "proposed_text": proposed_text}
    plan["_out_files_text"] = out


def _decide(e: dict, user_home: str, date: str, home: str) -> dict:
    """Decide the rewrite for one entry. Never fabricates, never grows, never
    drops without preserving old_text."""
    ref, action, text = e["ref"], e["recommended_action"], e["text"]
    flags = e.get("flags", {})
    base = {"ref": ref, "store": e["store"], "index": e["index"], "audit_action": action,
            "kind": e["kind"], "old_text": text, "new_text": text,
            "rewrite_action": "keep", "status": "applied",
            "archive": None, "merge_into": None, "absorbs": None,
            "rationale": "", "char_delta": 0}

    def finalize(p):
        nt = p["new_text"]
        p["char_delta"] = (len(nt) if nt is not None else 0) - len(text)
        return p

    if action == "keep":
        base["rationale"] = "kept byte-for-byte"
        return finalize(base)

    if action == "merge":
        base.update(rewrite_action="merge_absorb", merge_into=flags.get("duplicate_of"),
                    rationale=f"absorbed into {flags.get('duplicate_of')} (near-duplicate); "
                              "original preserved in manifest/archive")
        return finalize(base)

    if action in _REVIEW_ACTIONS:
        base.update(rewrite_action="review", status="review_needed", new_text=text,
                    rationale=("verify still current — preserved (no truth claim)"
                               if action == "verify_current" else "flagged for human review — preserved"))
        return finalize(base)

    if action == "rewrite_to_pointer":
        ptr = make_pointer(e, user_home)
        if ptr is None:
            base.update(rewrite_action="review", status="review_needed",
                        rationale="rewrite_to_pointer requested but no real destination found — "
                                  "preserved (no fabricated path)")
            return finalize(base)
        if len(ptr) >= len(text):  # no token win — don't grow/replace
            base.update(rewrite_action="review", status="review_needed",
                        rationale="pointer would not be shorter than the original — preserved for review")
            return finalize(base)
        base.update(rewrite_action="rewrite_to_pointer", new_text=ptr,
                    rationale="condensed content dump to a one-line pointer (full text in manifest/archive)")
        return finalize(base)

    if action in _REPLACE_WITH_POINTER:  # archive_to_note / move_to_note / move_to_skill
        kind_word = {"archive_to_note": "note", "move_to_note": "note", "move_to_skill": "skill"}[action]
        slug = archive_slug(date, e)
        final_path = home_archive_path(home, slug)
        ptr = make_archive_pointer(e, date, final_path)
        base["archive"] = {"kind": kind_word, "slug": slug, "destination": final_path,
                           "written": False, "existing_ref": best_destination(e, user_home)}
        if len(ptr) >= len(text):
            # A pointer longer than the (short) original is no win — archive and
            # remove from hot memory (original preserved; retrieve via search).
            base.update(rewrite_action="remove", new_text=None,
                        rationale=f"original short — archived to {kind_word} and removed from hot "
                                  "memory (pointer would be longer; original in manifest/archive)")
            return finalize(base)
        base.update(rewrite_action="archive_pointer", new_text=ptr,
                    rationale=f"replace with archive pointer; original → {kind_word} "
                              "(preserved in manifest/archive)")
        return finalize(base)

    if action in _REMOVE_ACTIONS:  # remove_after_archive
        slug = archive_slug(date, e)
        base["archive"] = {"kind": "note", "slug": slug, "destination": home_archive_path(home, slug),
                           "written": False, "existing_ref": best_destination(e, user_home)}
        base.update(rewrite_action="remove", new_text=None,
                    rationale="ephemeral status — archived then removed from hot memory "
                              "(original in manifest/archive)")
        return finalize(base)

    base.update(rewrite_action="review", status="review_needed",
                rationale=f"unrecognized audit action {action!r} — preserved")
    return finalize(base)


def _summarize(proposals: dict, out_files: dict) -> dict:
    by_rw = {}
    for p in proposals.values():
        by_rw[p["rewrite_action"]] = by_rw.get(p["rewrite_action"], 0) + 1
    oc = sum(f["original_chars"] for f in out_files.values())
    pc = sum(f["proposed_chars"] for f in out_files.values())
    return {
        "by_rewrite_action": by_rw,
        "kept": by_rw.get("keep", 0),
        "rewritten_to_pointer": by_rw.get("rewrite_to_pointer", 0),
        "archived_pointer": by_rw.get("archive_pointer", 0),
        "merged_absorbed": by_rw.get("merge_absorb", 0),
        "removed": by_rw.get("remove", 0),
        "review_needed": by_rw.get("review", 0),
        "original_chars": oc, "proposed_chars": pc,
        "reduction_chars": oc - pc,
        "reduction_pct": round(100 * (oc - pc) / oc, 1) if oc else 0.0,
        "grew": pc > oc,  # should always be False (no-growth invariant)
        "per_store": {s: {"original_chars": f["original_chars"], "proposed_chars": f["proposed_chars"],
                          "original_entries": f["original_entries"], "proposed_entries": f["proposed_entries"]}
                      for s, f in out_files.items()},
    }


# --------------------------------------------------------------------------- #
# Manifest                                                                    #
# --------------------------------------------------------------------------- #
def high_impact_review(proposals: list[dict], *, limit: int = 12) -> dict:
    """Summarize proposals that deserve human eyes before live apply.

    This is intentionally redundant with the full manifest: operators should not
    have to open a 1k-line JSON file to notice a high-impact removal. The summary
    highlights every entry that disappears from hot memory plus the largest
    archive/condense replacements. It is deterministic and purely extractive.
    """
    risky_actions = {"remove", "archive_pointer", "rewrite_to_pointer", "merge_absorb"}
    items = []
    for p in proposals:
        action = p.get("rewrite_action")
        if action not in risky_actions:
            continue
        old = p.get("old_text") or ""
        new = p.get("new_text")
        item = {
            "ref": p.get("ref"),
            "store": p.get("store"),
            "kind": p.get("kind"),
            "audit_action": p.get("audit_action"),
            "rewrite_action": action,
            "status": p.get("status"),
            "old_chars": len(old),
            "new_chars": 0 if new is None else len(new),
            "char_delta": p.get("char_delta"),
            "rationale": p.get("rationale"),
            "archive_destination": (p.get("archive") or {}).get("destination"),
            "merge_into": p.get("merge_into"),
            "old_preview": old[:220].replace("\n", " "),
            "new_preview": None if new is None else new[:220].replace("\n", " "),
        }
        # Rank removals first, then biggest shrink. Human review should focus on
        # irreversible-looking hot-memory disappearance before benign compression.
        rank = (0 if action == "remove" else 1, item["char_delta"] or 0)
        items.append((rank, item))
    items.sort(key=lambda x: (x[0][0], x[0][1]))
    selected = [i for _, i in items[:limit]]
    by_action = {}
    for _, i in items:
        by_action[i["rewrite_action"]] = by_action.get(i["rewrite_action"], 0) + 1
    return {
        "requires_human_review_before_apply": bool(items),
        "risky_count": len(items),
        "by_rewrite_action": by_action,
        "top_high_impact": selected,
        "note": "Review these before --apply/--yes. Originals are archived, but false-positive removals still hurt operator trust.",
    }


def build_manifest(plan: dict, archives: list[dict]) -> dict:
    proposals = [dict(p) for p in plan["proposals"]]
    return {
        "schema": "hermes-memory-rewrite-manifest/1", "tool_version": TOOL_VERSION,
        "generated_at": plan["generated_at"], "home": plan["home"],
        "source": {s: {"path": f["path"], "sha256": f["source_sha256"], "chars": f["original_chars"]}
                   for s, f in plan["_out_files_text"].items()},
        "proposed": {s: {"chars": f["proposed_chars"],
                         "sha256": MA.sha256_text(f["proposed_text"])}
                     for s, f in plan["_out_files_text"].items()},
        "proposals": proposals,
        "archives": archives, "summary": plan["summary"],
        "review": high_impact_review(proposals),
    }


def render_review_markdown(manifest: dict) -> str:
    review = manifest.get("review") or {}
    L = ["# High-impact rewrite review", "",
         manifest.get("generated_at", ""), "",
         "Review this file before applying live hot-memory rewrites.", "",
         f"Risky changes: {review.get('risky_count', 0)}",
         "By action: " + ", ".join(f"{k}={v}" for k, v in sorted((review.get('by_rewrite_action') or {}).items())),
         ""]
    for item in review.get("top_high_impact", []):
        L.append(f"## {item['ref']} — {item['rewrite_action']} ({item['old_chars']}→{item['new_chars']} chars)")
        L.append(f"- store/kind: {item['store']} / {item['kind']}")
        L.append(f"- audit action: {item['audit_action']}")
        L.append(f"- rationale: {item['rationale']}")
        if item.get("archive_destination"):
            L.append(f"- archive: {item['archive_destination']}")
        if item.get("merge_into"):
            L.append(f"- merge into: {item['merge_into']}")
        L.append(f"- old: {item['old_preview']}")
        if item.get("new_preview") is None:
            L.append("- new: REMOVED FROM HOT MEMORY (original preserved in manifest/archive)")
        else:
            L.append(f"- new: {item['new_preview']}")
        L.append("")
    if not review.get("top_high_impact"):
        L.append("No high-impact removals/archive replacements were proposed.")
    return "\n".join(L).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Render (writes ONLY under out-dir)                                          #
# --------------------------------------------------------------------------- #
def _canon(p: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(p)))


def render(plan: dict, out_dir: str, *, archive_dir: str | None = None) -> dict:
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    arch_dir = os.path.abspath(os.path.expanduser(archive_dir)) if archive_dir \
        else os.path.join(out_dir, "archive")
    input_paths = {_canon(p) for p in plan["_stores"].values()}

    archives = []
    for p in plan["proposals"]:
        needs = p["rewrite_action"] in ("archive_pointer", "remove", "rewrite_to_pointer") or p.get("absorbs")
        if not needs:
            continue
        os.makedirs(arch_dir, exist_ok=True)
        slug = (p.get("archive") or {}).get("slug") or slugify(f"{plan['generated_at']}-{p['ref'].replace('#', '-')}")
        dest = os.path.join(arch_dir, slug + ".md")
        if _canon(dest) in input_paths:
            raise RuntimeError(f"refusing to archive onto a live input file: {dest}")
        body = [f"# Archived from {p['ref']} ({p['audit_action']}) — {plan['generated_at']}", "", p["old_text"]]
        for ab in (p.get("absorbs") or []):
            body += ["", f"## Absorbed duplicate {ab['ref']}", "", ab["text"]]
        content = "\n".join(body) + "\n"
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
        if p.get("archive"):
            p["archive"]["scratch_path"] = dest
            p["archive"]["written"] = True
        archives.append({"ref": p["ref"], "scratch_path": dest,
                         "proposed_destination": (p.get("archive") or {}).get("destination"),
                         "sha256": MA.sha256_text(content)})

    written = {}
    for store, txt in {s: d["proposed_text"] for s, d in plan["_out_files_text"].items()}.items():
        name = {"memory": "MEMORY.proposed.md", "user": "USER.proposed.md"}.get(store, f"{store}.proposed.md")
        dest = os.path.join(out_dir, name)
        if _canon(dest) in input_paths:
            raise RuntimeError(f"refusing to write proposed output onto a live input file: {dest}")
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(txt)
        written[store] = dest

    manifest = build_manifest(plan, archives)
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    review_path = os.path.join(out_dir, "REVIEW.md")
    with open(review_path, "w", encoding="utf-8") as fh:
        fh.write(render_review_markdown(manifest))
    return {"out_dir": out_dir, "archive_dir": arch_dir, "proposed_files": written,
            "manifest": manifest_path, "review": review_path, "archives": archives}



@contextmanager
def file_lock(target: str):
    """Advisory flock on '<target>.lock', matching curator/auto-extract/temporal writers.

    The lock is taken on the logical live path (not the temp file) so rewrite apply
    serializes with cron sweep, memory tool writes, gateway writers, and auto-extract.
    On platforms without fcntl it degrades to a no-op, matching existing package
    conventions.
    """
    lock_path = os.path.abspath(os.path.expanduser(str(target))) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
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


def _lock_targets(plan: dict) -> list[str]:
    paths = []
    for f in (plan.get("_out_files_text") or {}).values():
        path = f.get("path")
        if path:
            paths.append(os.path.abspath(os.path.expanduser(path)))
    return sorted(set(paths))

# --------------------------------------------------------------------------- #
# Apply (gated; staleness-checked; archive-first). Not used in onboarding.    #
# --------------------------------------------------------------------------- #
def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    i = 1
    while os.path.exists(f"{path}.{i}"):
        i += 1
    return f"{path}.{i}"


def apply(plan: dict, *, confirm: bool, archive_dir: str, now: _dt.datetime | None = None) -> dict:
    result = {"applied": False, "steps": [], "errors": []}
    if not confirm:
        result["errors"].append("REFUSING: apply requires --confirm-apply")
        return result
    now = now or _dt.datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    arch_dir = os.path.abspath(os.path.expanduser(archive_dir))

    # Take every live hot-memory lock before checking SHA drift and hold the locks
    # through archive + atomic write. This closes the TOCTOU/concurrent-writer gap
    # with curator/monitor/consolidation crons and memory-tool writes.
    lock_targets = _lock_targets(plan)
    with ExitStack() as stack:
        for target in lock_targets:
            stack.enter_context(file_lock(target))
        if lock_targets:
            result["steps"].append({"locks_acquired": [p + ".lock" for p in lock_targets]})

        # Staleness guard: the live files must match what the audit saw (no drift).
        # This is intentionally done UNDER the locks so nobody can change the file
        # between the SHA check and the atomic replacement.
        for store, f in plan["_out_files_text"].items():
            src = f["path"]
            if not os.path.exists(src):
                result["errors"].append(f"live file missing since audit: {src} — refusing (stale)")
                return result
            live_sha = MA.sha256_text(MA.read_text(src))
            if f["source_sha256"] and live_sha != f["source_sha256"]:
                result["errors"].append(
                    f"live {os.path.basename(src)} changed since the audit (SHA drift) — "
                    "re-audit before apply")
                return result

        os.makedirs(arch_dir, exist_ok=True)
        # 1) Archive every live original FIRST (timestamped, no-clobber).
        for store, f in plan["_out_files_text"].items():
            src = f["path"]
            dst = _unique_path(os.path.join(arch_dir, f"{os.path.basename(src)}.pre-rewrite-{stamp}"))
            shutil.copy2(src, dst)
            result["steps"].append({"archived_original": dst, "sha256": MA.sha256_text(MA.read_text(dst))})
        # 2) Write a manifest of the change next to the archives.
        try:
            with open(os.path.join(arch_dir, f"rewrite-manifest-{stamp}.json"), "w", encoding="utf-8") as fh:
                json.dump(build_manifest(plan, []), fh, indent=2)
        except Exception:
            pass
        # 3) Atomically write proposed content to the live files.
        for store, f in plan["_out_files_text"].items():
            live = f["path"]
            tmp = live + f".rewrite.{stamp}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(f["proposed_text"])
            os.replace(tmp, live)
            result["steps"].append({"wrote_live": live})
    result["applied"] = True
    # 4) INTEG-3: record this rewrite's provenance into an EXISTING temporal layer.
    #    Gated by the same `confirm` that authorised the live write (we already
    #    returned above without it). If no temporal layer is present we skip with
    #    a warning rather than fabricate one as a side effect of a rewrite.
    _record_rewrite_to_temporal(plan, result)
    return result


def _temporal_layer_exists(home: str) -> bool:
    """True iff a temporal layer is already present for `home` — checked on disk
    WITHOUT constructing TemporalMemory (whose constructor would create an empty
    db as a side effect). The jsonl is the source of truth; the db is rebuildable."""
    if not home:
        return False
    base = os.path.abspath(os.path.expanduser(home))
    jsonl = os.path.join(base, "memories", "_versions", "history.jsonl")
    db = os.path.join(base, "memory_versions.db")
    return os.path.exists(jsonl) or os.path.exists(db)


def _record_rewrite_to_temporal(plan: dict, result: dict) -> None:
    """Best-effort provenance bridge: replay this rewrite's manifest into the
    temporal layer so the old→new chain (and its archive paths) are preserved.

    Never fails the rewrite. If the temporal layer is absent we skip with a note;
    if recording errors we keep the (already-applied) rewrite and warn. Idempotent
    — re-running a manifest already at its end-state records nothing (see
    temporal_migrate_onboard.plan_rewrite_events), so an explicit
    `temporal_migrate_onboard.py record-rewrite` step afterwards is a safe no-op."""
    home = plan.get("home")
    if not _temporal_layer_exists(home):
        result["temporal"] = {"recorded": False, "reason": "no temporal layer present"}
        print(f"  [temporal] no temporal layer at {home} — skipping provenance recording. "
              f"Run `temporal_migrate_onboard.py sync --home {home} --confirm-apply` to enable it.",
              file=sys.stderr)
        return
    try:
        import temporal_memory as _TM
        import temporal_migrate_onboard as _TMO
    except Exception as e:  # pragma: no cover - defensive (deps always co-located)
        result["temporal"] = {"recorded": False, "error": f"import failed: {e}"}
        print(f"  [temporal] WARNING: could not load the temporal layer ({e}); "
              f"rewrite kept, provenance NOT recorded.", file=sys.stderr)
        return
    manifest = build_manifest(plan, [])
    tm = None
    try:
        tm = _TM.TemporalMemory(home=home)
        res = _TMO.record_rewrite(tm, manifest, confirm=True)
        result["temporal"] = {"recorded": True, "events_recorded": res.get("events_recorded", 0),
                              "by_op": res.get("by_op", {}), "by_source": res.get("by_source", {})}
        print(f"  [temporal] recorded {res.get('events_recorded', 0)} provenance event(s) "
              f"(by_source={res.get('by_source', {})}).", file=sys.stderr)
    except Exception as e:
        result["temporal"] = {"recorded": False, "error": str(e)}
        print(f"  [temporal] WARNING: provenance recording failed ({e}); rewrite kept.",
              file=sys.stderr)
    finally:
        if tm is not None:
            try:
                tm.conn.close()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Reports                                                                     #
# --------------------------------------------------------------------------- #
def plan_json(plan: dict) -> dict:
    skip = {"_out_files_text", "_proposals", "_order", "_originals", "_drop_refs", "_stores", "_src_sha"}
    return {k: v for k, v in plan.items() if k not in skip}


def render_markdown(plan: dict) -> str:
    s = plan["summary"]
    L = ["# Hermes Hot-Memory Rewrite Plan (dry-run)", "",
         f"_Generated {plan['generated_at']} · tool v{plan['tool_version']}_", "",
         "## Summary", "",
         "- Rewrite actions: " + ", ".join(f"{k}={v}" for k, v in sorted(s["by_rewrite_action"].items())),
         f"- kept={s['kept']} · rewrite_to_pointer={s['rewritten_to_pointer']} · "
         f"archive_pointer={s['archived_pointer']} · merged={s['merged_absorbed']} · "
         f"removed={s['removed']} · review_needed={s['review_needed']}",
         f"- Chars: {s['original_chars']} → {s['proposed_chars']} "
         f"(reduction {s['reduction_chars']}, {s['reduction_pct']}%)"
         + ("  ⚠️ GROWTH" if s.get("grew") else "")]
    for store, ps in s["per_store"].items():
        L.append(f"  - {store}: {ps['original_chars']}→{ps['proposed_chars']} chars, "
                 f"{ps['original_entries']}→{ps['proposed_entries']} entries")
    L += ["", "## Proposals (old → new)", ""]
    for p in plan["proposals"]:
        if p["rewrite_action"] == "keep":
            continue
        L.append(f"### {p['ref']} — {p['rewrite_action']} ({p['status']})")
        L.append(f"_{p['rationale']}_")
        L.append(f"- OLD ({len(p['old_text'])} chars): {p['old_text'][:160].rstrip()}…"
                 if len(p['old_text']) > 160 else f"- OLD: {p['old_text']}")
        if p["new_text"] is None:
            L.append("- NEW: (removed from hot memory; original preserved in manifest/archive)")
        elif p["new_text"] != p["old_text"]:
            L.append(f"- NEW ({len(p['new_text'])} chars): {p['new_text']}")
        if p.get("merge_into"):
            L.append(f"- MERGE INTO: {p['merge_into']}")
        if p.get("archive"):
            L.append(f"- ARCHIVE → {p['archive']['destination']}")
        L.append("")
    L += ["---", "_Dry-run. No live files modified. `render` writes proposals under --out-dir._"]
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _add_input_args(sp):
    sp.add_argument("--audit", help="memory_audit.py JSON report to consume")
    sp.add_argument("--home", help="Hermes home (run audit internally if --audit absent)")
    sp.add_argument("--memory", help="MEMORY.md path (with --home)")
    sp.add_argument("--user", help="USER.md path (with --home)")
    sp.add_argument("--user-home", help="OS home for resolving ~/ paths (default: audit's, else $HOME)")


def cmd_plan(args) -> int:
    report = load_audit(args)
    plan = build_plan(report, user_home=report_user_home(report, args.user_home))
    obj = plan_json(plan)
    as_json = bool(args.json) or (args.out and args.out.endswith(".json"))
    text = json.dumps(obj, indent=2, default=str) if as_json else render_markdown(plan)
    if args.out:
        if _canon(args.out) in {_canon(p) for p in plan["_stores"].values()}:
            print(f"refusing to write plan onto a live input file: {args.out}", file=sys.stderr)
            return 2
        # Create the parent dir so `--out /tmp/new/plan.json` just works (matches
        # memory_audit --out and render --out-dir) instead of a bare errno + lost file.
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"[wrote plan] {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


def cmd_render(args) -> int:
    if not args.out_dir:
        print("render requires --out-dir", file=sys.stderr)
        return 2
    report = load_audit(args)
    plan = build_plan(report, user_home=report_user_home(report, args.user_home))
    try:
        res = render(plan, args.out_dir, archive_dir=args.archive_dir)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    s = plan["summary"]
    print(f"[render] proposed files: {list(res['proposed_files'].values())}")
    print(f"[render] archives: {len(res['archives'])} originals preserved under {res['archive_dir']}")
    print(f"[render] manifest: {res['manifest']}")
    print(f"[render] REVIEW: {res['review']}")
    review = MR_review = high_impact_review(plan["proposals"])
    if MR_review.get("risky_count"):
        print(f"[render] REVIEW REQUIRED before apply: {MR_review['risky_count']} high-impact change(s) "
              f"{MR_review['by_rewrite_action']}")
    print(f"[render] chars {s['original_chars']}→{s['proposed_chars']} (−{s['reduction_chars']}, "
          f"{s['reduction_pct']}%); kept={s['kept']} rewrite={s['rewritten_to_pointer']} "
          f"archive={s['archived_pointer']} merge={s['merged_absorbed']} remove={s['removed']} "
          f"review={s['review_needed']}")
    return 0


def cmd_apply(args) -> int:
    if not args.confirm_apply:
        print("REFUSING: apply requires --confirm-apply (this rewrites LIVE hot memory).", file=sys.stderr)
        print("Use `render` to produce reviewable proposals instead.", file=sys.stderr)
        return 2
    if not args.archive_dir:
        print("apply requires --archive-dir (live originals are archived first)", file=sys.stderr)
        return 2
    report = load_audit(args)
    plan = build_plan(report, user_home=report_user_home(report, args.user_home))
    res = apply(plan, confirm=args.confirm_apply, archive_dir=args.archive_dir)
    for step in res["steps"]:
        print(f"  {step}")
    t = res.get("temporal")
    if t:
        if t.get("recorded"):
            print(f"  temporal provenance: {t['events_recorded']} event(s) recorded "
                  f"(by_source={t.get('by_source', {})})")
        else:
            print(f"  temporal provenance: not recorded "
                  f"({t.get('reason') or t.get('error')})")
    for e in res["errors"]:
        print(f"  ERROR: {e}", file=sys.stderr)
    return 0 if res["applied"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memory_rewrite.py",
        description="Area 3 — pointer rewrite & consolidation (dry-run by default; never mutates live).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="plan/render never modify live MEMORY.md/USER.md. apply is gated by --confirm-apply, "
               "refuses a stale audit, and archives originals first.\n\n"
               "PIPELINE (Area 3 -> Area 4): steps 5-8 of RUNBOOK.md.\n"
               "  in:    --audit /tmp/mem-audit.json   (from memory_audit.py --out)\n"
               "  this:  memory_rewrite.py render --audit /tmp/mem-audit.json --out-dir /tmp/proposed\n"
               "  next:  temporal_migrate_onboard.py record-rewrite --manifest /tmp/proposed/manifest.json --confirm-apply")
    p.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("plan", help="dry-run: emit old→new proposals + estimated reduction")
    _add_input_args(pl)
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--out", help="write plan to this path (refuses a live input file)")
    pl.set_defaults(func=cmd_plan)

    rd = sub.add_parser("render", help="write proposed files + archived originals + manifest under --out-dir")
    _add_input_args(rd)
    rd.add_argument("--out-dir", required=True, help="output directory for proposals (never live)")
    rd.add_argument("--archive-dir", help="where to write archived originals (default <out-dir>/archive)")
    rd.set_defaults(func=cmd_render)

    ap = sub.add_parser("apply", help="GATED: rewrite live hot memory (staleness-checked; archives first)")
    _add_input_args(ap)
    ap.add_argument("--archive-dir", help="archive live originals here before writing")
    ap.add_argument("--confirm-apply", action="store_true", help="REQUIRED. Without it, apply refuses.")
    ap.set_defaults(func=cmd_apply)
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
