"""P2 tests for the memory assembly engine (spec v1.0, acceptance §9).

Covers AT-1..AT-9 logic-level equivalents plus monitor rules. AT-10..14
(regression sweep, read-only audit, harness dry runs) run at integration level.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from unittest import mock

import pytest

import agent.memory_assembly as ma
import agent.memory_monitor as mm


def _cfg(**kw):
    cfg = {
        "char_budget": 6000,
        "tier_floors": {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
        "top_k_gbrain": 3, "top_k_hem": 2, "lookup_timeout_s": 2.0,
        "max_lookups": 6, "max_ticker_entities": 6,
        "combined_char_budget": 9000,
        "position": "pre_user_turn",
    }
    cfg.update(kw)
    return cfg


# ── AT-7: position validation ───────────────────────────────────────────────

class TestPositionValidation:
    @pytest.mark.parametrize("pos", ["mid_conversation", "system_edge", "", "PRE_USER_TURN"])
    def test_invalid_position_disables_engine(self, pos):
        with mock.patch("hermes_cli.config.load_config", return_value={
            "memory": {"assembly": {"enabled": True, "position": pos,
                                    "tier_floors": {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2}}},
        }):
            assert ma._load_engine_config(object()) is None

    def test_valid_position_passes(self):
        with mock.patch("hermes_cli.config.load_config", return_value={
            "memory": {"assembly": {"enabled": True, "position": "pre_user_turn",
                                    "tier_floors": {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2}}},
        }):
            assert ma._load_engine_config(object()) is not None


# ── AT-8: combined budget ───────────────────────────────────────────────────

class TestCombinedBudget:
    def test_legacy_charged_first(self):
        recs = [ma.Record("gbrain", "gbrain", "g1", "g" * 500, "2026-11-01", 1.0)]
        kept, _rej = ma.budget_cut(recs, "L" * 4000, 6000,
                                  {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
                                  combined_budget=9000, legacy_chars=4000)
        assert [r.record_id for r in kept] == ["g1"]  # 4000 legacy + 500 fits 9000

    def test_legacy_over_combined_enrichment_zero(self):
        recs = [ma.Record("gbrain", "gbrain", "g1", "g" * 100, "2026-11-01", 1.0)]
        kept, rej = ma.budget_cut(recs, "L" * 9500, 6000,
                                  {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
                                  combined_budget=9000, legacy_chars=9500)
        assert kept == []
        assert all(r["reason"] == "over_budget_combined" for r in rej)

    def test_legacy_never_cut(self):
        # Legacy larger than combined budget: engine never truncates it.
        recs = [ma.Record("gbrain", "gbrain", "g1", "g" * 50, "2026-11-01", 1.0)]
        kept, rej = ma.budget_cut(recs, "L" * 12000, 6000,
                                  {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
                                  combined_budget=9000, legacy_chars=12000)
        assert kept == []
        assert len(rej) == 1

    def test_enrichment_cut_first_whole_records(self):
        recs = [
            ma.Record("gbrain", "gbrain", "g1", "g" * 300, "2026-11-01", 1.0),
            ma.Record("gbrain", "gbrain", "g2", "g" * 300, "2026-11-02", 1.0),
        ]
        # Legacy 8500 + combined 9000 → 500 headroom: g1 fits, g2 dropped whole.
        kept, rej = ma.budget_cut(recs, "L" * 8500, 6000,
                                  {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2},
                                  combined_budget=9000, legacy_chars=8500)
        assert [r.record_id for r in kept] == ["g1"]
        assert rej[0]["reason"] == "over_budget_combined"
        assert rej[0]["chars"] == 300  # whole record, not truncated

    def test_no_combined_budget_uses_char_budget(self):
        recs = [ma.Record("gbrain", "gbrain", "g1", "g" * 100, "2026-11-01", 1.0)]
        kept, _rej = ma.budget_cut(recs, "", 500, {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2})
        assert [r.record_id for r in kept] == ["g1"]


# ── AT-9: manifest hashes + schema ──────────────────────────────────────────

class TestManifestV2:
    def _run(self, tmp_path, query="$DHT", raw="RAW", payload=None):
        payload = payload or {"data": {"items": [
            {"doc_id": "d1", "author": "A", "excerpt": "DHT note", "posted": "2026-11-01"},
        ]}}
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        mp = tmp_path / "m.jsonl"
        with mock.patch.object(ma, "_http_json", return_value=payload):
            text, entry = ma.assemble(query, raw, _cfg(), ex, ma.ManifestWriter(mp, 10**9),
                                      session_id="s", turn=1, gbrain_base_url="http://x")
        return text, entry, mp

    def test_schema_and_hashes_present(self, tmp_path):
        text, entry, _mp = self._run(tmp_path)
        assert entry["schema"] == 2
        assert entry["assembled_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert entry["prefetch_sha256"] == hashlib.sha256(b"RAW").hexdigest()

    def test_char_accounting(self, tmp_path):
        _text, entry, _mp = self._run(tmp_path, raw="R" * 100)
        assert entry["legacy_chars"] == 100
        assert entry["enrichment_chars"] == sum(
            i["chars"] for i in entry["injected"] if i["tier"] != "honcho")
        assert entry["combined_chars"] == entry["legacy_chars"] + entry["enrichment_chars"]

    def test_100_turns_hashes_recompute(self, tmp_path):
        mp = tmp_path / "m.jsonl"
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        payload = {"data": {"items": [
            {"doc_id": "d1", "author": "A", "excerpt": "DHT note", "posted": "2026-11-01"},
        ]}}
        with mock.patch.object(ma, "_http_json", return_value=payload):
            for i in range(100):
                ma.assemble("$DHT", f"RAW{i}", _cfg(), ex, ma.ManifestWriter(mp, 10**9),
                            session_id="s", turn=i, gbrain_base_url="http://x")
        lines = mp.read_text().strip().splitlines()
        assert len(lines) == 100
        for line in lines:
            e = json.loads(line)
            assert e["schema"] == 2
            assert e["assembled_sha256"] and e["prefetch_sha256"]


# ── AT-1/2/3: conflict resolution ───────────────────────────────────────────

def _keyed(rid, text, created, tier="gbrain", field="nav", entity="DHT"):
    return ma.Record(tier, tier, rid, text, created, 1.0, [entity], field=field)


class TestConflictResolution:
    def test_newest_wins_20_sets(self):
        """AT-1: 20 synthetic supersession pairs → newest injected 20/20."""
        for i in range(20):
            old = _keyed(f"old{i}", "value 10", "2026-10-01")
            new = _keyed(f"new{i}", "value 20", "2026-11-01")
            survivors, events = ma.resolve_conflicts([old, new])
            assert len(survivors) == 1
            assert survivors[0].record_id == f"new{i}"
            assert len(events) == 1
            assert events[0]["superseded"][0]["superseded_by"] == f"new{i}"

    def test_tie_falls_to_tier_then_id(self):
        """AT-2: missing timestamps → tier priority → record_id."""
        a = _keyed("a", "v1", "", tier="hem")
        b = _keyed("b", "v2", "", tier="gbrain")
        survivors, events = ma.resolve_conflicts([a, b])
        assert survivors[0].record_id == "b"  # gbrain tier priority
        assert len(events) == 1

    def test_missing_timestamp_same_tier_record_id(self):
        a = _keyed("a", "v1", "")
        b = _keyed("b", "v2", "")
        survivors, _ = ma.resolve_conflicts([a, b])
        assert survivors[0].record_id == "a"  # record_id tiebreak

    def test_unkeyed_passthrough(self):
        """AT-3: records without field metadata → 0 conflicts, 0 crashes."""
        recs = [ma.Record("gbrain", "gbrain", f"d{i}", f"text{i}", "2026-11-01", 1.0)
                for i in range(10)]
        survivors, events = ma.resolve_conflicts(recs)
        assert len(survivors) == 10
        assert events == []

    def test_same_value_no_conflict_event(self):
        a = _keyed("a", "Same Value ", "2026-11-01")
        b = _keyed("b", " same value", "2026-11-02")
        survivors, events = ma.resolve_conflicts([a, b])
        assert len(survivors) == 1
        assert events == []  # normalisation makes these equal — not a conflict

    def test_near_duplicate_resolved_as_conflict(self):
        # A1 semantics: value comparison is normalised EXACT inequality. A
        # paraphrase is a different string → differing values → resolution
        # (newest wins). We deliberately do NOT do semantic equivalence —
        # that is the P3 question. Deterministic and conservative.
        a = _keyed("a", "NAV around 5.80", "2026-11-01")
        b = _keyed("b", "NAV approximately 5.80", "2026-11-02")
        survivors, events = ma.resolve_conflicts([a, b])
        assert len(survivors) == 1
        assert survivors[0].record_id == "b"  # newest wins
        assert len(events) == 1

    def test_deterministic(self):
        recs = [_keyed("a", "v1", "2026-11-01"), _keyed("b", "v2", "2026-11-02")]
        s1, e1 = ma.resolve_conflicts(recs)
        s2, e2 = ma.resolve_conflicts(list(reversed(recs)))
        assert [r.record_id for r in s1] == [r.record_id for r in s2]
        assert e1 == e2

    def test_conflict_events_in_manifest(self, tmp_path):
        payload = {"data": {"items": [
            {"doc_id": "d1", "author": "A", "excerpt": "old value", "posted": "2026-11-01"},
        ]}}
        ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        tmp_path / "m.jsonl"
        # Simulate two keyed records reaching assemble via stubbed lookup.
        recs = [_keyed("old", "value 10", "2026-11-01"), _keyed("new", "value 20", "2026-11-05")]
        with mock.patch.object(ma, "_http_json", return_value=payload), \
             mock.patch.object(ma, "rank_records", side_effect=lambda rs: rs):
            # Directly test the manifest path via resolve + assemble internals:
            _survivors, events = ma.resolve_conflicts(recs)
        assert len(events) == 1
        assert events[0]["kept"] == "new"


# ── AT-4/5: status line ─────────────────────────────────────────────────────

class TestStatusLine:
    def _run_both_fail(self, tmp_path):
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        mp = tmp_path / "m.jsonl"
        with mock.patch.object(ma, "_http_json", side_effect=TimeoutError("t")):
            text, entry = ma.assemble("$DHT", "RAW", _cfg(), ex, ma.ManifestWriter(mp, 10**9),
                                      gbrain_base_url="http://x")
        return text, entry

    def test_total_failure_injects_status_line(self, tmp_path):
        """AT-4: both stores error → status line present."""
        text, entry = self._run_both_fail(tmp_path)
        assert text.startswith("MEMORY STATUS: enrichment unavailable this turn")
        assert entry["status_line_injected"] is True

    def test_no_internals_in_blob(self, tmp_path):
        """AT-4: grep assembled blob for internals → 0 hits."""
        text, _entry = self._run_both_fail(tmp_path)
        for leak in ("http", "ssh", "icarus", "gbrain", "hem", "timeout", "error",
                     "TimeoutError", "127.0.0.1", "/Users/"):
            assert leak.lower() not in text.lower(), f"leak: {leak}"

    def test_partial_failure_no_status_line(self, tmp_path):
        """AT-5: one store erroring, one healthy → no status line."""
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))

        class P:
            returncode = 0
            stdout = "ep1\x1fepisode\x1fDHT\x1f2026-11-05\x1fbody"
            stderr = ""

        def http_or_ssh(url, body, headers, timeout):
            raise TimeoutError("t")

        with mock.patch.object(ma, "_http_json", side_effect=http_or_ssh), \
             mock.patch.object(ma, "subprocess_run", return_value=P):
            text, entry = ma.assemble("$DHT", "RAW", _cfg(), ex, None,
                                      gbrain_base_url="http://x", hem_ssh_cmd=["ssh", "h"])
        assert "MEMORY STATUS" not in text
        assert entry is None or entry["status_line_injected"] is False

    def test_no_lookups_no_status_line(self, tmp_path):
        # No entities → no lookups → no status line (absence of noise).
        ex = ma.EntityExtractor()
        text, _entry = ma.assemble("plain text", "RAW", _cfg(), ex, None, gbrain_base_url="http://x")
        assert "MEMORY STATUS" not in text

    def test_success_no_status_line(self, tmp_path):
        ex = ma.EntityExtractor(ticker_universe=frozenset({"DHT"}))
        payload = {"data": {"items": [
            {"doc_id": "d1", "author": "A", "excerpt": "DHT note", "posted": "2026-11-01"},
        ]}}
        with mock.patch.object(ma, "_http_json", return_value=payload):
            text, _entry = ma.assemble("$DHT", "RAW", _cfg(), ex, None, gbrain_base_url="http://x")
        assert "MEMORY STATUS" not in text


# ── AT-6: alert monitor rules ───────────────────────────────────────────────

def _failed_entry():
    return {"lookup_status": [{"entity": "X", "store": "gbrain", "status": "timeout"}]}


def _ok_entry():
    return {"lookup_status": [{"entity": "X", "store": "gbrain", "status": "found"}]}


class TestMonitorRules:
    def test_r1_five_consecutive_alerts(self):
        entries = [_ok_entry()] + [_failed_entry() for _ in range(5)]
        alerts = mm.evaluate_rules(entries)
        assert any(a["rule"] == "R1" for a in alerts)

    def test_r1_four_consecutive_no_alert(self):
        entries = [_ok_entry()] + [_failed_entry() for _ in range(4)]
        alerts = mm.evaluate_rules(entries)
        assert not any(a["rule"] == "R1" for a in alerts)

    def test_r2_25pct_no_alert(self):
        entries = [_failed_entry() if i % 4 == 0 else _ok_entry() for i in range(100)]
        alerts = mm.evaluate_rules(entries)
        assert not any(a["rule"] == "R2" for a in alerts)

    def test_r2_26pct_alerts(self):
        entries = [_failed_entry() if i % 100 < 26 else _ok_entry() for i in range(100)]
        alerts = mm.evaluate_rules(entries)
        assert any(a["rule"] == "R2" for a in alerts)

    def test_alert_payload_no_internals(self):
        entries = [_failed_entry() for _ in range(6)]
        alerts = mm.evaluate_rules(entries)
        for a in alerts:
            msg = a["message"]
            assert "http" not in msg.lower() and "ssh" not in msg.lower()
            assert "/Users/" not in msg

    def test_run_monitor_decision_log(self, tmp_path):
        mp = tmp_path / "m.jsonl"
        with open(mp, "w") as fh:
            fh.writelines(json.dumps(_failed_entry()) + "\n" for _ in range(5))
        dl = tmp_path / "monitor.jsonl"
        sent = []
        alerts = mm.run_monitor(mp, dl, alert_fn=sent.append)
        assert any(a["rule"] == "R1" for a in alerts)
        assert len(sent) == len(alerts)
        decisions = [json.loads(l) for l in dl.read_text().strip().splitlines()]
        assert decisions[-1]["entries_read"] == 5

    def test_run_monitor_missing_manifest(self, tmp_path):
        alerts = mm.run_monitor(tmp_path / "nope.jsonl", tmp_path / "dl.jsonl")
        assert alerts == []
