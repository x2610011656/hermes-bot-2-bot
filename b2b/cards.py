"""Cards — write a bot's AgentCard, so a fleet can be described from one place.

``b2b_list`` reads cards; this module writes them. Kept separate from roster.py
(which is read-only discovery) so a caller can tell read paths from write paths
at a glance.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from .model import AgentCard
from . import roster

logger = logging.getLogger(__name__)

CARD_FILE = "b2b-card.json"


def path_for(pid: str) -> Path:
    """Where a bot's card lives."""
    return roster.profile_dir(pid) / CARD_FILE


def read(pid: str) -> AgentCard:
    """Read a card (delegates to roster so there is one parser)."""
    return roster.card(pid)


def write(pid: str, *, name: str = "", description: str = "",
          lanes: Optional[Iterable[str]] = None,
          capabilities: Optional[Iterable[str]] = None) -> Path:
    """Write a bot's card. Only the fields given are written; the rest are
    omitted so the card stays minimal. Returns the path written."""
    data: dict[str, Any] = {}
    if name:
        data["name"] = name
    if description:
        data["description"] = description
    if lanes is not None:
        data["lanes"] = [str(x) for x in lanes]
    if capabilities is not None:
        data["capabilities"] = [str(x) for x in capabilities]

    target = path_for(pid)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return target


def remove(pid: str) -> bool:
    """Delete a bot's card. True when a file was removed."""
    target = path_for(pid)
    if target.is_file():
        target.unlink()
        return True
    return False


def valid_lanes() -> tuple[str, ...]:
    """The lanes a card may declare (mirrors model.Lane)."""
    from .model import Lane
    return tuple(lane.value for lane in Lane)
