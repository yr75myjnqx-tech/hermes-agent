"""Tests for the memory assembly engine (agent/memory_assembly.py).

Spec v1.1 acceptance checks C1-C5, C7-C8. Deliberately avoids the
activate_dependencies path (environmental tripwire clash, see As-Built doc).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

import agent.memory_assembly as ma


# ── Entity extraction (C1) ──────────────────────────────────────────────────

class TestEntityExtraction:
    def _ex(self, **kw):
        defaults = dict(
            ticker_universe=frozenset({"DHT", "TRMD", "GOGL"}),
            analyst_names=frozenset({"Boyer"}),
            sector_terms=frozenset({"tankers"}),
        )
        defaults.update(kw)
        return ma.EntityExtractor(**defaults)

    def test_dollar_ticker_always_extracted(self):
        e = self._ex().extract("$DHT NAV update")
        assert e.tickers == ["DHT"]

    def test_bare_caps_only_from_universe(self):
        e = self._ex().extract("DHT and TRMD rates")
        assert set(e.tickers) == {"DHT", "TRMD"}

    def test_bare_caps_not_in_universe_ignored(self):
        e = self._ex().extract("ZZZX some random caps")
        assert e.tickers == []

    def test_acronym_denylist(self):
        e = self._ex().extract("US GDP and NAV and AI impact on LNG")
        assert e.tickers == []

    def test_analyst_name_case_insensitive(self):
        e = self._ex().extract("boyer thinks rates firm")
        assert e.analysts == ["Boyer"]

    def test_sector_term(self):
        e = self._ex().extract("tankers market outlook")
        assert e.sectors == ["tankers"]

    def test_date_extraction(self):
        e = self._ex().extract("as of 2026-11-05 the fleet grew")
        assert e.dates == ["2026-11-05"]

    def test_max_tickers_cap(self):
        e = self._ex(max_tickers=2).extract("$A $B $C $D")
        assert len(e.tickers) == 2

    def test_empty_query(self):
        e = self._ex().extract("")
        assert e.is_empty()


# ── Ranking (C2) ────────────────────────────────────────────────────────────

class TestRanking:
    def test_entity_match_dominates(self):
        recs = [
            ma.Record("gbrain", "gbrain", "lex", "lexical only", "2026-11-09", 0.3),
            ma.Record("hem", "hem", "ent", "entity hit", "2026-11-01", 1.0),
        ]
        ranked = ma.rank_records(recs)
        assert ranked[0].record_id == "ent"

    def test_tier_priority_breaks_ties(self):
        recs = [
            ma.Record("honcho", "honcho", "h", "x", "2026-11-09", 1.0),
            ma.Record("gbrain", "gbrain", "g", "y", "2026-11-09", 1.0),
        ]
        ranked = ma.rank_records(recs)
        assert ranked[0].tier == "gbrain"

    def test_recency_breaks_ties_within_tier(self):
        recs = [
            ma.Record("gbrain", "gbrain", "old", "x", "2026-10-01", 1.0),
            ma.Record("gbrain", "gbrain", "new", "y", "2026-11-09", 1.0),
        ]
        ranked = ma.rank_records(recs)
        assert ranked[0].record_id == "new"

    def test_dedupe_aggregates_entities(self):
        recs = [
            ma.Record("gbrain", "gbrain", "d1", "text", "2026-11-01", 1.0, ["DHT"]),
            ma.Record("gbrain", "gbrain", "d1", "text", "2026-11-01", 1.0, ["TRMD"]),
        ]
        ranked = ma.rank_records(recs)
        assert len(ranked) == 1
        assert set(ranked[0].matched_entities) == {"DHT", "TRMD"}

    def test_deterministic(self):
        recs = [
            ma.Record("gbrain", "gbrain", f"d{i}", "t" * i, f"2026-11-0{(i % 9) + 1}", 0.3)
            for i in range(10)
        ]
        r1 = [r.record_id for r in ma.rank_records(recs)]
        r2 = [r.record_id for r in ma.rank_records(list(reversed(recs)))]
        assert r1 == r2


# ── Budget cut (C3) ─────────────────────────────────────────────────────────

class TestBudgetCut:
    def test_floors_guarantee_representation(self):
        recs = [
            ma.Record("gbrain", "gbrain", "g1", "g" * 400, "2026-11-01", 1.0),
            ma.Record("hem", "hem", "h1", "h" * 400, "2026-11-01", 1.0),
        ]
        kept, _rejected = ma.budget_cut(recs, "H" * 100, 1000, {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2})
        tiers = {r.tier for r in kept}
        assert tiers == {"gbrain", "hem", "honcho"}

    def test_over_budget_dropped_not_truncated(self):
        recs = [ma.Record("gbrain", "gbrain", "big", "x" * 900, "2026-11-01", 1.0)]
        kept, rejected = ma.budget_cut(recs, "", 500, {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2})
        assert kept == []
        assert rejected and rejected[0]["reason"] == "over_budget_rank"

    def test_rank_fills_remaining(self):
        recs = [
            ma.Record("gbrain", "gbrain", "g1", "g" * 100, "2026-11-01", 1.0),
            ma.Record("gbrain", "gbrain", "g2", "g" * 100, "2026-11-02", 1.0),
        ]
        kept, _rejected = ma.budget_cut(recs, "", 250, {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2})
        assert [r.record_id for r in kept] == ["g1", "g2"]  # both fit: 100+100 <= 250

    def test_kept_preserves_rank_order(self):
        recs = [
            ma.Record("gbrain", "gbrain", "g1", "g" * 100, "2026-11-01", 1.0),
            ma.Record("hem", "hem", "h1", "h" * 100, "2026-11-01", 1.0),
        ]
        kept, _ = ma.budget_cut(recs, "H" * 50, 1000, {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2})
        # Honcho passthrough participates as a pseudo-record (spec §3[B]).
        assert [r.record_id for r in kept] == ["g1", "h1", "honcho:prefetch"]


# ── Manifest (C4) ───────────────────────────────────────────────────────────

class TestManifest:
    def test_write_and_shape(self, tmp_path):
        mp = tmp_path / "m.jsonl"
        mw = ma.ManifestWriter(mp, 10**9)
        mw.write({"ts": "t", "injected": [], "rejected": []})
        entry = json.loads(mp.read_text().strip())
        assert "ts" in entry and "injected" in entry and "rejected" in entry

    def test_rotation(self, tmp_path):
        mp = tmp_path / "m.jsonl"
        mw = ma.ManifestWriter(mp, 200)
        for i in range(5):
            mw.write({"i": i, "pad": "x" * 80})
        rotated = list(tmp_path.glob("m.jsonl.*"))
        assert len(rotated) == 1
        assert mp.exists()

    def test_fail_open_on_unwritable(self):
        mw = ma.ManifestWriter(Path("/proc/nope/x.jsonl"), 1000)
        mw.write({"a": 1})  # must not raise


# ── Engine end-to-end (C5) ──────────────────────────────────────────────────

class TestAssemble:
    def _cfg(self, **kw):
        cfg = {
            "char_budget": 6000, "tier_floors": {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
            "top_k_gbrain": 3, "top_k_hem": 2, "lookup_timeout_s": 2.0, "max_lookups": 6,
            "max_ticker_entities": 6,
        }
        cfg.update(kw)
        return cfg

    def test_full_flow_with_stubbed_gbrain(self, tmp_path):
        payload = {"data": {"items": [
            {"doc_id": "d1", "author": "Boyer", "excerpt": "DHT NAV upside", "posted": "2026-11-01"},
        ]}}
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        mp = tmp_path / "m.jsonl"
        with mock.patch.object(ma, "_http_json", return_value=payload):
            text, entry = ma.assemble(
                "$DHT NAV", "HONCHO RAW", self._cfg(), ex, ma.ManifestWriter(mp, 10**9),
                session_id="s", turn=1, gbrain_base_url="http://x",
            )
        assert "[gbrain]" in text and "HONCHO RAW" in text
        assert entry["query_entities"]["tickers"] == ["DHT"]
        assert entry["injected"][0]["record_id"] == "d1"

    def test_unreachable_store_fail_open(self, tmp_path):
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        text, entry = ma.assemble(
            "$DHT NAV", "RAW", self._cfg(), ex, None,
            gbrain_base_url="http://127.0.0.1:1",  # nothing listens
        )
        assert "RAW" in text
        assert entry is None

    def test_no_entities_means_passthrough(self, tmp_path):
        ex = ma.EntityExtractor()
        mp = tmp_path / "m.jsonl"
        text, entry = ma.assemble(
            "nothing extractable here", "RAW", self._cfg(), ex, ma.ManifestWriter(mp, 10**9),
            gbrain_base_url="http://x",
        )
        assert text == "RAW"
        # No structured items; the honcho passthrough pseudo-record is the only entry.
        assert [i["tier"] for i in entry["injected"]] == ["honcho"]

    def test_global_deadline_honoured(self, tmp_path):
        def slow_http(*a, **kw):
            time.sleep(5)
            return {"data": {"items": []}}
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        t0 = time.monotonic()
        with mock.patch.object(ma, "_http_json", side_effect=slow_http):
            text, _ = ma.assemble(
                "$DHT", "RAW", self._cfg(lookup_timeout_s=0.5), ex, None,
                gbrain_base_url="http://x",
            )
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert "RAW" in text

    def test_hem_via_stubbed_ssh(self, tmp_path):
        class P:
            returncode = 0
            stdout = "ep1\x1fepisode\x1fDHT trim\x1f2026-11-05\x1fbody"
            stderr = ""
        with mock.patch.object(ma, "subprocess_run", return_value=P):
            recs, sts = ma._hem_search(["ssh", "icarus"], ["DHT"], 2, time.monotonic() + 5)
        assert recs[0].record_id == "ep1"
        assert sts[0].status == "found"


# ── Config gate + seam (C7/C8) ──────────────────────────────────────────────

class TestGate:
    def test_flag_off_passthrough(self):
        with mock.patch.object(ma, "_load_engine_config", return_value=None):
            assert ma.maybe_assemble_memory(object(), "q", "RAW") == "RAW"

    def test_empty_prefetch_short_circuit(self):
        with mock.patch.object(ma, "_load_engine_config", side_effect=AssertionError("must not load")):
            assert ma.maybe_assemble_memory(object(), "q", "") == ""

    def test_bad_floors_disable_engine(self):
        with mock.patch("hermes_cli.config.load_config", return_value={
            "memory": {"assembly": {"enabled": True, "tier_floors": {"gbrain": 0.9}}},
        }):
            assert ma._load_engine_config(object()) is None

    def test_flag_on_unreachable_gbrain_keeps_raw(self, tmp_path):
        uni = tmp_path / "uni.csv"
        uni.write_text("ticker\nDHT\n")
        cfg = {
            "enabled": True, "char_budget": 6000,
            "tier_floors": {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
            "ticker_universe_path": str(uni),
        }
        with mock.patch.object(ma, "_load_engine_config", return_value=cfg):
            out = ma.maybe_assemble_memory(object(), "$DHT", "RAW BLOB")
        assert "RAW BLOB" in out

    def test_extractor_cache(self, tmp_path):
        uni = tmp_path / "uni.csv"
        uni.write_text("ticker\nDHT\n")
        cfg = {"ticker_universe_path": str(uni), "max_ticker_entities": 6}
        ma._EXTRACTOR_CACHE.clear()
        e1 = ma._get_extractor(cfg)
        e2 = ma._get_extractor(cfg)
        assert e1 is e2
        assert "DHT" in e1._universe
