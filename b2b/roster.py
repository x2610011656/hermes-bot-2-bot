"""Roster — discovery. Which bots exist, what they declare, what is live.

This is A2A's discovery layer, downscaled: instead of a signed Agent Card
served over HTTP, a bot declares itself in ``<profile>/b2b-card.json`` and is
found by walking the profiles directory.

Three separate questions, deliberately kept apart because they have different
answers:

    who exists      -> profiles on disk (a bot can exist and be unreachable)
    what it declares-> its AgentCard (permissive when absent)
    what is live    -> advertised live sessions in the lease registry
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterator, Optional

from .model import AgentCard, Lane

logger = logging.getLogger(__name__)

_ROSTER_TTL_SECONDS = 5.0
_roster_cache: dict[str, tuple[float, dict[str, str]]] = {}

# A profile directory that is not a bot.
_NOT_BOTS = frozenset({"sessions", "logs", "cache", "bin", "tools", "installs"})


def hermes_home() -> Path:
    """The active profile's Hermes home (never hardcode ~/.hermes)."""
    from hermes_constants import get_process_hermes_home  # type: ignore
    return Path(str(get_process_hermes_home()))


def self_profile() -> str:
    """This process's profile id."""
    home = hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def profile_dir(pid: str) -> Path:
    """Resolve a profile id to its directory."""
    home = hermes_home()
    return home if pid == "default" else home / "profiles" / pid


def display_name(profile: Path) -> str:
    """A bot's friendly name from its profile.yaml, or ''."""
    try:
        import yaml  # type: ignore
        raw = profile / "profile.yaml"
        if not raw.is_file():
            return ""
        data = yaml.safe_load(raw.read_text(encoding="utf-8-sig", errors="replace")) or {}
        ui = data.get("ui_meta") if isinstance(data, dict) else None
        bots = ui.get("hermes-bots") if isinstance(ui, dict) else None
        title = (bots or {}).get("title", "") if isinstance(bots, dict) else ""
        return str(title or data.get("display_name") or "").strip()
    except Exception:
        return ""


def roster() -> dict[str, str]:
    """{profile_id: display_name} for every bot on this install, cached briefly."""
    home = hermes_home()
    key = str(home)
    now = time.monotonic()
    hit = _roster_cache.get(key)
    if hit and (now - hit[0]) < _ROSTER_TTL_SECONDS:
        return hit[1]

    out: dict[str, str] = {"default": ""}
    profiles = home / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if not entry.is_dir() or entry.name in _NOT_BOTS:
                continue
            if (entry / "config.yaml").is_file() or (entry / "profile.yaml").is_file():
                out[entry.name] = display_name(entry)
    _roster_cache[key] = (now, out)
    return out


def resolve(target: str) -> Optional[str]:
    """Accept a profile id, a display name, or a Bot Mode title -> canonical id."""
    target = (target or "").strip()
    if not target:
        return None
    known = roster()
    if target in known:
        return target
    low = target.lower()
    for pid, name in known.items():
        if pid.lower() == low or (name and low == name.lower()):
            return pid
    return None


# ── live sessions (lane 'existing') ──────────────────────────────────────────

def live_sessions(pid: str) -> list[dict]:
    """Every live session on a profile that advertises itself for delivery.

    Hermes' own ``find_canonical_live_owner`` is pinned to the session titled
    "Bot Chat". This returns ALL advertised live sessions so a caller can target
    one that is not the canonical Bot Chat. Newest first. Never raises.

    Note: the poller that consumes these deliveries runs in the desktop/TUI
    runtime only — a Telegram-gateway bot has no live owner to inject into.
    """
    try:
        from hermes_cli.active_sessions import active_session_registry_snapshot  # type: ignore
    except Exception:
        return []
    home = profile_dir(pid)
    try:
        entries = active_session_registry_snapshot(registry_home=home)
    except Exception:
        logger.debug("live-session snapshot failed for %s", pid, exc_info=True)
        return []
    out = []
    for entry in entries:
        meta = entry.get("metadata") or {}
        if meta.get("bot_live_delivery_consumer") is not True or not meta.get("live_session_id"):
            continue
        out.append({
            "profile_home": str(home),
            "session_id": entry.get("session_id"),
            "lease_id": entry.get("lease_id"),
            "live_session_id": meta["live_session_id"],
            "surface": entry.get("surface"),
            "started_at": entry.get("started_at") or 0,
        })
    out.sort(key=lambda e: e.get("started_at") or 0, reverse=True)
    return out


def session_title(pid: str, session_id: str) -> str:
    """Best-effort title for a session id (matching a live session by name)."""
    try:
        from hermes_state import SessionDB  # type: ignore
    except Exception:
        return ""
    home = profile_dir(pid)
    if not (home / "state.db").is_file():
        return ""
    try:
        db = SessionDB(db_path=home / "state.db", read_only=True)
        try:
            row = db.get_session(session_id)
            return str((row or {}).get("title") or "")
        finally:
            db.close()
    except Exception:
        return ""


def pick_live(pid: str, want_title: str = "") -> tuple[Optional[dict], str]:
    """Choose the live session for lane 'existing'.

    No title given  -> the most recent advertised live session.
    A title given   -> the live session with that title; else (None, reason) so
                       the caller can fall back to the named-session lane.
    """
    sessions = live_sessions(pid)
    if not sessions:
        return None, "no advertised live session"
    if not want_title:
        return sessions[0], ""
    low = want_title.strip().lower()
    for owner in sessions:
        title = session_title(pid, str(owner.get("session_id") or ""))
        if title and title.strip().lower() == low:
            return owner, ""
    return None, f"no live session titled '{want_title}'"


def any_live() -> bool:
    """Cheap probe: is ANY bot on this install advertising a live session?"""
    return any(live_sessions(pid) for pid in roster())


# ── cards ────────────────────────────────────────────────────────────────────

def card(pid: str) -> AgentCard:
    """A bot's capability card. Permissive default when absent or malformed."""
    import json

    path = profile_dir(pid) / "b2b-card.json"
    if not path.is_file():
        return AgentCard()
    try:
        return AgentCard.from_dict(json.loads(path.read_text(encoding="utf-8-sig")))
    except Exception:
        logger.debug("unreadable b2b-card.json for %s", pid, exc_info=True)
        return AgentCard()


def card_line(pid: str, name: str = "") -> str:
    """One roster line: label, live state, card summary."""
    label = pid + (f" ({name})" if name and name.lower() != pid.lower() else "")
    sessions = live_sessions(pid)
    live = f"live x{len(sessions)}" if sessions else "idle"
    return f"  - {label}  [Bot Chat: {live}]{card(pid).summary()}"
