"""
hdl_query tools — scoped read-only access to the Hermes Document Library.

Catalogue: ~/harvest7/docs-library/_index/catalogue.jsonl (one JSON object per
line; latest event per path wins; retirement events hide the doc).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_HDL_ROOT = Path.home() / "harvest7" / "docs-library"
_CATALOGUE = _HDL_ROOT / "_index" / "catalogue.jsonl"
_MAX_READ = 400_000
_SKIP_EVENTS = {"duplicate_retired", "retired", "retire"}


def _load_catalogue() -> Dict[str, dict]:
    """Latest event per path; retirement events drop the doc."""
    latest: Dict[str, tuple] = {}
    with open(_CATALOGUE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = rec.get("path")
            if not path:
                continue
            ev = (rec.get("event") or "").lower()
            ts = rec.get("ts") or ""
            prev = latest.get(path)
            if prev is None or ts >= prev[0]:
                latest[path] = (ts, rec, ev)
    return {p: rec for p, (ts, rec, ev) in latest.items() if ev not in _SKIP_EVENTS}


def _resolve_safe(rel: str) -> Path:
    if not rel or not isinstance(rel, str):
        raise ValueError("path must be a non-empty relative path")
    p = Path(rel)
    if p.is_absolute():
        raise ValueError("path must be RELATIVE to the HDL library root")
    resolved = (_HDL_ROOT / p).resolve()
    root = _HDL_ROOT.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("path escapes or equals the HDL library root — refused")
    return resolved


def _fmt(rec: dict) -> str:
    return (
        f"{rec.get('path')}\n"
        f"    {rec.get('doc_type','?')}/{rec.get('status','?')} · {rec.get('project','—')} · "
        f"by {rec.get('agent','?')} · {(rec.get('ts') or '')[:10]} · doc_id {rec.get('doc_id','—')}"
    )


def hdl_search(args: dict, **_: Any) -> str:
    try:
        q = args.get("q")
        if not q or not isinstance(q, str):
            return "ERROR: 'q' (keyword string) is required."
        limit = min(int(args.get("limit") or 10), 25)
        zone = args.get("zone")
        tokens = [t.lower() for t in q.split() if t]
        if not tokens:
            return "ERROR: 'q' needs at least one keyword."
        docs = _load_catalogue()
        scored = []
        for path, rec in docs.items():
            if zone and not path.lower().startswith(str(zone).lower().strip("/")):
                continue
            hay = " ".join(
                str(rec.get(k, "") or "") for k in ("title", "path", "project", "agent", "doc_type", "status")
            ).lower()
            hits = sum(1 for t in tokens if t in hay)
            if hits == len(tokens):
                scored.append((hits, path, rec))
        scored.sort(key=lambda x: x[2].get("ts") or "", reverse=True)
        if not scored:
            return (
                f"0 catalogue matches for {q!r}"
                + (f" under {zone}/" if zone else "")
                + f" ({len(docs)} live docs indexed). Try fewer/broader keywords, or hdl_list a zone."
            )
        lines = [f"{len(scored)} match(es) for {q!r}" + (f" under {zone}/" if zone else "") + " (newest first):"]
        for _, path, rec in scored[:limit]:
            lines.append(_fmt(rec))
        if len(scored) > limit:
            lines.append(f"  … and {len(scored) - limit} more — narrow with more keywords or a zone.")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("hdl_search failed")
        return f"ERROR: {e}"


def hdl_list(args: dict, **_: Any) -> str:
    try:
        zone = (args.get("zone") or "").strip("/")
        docs = _load_catalogue()
        if zone:
            docs = {p: r for p, r in docs.items() if p.lower().startswith(zone.lower() + "/") or p.lower() == zone.lower()}
        if not docs:
            zones = sorted({p.split("/")[0] for p in _load_catalogue()})
            return f"No catalogue entries under {zone!r}. Top-level zones: {', '.join(zones)}"
        lines = [f"{len(docs)} doc(s) under {zone or '/'} (newest first):"]
        for path, rec in sorted(docs.items(), key=lambda kv: kv[1].get("ts") or "", reverse=True)[:50]:
            lines.append(_fmt(rec))
        if len(docs) > 50:
            lines.append(f"  … and {len(docs) - 50} more — narrow the zone.")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("hdl_list failed")
        return f"ERROR: {e}"


def hdl_read(args: dict, **_: Any) -> str:
    try:
        path = args.get("path")
        if not path or not isinstance(path, str):
            return "ERROR: 'path' (relative string) is required."
        resolved = _resolve_safe(path)
        if not resolved.exists():
            return f"ERROR: not found in the library: {path} (use hdl_search to find exact paths)."
        if resolved.is_dir():
            return f"ERROR: {path} is a folder — use hdl_list."
        data = resolved.read_bytes()
        text = data.decode("utf-8", "replace")
        if len(text) <= _MAX_READ:
            return f"# {resolved.name} ({len(data)} bytes)\n\n{text}"
        head = text[: _MAX_READ // 2]
        tail = text[-(_MAX_READ // 2):]
        return (
            f"# {resolved.name} ({len(data)} bytes — TRUNCATED, showing head+tail)\n\n"
            f"{head}\n\n[… {len(text) - _MAX_READ} chars omitted …]\n\n{tail}"
        )
    except ValueError as e:
        return f"REFUSED: {e}"
    except Exception as e:
        logger.exception("hdl_read failed")
        return f"ERROR: {e}"


_SCHEMAS: Dict[str, dict] = {
    "hdl_search": {
        "type": "function",
        "function": {
            "name": "hdl_search",
            "description": (
                "Keyword-search the Hermes Document Library catalogue (title, path, project, "
                "agent, doc_type, status). All keywords must match (AND). Returns exact paths "
                "suitable for hdl_read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Keywords, e.g. 'plant review' or 'VLCC thesis'."},
                    "zone": {"type": "string", "description": "Optional zone prefix filter, e.g. 'efforts' or 'ops/iab'."},
                    "limit": {"type": "integer", "description": "Max results (1-25, default 10)."},
                },
                "required": ["q"],
            },
        },
    },
    "hdl_list": {
        "type": "function",
        "function": {
            "name": "hdl_list",
            "description": "List catalogue entries under an HDL zone/folder (e.g. 'efforts', 'ops', 'research'). Omit zone for a zone overview hint.",
            "parameters": {
                "type": "object",
                "properties": {"zone": {"type": "string", "description": "Folder prefix, e.g. 'ops/iab'."}},
            },
        },
    },
    "hdl_read": {
        "type": "function",
        "function": {
            "name": "hdl_read",
            "description": (
                "Read one document from the Hermes Document Library (read-only). Path is "
                "relative to the library root, exactly as returned by hdl_search/hdl_list. "
                "Large docs are returned as a head+tail window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path, e.g. 'ops/iab/2026-10-15 — HDL L-Series….md'."},
                },
                "required": ["path"],
            },
        },
    },
}

_HANDLERS: Dict[str, Callable[[dict], str]] = {
    "hdl_search": hdl_search,
    "hdl_list": hdl_list,
    "hdl_read": hdl_read,
}


def register_tools(ctx) -> None:
    """Register the hdl_* tools in the ``hdl_query`` toolset."""
    emoji = "\U0001f4da"  # books
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="hdl_query",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
