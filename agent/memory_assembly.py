"""Memory assembly engine (P1, spec v1.1).

Enriches the external-memory prefetch blob with entity-anchored structured
lookups before it is fenced into the ``<memory-context>`` block. Closes
validation findings 3 (no manifest), 4 (lexical-only retrieval), 5 (no
rank-cut) and 6 (HEM never auto-assembled).

Design contract (spec §3-§5):
- Read-only consumer: never writes GBrain, HEM, Honcho or MEMORY.md.
- Fail-open everywhere: an unreachable store contributes zero items and never
  blocks the turn (AT-1 determinism preserved).
- Deterministic ordering, no tuned weights in P1: sort key
  ``(entity_match, tier_priority, recency)``.
- Two-pass budget cut: tier floors first, remaining budget by global rank;
  a record that does not fit whole is dropped, never truncated (AT-5).
- The manifest is a log-file artifact ONLY — never model-visible (spec §5).
- Flag off (default) = the engine is never invoked; behaviour is
  byte-identical to legacy recall and no manifest file is created.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import logging
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Entity extraction (spec §3[E]) ──────────────────────────────────────────

# Common non-ticker uppercase tokens, second guard behind curated-list membership.
_ACRONYM_DENYLIST = frozenset({
    "AI", "CEO", "CFO", "COO", "CTO", "US", "USA", "UK", "EU", "IT", "NAV", "TP",
    "GDP", "CPI", "FOMC", "FED", "ECB", "BOJ", "OPEC", "IMO", "ETF", "IPO", "M&A",
    "YOY", "QOQ", "EPS", "EBITDA", "P/E", "ROE", "ROI", "USD", "AUD", "EUR", "GBP",
    "JPY", "CNY", "API", "URL", "JSON", "SQL", "CSV", "PDF", "HTML", "HTTP", "HTTPS",
    "LLM", "GPU", "CPU", "RAM", "SSD", "HDD", "NYC", "LNG", "LPG", "VLCC",
    "SUEZMAX", "AFRAMAX", "PANAMAX", "CAPESIZE", "MR", "LR1", "LR2", "ULCV",
})

_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{4}\b"),
)


@dataclass
class Entities:
    """Extracted query entities (spec §3[E] output shape)."""
    tickers: list[str] = field(default_factory=list)
    analysts: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "tickers": list(self.tickers), "analysts": list(self.analysts),
            "dates": list(self.dates), "sectors": list(self.sectors),
        }

    def is_empty(self) -> bool:
        return not (self.tickers or self.analysts or self.dates or self.sectors)


class EntityExtractor:
    """Deterministic, curated-list-only extraction (no LLM in P1).

    ``ticker_universe``: set of valid ticker symbols (stock universe + portfolio).
    ``analyst_names``: curated analyst/author names (exact match).
    ``sector_terms``: desk-taxonomy keywords.
    """

    _TICKER_RE = re.compile(r"\$([A-Za-z]{1,5})\b|\b([A-Z]{2,5})\b")

    def __init__(
        self,
        ticker_universe: frozenset[str] = frozenset(),
        analyst_names: frozenset[str] = frozenset(),
        sector_terms: frozenset[str] = frozenset(),
        max_tickers: int = 6,
    ) -> None:
        self._universe = {t.upper() for t in ticker_universe}
        self._analysts = analyst_names
        self._sectors = sector_terms
        self._max_tickers = max_tickers

    def extract(self, query: str) -> Entities:
        out = Entities()
        # Tickers: $-prefix always wins; bare caps ONLY on exact universe membership
        # and never in the denylist (spec §3[E], MOA fix #3).
        for m in self._TICKER_RE.finditer(query):
            sym = (m.group(1) or m.group(2) or "").upper()
            if not sym or sym in _ACRONYM_DENYLIST:
                continue
            if m.group(1) or sym in self._universe:
                if sym not in out.tickers:
                    out.tickers.append(sym)
            if len(out.tickers) >= self._max_tickers:
                break
        lowered = query.lower()
        for name in self._analysts:
            if name.lower() in lowered and name not in out.analysts:
                out.analysts.append(name)
        for pat in _DATE_PATTERNS:
            for m in pat.finditer(query):
                if m.group(0) not in out.dates:
                    out.dates.append(m.group(0))
        for term in self._sectors:
            if term.lower() in lowered and term not in out.sectors:
                out.sectors.append(term)
        return out


# ── Structured lookups (spec §3[L]) ─────────────────────────────────────────

@dataclass
class Record:
    """One candidate item from any tier."""
    tier: str                 # "gbrain" | "hem" | "honcho"
    store: str                # concrete store name for the manifest
    record_id: str
    text: str
    created: str              # ISO date string or ""
    entity_match: float       # 1.0 structured, 0.3 lexical-only
    matched_entities: list[str] = field(default_factory=list)
    field: str | None = None  # P2 WS-1: field/predicate metadata, if the store exposes it


@dataclass
class LookupStatus:
    """Per-entity lookup outcome for the manifest (MOA fix: distinguish
    'never retrieved' from 'ranked out')."""
    entity: str
    store: str
    status: str               # found | not_found | timeout | skipped | error


def _http_json(url: str, body: dict | None, headers: dict[str, str], timeout: float) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _gbrain_search(
    base_url: str, api_key: str, terms: list[str], top_k: int, deadline: float,
) -> tuple[list[Record], list[LookupStatus]]:
    records: list[Record] = []
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return records, [LookupStatus(t, "gbrain", "skipped") for t in terms]
    try:
        payload = _http_json(
            f"{base_url.rstrip('/')}/v1/search",
            {"query": " ".join(terms), "limit": top_k},
            {"Content-Type": "application/json", **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
            timeout=max(0.1, remaining),
        )
    except Exception as exc:  # fail-open: timeout, connection, parse — all the same
        status = "timeout" if isinstance(exc, TimeoutError) else "error"
        logger.debug("gbrain lookup failed (fail-open): %s", exc)
        return records, [LookupStatus(t, "gbrain", status) for t in terms]
    items = (payload or {}).get("data", {}).get("items", []) if isinstance(payload, dict) else []
    for item in items[:top_k]:
        text = str(item.get("excerpt", ""))[:1200]
        author = str(item.get("author", ""))
        ents = [e for e in terms if e.lower() in text.lower() or e.lower() in author.lower()]
        records.append(Record(
            tier="gbrain", store="gbrain", record_id=str(item.get("doc_id", "")),
            text=text, created=str(item.get("posted", "") or item.get("collected", "")),
            entity_match=1.0 if ents else 0.3, matched_entities=ents,
        ))
    return records, [LookupStatus(t, "gbrain", "found" if items else "not_found") for t in terms]


def _hem_search(
    ssh_cmd: list[str], terms: list[str], top_k: int, deadline: float,
) -> tuple[list[Record], list[LookupStatus]]:
    """HEM lookup over SSH to the PG host (hem@127.0.0.1 is host-local only).

    ONE batched query per turn for all terms (SSH round-trip cost). Read-only
    SELECT as hem_rw; the engine never writes HEM.
    """
    statuses = [LookupStatus(t, "hem", "skipped") for t in terms]
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not terms:
        return [], statuses
    tsquery = " OR ".join(terms)
    sql = (
        "SELECT id, record_type, title, to_char(created,'YYYY-MM-DD') AS day, left(body,600) AS ex "
        "FROM hem_index.records WHERE superseded_by IS NULL AND record_type='episode' "
        "AND tsv @@ websearch_to_tsquery('english', "
        + "'" + tsquery.replace("'", "''") + "'"
        + ") ORDER BY ts_rank(tsv, websearch_to_tsquery('english', "
        + "'" + tsquery.replace("'", "''") + "'"
        + ")) DESC, created DESC LIMIT " + str(int(top_k)) + ";"
    )
    try:
        proc = subprocess_run(ssh_cmd + ["psql", "-X", "-q", "-A", "-F", "\x1f", "-c", sql],
                              timeout=max(0.2, remaining))
    except Exception as exc:
        status = "timeout" if isinstance(exc, TimeoutError) else "error"
        logger.debug("hem lookup failed (fail-open): %s", exc)
        return [], [LookupStatus(t, "hem", status) for t in terms]
    if proc.returncode != 0:
        logger.debug("hem psql rc=%s (fail-open)", proc.returncode)
        return [], [LookupStatus(t, "hem", "error") for t in terms]
    records: list[Record] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        rid, rtype, title, day, excerpt = parts
        ents = [t for t in terms if t.lower() in (title + " " + excerpt).lower()]
        records.append(Record(
            tier="hem", store="hem", record_id=rid,
            text=f"[{rtype}] {title}: {excerpt}", created=day,
            entity_match=1.0 if ents else 0.3, matched_entities=ents,
        ))
    return records, [LookupStatus(t, "hem", "found" if records else "not_found") for t in terms]


def subprocess_run(cmd: list[str], timeout: float):
    """Indirection for testability (tests stub this)."""
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


# ── Ranking + budget cut (spec §3[R]/§3[B]) ─────────────────────────────────

_TIER_PRIORITY = {"gbrain": 3, "hem": 2, "honcho": 1}


def rank_records(records: list[Record]) -> list[Record]:
    """Deterministic ordering: (entity_match desc, tier desc, recency desc).

    Cross-entity dedupe on canonical record_id BEFORE ranking; entity matches
    aggregate on the surviving record.
    """
    by_id: dict[str, Record] = {}
    for rec in records:
        key = rec.record_id or f"{rec.tier}:{hash(rec.text) & 0xffff:04x}"
        existing = by_id.get(key)
        if existing is None:
            by_id[key] = rec
        else:
            existing.entity_match = max(existing.entity_match, rec.entity_match)
            for ent in rec.matched_entities:
                if ent not in existing.matched_entities:
                    existing.matched_entities.append(ent)
    return sorted(
        by_id.values(),
        key=lambda r: (-r.entity_match, -_TIER_PRIORITY.get(r.tier, 0), _sort_date_desc(r.created), r.record_id),
    )


# ── WS-1: read-side conflict/supersession resolution (P2, deterministic) ────

def _norm_text(s: str) -> str:
    """A1 normalisation: case-fold, trim, collapse whitespace."""
    return " ".join(s.split()).casefold()


def _record_field(rec: Record) -> str | None:
    """Extract the field/predicate from record metadata, if any.

    Coverage limitation (spec §8.1.2, honest): only records whose metadata
    carries a field/predicate can be keyed. Today no store exposes one, so
    this reads the optional ``field`` attribute that future store adapters may
    set; everything else passes through unconflicted. Do not fake it.
    """
    fld = getattr(rec, "field", None)
    return str(fld) if fld else None


def resolve_conflicts(
    records: list[Record],
) -> tuple[list[Record], list[dict[str, Any]]]:
    """Deterministic per-turn supersession (spec §8.1, A1).

    Groups keyed records by normalised ``(entity, field)``; within a group
    holding differing values, keeps exactly one by: newest timestamp → tier
    priority → record_id. Value comparison is normalised exact string
    inequality — near-duplicates are NOT conflicts (P3 semantic question).

    Returns (surviving_records, conflict_events). Unkeyed records pass through
    untouched. Resolution is recomputed each turn; nothing persists.
    """
    groups: dict[tuple[str, str], list[Record]] = {}
    keyed: set[int] = set()
    for rec in records:
        fld = _record_field(rec)
        if not fld:
            continue
        keyed.add(id(rec))
        # Entity: the first matched entity, else the record id (deterministic).
        ent = rec.matched_entities[0] if rec.matched_entities else rec.record_id
        groups.setdefault((_norm_text(ent), _norm_text(fld)), []).append(rec)
    conflict_events: list[dict[str, Any]] = []
    survivors: list[Record] = []
    for rec in records:
        if id(rec) not in keyed:
            survivors.append(rec)
    for key, group in groups.items():
        if len(group) == 1:
            survivors.append(group[0])
            continue
        values = {_norm_text(r.text) for r in group}
        if len(values) <= 1:
            # Same value repeated: keep one (highest rank), no conflict event.
            group_sorted = sorted(group, key=lambda r: (-r.entity_match, r.record_id))
            survivors.append(group_sorted[0])
            continue
        # Differing values: resolve newest → tier → record_id.
        group_sorted = sorted(
            group,
            key=lambda r: (_sort_date_desc(r.created), -_TIER_PRIORITY.get(r.tier, 0), r.record_id),
        )
        winner = group_sorted[0]
        survivors.append(winner)
        conflict_events.append({
            "entity": key[0], "field": key[1],
            "kept": winner.record_id,
            "superseded": [
                {"record_id": r.record_id, "superseded_by": winner.record_id}
                for r in group_sorted[1:]
            ],
        })
    return survivors, conflict_events


def _sort_date_desc(created: str) -> float:
    """Recency sort key: NEWER = SMALLER (negated ordinal), so ascending sort
    puts newest first. Undated/unparseable records sink to the end."""
    if not created:
        return float("inf")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", created)
    if not m:
        return float("inf")
    try:
        import datetime
        return -datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).toordinal()
    except ValueError:
        return float("inf")


def budget_cut(
    ranked: list[Record], honcho_text: str, char_budget: int, tier_floors: dict[str, float],
    combined_budget: int | None = None, legacy_chars: int = 0,
) -> tuple[list[Record], list[dict[str, Any]]]:
    """Two-pass allocation (spec §3[B]): floors first, then global rank.

    P2 combined-budget semantics (spec §4.2, A3): when ``combined_budget`` is
    given, the legacy prefetch text is charged FIRST against it and the
    enrichment fills the remainder; the per-enrichment ``char_budget`` remains
    a sub-ceiling. The legacy text is NEVER cut by the engine: if legacy alone
    meets or exceeds the combined budget, enrichment contributes zero records
    (all rejected ``over_budget_combined``).

    Returns (kept_records, rejected_entries). A record that does not fit whole
    is dropped, never truncated. The Honcho passthrough participates as one
    pseudo-record in its tier floor.
    """
    rejected: list[dict[str, Any]] = []
    kept: list[Record] = []
    used = 0
    honcho_rec = None
    if honcho_text.strip():
        honcho_rec = Record(tier="honcho", store="honcho", record_id="honcho:prefetch",
                            text=honcho_text, created="", entity_match=0.3)
    pool = list(ranked)
    if honcho_rec:
        if combined_budget is not None:
            # P2 combined accounting: the honcho passthrough IS the legacy text,
            # already charged as legacy_chars. Charging it again here would
            # double-count it against the combined budget — exclude it.
            pass
        else:
            pool.append(honcho_rec)
    # Combined-budget headroom: legacy charged first; enrichment gets the remainder.
    if combined_budget is not None:
        enrichment_budget = max(0, combined_budget - legacy_chars)
    else:
        enrichment_budget = char_budget
    # Pass 1: tier floors, filled in rank order within each tier. Floors are
    # computed against the enrichment budget (never against legacy's share).
    floor_chars = {tier: int(enrichment_budget * frac) for tier, frac in tier_floors.items()}
    floor_used: dict[str, int] = {tier: 0 for tier in floor_chars}
    deferred: list[Record] = []
    for rec in pool:
        floor = floor_chars.get(rec.tier, 0)
        size = len(rec.text)
        if floor and floor_used[rec.tier] + size <= floor:
            kept.append(rec)
            floor_used[rec.tier] += size
            used += size
        else:
            deferred.append(rec)
    # Pass 2: remaining budget by global rank (deferred list is already rank-ordered).
    for rec in deferred:
        size = len(rec.text)
        if used + size <= enrichment_budget:
            kept.append(rec)
            used += size
        else:
            reason = "over_budget_combined" if combined_budget is not None else "over_budget_rank"
            rejected.append({
                "record_id": rec.record_id, "tier": rec.tier,
                "reason": reason,
                "chars": size,
            })
    # Preserve global rank order in the kept list.
    order = {id(r): i for i, r in enumerate(pool)}
    kept.sort(key=lambda r: order.get(id(r), 10**9))
    return kept, rejected


# ── Manifest (spec §3[M]) ───────────────────────────────────────────────────

class ManifestWriter:
    """Append-only JSONL manifest; rotation at size cap; write failures are
    logged and swallowed — a manifest problem never breaks the turn."""

    def __init__(self, path: Path, rotate_bytes: int) -> None:
        self._path = path
        self._rotate = rotate_bytes

    def write(self, entry: dict[str, Any]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists() and self._path.stat().st_size >= self._rotate:
                rotated = self._path.with_suffix(
                    self._path.suffix + "." + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                )
                os.replace(self._path, rotated)
            line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
            with open(self._path, "a", encoding="utf-8") as fh:  # O_APPEND semantics via 'a'
                fh.write(line)
        except Exception as exc:
            logger.warning("memory manifest write failed (non-fatal): %s", exc)


# ── Engine entry point (spec §3, insertion between H2 and H3) ───────────────

def _load_engine_config(agent: Any) -> dict[str, Any] | None:
    """Read memory.assembly config; None = engine disabled (flag off or bad config).

    Config is validated here at every call (cheap dict reads); a malformed
    subtree disables the engine for the call and logs loudly (spec §4).
    """
    try:
        from hermes_cli.config import load_config
        mem = (load_config() or {}).get("memory", {}) or {}
    except Exception as exc:
        logger.warning("memory.assembly: config load failed, engine disabled: %s", exc)
        return None
    asm = mem.get("assembly")
    if not isinstance(asm, dict):
        return None
    if asm.get("enabled") is not True:
        return None
    floors = asm.get("tier_floors")
    if not isinstance(floors, dict) or abs(sum(float(v) for v in floors.values()) - 1.0) > 0.01:
        logger.warning("memory.assembly: invalid tier_floors %r, engine disabled", floors)
        return None
    # P2 (WS-4.1, A2): pinned injection position. Only pre_user_turn is
    # implemented; any other value (including reserved system_edge) disables
    # the engine exactly like the tier-floor check.
    position = asm.get("position", "pre_user_turn")
    if position != "pre_user_turn":
        logger.warning("memory.assembly: unsupported position %r (only "
                       "'pre_user_turn' implemented; 'system_edge' reserved), engine disabled",
                       position)
        return None
    return asm


def maybe_assemble_memory(
    agent: Any, query: str, raw_prefetch: str, *, session_id: str = "",
) -> str:
    """Flag-gated seam called from turn_context after prefetch_all.

    Returns the (possibly enriched) prefetch text. Flag off, empty prefetch,
    or any engine error → the input text unchanged (fail-open).
    """
    if not raw_prefetch or not raw_prefetch.strip():
        return raw_prefetch
    cfg = _load_engine_config(agent)
    if cfg is None:
        return raw_prefetch
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        manifest = ManifestWriter(
            home / cfg.get("manifest_path", "logs/memory_manifest.jsonl"),
            int(cfg.get("manifest_rotate_bytes", 50_000_000)),
        )
        extractor = _get_extractor(cfg)
        turn = int(getattr(agent, "_user_turn_count", 0) or 0)
        assembled, _entry = assemble(
            query, raw_prefetch, cfg, extractor, manifest,
            session_id=session_id, turn=turn,
            gbrain_base_url=str(cfg.get("gbrain_base_url", "http://127.0.0.1:5052")),
            gbrain_api_key=str(cfg.get("gbrain_api_key", "")),
            hem_ssh_cmd=cfg.get("hem_ssh_cmd") or None,
        )
        return assembled or raw_prefetch
    except Exception as exc:
        logger.warning("memory assembly failed (fail-open, raw prefetch kept): %s", exc)
        return raw_prefetch


_EXTRACTOR_CACHE: dict[str, EntityExtractor] = {}


def _get_extractor(cfg: dict[str, Any]) -> EntityExtractor:
    """Build (and cache) the extractor from the stock-universe CSV + curated lists."""
    cache_key = str(cfg.get("ticker_universe_path", "")) + "|" + str(cfg.get("max_ticker_entities", 6))
    cached = _EXTRACTOR_CACHE.get(cache_key)
    if cached is not None:
        return cached
    universe: set[str] = set()
    uni_path = cfg.get("ticker_universe_path", "")
    if uni_path:
        try:
            import csv
            with open(uni_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    sym = (row.get("ticker") or row.get("symbol") or "").strip().upper()
                    if sym:
                        universe.add(sym)
        except Exception as exc:
            logger.warning("memory.assembly: ticker universe load failed: %s", exc)
    analysts = frozenset(str(n) for n in cfg.get("analyst_names", []))
    sectors = frozenset(str(s) for s in cfg.get("sector_terms", []))
    ex = EntityExtractor(
        ticker_universe=frozenset(universe), analyst_names=analysts,
        sector_terms=sectors, max_tickers=int(cfg.get("max_ticker_entities", 6)),
    )
    _EXTRACTOR_CACHE[cache_key] = ex
    return ex


def assemble(
    query: str,
    raw_prefetch: str,
    config: dict[str, Any],
    extractor: EntityExtractor,
    manifest: ManifestWriter | None,
    *,
    session_id: str = "",
    turn: int = 0,
    gbrain_base_url: str = "",
    gbrain_api_key: str = "",
    hem_ssh_cmd: list[str] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Assemble the enriched memory-context text from the raw prefetch.

    Returns ``(assembled_text, manifest_entry_or_None)``. All lookups run
    concurrently under ONE global deadline; any failure contributes zero items.
    """
    cfg = config
    deadline = time.monotonic() + float(cfg.get("lookup_timeout_s", 2.0))
    entities = extractor.extract(query)
    lookups: list[Callable[[], tuple[list[Record], list[LookupStatus]]]] = []
    terms: list[str] = []
    terms.extend(entities.tickers)
    terms.extend(entities.analysts)
    if terms and gbrain_base_url:
        lookups.append(lambda t=terms: _gbrain_search(
            gbrain_base_url, gbrain_api_key, t, int(cfg.get("top_k_gbrain", 3)), deadline))
    if terms and hem_ssh_cmd:
        lookups.append(lambda t=terms: _hem_search(
            hem_ssh_cmd, t, int(cfg.get("top_k_hem", 2)), deadline))
    max_lookups = int(cfg.get("max_lookups", 6))
    lookups = lookups[:max_lookups]
    records: list[Record] = []
    statuses: list[LookupStatus] = []
    if lookups:
        # No context manager: executor.__exit__ would JOIN stuck threads, defeating
        # the global deadline. shutdown(wait=False) leaves stragglers on daemon
        # threads; their results are simply never collected (fail-open).
        ex = cf.ThreadPoolExecutor(max_workers=max(1, len(lookups)))
        futures = [ex.submit(fn) for fn in lookups]
        try:
            for fut in futures:
                try:
                    recs, sts = fut.result(timeout=max(0.1, deadline - time.monotonic()))
                    records.extend(recs)
                    statuses.extend(sts)
                except Exception as exc:
                    logger.debug("assembly lookup failed (fail-open): %s", exc)
                    statuses.append(LookupStatus("", "engine", "error"))
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    ranked = rank_records(records)
    # P2 WS-1: deterministic supersession, post-lookup / pre-rank. Unkeyed
    # records pass through; conflict events go to the manifest only.
    survivors, conflict_events = resolve_conflicts(ranked)
    ranked = rank_records(survivors)
    combined_budget = int(cfg.get("combined_char_budget", 0)) or None
    kept, rejected = budget_cut(
        ranked, raw_prefetch, int(cfg.get("char_budget", 6000)),
        dict(cfg.get("tier_floors", {"gbrain": 0.5, "honcho": 0.3, "hem": 0.2})),
        combined_budget=combined_budget, legacy_chars=len(raw_prefetch),
    )
    # Compose: structured tiers first (rank order), Honcho passthrough last.
    parts: list[str] = []
    honcho_part = ""
    for rec in kept:
        if rec.tier == "honcho":
            honcho_part = rec.text
        else:
            parts.append(f"- [{rec.tier}] {rec.text}")
    assembled = "\n".join(parts)
    if honcho_part:
        assembled = (assembled + "\n" + honcho_part) if assembled else honcho_part
    # P2 WS-2: model-visible status line ONLY on total lookup failure (every
    # lookup timeout/error, zero usable results). Fixed compile-time constant —
    # never composed from store content or error text. Partial failure: nothing.
    usable = any(s.status in ("found", "not_found") for s in statuses)
    had_lookups = bool(lookups)
    total_failure = had_lookups and not usable
    if total_failure:
        status_line = (
            "MEMORY STATUS: enrichment unavailable this turn (lookup failure); "
            "base prefetch only."
        )
        assembled = (status_line + "\n" + assembled) if assembled else status_line
    entry = None
    if manifest is not None:
        legacy_chars = len(raw_prefetch)
        # Enrichment chars = structured records kept (honcho passthrough is
        # legacy, not enrichment; the status line is a constant, not content).
        enrichment_chars = sum(len(r.text) for r in kept if r.tier != "honcho")
        entry = {
            "schema": 2,
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id, "turn": turn,
            "query_entities": entities.as_dict(),
            "lookup_status": [s.__dict__ for s in statuses],
            "injected": [
                {"tier": r.tier, "store": r.store, "record_id": r.record_id,
                 "chars": len(r.text), "entity_match": r.entity_match,
                 "matched_entities": r.matched_entities}
                for r in kept
            ],
            "rejected": rejected,
            "conflicts": conflict_events,
            "status_line_injected": total_failure,
            "legacy_chars": legacy_chars,
            "enrichment_chars": enrichment_chars,
            "combined_chars": legacy_chars + enrichment_chars,
            "assembled_sha256": hashlib.sha256(assembled.encode("utf-8")).hexdigest(),
            "prefetch_sha256": hashlib.sha256(raw_prefetch.encode("utf-8")).hexdigest(),
        }
        manifest.write(entry)
    return assembled, entry
