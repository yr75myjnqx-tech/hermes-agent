from __future__ import annotations
import logging
logger = logging.getLogger(__name__)
__all__ = ["register"]


def check_requirements() -> bool:
    return True


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    return True


def interactive_setup() -> None:
    print("whatsapp_send: scoped outbound send; targets allowlisted in code.")


def register(ctx) -> None:
    from .tools import register_tools
    register_tools(ctx)
