# Semantic Retrieval + Shadow Mode Ops Notes — 2026-06-25

This note captures the install/debug path for bringing semantic retrieval online in an environment where the agent runtime Python is separate from the semantic/vector Python.

## What failed first

The active agent shell resolved `python3` to the Hermes agent venv on Python 3.11. That venv intentionally did not have heavy vector dependencies:

```text
python3 -> Python 3.11.x agent venv
chromadb: ModuleNotFoundError
sentence_transformers: ModuleNotFoundError
torch: ModuleNotFoundError
```

A Python 3.14 interpreter had the semantic stack:

```text
python3.14 -> Python 3.14.x
chromadb: OK
sentence_transformers: OK
torch: OK
```

The installed script directory also lagged behind the package: session semantic scripts were present, but the newer per-entry memory index and shadow projection scripts were missing.

## Fixes

1. Keep heavy vector dependencies out of the agent venv.
2. Use a dedicated semantic interpreter (`HERMES_SEMANTIC_PYTHON` / `python3.14`) for ChromaDB and sentence-transformers.
3. Install/copy the complete script set together:
   - `semantic_index.py`
   - `semantic_query.py`
   - `memory_entry_index.py`
   - `memory_project.py`
   - `memory_shadow.py`
   - `memory_audit.py`
   - `temporal_memory.py`
   - `memory_signals.py`
4. Patch `install.sh` so semantic install/index/daemon operations use the semantic interpreter, not whichever `python3` the agent shell happens to expose.
5. Patch `memory_project.py` so query-aware projection falls back to `memory_entry_index.py search ... --json` under Python 3.14 when local Chroma imports are unavailable.
6. Keep the fallback transparent in telemetry via notes such as:

```text
relevance_source: memories-index:20 hits via subprocess:python3.14
```
7. Expose both session and memory collection counts through daemon ping so an empty/stale `memories` collection is visible:

```text
{"collection_counts": {"sessions": 224, "memories": 61}}
```

## Verification commands

```bash
# Dependency split
python3 - <<'PY'
import sys
print(sys.executable)
for m in ['chromadb', 'sentence_transformers', 'torch']:
    try:
        mod = __import__(m); print(m, 'OK', getattr(mod, '__version__', '?'))
    except Exception as e:
        print(m, 'ERR', type(e).__name__, e)
PY

python3.14 - <<'PY'
import sys
print(sys.executable)
for m in ['chromadb', 'sentence_transformers', 'torch']:
    mod = __import__(m); print(m, 'OK', getattr(mod, '__version__', '?'))
PY

# Session semantic daemon
python3.14 ~/.hermes/scripts/semantic_query.py --ping

# Session indexing
python3.14 ~/.hermes/scripts/semantic_index.py --json

# Per-entry memory indexing
python3.14 ~/.hermes/scripts/memory_entry_index.py index --home ~/.hermes --reset --json

# Per-entry memory search
python3.14 ~/.hermes/scripts/memory_entry_index.py search \
  'memory projection shadow mode rollout' --home ~/.hermes --n 5 --json

# Shadow mode under agent Python; should still use semantic fallback
python3 ~/.hermes/scripts/memory_shadow.py \
  --home ~/.hermes \
  --query 'semantic retrieval and shadow mode real turns' \
  --budget 1200 \
  --json
```

## Example live results

```text
session semantic daemon: ok, collection_count=184
memory entry index: entries_seen=61, newly_indexed=61, collection_count=61
shadow mode: full 4648 tokens -> projected ~1185 tokens, 74.5% savings
real-turn shadow batch: 5/5 used memories-index:20 hits via subprocess:python3.14
raw memory blocks logged: false
```

## Lessons

- Do not assume `python3` is the semantic Python. In agent environments it may be a lean venv.
- Always verify installed scripts, not just package files.
- Per-entry semantic retrieval needs sibling modules installed beside it.
- Projection should fail closed to static mode if semantic retrieval is unavailable, but should use a Python 3.14 subprocess when available.
- Shadow telemetry should log refs/hashes/diffs by default, not raw memory blocks.
