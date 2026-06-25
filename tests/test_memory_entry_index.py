#!/usr/bin/env python3
"""Tests for memory_entry_index.py (Phase 2a per-entry memory index)."""
from __future__ import annotations

import atexit
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)

MEI_PATH = os.path.join(SCRIPTS, "memory_entry_index.py")


def _load():
    spec = importlib.util.spec_from_file_location("memory_entry_index", MEI_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mei = _load()
DELIM = "\n§\n"


def make_home(memory_entries=None, user_entries=None) -> str:
    root = tempfile.mkdtemp(prefix="mei_home_")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    mem_dir = os.path.join(root, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write(DELIM.join(memory_entries or []))
    with open(os.path.join(mem_dir, "USER.md"), "w", encoding="utf-8") as fh:
        fh.write(DELIM.join(user_entries or []))
    return root


class FakeVec(list):
    def tolist(self):
        return list(self)


class FakeModel:
    def encode(self, docs, **_kw):
        return FakeVec([[float(i), 0.0, 1.0] for i, _ in enumerate(docs)])


class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.add_calls = 0

    def count(self):
        return len(self.rows)

    def get(self, include=None):
        return {"ids": list(self.rows)}

    def add(self, ids, embeddings, documents, metadatas):
        self.add_calls += 1
        for i, cid in enumerate(ids):
            self.rows[cid] = {
                "embedding": embeddings[i],
                "document": documents[i],
                "metadata": metadatas[i],
            }

    def delete(self, ids):
        for cid in ids:
            self.rows.pop(cid, None)

    def query(self, query_embeddings, n_results, include, where=None):
        self.last_where = where
        ids = list(self.rows)[:n_results]
        return {
            "ids": [ids],
            "documents": [[self.rows[i]["document"] for i in ids]],
            "metadatas": [[self.rows[i]["metadata"] for i in ids]],
            "distances": [[0.1 + (j * 0.01) for j, _ in enumerate(ids)]],
        }


class TestLoadMemoryEntries(unittest.TestCase):
    def test_loads_memory_and_user_entries_with_shared_identity(self):
        root = make_home(
            memory_entries=["Trading safety: never place live trades without approval."],
            user_entries=["User prefers blunt direct correction over reassurance."],
        )
        entries = mei.load_memory_entries(root, user_home=root)
        self.assertEqual(len(entries), 2)
        stores = {e["metadata"]["store"] for e in entries}
        self.assertEqual(stores, {"MEMORY.md", "USER.md"})
        for e in entries:
            self.assertIn("::", e["id"])
            self.assertEqual(e["id"].split("::", 1)[1], e["metadata"]["content_hash"])
            self.assertTrue(e["metadata"]["fact_key"])
            self.assertTrue(e["metadata"]["entry_ref"])
            self.assertTrue(os.path.isabs(e["metadata"]["source_path"]))

    def test_embedding_text_is_capped(self):
        text = "x" * (mei.EMBED_TEXT_MAX + 100)
        self.assertEqual(len(mei.embed_text_for_entry(text)), mei.EMBED_TEXT_MAX)

    def test_entry_id_distinguishes_memory_and_user(self):
        self.assertNotEqual(mei.entry_id("memory", "abc"), mei.entry_id("user", "abc"))
        self.assertEqual(mei.entry_id("memory", "abc"), "MEMORY.md::abc")
        self.assertEqual(mei.entry_id("user", "abc"), "USER.md::abc")


class TestIndexMemories(unittest.TestCase):
    def test_indexes_incrementally_into_collection(self):
        root = make_home(
            memory_entries=["NCLEX: canonical repo lives under /tmp/nclex."],
            user_entries=["User prefers testing before moving ahead."],
        )
        collection = FakeCollection()
        result = mei.index_memories(root, collection=collection, model=FakeModel())
        self.assertTrue(result["ok"])
        self.assertEqual(result["entries_seen"], 2)
        self.assertEqual(result["newly_indexed"], 2)
        self.assertEqual(result["collection_count"], 2)
        self.assertEqual(collection.add_calls, 1)

        second = mei.index_memories(root, collection=collection, model=FakeModel())
        self.assertEqual(second["newly_indexed"], 0)
        self.assertEqual(second["already_indexed"], 2)
        self.assertEqual(collection.add_calls, 1)  # no second add

    def test_edit_evicts_stale_content_hash_doc(self):
        first = "Trading safety: never place live trades without approval."
        second = "Trading safety: do not place live trades without explicit approval."
        root = make_home(memory_entries=[first])
        collection = FakeCollection()
        mei.index_memories(root, collection=collection, model=FakeModel())
        self.assertEqual(collection.count(), 1)
        old_id = next(iter(collection.rows))

        with open(os.path.join(root, "memories", "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(second)
        result = mei.index_memories(root, collection=collection, model=FakeModel())
        self.assertEqual(result["stale_deleted"], 1)
        self.assertEqual(result["newly_indexed"], 1)
        self.assertEqual(collection.count(), 1)
        self.assertNotIn(old_id, collection.rows)
        only = next(iter(collection.rows.values()))
        self.assertIn("explicit approval", only["document"])

    def test_search_shapes_memory_hits(self):
        root = make_home(memory_entries=["Trading safety: never place live trades without approval."])
        collection = FakeCollection()
        mei.index_memories(root, collection=collection, model=FakeModel())
        hits = mei.search_memories("trading safety", root, collection=collection, model=FakeModel(), n_results=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["store"], "MEMORY.md")
        self.assertIn("Trading safety", hits[0]["document"])
        self.assertEqual(hits[0]["__search_source"], "direct")
        self.assertGreater(hits[0]["score"], 0.0)

    def test_daemon_search_is_preferred_and_handle_only(self):
        root = make_home(memory_entries=["Memory projection uses semantic relevance."])
        calls = []

        def fake_daemon(req, timeout=30.0):
            calls.append((req, timeout))
            return {
                "ok": True,
                "results": [{
                    "chroma_id": "MEMORY.md::abc",
                    "entry_ref": "memory#1",
                    "store": "MEMORY.md",
                    "content_hash": "abc",
                    "fact_key": "memory_projection",
                    "preview": "Memory projection uses semantic relevance.",
                    "score": 0.91,
                    "distance": 0.09,
                    "document": "SHOULD_NOT_CROSS_HANDLE_PATH",
                }],
            }

        old = mei.sys.modules.get("semantic_query")
        import types
        mei.sys.modules["semantic_query"] = types.SimpleNamespace(_daemon_request=fake_daemon)
        try:
            hits = mei.search_memories("projection", root, n_results=3, where={"store": "MEMORY.md"})
        finally:
            if old is None:
                mei.sys.modules.pop("semantic_query", None)
            else:
                mei.sys.modules["semantic_query"] = old
        self.assertEqual(calls[0][0]["collection"], "memories")
        self.assertEqual(calls[0][0]["fields"], "handle")
        self.assertEqual(calls[0][0]["where"], {"store": "MEMORY.md"})
        self.assertEqual(hits[0]["entry_ref"], "memory#1")
        self.assertEqual(hits[0]["document"], "")
        self.assertEqual(hits[0]["__search_source"], "daemon")


if __name__ == "__main__":
    unittest.main(verbosity=2)
