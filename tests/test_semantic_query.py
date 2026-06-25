#!/usr/bin/env python3
"""Tests for semantic_query.py daemon collection routing."""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
SQ_PATH = os.path.join(SCRIPTS, "semantic_query.py")


def _load():
    spec = importlib.util.spec_from_file_location("semantic_query", SQ_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeVec(list):
    def tolist(self):
        return list(self)


class FakeModel:
    def encode(self, docs, **_kw):
        return FakeVec([[0.1, 0.2, 0.3] for _ in docs])


class FakeCollection:
    def __init__(self, name):
        self.name = name
        self.last_where = None
        self.last_include = None

    def count(self):
        return 7 if self.name == "sessions" else 3

    def query(self, query_embeddings, n_results, where=None, include=None):
        self.last_where = where
        self.last_include = include
        if self.name == "memories":
            return {
                "ids": [["MEMORY.md::abc"]],
                "documents": [["Memory projection body"]],
                "metadatas": [[{
                    "entry_ref": "memory#1",
                    "store": "MEMORY.md",
                    "content_hash": "abc",
                    "fact_key": "memory_projection",
                    "kind": "fact",
                    "preview": "Memory projection preview",
                    "source_path": "/tmp/MEMORY.md",
                }]],
                "distances": [[0.12]],
            }
        return {
            "ids": [["default::session1"]],
            "documents": [["Session body"]],
            "metadatas": [[{"session_id": "session1", "title": "Session title", "preview": "Session preview"}]],
            "distances": [[0.2]],
        }


class FakeClient:
    def __init__(self):
        self.collections = {"sessions": FakeCollection("sessions"), "memories": FakeCollection("memories")}

    def get_collection(self, name):
        return self.collections[name]

    def list_collections(self):
        return list(self.collections.values())


class TestSemanticQueryCollections(unittest.TestCase):
    def setUp(self):
        self.sq = _load()
        self.client = FakeClient()
        self.sq._chroma_client = self.client
        self.sq._collections = {}
        self.sq._collection = None
        self.sq._model = FakeModel()

    def test_semantic_search_can_query_memories_handle_only(self):
        hits = self.sq.semantic_search(
            "projection",
            n_results=5,
            collection_name="memories",
            where={"store": "MEMORY.md"},
            include_document=False,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["entry_ref"], "memory#1")
        self.assertEqual(hits[0]["content_hash"], "abc")
        self.assertNotIn("document", hits[0])
        self.assertEqual(self.client.collections["memories"].last_where, {"store": "MEMORY.md"})

    def test_handle_request_threads_collection_where_and_fields(self):
        resp = self.sq._handle_request({
            "mode": "semantic",
            "collection": "memories",
            "query": "projection",
            "n": 3,
            "where": {"store": "MEMORY.md"},
            "fields": "handle",
        })
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["collection"], "memories")
        self.assertEqual(resp["results"][0]["entry_ref"], "memory#1")
        self.assertNotIn("document", resp["results"][0])

    def test_ping_counts_both_collections(self):
        resp = self.sq._handle_request({"mode": "ping"})
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["collection_counts"]["sessions"], 7)
        self.assertEqual(resp["collection_counts"]["memories"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
