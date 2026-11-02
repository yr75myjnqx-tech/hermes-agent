"""
cascade_query tools — let Quark read the Cascade v2 engine output.

Read-only SELECTs against the harvest7 Postgres cascade tables. Stdlib +
psycopg2 only. No writes, no DDL, no shell, no file access.

The engine's contract ends at a well-scoped, signed, evidence-tagged
candidate list. Quark makes the final call (which channel dominates,
actual stock picks, deep-dive calls, contradiction adjudication).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict

import psycopg2
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB connection (harvest7 live DB). Read-only SELECTs only.
# ---------------------------------------------------------------------------
_DSN = os.environ.get(
    "CASCADE_DB_DSN",
    "postgresql://minipro@icarus:5432/harvest7_20260810",
)  # 2026-11-03: PG primary moved to icarus (2026-10-03 plant migration);
# MiniPro :5432 is a stale read-locked decoy. Auth via ~/.pgpass (icarus rows).


def _conn():
    """Open a read-only connection to the harvest7 cascade DB."""
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only SELECT and return rows as dicts."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _fmt_float(v, nd=3):
    return round(float(v), nd) if v is not None else None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def cascade_status(args: dict, **_: Any) -> str:
    """Return Cascade v2 engine state: thesis count, scored transmissions,
    stock impacts, mechanisms, and the latest backtest summary."""
    try:
        theses = _rows("SELECT count(*) AS n FROM harvest.macro_theses")[0]["n"]
        tx = _rows("SELECT count(*) AS n FROM harvest.sector_transmissions")[0]["n"]
        si = _rows("SELECT count(*) AS n FROM harvest.stock_impacts")[0]["n"]
        mech = _rows("SELECT count(*) AS n FROM harvest.mechanisms")[0]["n"]
        # Latest backtest summary if present.
        bt = _rows(
            "SELECT evaluated_at, status, count(*) AS n "
            "FROM harvest.cascade_backtest_cases GROUP BY evaluated_at, status "
            "ORDER BY evaluated_at DESC LIMIT 3"
        )
        lines = [
            "Cascade v2 engine status:",
            f"- macro_theses: {theses}",
            f"- sector_transmissions: {tx}",
            f"- stock_impacts: {si}",
            f"- mechanisms: {mech}",
        ]
        if bt:
            lines.append("- backtest cases (latest):")
            for r in bt:
                lines.append(
                    f"    {str(r['evaluated_at'])[:10]} {r['status']}: {r['n']}"
                )
        return "\n".join(lines)
    except Exception as e:  # pragma: no cover
        return f"cascade_status failed: {e}"


def cascade_ranked(args: dict, **_: Any) -> str:
    """Return the ranked, signed, evidence-tagged candidate list for a thesis
    or mechanism. Quark's judgement input — the engine's contract ends here."""
    thesis_id = str(args.get("thesis_id") or "").strip()
    mechanism_id = str(args.get("mechanism_id") or "").strip()
    limit = int(args.get("limit") or 20)
    limit = max(1, min(limit, 50))

    where = []
    params: list = []
    if thesis_id:
        where.append("si.transmission_id IN (SELECT transmission_id FROM harvest.sector_transmissions WHERE thesis_id = %s)")
        params.append(thesis_id)
    if mechanism_id:
        where.append("si.transmission_id IN (SELECT transmission_id FROM harvest.sector_transmissions WHERE mechanism_id = %s)")
        params.append(mechanism_id)
    if not where:
        return "Error: provide 'thesis_id' or 'mechanism_id'."

    sql = f"""
        SELECT si.ticker, si.mechanism_stance, si.signed_exposure,
               si.exposure_source, si.exposure_confidence, si.exposure_evidence,
               si.rank, si.candidate_origin, si.priced_in, si.conviction,
               si.market_cap, si.adv_30d, si.as_of,
               st.channel, st.sector, st.sub_sector, st.direction,
               st.expected_impact, st.priority_score, st.novelty_gate,
               st.contradiction_state, st.beneficiary_type
        FROM harvest.stock_impacts si
        JOIN harvest.sector_transmissions st ON st.transmission_id = si.transmission_id
        WHERE {' AND '.join(where)}
        ORDER BY si.rank NULLS LAST, ABS(si.signed_exposure) DESC
        LIMIT %s
    """
    params.append(limit)
    try:
        rows = _rows(sql, tuple(params))
    except Exception as e:  # pragma: no cover
        return f"cascade_ranked failed: {e}"
    if not rows:
        return "No ranked candidates for that thesis/mechanism."

    lines = [f"Ranked candidates ({len(rows)}):"]
    for r in rows:
        stance = r["mechanism_stance"] or "?"
        signed = _fmt_float(r["signed_exposure"])
        conf = r["exposure_confidence"] or "?"
        src = r["exposure_source"] or "?"
        origin = r["candidate_origin"] or "?"
        tx_dir = r["direction"] or "?"
        exp = _fmt_float(r["expected_impact"])
        prio = _fmt_float(r["priority_score"])
        gate = r["novelty_gate"] or "?"
        contra = r["contradiction_state"] or ""
        contra_s = f" [{contra}]" if contra and contra != "dominant" else ""
        lines.append(
            f"- {r['ticker']} ({r['channel']}/{r['sector']}): stance={stance} "
            f"signed_exposure={signed} conf={conf} src={src} origin={origin} "
            f"tx_dir={tx_dir} expected_impact={exp} priority={prio} gate={gate}{contra_s}"
        )
        ev = r["exposure_evidence"]
        if ev:
            lines.append(f"    evidence: {str(ev)[:200]}")
    return "\n".join(lines)


def cascade_deep_dive(args: dict, **_: Any) -> str:
    """Return the Stage 5 deep-dive entries (top-3 by priority × |signed_exposure|)
    for a thesis or mechanism — exposure evidence, valuation, catalyst, risk,
    falsifiable kill-switch."""
    thesis_id = str(args.get("thesis_id") or "").strip()
    mechanism_id = str(args.get("mechanism_id") or "").strip()
    if not thesis_id and not mechanism_id:
        return "Error: provide 'thesis_id' or 'mechanism_id'."

    where = []
    params: list = []
    if thesis_id:
        where.append("si.transmission_id IN (SELECT transmission_id FROM harvest.sector_transmissions WHERE thesis_id = %s)")
        params.append(thesis_id)
    if mechanism_id:
        where.append("si.transmission_id IN (SELECT transmission_id FROM harvest.sector_transmissions WHERE mechanism_id = %s)")
        params.append(mechanism_id)

    sql = f"""
        SELECT si.ticker, si.mechanism_stance, si.signed_exposure, si.deep_dive,
               si.exposure_evidence, si.exposure_confidence, si.rank,
               st.channel, st.sector, st.direction, st.expected_impact
        FROM harvest.stock_impacts si
        JOIN harvest.sector_transmissions st ON st.transmission_id = si.transmission_id
        WHERE {' AND '.join(where)} AND si.deep_dive IS NOT NULL
        ORDER BY si.rank NULLS LAST
    """
    try:
        rows = _rows(sql, tuple(params))
    except Exception as e:  # pragma: no cover
        return f"cascade_deep_dive failed: {e}"
    if not rows:
        return "No deep-dive entries for that thesis/mechanism."

    lines = [f"Deep-dive entries ({len(rows)}):"]
    for r in rows:
        lines.append(
            f"## {r['ticker']} ({r['channel']}/{r['sector']}) — stance={r['mechanism_stance']} "
            f"signed_exposure={_fmt_float(r['signed_exposure'])}"
        )
        dd = r["deep_dive"]
        if isinstance(dd, dict):
            for k, v in dd.items():
                lines.append(f"  {k}: {str(v)[:300]}")
        elif dd:
            lines.append(f"  {str(dd)[:500]}")
    return "\n".join(lines)


def cascade_contradictions(args: dict, **_: Any) -> str:
    """Return M4 contradiction-netted transmissions — where opposing signs
    within a (mechanism, sector) group have been netted and marked."""
    try:
        rows = _rows(
            "SELECT transmission_id, mechanism_id, channel, sector, sub_sector, "
            "       direction, expected_impact, contradiction_state "
            "FROM harvest.sector_transmissions "
            "WHERE contradiction_state IS NOT NULL AND contradiction_state != 'dominant' "
            "ORDER BY mechanism_id, sector"
        )
    except Exception as e:  # pragma: no cover
        return f"cascade_contradictions failed: {e}"
    if not rows:
        return "No contradicted transmissions."

    lines = [f"Contradicted transmissions ({len(rows)}):"]
    for r in rows:
        lines.append(
            f"- {r['mechanism_id']} / {r['sector']} / {r['sub_sector'] or ''}: "
            f"{r['direction']} expected_impact={_fmt_float(r['expected_impact'])} "
            f"state={r['contradiction_state']}"
        )
    return "\n".join(lines)


def cascade_theses(args: dict, **_: Any) -> str:
    """Return the macro theses currently in the engine (core claim, mechanism,
    direction, p_thesis, thesis_state) so Quark can pick one to judge."""
    limit = int(args.get("limit") or 20)
    limit = max(1, min(limit, 50))
    try:
        rows = _rows(
            "SELECT thesis_id, publication, core_claim, mechanism, mechanism_id, "
            "       direction, confidence, p_thesis, thesis_state, created_at "
            "FROM harvest.macro_theses ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
    except Exception as e:  # pragma: no cover
        return f"cascade_theses failed: {e}"
    if not rows:
        return "No macro theses in the engine."

    lines = [f"Macro theses ({len(rows)}):"]
    for r in rows:
        lines.append(
            f"- [{str(r['created_at'])[:10]}] {r['publication'] or '?'} | "
            f"mech={r['mechanism'] or r['mechanism_id'] or '?'} dir={r['direction'] or '?'} "
            f"p_thesis={_fmt_float(r['p_thesis'])} state={r['thesis_state'] or '?'}"
        )
        cc = r["core_claim"]
        if cc:
            lines.append(f"    claim: {str(cc)[:160]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_SCHEMAS: Dict[str, dict] = {
    "cascade_status": {
        "type": "function",
        "function": {
            "name": "cascade_status",
            "description": "Return Cascade v2 engine state: thesis count, scored transmissions, stock impacts, mechanisms, latest backtest summary.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "cascade_theses": {
        "type": "function",
        "function": {
            "name": "cascade_theses",
            "description": "List the macro theses currently in the Cascade engine (core claim, mechanism, direction, p_thesis, state) so you can pick one to judge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max theses (1-50, default 20)."},
                },
            },
        },
    },
    "cascade_ranked": {
        "type": "function",
        "function": {
            "name": "cascade_ranked",
            "description": (
                "Return the ranked, signed, evidence-tagged candidate list for a thesis or mechanism. "
                "This is the engine's contract output — your judgement input. Each candidate carries "
                "ticker, mechanism_stance, signed_exposure, exposure_source, exposure_confidence, "
                "exposure_evidence, rank, candidate_origin, priced_in, conviction, plus the transmission's "
                "channel/sector/direction/expected_impact/priority/novelty_gate/contradiction_state. "
                "Provide thesis_id or mechanism_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thesis_id": {"type": "string", "description": "The thesis id (e.g. item:...:mech:...)."},
                    "mechanism_id": {"type": "string", "description": "The mechanism id (e.g. permian_produced_water)."},
                    "limit": {"type": "integer", "description": "Max candidates (1-50, default 20)."},
                },
            },
        },
    },
    "cascade_deep_dive": {
        "type": "function",
        "function": {
            "name": "cascade_deep_dive",
            "description": (
                "Return the Stage 5 deep-dive entries (top-3 by priority x |signed_exposure|) for a thesis "
                "or mechanism — exposure evidence, valuation, catalyst, risk, falsifiable kill-switch. "
                "Provide thesis_id or mechanism_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thesis_id": {"type": "string", "description": "The thesis id."},
                    "mechanism_id": {"type": "string", "description": "The mechanism id."},
                },
            },
        },
    },
    "cascade_contradictions": {
        "type": "function",
        "function": {
            "name": "cascade_contradictions",
            "description": "Return M4 contradiction-netted transmissions — where opposing signs within a (mechanism, sector) group have been netted and marked contradicted. These are excluded from ranking and surfaced for your adjudication.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
}

_HANDLERS: Dict[str, Callable[[dict], str]] = {
    "cascade_status": cascade_status,
    "cascade_theses": cascade_theses,
    "cascade_ranked": cascade_ranked,
    "cascade_deep_dive": cascade_deep_dive,
    "cascade_contradictions": cascade_contradictions,
}


def register_tools(ctx) -> None:
    """Register the cascade_* tools in the ``cascade_query`` toolset."""
    emoji = "\U0001f4c8"  # chart increasing
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="cascade_query",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
