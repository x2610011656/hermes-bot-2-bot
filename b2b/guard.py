"""Guardrails — the policy every delivery passes before anything is spawned.

Three independent gates, checked in order, each failing closed:

    1. size     — a message may not exceed ``MAX_MESSAGE_CHARS``
    2. lane     — only the three declared lanes exist; 'bot' is the default
    3. targets  — a bot may be reached only if policy allows it

Policy comes from ``<hermes-home>/b2b-policy.json`` (optional):

    {
      "allowed_targets": ["marnie", "jefferson"],   // absent = every bot
      "denied_targets": ["vinny"],                  // always wins
      "allowed_lanes": ["bot", "existing"],         // absent = every lane
      "max_message_chars": 16000,
      "allow_fanout": true
    }

Absent file = permissive defaults, which is what a single-owner install wants.
A present file tightens; nothing here loosens a hard limit (``HARD_MAX_CHARS``).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional

from .model import Lane, Reason, B2BError

logger = logging.getLogger(__name__)

# Hard ceiling that policy cannot raise — a runaway caller must not be able to
# queue an unbounded blob into another profile's session.
HARD_MAX_CHARS = 64000
DEFAULT_MAX_CHARS = 16000

_POLICY_FILE = "b2b-policy.json"


class Policy:
    """Resolved guardrail settings for one call."""

    def __init__(self, data: Optional[dict] = None):
        data = data if isinstance(data, dict) else {}

        allowed = data.get("allowed_targets")
        denied = data.get("denied_targets")
        lanes = data.get("allowed_lanes")

        self.allowed_targets: Optional[frozenset[str]] = (
            frozenset(str(x).strip().lower() for x in allowed)
            if isinstance(allowed, list) and allowed else None
        )
        self.denied_targets: frozenset[str] = (
            frozenset(str(x).strip().lower() for x in denied)
            if isinstance(denied, list) else frozenset()
        )
        self.allowed_lanes: Optional[frozenset[str]] = (
            frozenset(str(x).strip().lower() for x in lanes)
            if isinstance(lanes, list) and lanes else None
        )
        self.allow_fanout: bool = bool(data.get("allow_fanout", True))

        try:
            requested = int(data.get("max_message_chars") or DEFAULT_MAX_CHARS)
        except (TypeError, ValueError):
            requested = DEFAULT_MAX_CHARS
        self.max_message_chars: int = max(1, min(requested, HARD_MAX_CHARS))

    # ── gates ────────────────────────────────────────────────────────────────

    def check_message(self, text: str) -> None:
        if not text:
            raise B2BError(Reason.BAD_MESSAGE, "The message is empty.")
        if len(text) > self.max_message_chars:
            raise B2BError(
                Reason.TOO_LARGE,
                f"Message is {len(text)} chars; the limit is {self.max_message_chars}.",
            )

    def check_lane(self, lane: Lane) -> None:
        if self.allowed_lanes is not None and lane.value not in self.allowed_lanes:
            raise B2BError(
                Reason.DENIED,
                f"Lane '{lane.value}' is not permitted. Allowed: "
                f"{', '.join(sorted(self.allowed_lanes))}.",
            )

    def check_target(self, pid: str) -> None:
        key = pid.strip().lower()
        if key in self.denied_targets:
            raise B2BError(Reason.DENIED, f"Bot '{pid}' is denied by policy.")
        if self.allowed_targets is not None and key not in self.allowed_targets:
            raise B2BError(
                Reason.DENIED,
                f"Bot '{pid}' is not in allowed_targets. Allowed: "
                f"{', '.join(sorted(self.allowed_targets))}.",
            )

    def check_fanout(self, pids: Iterable[str]) -> None:
        if not self.allow_fanout and len(list(pids)) > 1:
            raise B2BError(
                Reason.DENIED,
                "Fan-out is disabled by policy; name a single target.",
            )

    def describe(self) -> dict:
        """Policy as data (for b2b_list / diagnostics)."""
        return {
            "allowed_targets": sorted(self.allowed_targets) if self.allowed_targets else "all",
            "denied_targets": sorted(self.denied_targets),
            "allowed_lanes": sorted(self.allowed_lanes) if self.allowed_lanes else "all",
            "max_message_chars": self.max_message_chars,
            "allow_fanout": self.allow_fanout,
        }


_cache: dict[str, tuple[float, Policy]] = {}
_TTL_SECONDS = 5.0


def load(hermes_home: Path) -> Policy:
    """Read the policy file, cached briefly. Never raises: an unreadable policy
    file falls back to permissive defaults rather than breaking every call."""
    import time

    key = str(hermes_home)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < _TTL_SECONDS:
        return hit[1]

    path = hermes_home / _POLICY_FILE
    data: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            logger.warning("unreadable %s; using permissive defaults", path, exc_info=True)
    policy = Policy(data)
    _cache[key] = (now, policy)
    return policy
