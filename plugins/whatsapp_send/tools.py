"""Scoped outbound WhatsApp send tool for a persona profile (allowlisted targets).

Target allowlist is loaded from ``~/.hermes/whatsapp_targets.json`` so phone
numbers, JIDs, and group IDs are never committed to source control.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger(__name__)

_BRIDGE_URL = "http://127.0.0.1:3000"  # the bridge /send endpoint
_CONFIG_PATH = Path(os.environ.get("WHATSAPP_TARGETS_PATH", "~/.hermes/whatsapp_targets.json")).expanduser()


def _load_targets() -> Tuple[Dict[str, str], Dict[str, str]]:
    """Load the allowlisted targets and their human-friendly labels.

    Returns (targets, labels). Raises RuntimeError if the config is missing or
    invalid so the failure is loud and obvious at tool-use time.
    """
    if not _CONFIG_PATH.exists():
        raise RuntimeError(
            f"WhatsApp targets config not found: {_CONFIG_PATH}\n"
            "Create it from the template at plugins/whatsapp_send/targets.example.json"
        )
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not parse {_CONFIG_PATH}: {exc}")

    targets = data.get("targets")
    labels = data.get("labels") or {}
    if not isinstance(targets, dict) or not targets:
        raise RuntimeError(f"Invalid {_CONFIG_PATH}: 'targets' must be a non-empty object")

    return targets, labels


def _send_to_bridge(chat_id: str, message: str) -> str:
    payload = json.dumps({"chatId": chat_id, "message": message}).encode("utf-8")
    req = urllib.request.Request(
        f"{_BRIDGE_URL}/send",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", "replace")
            data = json.loads(body) if body else {}
            mid = data.get("messageId", data.get("messageIds", ["unknown"]))
            return f"Sent okay. messageId={mid}"
    except urllib.error.HTTPError as e:
        return f"Send failed HTTP {e.code}"
    except Exception as e:
        return f"Send failed: {e}"


def whatsapp_send(args: dict, **_: Any) -> str:
    """Send an outbound WhatsApp message to an allowlisted recipient or group."""
    try:
        allowed_targets, target_labels = _load_targets()
    except RuntimeError as exc:
        return f"Error: {exc}"

    target = str(args.get("target") or "").strip().lower()
    message = str(args.get("message") or "").strip()
    if not message:
        return "Error: 'message' is required."
    if not target:
        return "Error: 'target' required: " + ", ".join(allowed_targets.keys())

    chat_id = allowed_targets.get(target)
    if not chat_id:
        return f"Error: '{target}' not allowed. Allowed: {', '.join(allowed_targets.keys())}"

    return _send_to_bridge(chat_id, message)


def _build_schema() -> dict:
    """Build the function schema from the external allowlist."""
    try:
        allowed_targets, target_labels = _load_targets()
    except Exception as exc:
        logger.warning("whatsapp_send could not load targets for schema: %s", exc)
        allowed_targets, target_labels = {}, {}

    labels = [f"{k}={target_labels.get(k, k)}" for k in allowed_targets]

    return {
        "type": "function",
        "function": {
            "name": "whatsapp_send",
            "description": (
                "Send an outbound WhatsApp message to an allowlisted recipient or group. "
                "Targets: " + ", ".join(labels)
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "enum": list(allowed_targets.keys()),
                        "description": "Allowlisted recipient name.",
                    },
                    "message": {"type": "string", "description": "Text to send."},
                },
                "required": ["target", "message"],
            },
        },
    }


_SCHEMAS: Dict[str, dict] = {"whatsapp_send": _build_schema()}
_HANDLERS: Dict[str, Callable[[dict], str]] = {"whatsapp_send": whatsapp_send}


def register_tools(ctx) -> None:
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="whatsapp_send",
            schema=schema,
            handler=_HANDLERS[name],
            description=schema["function"]["description"],
            emoji="\U0001f4ac",
        )
