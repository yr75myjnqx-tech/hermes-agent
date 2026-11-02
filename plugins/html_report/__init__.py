"""
HTML Report plugin (HerbAI)

Registers ``write_html_report`` / ``list_html_reports`` in the ``html_report``
toolset so the HerbAI profile can produce styled HTML reports itself, without
needing terminal/file/code_execution access.

Why this exists: Hermes deliberately denies Herb the terminal, file, and
code_execution toolsets (scoped sandbox — Herb is the voice, not the hands).
Previously Herb had to escalate to Spock whenever Nic wanted an HTML report.
This plugin gives Herb ONE constrained capability: writing a self-contained
HTML file to a single approved directory. It grants NO shell, NO arbitrary
file access, NO writes outside the approved directory.

Safety: the only writable location is the approved Herb_Reports directory
(resolved at import from HERB_REPORTS_DIR or APPLED). write_html_report
whitelists the filename (no slash, no '..', must end .html, size-capped) and
writes through an atomic temp-file + os.replace. list_html_reports is read-only
on that same directory.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def check_requirements() -> bool:
    """No external deps — stdlib only."""
    return True


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return True


def interactive_setup() -> None:
    print("HTML Report (HerbAI): a scoped HTML report writer for the herbai profile.")
    print("Writes self-contained .html files to the approved Herb_Reports dir under MF4tb.")


def register(ctx) -> None:
    try:
        from .tools import register_tools
        register_tools(ctx)
    except Exception as e:  # pragma: no cover
        logger.exception("html_report plugin register failed: %s", e)
