#!/usr/bin/env python3
"""Tests for memory_search_map.py — the cheap dynamic search-map router.

Hermetic: every test builds a synthetic HERMES_HOME in a temp dir (MEMORY.md /
USER.md, notes index, a temporal DB, a spine DB, a shadow report) and never
touches live data or the semantic daemon. The semantic lane therefore degrades to
"down", which is exactly what we assert it does gracefully.

Run:
    python3 -m unittest tests.test_memory_search_map -v
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPTS = os.path.join(ROOT, "scripts")
SCRIPT = os.path.join(SCRIPTS, "memory_search_map.py")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import memory_search_map as MSM  # noqa: E402

DELIM = "\n§\n"

# A pattern-matching but obviously-fake credential, assembled at runtime so the
# literal never appears as a contiguous token in this source file (keeps secret
# scanners quiet while still exercising the redactor).
FAKE_SECRET = "sk-" + ("A" * 40)


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _make_temporal_db(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE versions (fact_key TEXT, store TEXT, is_current INTEGER, "
                "recorded_at TEXT, content TEXT)")
    con.executemany(
        "INSERT INTO versions (fact_key, store, is_current, recorded_at, content) VALUES (?,?,?,?,?)",
        [
            ("provider-failover", "MEMORY.md", 1, "2026-06-24T10:00:00+00:00", "v2"),
            ("provider-failover", "MEMORY.md", 0, "2026-06-01T10:00:00+00:00", "v1"),
            ("trading-poi", "MEMORY.md", 1, "2026-06-20T10:00:00+00:00", "poi"),
        ],
    )
    con.commit()
    con.close()


def _make_spine_db(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, body TEXT)")
    con.execute("CREATE TABLE artifacts (id INTEGER PRIMARY KEY, path TEXT)")
    con.executemany("INSERT INTO events (ts, body) VALUES (?,?)",
                    [("2026-06-25T09:00:00+00:00", "ev1"), ("2026-06-24T09:00:00+00:00", "ev2")])
    con.execute("INSERT INTO artifacts (path) VALUES ('a.md')")
    con.commit()
    con.close()


def build_home(tmp: str, *, with_notes=True, with_temporal=True, with_spine=True,
               with_shadow=True, with_memory=True) -> str:
    home = os.path.join(tmp, ".hermes")
    os.makedirs(home, exist_ok=True)
    if with_memory:
        mem = DELIM.join([
            "Long-form notes live in `~/.hermes/notes/INDEX.md`. Read it first.",
            "Hermes routing: failover automation prefers Anthropic, Xiaomi fallback role on outage.",
            "User prefers blunt ROI-focused correction over reassurance, always.",
            # an entry whose first line carries a fake credential -> must be redacted
            f"GBrain key policy: API key {FAKE_SECRET} must never be shared; gateway-only.",
            "Trading POI spec: order blocks and liquidity inducement define points of interest.",
            "Memory stack project: semantic retrieval shadow mode rollout in progress.",
            # a multi-line entry: only the FIRST line may surface as a label; the
            # body line below must never be dumped into the map.
            "NCLEX trainer status: phase 2.\nZZBODYDETAIL deep rationale that must stay private xyz.",
        ])
        _write(os.path.join(home, "memories", "MEMORY.md"), mem)
        usr = DELIM.join([
            "Sample Owner: trader-engineer hybrid, money-minded, anti-pivot.",
            "User requires real verification of delegated output before trusting it.",
            "Model routing: auto-delegate complex debugging to Anthropic without setup.",
        ])
        _write(os.path.join(home, "memories", "USER.md"), usr)
    if with_notes:
        _write(os.path.join(home, "notes", "INDEX.md"),
               "# Notes\n\n## Index\n\n"
               "- `trading/poiwatcher-architecture.md` — Frontend PWA + Render backend layout.\n"
               "- `hermes/routing.md` — Provider failover and routing config canonical doc.\n"
               "- `nclex/project-status.md` — Current NCLEX repo and pipeline status.\n")
        _write(os.path.join(home, "notes", "MASTER_CONTEXT_INDEX.md"),
               "# Master\n\n- `atlas-status.md` — Atlas autonomous agent status map.\n")
    if with_temporal:
        _make_temporal_db(os.path.join(home, "memory_versions.db"))
    if with_spine:
        _make_spine_db(os.path.join(home, "memory_spine", "memory_spine.sqlite"))
    if with_shadow:
        _write(os.path.join(home, "reports", "shadow-report-2026-06-25.json"),
               json.dumps({"status": "PASS", "generated_at": "2026-06-25"}))
    return home



def _write_shadow_jsonl(home: str, rows: list[dict]) -> str:
    path = os.path.join(home, "reports", "shadow-p3-test.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path

TODAY = "2026-06-25"

# Injected "daemon up" semantic health for hermetic PASS paths (no real daemon
# runs in tests). Same shape probe_semantic() returns.
SEMANTIC_UP = {"present": True, "path": "~/.hermes/chroma/sessions", "daemon": "up",
               "sessions": 5, "memories": 3, "status": "ok"}


class SearchMapBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.home = build_home(self.td.name)

    def build(self, home=None, **kw):
        import datetime as dt
        return MSM.build_map(home or self.home, today=dt.date.fromisoformat(TODAY),
                             ping_timeout=0.2, **kw)


class TestBuild(SearchMapBase):
    def test_stores_present_and_counted(self):
        m = self.build()
        s = m["stores"]
        self.assertEqual(s["hot_memory"]["status"], "ok")
        self.assertEqual(s["hot_memory"]["entries"], 7)
        self.assertEqual(s["user_memory"]["entries"], 3)
        self.assertEqual(s["notes_index"]["topics_indexed"], 3)
        self.assertEqual(s["master_index"]["topics_indexed"], 1)
        self.assertEqual(s["temporal"]["current_facts"], 2)
        self.assertEqual(s["temporal"]["total_versions"], 3)
        self.assertEqual(s["spine"]["events"], 2)
        self.assertEqual(s["spine"]["artifacts"], 1)
        self.assertEqual(s["shadow_report"]["report_status"], "PASS")

    def test_semantic_degrades_without_daemon(self):
        m = self.build()
        sem = m["stores"]["semantic"]
        self.assertNotEqual(sem.get("daemon"), "up")
        self.assertIn(sem["status"], ("down", "no-socket", "missing", "ping-failed"))
        # the memory-entry lane is still listed, just degraded
        lane = next(l for l in m["lanes"] if l["id"] == "memory-entry")
        self.assertIn(lane["availability"], ("degraded", "missing", "ok"))

    def test_topics_built_and_capped(self):
        m = self.build(max_topics=5)
        self.assertLessEqual(len(m["topics"]), 5)
        self.assertGreater(m["topics_total"], 5)
        keys = {t["key"] for t in m["topics"]}
        self.assertTrue(any("routing" in k or "trading" in k or "memory" in k for k in keys))

    def test_secret_is_redacted_in_topics(self):
        m = self.build()
        blob = json.dumps(MSM._strip_internal(m))
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertNotIn("sk-AAAA", blob)
        self.assertIn("[REDACTED]", blob)

    def test_no_raw_bodies_emitted(self):
        m = self.build()
        blob = json.dumps(MSM._strip_internal(m))
        # only first lines may become labels; a second-line body phrase must not
        # be dumped into the map.
        self.assertNotIn("ZZBODYDETAIL", blob)
        self.assertNotIn("deep rationale that must stay private", blob)
        # internal full-topic pool must be stripped from the public map
        self.assertNotIn("_all_topics", blob)

    def test_lanes_cover_required_kinds(self):
        m = self.build()
        ids = {l["id"] for l in m["lanes"]}
        for required in ("memory-entry", "session-semantic", "temporal", "notes-canonical", "source-code"):
            self.assertIn(required, ids)


class TestMarkdownBudget(SearchMapBase):
    def test_markdown_under_budget(self):
        m = self.build()
        md = MSM.render_map_markdown(MSM._strip_internal(m))
        md, _ = MSM._trim_markdown_to_budget(m, md, MSM.DEFAULT_MAP_TOKEN_BUDGET)
        self.assertLessEqual(MSM.est_tokens(md), MSM.DEFAULT_MAP_TOKEN_BUDGET)
        self.assertIn("# Memory Search Map", md)
        self.assertIn("## Lanes", md)

    def test_markdown_trims_when_over_tiny_budget(self):
        m = self.build()
        md = MSM.render_map_markdown(MSM._strip_internal(m))
        trimmed_md, trimmed = MSM._trim_markdown_to_budget(m, md, 200)
        self.assertTrue(trimmed or MSM.est_tokens(trimmed_md) <= 200 + 50)
        self.assertLessEqual(len(trimmed_md), len(md) + 80)


class TestQuery(SearchMapBase):
    def query(self, q):
        m = self.build()
        return MSM.rank_lanes(m, q, home=self.home)

    def test_preference_routes_to_memory_entry(self):
        r = self.query("what does the user prefer for correction style")
        self.assertEqual(r["recommended_lane"], "memory-entry")
        top = r["lanes"][0]
        self.assertIn("memory_entry_index.py", top["command"])
        self.assertIn("correction", r["query_terms"])

    def test_history_routes_to_temporal_or_relevant(self):
        r = self.query("what changed in the provider failover since last month")
        ranked = {l["id"]: l for l in r["lanes"]}
        # temporal must score above the availability-only floor (intent hit)
        self.assertGreater(ranked["temporal"]["score"], 1.0)
        self.assertTrue(any("intent" in x for x in ranked["temporal"]["reasons"]))

    def test_notes_canonical_surfaces_path(self):
        r = self.query("where is the routing canonical documentation note")
        notes = next(l for l in r["lanes"] if l["id"] == "notes-canonical")
        self.assertGreater(notes["score"], 1.0)
        # a matched note topic should fill the read_file path with a real .md
        joined = notes["command"] + json.dumps(notes["matched_topics"])
        self.assertIn(".md", joined)

    def test_source_code_intent(self):
        r = self.query("which function implements the knapsack in memory_project.py")
        ranked = {l["id"]: l for l in r["lanes"]}
        self.assertTrue(any("intent" in x for x in ranked["source-code"]["reasons"]))

    def test_no_match_uses_availability_note(self):
        r = self.query("qqzz wwxx vvyy")
        self.assertFalse(r["matched"])
        self.assertIsNotNone(r["note"])
        self.assertIsNotNone(r["recommended_lane"])

    def test_query_command_has_query_substituted(self):
        r = self.query("semantic retrieval shadow mode")
        top = r["lanes"][0]
        self.assertIn("semantic retrieval shadow mode", top["command"])

    def test_matched_topics_deduped(self):
        r = self.query("memory stack semantic retrieval shadow")
        for lane in r["lanes"]:
            seen = [(m["key"], m["where"]) for m in lane["matched_topics"]]
            self.assertEqual(len(seen), len(set(seen)))

    def test_route_packet_filters_user_memory_when_topic_source_is_user(self):
        r = self.query("requires real verification delegated output trusting")
        self.assertEqual(r["recommended_lane"], "memory-entry")
        self.assertEqual(r["route_packet"]["memory_where"], {"store_key": "user"})
        self.assertIn("requires", r["route_packet"]["query_terms"])

    def test_lane_feedback_from_shadow_jsonl_adjusts_ranking(self):
        rows = []
        for i in range(5):
            rows.append({
                "tool": "memory_shadow", "mode": "shadow", "turn_id": f"ok{i}",
                "generated_at": "2026-06-25T09:00:00+00:00",
                "projected": {
                    "savings_pct": 57.0,
                    "relevance_source": "memories-index:20 hits via daemon",
                    "retrieval_telemetry": {"path": "daemon"},
                    "route_packet": {"recommended_lane": "memory-entry"},
                },
                "answer_usage": {"used_missing_from_projection": [], "used_selected_count": 2},
            })
        for i in range(5):
            rows.append({
                "tool": "memory_shadow", "mode": "shadow", "turn_id": f"bad{i}",
                "generated_at": "2026-06-25T10:00:00+00:00",
                "projected": {
                    "savings_pct": 10.0,
                    "route_packet": {"recommended_lane": "session-semantic"},
                    "retrieval_telemetry": {"path": "subprocess"},
                },
                "answer_usage": {"used_missing_from_projection": [{"entry_ref": "memory#1"}], "used_selected_count": 0},
            })
        _write_shadow_jsonl(self.home, rows)
        m = self.build()
        fb = m["lane_feedback"]["lanes"]
        self.assertEqual(fb["memory-entry"]["health"], "ok")
        self.assertGreater(fb["memory-entry"]["score_adjustment"], 0)
        self.assertEqual(fb["session-semantic"]["health"], "warn")
        self.assertLess(fb["session-semantic"]["score_adjustment"], 0)
        r = MSM.rank_lanes(m, "qqzz wwxx vvyy", home=self.home)
        mem = next(l for l in r["lanes"] if l["id"] == "memory-entry")
        self.assertTrue(any("feedback" in x for x in mem["reasons"]))

    def test_single_poisoned_or_unattributed_event_does_not_move_ranking(self):
        _write_shadow_jsonl(self.home, [
            {
                "tool": "memory_shadow", "mode": "shadow", "turn_id": "legacy",
                "generated_at": "2026-06-25T09:00:00+00:00",
                "projected": {
                    "relevance_source": "memories-index:20 hits via daemon",
                    "retrieval_telemetry": {"path": "daemon"},
                },
                "answer_usage": {"used_missing_from_projection": []},
            },
            {
                "tool": "memory_shadow", "mode": "shadow", "turn_id": "poison",
                "generated_at": "2026-06-25T10:00:00+00:00",
                "projected": {
                    "route_packet": {"recommended_lane": "memory-entry"},
                    "retrieval_telemetry": {"path": "daemon"},
                },
                "answer_usage": {"used_missing_from_projection": [{"entry_ref": "memory#1"}]},
            },
        ])
        m = self.build()
        self.assertEqual(m["lane_feedback"]["unattributed_events"], 1)
        fb = m["lane_feedback"]["lanes"]["memory-entry"]
        self.assertEqual(fb["health"], "insufficient")
        self.assertEqual(fb["score_adjustment"], 0.0)
        r = MSM.rank_lanes(m, "qqzz wwxx vvyy", home=self.home)
        self.assertFalse(r["matched"])


class TestDoctor(SearchMapBase):
    def test_pass_when_all_present(self):
        m = self.build(semantic_health=SEMANTIC_UP)
        rep = MSM.doctor(m)
        self.assertEqual(rep["status"], "PASS", rep)
        self.assertEqual(rep["failures"], [])

    def test_warn_when_semantic_daemon_down(self):
        # honest degradation: a down daemon is a WARN, not a silent pass
        m = self.build()  # no injected daemon
        rep = MSM.doctor(m)
        self.assertEqual(rep["status"], "WARN")
        self.assertTrue(any("semantic daemon" in w for w in rep["warnings"]))

    def test_fail_when_memory_missing(self):
        home = build_home(os.path.join(self.td.name, "nomem"), with_memory=False)
        import datetime as dt
        m = MSM.build_map(home, today=dt.date.fromisoformat(TODAY), ping_timeout=0.2)
        rep = MSM.doctor(m)
        self.assertEqual(rep["status"], "FAIL")
        self.assertTrue(any("MEMORY.md" in f for f in rep["failures"]))

    def test_warn_when_optional_missing(self):
        home = build_home(os.path.join(self.td.name, "nonotes"), with_notes=False, with_spine=False)
        import datetime as dt
        m = MSM.build_map(home, today=dt.date.fromisoformat(TODAY), ping_timeout=0.2)
        rep = MSM.doctor(m)
        self.assertEqual(rep["status"], "WARN")
        self.assertTrue(any("notes_index" in w for w in rep["warnings"]))


class TestReadOnlySafety(SearchMapBase):
    def test_temporal_db_not_mutated_and_no_sidecars(self):
        db = os.path.join(self.home, "memory_versions.db")
        before = (os.path.getsize(db), os.path.getmtime(db))
        self.build()
        after = (os.path.getsize(db), os.path.getmtime(db))
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(db + "-wal"))
        self.assertFalse(os.path.exists(db + "-shm"))

    def test_empty_home_does_not_crash(self):
        empty = os.path.join(self.td.name, "empty", ".hermes")
        os.makedirs(empty, exist_ok=True)
        import datetime as dt
        m = MSM.build_map(empty, today=dt.date.fromisoformat(TODAY), ping_timeout=0.2)
        self.assertEqual(m["stores"]["hot_memory"]["status"], "missing")
        rep = MSM.doctor(m)
        self.assertEqual(rep["status"], "FAIL")
        # query must still produce a route even with no stores
        r = MSM.rank_lanes(m, "anything at all", home=empty)
        self.assertIsNotNone(r["recommended_lane"])


class TestRedactor(unittest.TestCase):
    def test_redacts_common_token_shapes(self):
        for raw in (FAKE_SECRET, "ghp_" + "B" * 36, "AKIA" + "C" * 16,
                    "password: hunter2supersecret", "A" * 50):
            self.assertIn("[REDACTED]", MSM.redact(f"prefix {raw} suffix"))

    def test_preserves_short_content_hash(self):
        # 16-hex content hashes must survive (they are identifiers, not secrets)
        self.assertNotIn("[REDACTED]", MSM.redact("hash abcdef0123456789 here"))


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.home = build_home(self.td.name)

    def run_cli(self, *args):
        return subprocess.run(["python3", SCRIPT, *args, "--today", TODAY, "--ping-timeout", "0.2"],
                              capture_output=True, text=True, timeout=60)

    def test_build_json(self):
        r = self.run_cli("build", "--home", self.home, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["tool"], "memory_search_map")
        self.assertIn("stores", data)

    def test_build_markdown_runs(self):
        r = self.run_cli("build", "--home", self.home, "--markdown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("# Memory Search Map", r.stdout)

    def test_query_json(self):
        r = self.run_cli("query", "--home", self.home, "--query", "provider failover history", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["mode"], "query")
        self.assertTrue(data["lanes"])

    def test_doctor_json_exit_zero_when_not_failing(self):
        # No real daemon in tests, so status is PASS or WARN; either exits 0.
        r = self.run_cli("doctor", "--home", self.home, "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(json.loads(r.stdout)["status"], ("PASS", "WARN"))

    def test_doctor_fail_exit_one(self):
        home = build_home(os.path.join(self.td.name, "x"), with_memory=False)
        r = self.run_cli("doctor", "--home", home, "--json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["status"], "FAIL")

    def test_out_file_written(self):
        out = os.path.join(self.td.name, "map.json")
        r = self.run_cli("build", "--home", self.home, "--json", "--out", out)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as fh:
            json.load(fh)


if __name__ == "__main__":
    unittest.main()
