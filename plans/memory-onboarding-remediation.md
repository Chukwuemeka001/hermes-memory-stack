# Memory Stack Onboarding — Remediation Pipeline

## The Problem

Every new user adopting this memory system will already have a mess. MEMORY.md stuffed with stale dumps, state.db bloated, notes scattered, no pointer discipline. The install must FIX the mess first, then maintain it. Maintenance on top of garbage = garbage maintenance.

## Principle: Never Permanently Delete

Everything that gets cleaned up gets ARCHIVED first. The user can always go back. We restructure, consolidate, and move — we never destroy.

## The Five Remediation Areas

Each gets a dedicated Opus session with full context of the memory system architecture.

---

### Area 1: State.db Audit & Systematic Cleanup

**Why it's complex:** state.db cleanup isn't just "delete old stuff." Every profile has different usage patterns. Some sessions are genuinely valuable history. Compression parents hold the original transcript of a conversation that was summarized — deleting them loses the ability to go back to the full version. The trigram FTS index is 49% of the file but removing it changes search behavior. Unclosed sessions might be resumable work.

**Questions that must be answered before any deletion:**
- What profiles exist and which are active vs dormant?
- What's the age distribution of sessions? (some users want 30 days, some want 365)
- What percentage of sessions are unclosed and WHY? (crash? intentional keep-alive? abandoned?)
- What are compression parents holding? (are the summaries good enough to replace the originals?)
- What does the FTS index look like? (trigram vs unicode61 — what search behavior changes?)
- What's the WAL/journal state? (orphaned WAL = uncommitted data)
- Are there sessions from other tools/agents that shouldn't be touched?

**Rules:**
1. ALWAYS backup before any modification (`cp state.db state.db.pre-remediation`)
2. NEVER delete without archiving (tar the original first)
3. Ask the user: retention period? (30/60/90/180/365 days)
4. Ask the user: drop trigram FTS? (explains the trade-off: ~50% size reduction, loses substring search)
5. Ask the user: prune unclosed sessions by age? (85% never close — they're dead weight eventually)
6. Ask the user: delete compression parent transcripts? (originals vs summaries — which matters more?)
7. Run cleanup on a COPY first, verify the result, then apply to the real file
8. Report: before/after sizes, rows deleted, search behavior changes

**Deliverable:** A systematic cleanup script that asks questions, takes answers, archives, cleans, and reports.

---

### Area 2: MEMORY.md Audit & Quality Assessment

**Why it's complex:** MEMORY.md is a flat text file with entries written by different agent sessions over months. There's no schema enforcement. Entries might be:
- Duplicates (same fact stated differently in two entries)
- Status updates that should have been temporary ("Phase 3 is in progress")
- Debugging findings that don't belong ("The gateway crashed because of X")
- Content dumps (entire paragraphs that should be pointers)
- Stale facts ("Provider X is default" when it was changed weeks ago)
- Contradictions (two entries saying opposite things)
- Too vague ("Trading system needs work")
- Too specific (entire config dumps that belong in notes)

**What the audit must check:**
- Character count vs limit (how close to the ceiling?)
- Entry count (how many discrete facts?)
- Duplicate detection (semantic similarity between entries)
- Staleness check (does the fact still match reality? cross-reference with config, notes, recent sessions)
- Quality score per entry (is it a proper pointer? does it point to something? is it self-contained?)
- Format compliance (does it follow the pointer convention? § entry → source: path)

**Deliverable:** A quality report with per-entry scores, flagged issues, and recommended actions (keep/rewrite/archive/merge).

---

### Area 3: Pointer Rewrite & Consolidation

**Why it's complex:** Rewriting messy entries into proper pointers is a JUDGMENT task, not a mechanical one. Consider:

- "The trading system uses liquidity inducement and order blocks, the main strategy is based on POI identification" → This is a content dump. Should become: "§ Trading brain strategy: liquidity inducement + order blocks → ~/liquidity-inducement-trader/docs/"
- "Provider X is the default, then OpenRouter, then Grok" → This is a fact. Should stay as-is but needs a pointer to where provider config lives.
- "The gateway keeps crashing" → This is a status update. Should be archived, not kept as a pointer.
- "Emeka prefers concise responses" → This is a preference. Should stay as-is (it's a proper pointer).

The rewrite must:
1. Preserve the FACT (what is being said)
2. Remove the DUMP (how much detail is inline vs referenced)
3. Add the POINTER (where to find the full context)
4. Handle EDGE CASES (facts that don't have a natural source file, preferences that are self-contained)
5. Stay within character limits (every byte matters in system prompt)
6. Avoid creating new duplicates (check against existing entries)

**Rules:**
1. NEVER delete the original entry — archive it first
2. Run rewrite in DRY-RUN mode — show the user what would change
3. Each rewrite must be independently reviewable (old → new, with reasoning)
4. If an entry is ambiguous (could be a pointer or a dump), ask the user
5. Preserve the ORDER of entries (most important/frequently accessed first)
6. The rewritten MEMORY.md must pass a quality check before replacing the original

**Deliverable:** A rewrite engine that reads current MEMORY.md, proposes rewrites, and produces a cleaned version. Dry-run by default.

---

### Area 4: Temporal Migration (Existing → Versioned)

**Why it's complex:** Migrating existing entries to temporal versioning isn't just "add a timestamp." Questions:

- What's the "creation date" of an entry that's been in MEMORY.md for months? (file mtime? session date? unknown?)
- Should ALL entries be versioned or just the ones that are facts (not preferences or system config)?
- How do we handle entries that were already updated in-place? (we lost the history — do we note that?)
- What's the initial version number? (1? or do we try to infer history?)
- How do we link entries to their source sessions? (some entries have clear sources, some don't)

**Rules:**
1. Archive current MEMORY.md before migration
2. Version entries that are facts/preferences (not system config or temporary status)
3. Set creation date to: (a) source session date if known, (b) file mtime if not, (c) "unknown" if ambiguous
4. Initial version = 1 for all (we can't recover lost history, but we start tracking from now)
5. Preserve original format in the version record (so we can always reconstruct the original)
6. Verify: can the temporal DB reconstruct the current MEMORY.md exactly?

**Deliverable:** Migration script that versions all existing entries, with source tracking and original preservation.

---

### Area 5: Maintenance Handoff & Self-Monitoring

**Why it's complex:** The transition from "one-time cleanup" to "ongoing maintenance" needs to be seamless. Questions:

- Which crons should run? (daily sweep, capacity monitor, weekly consolidation, semantic re-index, auto-extraction)
- What are the default thresholds? (memory capacity %, stale days, max entries)
- How does the system alert when something is wrong? (Telegram notification? local log? both?)
- What happens when the user makes a manual edit to MEMORY.md? (the curator must not overwrite it)
- How do we prevent the maintenance from becoming the new mess? (who watches the watchers?)

**What must be configured:**
1. Curator daily sweep: schedule, stale threshold, capacity warning %
2. Curator monitor: schedule, alert delivery (Telegram/local)
3. Curator weekly LLM: schedule, consolidation prompt, delivery
4. Semantic re-index: schedule, incremental vs full
5. Auto-extraction: schedule, dry-run mode, signal words, caps
6. Dreaming: schedule, extraction aggressiveness, consolidation rules

**Rules:**
1. All crons must be individually pausable (user can disable any without affecting others)
2. All crons must have a dry-run mode
3. All crons must log what they did (so the user can review)
4. Alerts must be actionable (not just "something happened" but "here's what happened and what to do")
5. The system must self-monitor (if a cron fails 3 times, alert the user)
6. Manual MEMORY.md edits must be preserved (curator proposes changes, doesn't auto-apply)

**Deliverable:** A handoff script that configures all crons, sets thresholds, and runs a verification pass to confirm everything is wired correctly.

---

## Execution Order

These must run SEQUENTIALLY (each depends on the previous):

1. **State.db Cleanup** (Area 1) — clean the database first
2. **MEMORY.md Audit** (Area 2) — assess what we're working with
3. **Pointer Rewrite** (Area 3) — fix the entries based on audit findings
4. **Temporal Migration** (Area 4) — version the cleaned entries
5. **Maintenance Handoff** (Area 5) — set up ongoing maintenance

Each area gets its own Opus session with FULL CONTEXT of:
- The memory system architecture (three-tier, semantic, auto-extraction)
- The results of all previous areas (what was cleaned, what was rewritten, what was versioned)
- The user's preferences (never delete, always archive, dry-run first)
- The export packaging goal (this must work for any Hermes user, not just Emeka)

## Where This Lives

~/.hermes/plans/memory-onboarding-remediation.md (this file)

Queue for execution AFTER current Opus sessions complete (auto-extraction + temporal versioning). Before shipping the memory stack.
