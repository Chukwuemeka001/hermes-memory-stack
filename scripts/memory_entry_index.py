#!/usr/bin/env python3
"""Per-entry semantic index for Hermes hot memory (Phase 2a keystone).

Indexes MEMORY.md / USER.md entries as individual ChromaDB documents keyed by
stable content identity. This is separate from semantic_index.py, which indexes
sessions. The memory projection engine and future memory_search tool need entry-
level retrieval; session-level semantic search is not enough.

Safety: read-only with respect to hot memory files. Writes only to the ChromaDB
index under ``$HERMES_HOME/chroma/sessions`` (same persistent client directory as
session semantic search, separate collection named ``memories``).

CLI:
    python3 scripts/memory_entry_index.py index --home ~/.hermes --json
    python3 scripts/memory_entry_index.py index --home ~/.hermes --reset --json
    python3 scripts/memory_entry_index.py search "trading safety" --home ~/.hermes --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time
from typing import Iterable

# Quiet, deterministic, offline-friendly. Must be set before heavy imports.
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import memory_audit as MA  # noqa: E402
import temporal_memory as TM  # noqa: E402

MODEL_NAME = "all-MiniLM-L6-v2"
COLLECTION_NAME = "memories"
# Keep the same persistent Chroma root as semantic_index.py so one daemon/env
# owns both collections. Collection name separates sessions vs memory entries.
DEFAULT_CHROMA_DIRNAME = os.path.join("chroma", "sessions")
EMBED_TEXT_MAX = 800

_chroma_client = None
_model = None


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def hermes_home(path: str | None = None) -> str:
    return os.path.abspath(os.path.expanduser(path or os.environ.get("HERMES_HOME", "~/.hermes")))


def chroma_path(home: str) -> str:
    return os.path.join(home, DEFAULT_CHROMA_DIRNAME)


def _get_chroma(home: str):
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        from chromadb.config import Settings

        os.makedirs(chroma_path(home), exist_ok=True)
        _chroma_client = chromadb.PersistentClient(
            path=chroma_path(home),
            settings=Settings(anonymized_telemetry=False),
        )
    return _chroma_client


def _get_collection(home: str):
    client = _get_chroma(home)
    return client.get_or_create_collection(
        COLLECTION_NAME,
        metadata={"hnsw:space": "cosine", "kind": "hermes-hot-memory-entries"},
    )


def _delete_collection_if_exists(home: str) -> None:
    client = _get_chroma(home)
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        t0 = time.time()
        _log(f"[memory_entry_index] loading model {MODEL_NAME} ...")
        _model = SentenceTransformer(MODEL_NAME)
        _log(f"[memory_entry_index] model ready in {time.time() - t0:.1f}s")
    return _model


def _default_paths(home: str) -> tuple[str, str]:
    # Reuse the audit module's default path resolver. It already handles Hermes'
    # current memory layout convention.
    return MA._default_paths(home)


def _store_filename(store: str) -> str:
    return "USER.md" if store == "user" else "MEMORY.md"


def entry_id(store: str, content_hash: str) -> str:
    # Chroma IDs are collection-global; include the store so identical content in
    # MEMORY.md and USER.md remains distinguishable.
    return f"{_store_filename(store)}::{content_hash}"


def embed_text_for_entry(text: str) -> str:
    # Keep embedding focused on the entry itself. Long entries should already be
    # rewritten to pointers by the remediation pipeline, but cap defensively.
    return text.strip()[:EMBED_TEXT_MAX]


def load_memory_entries(home: str, *, today: _dt.date | None = None, user_home: str | None = None) -> list[dict]:
    """Return per-entry documents + metadata for MEMORY.md and USER.md.

    Pure/read-only/stdlib except for importing existing memory modules. Useful in
    tests without ChromaDB installed.
    """
    home = hermes_home(home)
    today = today or _dt.date.today()
    user_home = os.path.abspath(os.path.expanduser(user_home or os.path.expanduser("~")))
    owner_stop = MA.owner_stopwords(user_home)
    memory_path, user_path = _default_paths(home)
    out: list[dict] = []
    for path, store in ((memory_path, "memory"), (user_path, "user")):
        audit = MA.audit_file(
            path,
            store,
            user_home,
            max_entry_chars=MA.DEFAULT_MAX_ENTRY_CHARS,
            today=today,
            stale_days=MA.DEFAULT_STALE_AFTER_DAYS,
            owner_stop=owner_stop,
        )
        for entry in audit["entries"]:
            text = entry["text"]
            h = TM.content_hash(text)
            k = TM.derive_key(text)
            sid = entry_id(store, h)
            out.append({
                "id": sid,
                "document": embed_text_for_entry(text),
                "text": text,
                "metadata": {
                    "entry_ref": entry["ref"],
                    "store": _store_filename(store),
                    "store_key": store,
                    "index": int(entry["index"]),
                    "content_hash": h,
                    "fact_key": k,
                    "kind": str(entry.get("kind") or ""),
                    "key": str(entry.get("key") or ""),
                    "preview": str(entry.get("preview") or "")[:200],
                    "source_path": os.path.realpath(path),
                    "chars": int(entry.get("chars") or len(text)),
                },
            })
    return out


def _existing_ids(collection) -> set[str]:
    try:
        if collection.count() <= 0:
            return set()
        return set(collection.get(include=[]).get("ids", []))
    except Exception:
        return set()


def index_memories(home: str | None = None, *, reset: bool = False, batch_size: int = 64,
                   today: _dt.date | None = None, collection=None, model=None) -> dict:
    """Index hot-memory entries into the Chroma ``memories`` collection.

    ``collection`` and ``model`` are injectable for hermetic tests. The real path
    lazily imports ChromaDB + sentence-transformers.
    """
    t0 = time.time()
    home = hermes_home(home)
    if reset:
        _delete_collection_if_exists(home)
    collection = collection or _get_collection(home)
    entries = load_memory_entries(home, today=today)
    existing = set() if reset else _existing_ids(collection)
    live_ids = {e["id"] for e in entries}
    stale_ids = sorted(existing - live_ids)
    if stale_ids:
        collection.delete(ids=stale_ids)
        existing -= set(stale_ids)
    to_index = [e for e in entries if e["id"] not in existing]

    if to_index:
        model = model or _get_model()
        for i in range(0, len(to_index), max(1, int(batch_size))):
            batch = to_index[i:i + max(1, int(batch_size))]
            docs = [b["document"] for b in batch]
            vecs = model.encode(docs, show_progress_bar=False, normalize_embeddings=True).tolist()
            collection.add(
                ids=[b["id"] for b in batch],
                embeddings=vecs,
                documents=docs,
                metadatas=[b["metadata"] for b in batch],
            )
            existing.update(b["id"] for b in batch)

    return {
        "ok": True,
        "collection": COLLECTION_NAME,
        "chroma_path": chroma_path(home),
        "model": MODEL_NAME,
        "home": home,
        "entries_seen": len(entries),
        "newly_indexed": len(to_index),
        "stale_deleted": len(stale_ids),
        "already_indexed": len(entries) - len(to_index),
        "collection_count": int(collection.count()) if hasattr(collection, "count") else len(existing),
        "elapsed_sec": round(time.time() - t0, 2),
    }


def _hit_from(chroma_id, document, metadata, distance) -> dict:
    metadata = metadata or {}
    try:
        distance = float(distance)
    except (TypeError, ValueError):
        distance = None
    return {
        "entry_id": str(chroma_id),
        "entry_ref": metadata.get("entry_ref", ""),
        "store": metadata.get("store", ""),
        "content_hash": metadata.get("content_hash", ""),
        "fact_key": metadata.get("fact_key", ""),
        "kind": metadata.get("kind", ""),
        "preview": metadata.get("preview", ""),
        "source_path": metadata.get("source_path", ""),
        "score": round(1.0 - distance, 4) if distance is not None else None,
        "distance": round(distance, 4) if distance is not None else None,
        "document": document or "",
    }


def _daemon_search_memories(query: str, *, n_results: int, where: dict | None = None) -> list[dict] | None:
    """Ask the warm semantic daemon for memory-entry hits.

    Returns None when the daemon/path is unavailable so callers can fall back to
    the cold direct Chroma path. Returned hits are handle-first and tagged with
    ``__search_source=daemon`` for projection telemetry.
    """
    try:
        import semantic_query as SQ  # noqa: WPS433
        resp = SQ._daemon_request({
            "mode": "semantic",
            "collection": COLLECTION_NAME,
            "query": query,
            "n": max(1, int(n_results)),
            "where": where or None,
            "fields": "handle",
        }, timeout=float(os.environ.get("HERMES_MEMORY_ENTRY_DAEMON_TIMEOUT", "8")))
    except Exception:
        return None
    if not resp.get("ok") or not isinstance(resp.get("results"), list):
        return None
    out = []
    for h in resp.get("results") or []:
        out.append({
            "entry_id": h.get("chroma_id") or h.get("entry_id") or "",
            "entry_ref": h.get("entry_ref", ""),
            "store": h.get("store", ""),
            "content_hash": h.get("content_hash", ""),
            "fact_key": h.get("fact_key", ""),
            "kind": h.get("kind", ""),
            "preview": h.get("preview", ""),
            "source_path": h.get("source_path", ""),
            "score": h.get("score"),
            "distance": h.get("distance"),
            # Handle-only hot path: intentionally omit full document/body.
            "document": "",
            "__search_source": "daemon",
        })
    return out


def search_memories(query: str, home: str | None = None, *, n_results: int = 5,
                    collection=None, model=None, where: dict | None = None) -> list[dict]:
    """Semantic top-k over indexed memory entries. Returns [] on any miss."""
    if not query or not query.strip():
        return []
    home = hermes_home(home)
    if collection is None and model is None and os.environ.get("HERMES_MEMORY_ENTRY_DAEMON", "1") not in {"0", "false", "False"}:
        hits = _daemon_search_memories(query, n_results=n_results, where=where)
        if hits is not None:
            return hits
    try:
        collection = collection or _get_collection(home)
        model = model or _get_model()
        q_emb = model.encode([query], show_progress_bar=False, normalize_embeddings=True).tolist()
        res = collection.query(
            query_embeddings=q_emb,
            n_results=max(1, int(n_results)),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        _log(f"[memory_entry_index] search failed: {e}")
        return []
    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    hits = [_hit_from(ids[i], docs[i] if i < len(docs) else "", metas[i] if i < len(metas) else {}, dists[i] if i < len(dists) else None) for i in range(len(ids))]
    for h in hits:
        h["__search_source"] = "direct"
    return hits


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Index/search Hermes MEMORY.md and USER.md entries semantically.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ip = sub.add_parser("index", help="index hot-memory entries into the memories collection")
    ip.add_argument("--home", default=None, help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    ip.add_argument("--reset", action="store_true", help="drop and rebuild the memories collection")
    ip.add_argument("--batch-size", type=int, default=64)
    ip.add_argument("--json", action="store_true")

    sp = sub.add_parser("search", help="search indexed hot-memory entries")
    sp.add_argument("query")
    sp.add_argument("--home", default=None)
    sp.add_argument("--n", type=int, default=5)
    sp.add_argument("--json", action="store_true")

    args = ap.parse_args(list(argv) if argv is not None else None)
    try:
        if args.cmd == "index":
            result = index_memories(args.home, reset=args.reset, batch_size=args.batch_size)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                _log(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        hits = search_memories(args.query, args.home, n_results=args.n)
        if args.json:
            print(json.dumps({"ok": True, "count": len(hits), "results": hits}, ensure_ascii=False))
        else:
            for i, h in enumerate(hits, 1):
                print(f"{i}. score={h.get('score')} {h.get('entry_ref')} {h.get('preview')}")
        return 0
    except Exception as e:
        err = {"ok": False, "error": str(e)}
        if getattr(args, "json", False):
            print(json.dumps(err))
        else:
            _log(f"[memory_entry_index] FAILED: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
