#!/usr/bin/env python3
"""
semantic_query.py — Semantic + hybrid retrieval over Hermes sessions (ChromaDB).

Importable module AND CLI. Companion to ``semantic_index.py``.

Public API
----------
    semantic_search(query, n_results=10, db_path=None) -> list[dict]
        Pure vector (cosine) nearest-neighbour search over the indexed session
        summaries. Each hit: session_id, score (1-distance), distance, source,
        title, preview, profile, db_path, started_at, parent_session_id.

    hybrid_search(query, fts_results=None, n_results=10, k=60, db_path=None) -> list[dict]
        Merge semantic ranking with an FTS5 ranking (a list of session_ids in
        rank order) using Reciprocal Rank Fusion:

            score(doc) = Σ_i  1 / (k + rank_i + 1)    (k = 60, rank_i 0-based)

        Each merged hit carries a ``retrieval`` tag: "keyword" | "semantic" |
        "both" and an ``rrf_score``. With ``fts_results=None`` it degrades to a
        pure-semantic ranking expressed through the same RRF shape.

The ``session_search`` tool (agent venv, Python 3.11 — no chromadb) shells out to
this script's ``--json`` mode under python3.14 and does the RRF merge in-process.

CLI
---
    python3 semantic_query.py "telegram gateway recovery"
    python3 semantic_query.py "trading strategy" --n 5
    python3 semantic_query.py "memory architecture" --json --db ~/.hermes/state.db
    python3 semantic_query.py "nclex flashcards" --hybrid --fts ID1,ID2,ID3

Heavy deps (chromadb, sentence_transformers) are imported lazily and live only
under system Python 3.14. Run with python3 / python3.14, not the agent venv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Quiet + deterministic. Must precede heavy imports. Keeps stdout clean so
# --json output is machine-parseable (all model/telemetry noise -> stderr).
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# ---- Shared contract with semantic_index.py (keep in sync) ----
HERMES_HOME = os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes"))
CHROMA_PATH = os.path.join(HERMES_HOME, "chroma", "sessions")
COLLECTION_NAME = "sessions"
MODEL_NAME = "all-MiniLM-L6-v2"
RRF_K = 60

_chroma_client = None
_model = None
_collection = None


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _get_chroma():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        from chromadb.config import Settings

        _chroma_client = chromadb.PersistentClient(
            path=CHROMA_PATH,
            settings=Settings(anonymized_telemetry=False),
        )
    return _chroma_client


def _get_collection():
    """Return the sessions collection, or None if it has never been indexed."""
    global _collection
    if _collection is None:
        try:
            _collection = _get_chroma().get_collection(COLLECTION_NAME)
        except Exception:
            return None
    return _collection


def _reset_collection_cache():
    """Drop the cached client + collection handle.

    A long-lived daemon caches the collection handle, which pins a collection
    UUID. If another process runs ``semantic_index.py --reset`` (delete +
    recreate), the cached handle then raises NotFoundError on every query. We
    reset and re-fetch so the daemon self-heals instead of silently serving
    empty results until a manual restart.
    """
    global _collection, _chroma_client
    _collection = None
    _chroma_client = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _hit_from(chroma_id, document, metadata, distance) -> dict:
    """Shape one ChromaDB hit into the public result dict."""
    metadata = metadata or {}
    try:
        distance = float(distance)
    except (TypeError, ValueError):
        distance = None
    score = round(1.0 - distance, 4) if distance is not None else None
    return {
        # bare session id (composite chroma id splits on '::') — what the DB knows
        "session_id": metadata.get("session_id") or str(chroma_id).split("::", 1)[-1],
        "chroma_id": str(chroma_id),
        "distance": round(distance, 4) if distance is not None else None,
        "score": score,
        "source": metadata.get("source", ""),
        "title": metadata.get("title", ""),
        "preview": metadata.get("preview", ""),
        "profile": metadata.get("profile", ""),
        "db_path": metadata.get("db_path", ""),
        "parent_session_id": metadata.get("parent_session_id", ""),
        "started_at": metadata.get("started_at", 0),
        "document": document,
    }


def semantic_search(query: str, n_results: int = 10, n: int = None, db_path: str = None) -> list:
    """Pure vector search. Returns ranked hits (best first). [] on any miss.

    ``db_path`` scopes results to a single state.db (matches the value stored at
    index time, realpath-normalized) — used by the tool to keep merged sessions
    resolvable against the profile it is running in.
    """
    if n is not None:
        n_results = n
    if not query or not query.strip():
        return []

    where = None
    if db_path:
        where = {"db_path": os.path.realpath(os.path.expanduser(db_path))}

    try:
        model = _get_model()
        q_emb = model.encode([query], show_progress_bar=False, normalize_embeddings=True).tolist()
    except Exception as e:
        _log(f"[semantic_query] embed failed: {e}")
        return []

    # Query with one self-healing retry: a separate process may have dropped and
    # recreated the collection (semantic_index.py --reset), invalidating a cached
    # handle. On error, refresh the cache and try once more before giving up.
    res = None
    for attempt in (1, 2):
        collection = _get_collection()
        if collection is None:
            return []
        try:
            res = collection.query(
                query_embeddings=q_emb,
                n_results=max(1, int(n_results)),
                where=where,
                include=["documents", "metadatas", "distances"],
            )
            break
        except Exception as e:
            _log(f"[semantic_query] query failed (attempt {attempt}): {e}")
            _reset_collection_cache()
            if attempt == 2:
                return []
    if res is None:
        return []

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    hits = []
    for i in range(len(ids)):
        doc = docs[i] if i < len(docs) else None
        meta = metas[i] if i < len(metas) else {}
        dist = dists[i] if i < len(dists) else None
        hits.append(_hit_from(ids[i], doc, meta, dist))
    return hits


def hybrid_search(
    query: str,
    fts_results: list = None,
    n_results: int = 10,
    n: int = None,
    k: int = RRF_K,
    db_path: str = None,
) -> list:
    """Reciprocal Rank Fusion of semantic + FTS5 rankings, keyed by session_id.

    ``fts_results``: session_ids in FTS5 rank order (best first). None/empty =>
    pure-semantic ranking via the same fusion shape.
    """
    if n is not None:
        n_results = n
    semantic_hits = semantic_search(query, n_results=max(n_results * 2, n_results), db_path=db_path)

    scores: dict = {}
    info: dict = {}
    in_sem: set = set()
    in_fts: set = set()

    for rank, hit in enumerate(semantic_hits):
        sid = hit["session_id"]
        # Score each bare session_id at most once: ids are NOT unique across
        # profiles (shared branches), so an unscoped search can return the same
        # bare id twice and double-count its RRF contribution.
        if sid in in_sem:
            continue
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
        info.setdefault(sid, hit)
        in_sem.add(sid)

    for rank, sid in enumerate(fts_results or []):
        if not sid or sid in in_fts:
            continue
        scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank + 1)
        info.setdefault(sid, {"session_id": sid})
        in_fts.add(sid)

    merged = []
    for sid, score in scores.items():
        entry = dict(info.get(sid, {"session_id": sid}))
        if sid in in_sem and sid in in_fts:
            entry["retrieval"] = "both"
        elif sid in in_sem:
            entry["retrieval"] = "semantic"
        else:
            entry["retrieval"] = "keyword"
        entry["rrf_score"] = round(score, 6)
        entry["_raw_score"] = score
        merged.append(entry)

    # Sort on the UNROUNDED score with a stable secondary key so ordering is
    # deterministic (6dp rounding can otherwise manufacture ties).
    merged.sort(key=lambda e: (-e["_raw_score"], e.get("session_id", "")))
    for e in merged:
        e.pop("_raw_score", None)
    return merged[:n_results]


# ----------------------------------------------------------------- warm daemon
#
# A long-lived process that loads the model + collection ONCE and answers queries
# over a Unix-domain socket in ~10-80ms (vs ~20s cold per subprocess). The
# session_search tool — which runs under the agent venv (Python 3.11, no
# chromadb) — talks to this with pure-stdlib sockets, so the heavy deps never
# load in the agent runtime. Start with: python3.14 semantic_query.py --serve

SOCK_PATH = os.environ.get("HERMES_SEMANTIC_SOCK", os.path.join(HERMES_HOME, "chroma", "semantic.sock"))
PID_PATH = os.path.join(HERMES_HOME, "chroma", "semantic.pid")
_MAX_REQ_BYTES = 1_000_000
_MAX_EMBED_BATCH = 512   # cap one embed request (hot-memory files are small by design)


def _handle_request(req: dict) -> dict:
    mode = (req.get("mode") or "semantic").lower()
    if mode == "ping":
        return {"ok": True, "pong": True, "collection_count": _safe_count()}
    if mode == "embed":
        # INTEG-9: embed arbitrary texts (e.g. hot-memory entries for semantic
        # near-duplicate detection in memory_audit.py --semantic). The daemon already
        # holds the model warm, so callers get unit vectors over the socket without
        # ever importing chromadb / sentence-transformers themselves.
        texts = req.get("texts")
        if not isinstance(texts, list):
            return {"ok": False, "error": "embed requires a 'texts' list"}
        texts = [("" if t is None else str(t)) for t in texts][:_MAX_EMBED_BATCH]
        if not texts:
            return {"ok": True, "mode": "embed", "count": 0, "dim": 0, "embeddings": []}
        try:
            model = _get_model()
            vecs = model.encode(texts, show_progress_bar=False,
                                normalize_embeddings=True).tolist()
        except Exception as e:
            _log(f"[semantic_query] embed failed: {e}")
            return {"ok": False, "error": f"embed failed: {e}"}
        return {"ok": True, "mode": "embed", "count": len(vecs),
                "dim": (len(vecs[0]) if vecs else 0), "embeddings": vecs}
    query = req.get("query") or ""
    db_path = req.get("db_path") or None
    try:
        n = int(req.get("n") or 10)
    except (TypeError, ValueError):
        n = 10
    n = max(1, min(n, 200))
    if mode == "hybrid":
        hits = hybrid_search(query, fts_results=req.get("fts") or None, n_results=n, db_path=db_path)
    else:
        hits = semantic_search(query, n_results=n, db_path=db_path)
    return {"ok": True, "mode": mode, "count": len(hits), "results": hits}


def _safe_count() -> int:
    for attempt in (1, 2):
        c = _get_collection()
        if c is None:
            return 0
        try:
            return c.count()
        except Exception:
            _reset_collection_cache()  # self-heal a stale handle (see semantic_search)
            if attempt == 2:
                return 0
    return 0


def _serve_conn(conn) -> None:
    """Handle one client connection: read a JSON line, answer, close.

    Runs in its own short-lived thread so a slow/stalled client cannot block
    other clients (the model + collection are loaded once before listen() and
    are read-only here; ChromaDB reads and model.encode are concurrency-safe).
    """
    try:
        conn.settimeout(20)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_REQ_BYTES:
                break
        line = buf.split(b"\n", 1)[0].strip()
        if line:
            try:
                resp = _handle_request(json.loads(line.decode("utf-8")))
            except Exception as e:
                resp = {"ok": False, "error": str(e)}
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    except Exception as e:
        _log(f"[semantic_query] request error: {e}")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve(sock_path: str = None) -> int:
    """Run the warm query daemon (blocks). One JSON request per connection."""
    import socket
    import signal
    import threading

    sock_path = sock_path or SOCK_PATH
    os.makedirs(os.path.dirname(sock_path), exist_ok=True)

    # Warm the heavy bits before accepting traffic.
    _get_collection()
    try:
        _get_model().encode(["warmup"], show_progress_bar=False, normalize_embeddings=True)
    except Exception as e:
        _log(f"[semantic_query] warmup failed: {e}")

    if os.path.exists(sock_path):
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    try:
        os.chmod(sock_path, 0o600)
    except OSError:
        pass
    srv.listen(64)
    my_pid = os.getpid()
    try:
        with open(PID_PATH, "w") as f:
            f.write(str(my_pid))
    except OSError:
        pass

    def _cleanup(*_a):
        # Ownership-aware: only remove the pidfile if it still names THIS process
        # (a newer daemon may have taken over during a bounce). Leave the socket
        # to the owner that bound it last.
        try:
            with open(PID_PATH) as f:
                owner = f.read().strip()
            if owner == str(my_pid):
                os.unlink(PID_PATH)
        except OSError:
            pass
        try:
            os.unlink(sock_path)
        except OSError:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)
    _log(f"[semantic_query] daemon ready on {sock_path} (collection_count={_safe_count()})")

    while True:
        try:
            conn, _ = srv.accept()
        except (KeyboardInterrupt, SystemExit):
            _cleanup()
        except OSError:
            continue
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


def _daemon_request(req: dict, sock_path: str = None, timeout: float = 30.0) -> dict:
    """Send one JSON request to the warm daemon and return its JSON reply.
    Pure stdlib (AF_UNIX) — no chromadb / sentence-transformers in the caller.
    Returns {"ok": False, "error": ...} on any transport failure."""
    import socket

    sock_path = sock_path or SOCK_PATH
    try:
        payload = (json.dumps(req) + "\n").encode("utf-8")
    except (TypeError, ValueError) as e:
        return {"ok": False, "error": f"unserialisable request: {e}"}
    s = None
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if s is not None:
            try:
                s.close()
            except OSError:
                pass


def ping(sock_path: str = None, timeout: float = 5.0) -> dict:
    """Client-side daemon health check (pure stdlib — mirrors the tool client)."""
    return _daemon_request({"mode": "ping"}, sock_path=sock_path, timeout=timeout)


def embed_texts(texts, sock_path: str = None, timeout: float = 30.0):
    """Client: ask the warm daemon to embed `texts` into unit vectors.

    Returns a list of vectors (one per input, same order) on success, or **None**
    when the daemon is unavailable or doesn't support embedding (an OLD daemon that
    predates this mode answers without an 'embeddings' field — treated as a miss so
    callers fall back cleanly). Pure stdlib; the heavy model stays in the daemon.
    """
    texts = list(texts)
    if not texts:
        return []
    resp = _daemon_request({"mode": "embed", "texts": texts}, sock_path=sock_path, timeout=timeout)
    if not resp.get("ok") or "embeddings" not in resp:
        return None
    vecs = resp.get("embeddings")
    if not isinstance(vecs, list) or len(vecs) != len(texts):
        return None
    return vecs


# --------------------------------------------------------------------------- CLI

def _print_human(query: str, hits: list, hybrid: bool) -> None:
    label = "HYBRID (RRF)" if hybrid else "SEMANTIC"
    print(f"\n{label} results for: {query!r}\n")
    if not hits:
        print("  (no results — is the index built? run semantic_index.py)\n")
        return
    for i, h in enumerate(hits, 1):
        if hybrid:
            tag = h.get("retrieval", "?")
            head = f"{i}. [{tag:<8}] rrf={h.get('rrf_score')}  {h.get('session_id')}"
        else:
            head = f"{i}. [score {h.get('score')}] {h.get('session_id')}"
        print(head)
        meta = f"   {h.get('profile','')}/{h.get('source','')}"
        if h.get("title"):
            meta += f"  · {h['title']}"
        print(meta)
        prev = (h.get("preview") or h.get("document") or "").replace("\n", " ")
        if prev:
            print(f"   {prev[:160]}")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Semantic / hybrid search over Hermes sessions.")
    ap.add_argument("query", nargs="*", help="query text")
    ap.add_argument("--n", type=int, default=10, help="number of results (default 10)")
    ap.add_argument("--db", default=None, help="scope to a single state.db path")
    ap.add_argument("--json", action="store_true", help="emit JSON on stdout (machine mode)")
    ap.add_argument("--hybrid", action="store_true", help="RRF-merge with --fts ids")
    ap.add_argument("--fts", default=None, help="comma-separated FTS5 session_ids (rank order) for --hybrid")
    ap.add_argument("--serve", action="store_true", help="run the warm query daemon (blocks)")
    ap.add_argument("--ping", action="store_true", help="health-check a running daemon")
    args = ap.parse_args(argv)

    if args.serve:
        return serve()
    if args.ping:
        resp = ping()
        print(json.dumps(resp))
        return 0 if resp.get("ok") else 1

    query = " ".join(args.query).strip()
    if not query:
        print(json.dumps({"error": "empty query", "results": []}) if args.json
              else "usage: semantic_query.py <query> [--n N] [--db PATH] [--json] [--hybrid --fts a,b,c]")
        return 2

    if args.hybrid:
        fts = [s for s in (args.fts or "").split(",") if s.strip()]
        hits = hybrid_search(query, fts_results=fts, n_results=args.n, db_path=args.db)
    else:
        hits = semantic_search(query, n_results=args.n, db_path=args.db)

    if args.json:
        print(json.dumps({
            "query": query,
            "db_path": os.path.realpath(os.path.expanduser(args.db)) if args.db else None,
            "mode": "hybrid" if args.hybrid else "semantic",
            "count": len(hits),
            "results": hits,
        }, ensure_ascii=False))
    else:
        _print_human(query, hits, hybrid=args.hybrid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
