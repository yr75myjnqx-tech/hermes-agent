"""
write_html_report / list_html_reports — Herb writes self-contained HTML reports.

The plugin writes HTML ONLY to an approved directory. It grants no other file
access and never executes anything. Stdlib only.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Approved output directory (the ONLY writable location)
# ---------------------------------------------------------------------------
# Herb reports live in the Hermes documents home on this box, under Herb's home
# base. This is resolved once at import; override via the HERB_REPORTS_DIR env
# var if the path ever changes.
_APPROVED_DIR = os.environ.get(
    "HERB_REPORTS_DIR",
    "/Users/minipro/Documents/Hermes/WhatsApp/Herb_and_I/Herb_Reports",
)

_MAX_HTML_BYTES = 2 * 1024 * 1024  # 2 MB cap — reports are HTML text, never larger


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_filename(name: str) -> str:
    """Return a safe basename or raise ValueError."""
    name = (name or "").strip()
    if not name:
        raise ValueError("filename is required")
    if "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("filename must be a plain basename (no path separators)")
    if not name.lower().endswith(".html"):
        raise ValueError("filename must end with .html")
    # Only allow a conservative safe character set: alnum, - _ . spaces
    if not re.fullmatch(r"[A-Za-z0-9._ -]+", name):
        raise ValueError("filename contains disallowed characters")
    if len(name) > 120:
        raise ValueError("filename too long")
    return name


def _mkdir_approved() -> str:
    os.makedirs(_APPROVED_DIR, exist_ok=True)
    return _APPROVED_DIR


# ---------------------------------------------------------------------------
# Tool logic
# ---------------------------------------------------------------------------
def write_html_report(args: dict, **_: Any) -> str:
    """Write a self-contained HTML report to the approved Herb_Reports dir."""
    filename = _validate_filename(str(args.get("filename") or args.get("file") or ""))
    title = str(args.get("title") or os.path.splitext(filename)[0]).strip()
    body = args.get("body") or args.get("html") or ""
    if not isinstance(body, str):
        body = str(body)
    if not body.strip():
        raise ValueError("body (the HTML content) is required")
    if len(body.encode("utf-8")) > _MAX_HTML_BYTES:
        raise ValueError(f"HTML content exceeds {_MAX_HTML_BYTES // (1024*1024)} MB cap")

    # Build a self-contained page. If the body already smells like a full <html>
    # document, write it verbatim; otherwise wrap it in a clean page shell.
    low = body.lower().lstrip()
    if low.startswith("<!doctype") or low.startswith("<html"):
        document = body
    else:
        document = (
            "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
            "<meta charset=\"UTF-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
            f"<title>{title}</title>\n"
            "<style>\n"
            "  :root{--bg:#0e1116;--panel:#171c24;--panel2:#1e2530;--text:#e6ebf2;--muted:#9aa6b8;"
            "--accent:#3da9fc;--line:#2a323e;}\n"
            "  *{box-sizing:border-box;}\n"
            "  body{margin:0;background:var(--bg);color:var(--text);"
            "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
            "line-height:1.55;padding:32px 20px;}\n"
            "  .wrap{max-width:960px;margin:0 auto;}\n"
            "  h1{font-size:26px;letter-spacing:-0.5px;}\n"
            "  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;"
            "padding:16px 18px;margin:16px 0;}\n"
            "  .muted{color:var(--muted);}\n"
            "</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
            f"<h1>{title}</h1>\n"
            f"{body}\n"
            "</div>\n</body>\n</html>\n"
        )

    directory = _mkdir_approved()
    path = os.path.join(directory, filename)
    if os.path.exists(path):
        # Never overwrite silently — force the agent to pick a new name.
        return (
            f"A file already exists at {path}. Refusing to overwrite. "
            "Choose a new filename (or the caller should clear the old file)."
        )

    # Atomic write: temp file in the same dir, then os.replace.
    fd, tmp = tempfile.mkstemp(prefix=".herb_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(document)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise

    return (
        f"Report written to:\n{path}\n\n"
        f"Bytes: {len(document.encode('utf-8'))}\n"
        "To deliver it to a chat, reference this absolute path with MEDIA:."
    )


def list_html_reports(args: dict, **_: Any) -> str:
    """List HTML reports already written to the approved directory (read-only)."""
    if not os.path.isdir(_APPROVED_DIR):
        return f"No reports yet — the approved directory {_APPROVED_DIR} does not exist."
    try:
        entries = sorted(
            (e for e in os.listdir(_APPROVED_DIR) if e.lower().endswith(".html")),
            key=lambda e: e.lower(),
        )
    except Exception as e:  # pragma: no cover
        return f"Could not list reports: {e}"
    if not entries:
        return f"No HTML reports found in {_APPROVED_DIR}."
    return f"{len(entries)} report(s) in {_APPROVED_DIR}:\n" + "\n".join(f"- {e}" for e in entries)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
_WRITE_SCHEMA: Dict[str, dict] = {
    "type": "function",
    "function": {
        "name": "write_html_report",
        "description": (
            "Write a self-contained HTML report to the approved Herb_Reports directory "
            "on MF4tb. Use when asked to produce an HTML report. Supply 'filename' "
            "(a plain basename ending in .html, no slashes), an optional 'title', and "
            "'body' — the full HTML for the page. If body is a complete <html> document "
            "it is written verbatim; otherwise it is wrapped in a clean dark-themed "
            "page shell. Refuses to overwrite an existing file. Grants no other file "
            "access."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Output filename, a plain basename ending in .html (e.g. report_20260817.html). No paths.",
                },
                "title": {
                    "type": "string",
                    "description": "Optional page <title> (defaults to the filename).",
                },
                "body": {
                    "type": "string",
                    "description": "The full HTML content for the page body (or a complete <html> document).",
                },
            },
            "required": ["filename", "body"],
        },
    },
}

_LIST_SCHEMA: Dict[str, dict] = {
    "type": "function",
    "function": {
        "name": "list_html_reports",
        "description": "List the HTML reports already written to the approved Herb_Reports directory (read-only).",
        "parameters": {"type": "object", "properties": {}},
    },
}

_HANDLERS: Dict[str, Callable[[dict], str]] = {
    "write_html_report": write_html_report,
    "list_html_reports": list_html_reports,
}

_SCHEMAS: Dict[str, dict] = {
    "write_html_report": _WRITE_SCHEMA,
    "list_html_reports": _LIST_SCHEMA,
}


def register_tools(ctx) -> None:
    """Register the html_report tools in the ``html_report`` toolset."""
    emoji = "\U0001f4c4"  # page facing up
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="html_report",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji=emoji,
        )
