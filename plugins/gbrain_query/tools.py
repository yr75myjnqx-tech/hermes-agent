"""
gbrain_query tool — lets Herb read the Harvest-Aegis GBrain knowledge base.

Read-only GET queries against the GBrain v7 agent API on icarus:5052.
Stdlib only (urllib). No writes are ever issued.

2026-11-03: GBrain moved to icarus in the 2026-10-03 plant migration;
127.0.0.1:5052 on MiniPro is dead. Base is env-overridable via GBRAIN_BASE.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GBrain endpoint
# ---------------------------------------------------------------------------
_GBRAIN_BASE = os.environ.get("GBRAIN_BASE", "http://icarus:5052")


# ---------------------------------------------------------------------------
# Core query logic
# ---------------------------------------------------------------------------
def _get(path: str, params: Dict[str, Any] | None = None) -> str:
    url = f"{_GBRAIN_BASE}{path}"
    if params:
        qs = urllib.parse.urlencode(params)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{qs}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            return body
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace") if e.fp else ""
        return f"GBrain HTTP {e.code}: {detail[:400]}"
    except Exception as e:  # pragma: no cover
        return f"GBrain query failed: {e}"


def _clean_summary(raw: str) -> str:
    """Return a compact, provenance-labelled summary of a search response."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(data, dict) or data.get("ok") is not True:
        return raw
    items = (data.get("data") or {}).get("items") or []
    if not items:
        return "GBrain returned 0 results."
    lines = []
    for it in items[:15]:
        src = f"{it.get('platform','?')}/{it.get('author','?')}"
        date = (it.get("published_at") or "")[:10]
        title = it.get("title") or it.get("summary") or "(untitled)"
        did = it.get("doc_id") or "(no-doc-id)"
        # Surface the source URL (when present) so each result carries a
        # clickable source link for verification — the practical value of
        # citation. source_path is a URL for x/substack, None for rocketchat.
        src_url = it.get("source_path") or ""
        ent = it.get("entities") or []
        ent_str = f" entities={','.join(ent)}" if ent else ""
        sent = it.get("sentiment")
        sig = it.get("signal_score")
        meta = ""
        if sent is not None or sig is not None:
            meta = f" sent={sent} sig={sig}"
        line = f"- [{src} {date}] {title}  [doc_id: {did}]{ent_str}{meta}"
        if src_url:
            line += f"  (source: {src_url})"
        lines.append(line)
    lines.append(f"({len(items)} of {data.get('data',{}).get('total',len(items))} total)")
    return "\n".join(lines)


def gbrain_status(args: dict, **_: Any) -> str:
    """Return GBrain index size + per-platform freshness."""
    return _get("/v1/status")


def gbrain_search(args: dict, **_: Any) -> str:
    """Semantic + full-text search across the Harvest-Aegis knowledge base."""
    q = str(args.get("q") or args.get("query") or "").strip()
    if not q:
        return "Error: 'q' (the search query) is required."
    limit = int(args.get("limit") or 5)
    params: Dict[str, Any] = {"q": q, "limit": max(1, min(limit, 20))}
    for k in ("platform", "since", "until", "entities", "author", "publication",
              "channel", "doc_type", "sentiment_min", "sentiment_max", "signal_score_min"):
        v = args.get(k)
        if v:
            params[k] = v
    raw = _get("/v1/search", params)
    return _clean_summary(raw)


def gbrain_recall(args: dict, **_: Any) -> str:
    """Fetch one full document from GBrain by its doc_id."""
    doc_id = str(args.get("doc_id") or args.get("id") or "").strip()
    if not doc_id:
        return "Error: 'doc_id' is required."
    return _get(f"/v1/recall?doc_id={urllib.parse.quote(doc_id)}")


def gbrain_facets(args: dict, **_: Any) -> str:
    """Distinct values + counts for a metadata field (author, platform, publication)."""
    field = str(args.get("field") or args.get("f") or "").strip()
    if not field:
        return "Error: 'field' is required (e.g. author, platform, publication)."
    return _get(f"/v1/facets?field={urllib.parse.quote(field)}")


def gbrain_stock_sentiment(args: dict, **_: Any) -> str:
    """Per-stock bullish/bearish/neutral claim counts across the GBrain corpus.

    Groups harvest.claims by subject_entity (the stock) and direction, joined onto
    gbrain.documents so the same metadata filters as gbrain_search apply. Returns
    each stock's bullish/bearish/neutral counts plus a net score, so 'which stocks
    have the most bullish/bearish outlook' is answerable in one call.
    """
    params: Dict[str, Any] = {}
    for k in ("entities", "author", "publication", "platform", "since", "until",
              "direction", "min_mentions", "limit", "sort"):
        v = args.get(k)
        if v is not None and str(v).strip() != "":
            params[k] = v
    if not params:
        return "Error: provide at least one filter (e.g. entities, author, since, direction)."
    raw = _get("/v1/stock_sentiment", params)
    # Pretty-print the JSON for readability.
    try:
        data = json.loads(raw)
        if data.get("ok") is not True:
            return raw
        stocks = (data.get("data") or {}).get("stocks") or []
        if not stocks:
            return "No stock sentiment data matched the filters."
        lines = [f"Per-stock sentiment ({len(stocks)} shown):"]
        for s in stocks:
            lines.append(
                f"- {s['stock']}: bull={s['bullish']} bear={s['bearish']} "
                f"neu={s['neutral']} total={s['total']} net={s['net']:+d}"
            )
        return "\n".join(lines)
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_SCHEMAS: Dict[str, dict] = {
    "gbrain_status": {
        "type": "function",
        "function": {
            "name": "gbrain_status",
            "description": "Return GBrain knowledge-base index size and per-platform freshness (documents, claims, last fetch).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    "gbrain_search": {
        "type": "function",
        "function": {
            "name": "gbrain_search",
            "description": (
                "Semantic + full-text search across the Harvest-Aegis GBrain knowledge base. "
                "Returns ranked items with platform/author/date/title provenance. "
                "Use for recalling ingested research, sentiment, or investing education library content. "
                "Filter by ticker/company via 'entities' (resolves through the ontology, so a company "
                "name like 'Frontline' or a ticker like 'FRO' both work)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query (keywords or semantic intent)."},
                    "limit": {"type": "integer", "description": "Max results (1-20, default 5)."},
                    "platform": {"type": "string", "description": "Filter by platform: library, x, rocketchat, seekingalpha, substack, x_bookmarks."},
                    "since": {"type": "string", "description": "Earliest date (ISO, e.g. 2026-01-01)."},
                    "until": {"type": "string", "description": "Latest date (ISO)."},
                    "entities": {"type": "string", "description": "Filter by stock/entity (ticker or company name, e.g. 'FRO' or 'Frontline'). Resolves through the ontology."},
                    "author": {"type": "string", "description": "Filter by author."},
                    "publication": {"type": "string", "description": "Filter by publication."},
                    "channel": {"type": "string", "description": "Filter by source channel."},
                    "doc_type": {"type": "string", "description": "Filter by document type."},
                    "sentiment_min": {"type": "number", "description": "Minimum sentiment (-1 to 1)."},
                    "sentiment_max": {"type": "number", "description": "Maximum sentiment (-1 to 1)."},
                    "signal_score_min": {"type": "number", "description": "Minimum signal score."},
                },
                "required": ["q"],
            },
        },
    },
    "gbrain_recall": {
        "type": "function",
        "function": {
            "name": "gbrain_recall",
            "description": "Fetch one full document from GBrain by its doc_id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "The GBrain document id (e.g. doc:XXXX)."}
                },
                "required": ["doc_id"],
            },
        },
    },
    "gbrain_facets": {
        "type": "function",
        "function": {
            "name": "gbrain_facets",
            "description": "Distinct values + counts for a metadata field (author, platform, publication) in GBrain.",
            "parameters": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "Field to facet on: author, platform, publication."}
                },
                "required": ["field"],
            },
        },
    },
    "gbrain_stock_sentiment": {
        "type": "function",
        "function": {
            "name": "gbrain_stock_sentiment",
            "description": (
                "Per-stock bullish/bearish/neutral claim counts across the GBrain corpus. "
                "Groups harvest.claims by subject_entity (the stock) and direction, joined onto "
                "gbrain.documents so the same metadata filters as gbrain_search apply. Returns "
                "each stock's bullish/bearish/neutral counts plus a net score, so 'which stocks "
                "have the most bullish/bearish outlook' is answerable in one call. "
                "Provide at least one filter (entities, author, since, direction, etc.)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entities": {"type": "string", "description": "Restrict to specific tickers/company names (e.g. 'FRO' or 'Frontline'). Resolves through the ontology."},
                    "author": {"type": "string", "description": "Restrict to author(s)."},
                    "publication": {"type": "string", "description": "Restrict by publication."},
                    "platform": {"type": "string", "description": "Restrict by platform: library, x, rocketchat, seekingalpha, substack, x_bookmarks."},
                    "since": {"type": "string", "description": "Earliest date (ISO, e.g. 2026-01-01)."},
                    "until": {"type": "string", "description": "Latest date (ISO)."},
                    "direction": {"type": "string", "description": "Restrict to one direction: bullish, bearish, or neutral."},
                    "min_mentions": {"type": "integer", "description": "Drop stocks with fewer than N total claims (default 1)."},
                    "limit": {"type": "integer", "description": "Max stocks returned (default 50)."},
                    "sort": {"type": "string", "description": "Sort by: bullish, bearish, neutral, total, or net (default total)."},
                },
            },
        },
    },
}

_HANDLERS: Dict[str, Callable[[dict], str]] = {
    "gbrain_status": gbrain_status,
    "gbrain_search": gbrain_search,
    "gbrain_recall": gbrain_recall,
    "gbrain_facets": gbrain_facets,
    "gbrain_stock_sentiment": gbrain_stock_sentiment,
}


def register_tools(ctx) -> None:
    """Register the gbrain_* tools in the ``gbrain_query`` toolset."""
    emoji = "\U0001f50d"  # magnifying glass
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="gbrain_query",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
