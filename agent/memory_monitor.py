"""Memory assembly monitor (P2 WS-2): alert rules over the manifest.

Reads ``logs/memory_manifest.jsonl`` and evaluates:
- R1: five consecutive turns with total lookup failure (all lookups
  timeout/error) → alert.
- R2: rolling 100-turn window with timeout+error rate above 25% of turns → alert.

Sub-threshold events are manifest-only (no alert). The monitor reads the
manifest and nothing else; it writes its own decision log outside the engine's
write path. Alert payloads carry counts and status enums — never store
internals, endpoints, or error strings (spec §1.3).

Runs as an hourly cron read (A5) — no daemon, no event hook.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

R1_CONSECUTIVE = 5
R2_WINDOW = 100
R2_THRESHOLD = 0.25

# Fixed alert templates — counts and enums only, never composed from store data.
R1_TEMPLATE = (
    "MEMORY MONITOR R1: {n} consecutive turns with total lookup failure "
    "(manifest: {path})."
)
R2_TEMPLATE = (
    "MEMORY MONITOR R2: lookup failure rate {rate:.0%} over last {window} turns "
    "(threshold 25%; manifest: {path})."
)


def _turn_failed(entry: dict[str, Any]) -> bool:
    """A turn 'failed' when it had lookups and every lookup was timeout/error."""
    statuses = entry.get("lookup_status") or []
    if not statuses:
        return False
    return all(s.get("status") in ("timeout", "error") for s in statuses)


def evaluate_rules(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate R1/R2 over manifest entries (oldest first). Returns alerts."""
    alerts: list[dict[str, Any]] = []
    failed = [_turn_failed(e) for e in entries]
    # R1: longest consecutive failure run at the tail.
    run = 0
    for f in reversed(failed):
        if f:
            run += 1
        else:
            break
    if run >= R1_CONSECUTIVE:
        alerts.append({
            "rule": "R1", "consecutive_failures": run,
            "message": R1_TEMPLATE.format(n=run, path="memory_manifest.jsonl"),
        })
    # R2: rolling window failure rate (only if enough turns to be meaningful).
    window = failed[-R2_WINDOW:]
    if len(window) >= 10:  # below 10 turns the rate is noise
        rate = sum(window) / len(window)
        if rate > R2_THRESHOLD:
            alerts.append({
                "rule": "R2", "window": len(window), "rate": rate,
                "message": R2_TEMPLATE.format(rate=rate, window=len(window),
                                              path="memory_manifest.jsonl"),
            })
    return alerts


def read_manifest(path: Path, max_entries: int = 5000) -> list[dict[str, Any]]:
    """Read the manifest tail (oldest-first). Unparseable lines are skipped."""
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-max_entries:]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def run_monitor(
    manifest_path: Path,
    decision_log_path: Path,
    alert_fn=None,
) -> list[dict[str, Any]]:
    """One monitor pass: read manifest, evaluate rules, log decisions, alert.

    ``alert_fn`` receives the alert dicts (wired to the Telegram Notifications
    channel by the cron wrapper). Decision log is append-only JSONL, outside
    the engine's write path. All monitor failures are swallowed — monitoring
    must never break the turn or the cron.
    """
    try:
        entries = read_manifest(manifest_path)
        alerts = evaluate_rules(entries)
        decision = {
            "ts": datetime.now(UTC).isoformat(),
            "entries_read": len(entries),
            "alerts": [a["rule"] for a in alerts],
        }
        try:
            decision_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(decision_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(decision, separators=(",", ":")) + "\n")
        except Exception as exc:
            logger.warning("memory monitor decision log failed (non-fatal): %s", exc)
        for alert in alerts:
            if alert_fn is not None:
                try:
                    alert_fn(alert["message"])
                except Exception as exc:
                    logger.warning("memory monitor alert delivery failed: %s", exc)
        return alerts
    except Exception as exc:
        logger.warning("memory monitor pass failed (non-fatal): %s", exc)
        return []
