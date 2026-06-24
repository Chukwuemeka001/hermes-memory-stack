#!/usr/bin/env python3
"""One-shot, idempotent migration: seed temporal version history from the
existing Hermes hot-memory files and the curator's archive blocks.

Run order (final state is order-independent — the materializer sorts a fact's
events by real-world time — but files-first gives accurate linkage reporting):
  1. ingest MEMORY.md + USER.md   -> v1 (or current) for every live entry
  2. ingest-archives              -> reconstruct PRIOR full-content versions
                                     from ~/.hermes/memories/_archive/curator/*.md
                                     so already-archived facts get real history
                                     beneath their current `↪` pointer stub.

Idempotent: re-running records nothing new (same content_hash => skipped).
Safe to run before auto-extraction goes live (gives the first UPDATE a baseline).

Usage:
  python3 ~/.hermes/scripts/temporal_migrate.py [--home DIR] [--db PATH] [--jsonl PATH] [--json]
"""
from __future__ import annotations

import argparse
import json

from temporal_memory import TemporalMemory, now_iso


def migrate(home=None, db_path=None, jsonl_path=None) -> dict:
    tm = TemporalMemory(home=home, db_path=db_path, jsonl_path=jsonl_path)
    before = tm.stats()
    files = tm.ingest_files(["MEMORY.md", "USER.md"], source="migration",
                            actor="temporal_migrate.py")
    archives = tm.ingest_archives()
    after = tm.stats()
    return {
        "ran_at": now_iso(),
        "files_reconcile": files,
        "archive_reconstruction": archives,
        "facts_before": before["facts"],
        "facts_after": after["facts"],
        "facts_with_history": after["facts_with_history"],
        "total_versions": after["total_versions"],
        "current_by_store": after["current_by_store"],
        "jsonl": after["jsonl"],
        "db": after["db"],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Seed temporal memory history (idempotent).")
    p.add_argument("--home"); p.add_argument("--db"); p.add_argument("--jsonl")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    result = migrate(home=args.home, db_path=args.db, jsonl_path=args.jsonl)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        f = result["files_reconcile"]
        a = result["archive_reconstruction"]
        print("=" * 70)
        print(f"TEMPORAL MEMORY MIGRATION — {result['ran_at']}")
        print("=" * 70)
        print(f"Live entries: created={f['created']} updated={f['updated']} "
              f"duplicate={f['duplicate']} archived={f['archived']} deleted={f['deleted']}")
        print(f"Archive reconstruction: {a['blocks']} blocks -> recorded={a['recorded']} "
              f"linked={a['linked']} standalone={a['standalone']}")
        print(f"Facts now versioned : {result['facts_after']} "
              f"({result['facts_with_history']} with >1 version)")
        print(f"Total versions      : {result['total_versions']}")
        print(f"Current by store    : {result['current_by_store']}")
        print(f"JSONL (truth)       : {result['jsonl']}")
        print(f"SQLite index        : {result['db']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
