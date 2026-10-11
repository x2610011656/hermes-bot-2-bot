"""Vocabulary — the data model, downscaled from A2A's canonical types.

A2A has Task / Message / Part / Artifact / AgentCard / Extension. Locally the
useful subset is:

    TaskState   the lifecycle enum (A2A: submitted/working/input-required/
                completed/canceled/failed; here mapped onto the mailbox's
                queued/claimed/settled/cancelled/failed so the two agree)
    Message     one outbound message (A2A: role + parts; here: text + author)
    Task        one delivery (A2A: id + state + artifacts; here: a receipt)
    AgentCard   a bot's declared identity + capabilities
    Lane        which session a message targets
    B2BError    a structured refusal (reason code + human meaning)
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional

# ── lifecycle ────────────────────────────────────────────────────────────────

class TaskState(str, Enum):
    """Delivery lifecycle. The first three mirror the mailbox's own states.

    ``PENDING`` is the local name for A2A's ``submitted``; ``WORKING`` for
    ``working``/``input-required``. The terminal set is what a receipt can end
    in, and what ``b2b_cancel`` refuses to touch.
    """
    PENDING = "queued"          # admitted, not yet picked up
    WORKING = "claimed"         # a consumer owns it
    DONE = "settled"            # finished with a reply
    CANCELLED = "cancelled"
    FAILED = "failed"

    @classmethod
    def terminal(cls) -> frozenset["TaskState"]:
        return frozenset({cls.DONE, cls.CANCELLED, cls.FAILED})

    @classmethod
    def live(cls) -> frozenset["TaskState"]:
        """States a delivery can still be cancelled from."""
        return frozenset({cls.PENDING, cls.WORKING})

    @property
    def is_terminal(self) -> bool:
        return self in self.terminal()


# ── lanes ────────────────────────────────────────────────────────────────────

class Lane(str, Enum):
    """Which session a message targets.

    ``BOT`` is the default and the only lane that works on every surface: it
    relays into the target's canonical Bot Chat. The other two are opt-in and
    exist so a caller can reach a different session on purpose.
    """
    BOT = "bot"              # the target's canonical Bot Chat (DEFAULT)
    NEW = "new"              # a fresh session
    EXISTING = "existing"    # a pre-existing session (live if open, else named)

    @classmethod
    def parse(cls, value: Any) -> Optional["Lane"]:
        try:
            return cls(str(value or cls.BOT.value).strip().lower())
        except ValueError:
            return None


DEFAULT_LANE = Lane.BOT

# ── errors ───────────────────────────────────────────────────────────────────

class Reason(str, Enum):
    """Structured refusal codes. Aligned with tools/bot_failure_reasons.py
    where they map, so a caller sees the same vocabulary across both."""
    BAD_TARGET = "bad_target"
    BAD_MESSAGE = "bad_message"
    BAD_LANE = "bad_lane"
    BAD_SESSION = "bad_session"
    TARGET_BUSY = "target_busy"
    NO_LIVE_OWNER = "no_live_owner"
    SPAWN_FAILED = "spawn_failed"
    ASYNC_UNSUPPORTED = "async_unsupported"
    NOT_FOUND = "not_found"
    NOT_CANCELLABLE = "not_cancellable"
    LANE_UNSUPPORTED = "lane_unsupported"
    DENIED = "denied"
    TOO_LARGE = "too_large"


MEANINGS: dict[Reason, str] = {
    Reason.BAD_TARGET: "No such bot on this install.",
    Reason.BAD_MESSAGE: "Message missing or too long.",
    Reason.BAD_LANE: "Unknown session lane; use 'bot', 'new', or 'existing'.",
    Reason.BAD_SESSION: "session='existing' needs a session name, or a live session to target.",
    Reason.TARGET_BUSY: "That bot's Bot Chat is open on another surface right now.",
    Reason.NO_LIVE_OWNER: "That bot has no live session open (the live lane is desktop/TUI only).",
    Reason.SPAWN_FAILED: "The delivery process could not be started.",
    Reason.ASYNC_UNSUPPORTED: "This session can't receive an async reply; use mode='wait'.",
    Reason.NOT_FOUND: "No delivery receipt with that id.",
    Reason.NOT_CANCELLABLE: "That delivery already reached a terminal state.",
    Reason.LANE_UNSUPPORTED: "That bot's card does not accept this lane.",
    Reason.DENIED: "The policy refused this delivery.",
    Reason.TOO_LARGE: "The message exceeds the size limit.",
}


class B2BError(Exception):
    """A structured refusal. ``to_json`` is what tools return to the caller."""

    def __init__(self, reason: Reason, detail: str = ""):
        self.reason = reason
        self.detail = detail or MEANINGS.get(reason, "")
        super().__init__(self.detail)

    def to_json(self) -> str:
        return json.dumps({
            "error": self.detail,
            "reason": self.reason.value,
            "meaning": MEANINGS.get(self.reason, ""),
        })


def err(reason: Reason, detail: str = "") -> str:
    """Build a refusal payload (the tools' return shape)."""
    return B2BError(reason, detail).to_json()


# ── messages and tasks ───────────────────────────────────────────────────────

@dataclass
class Author:
    """Who sent a message. A2A carries a role; locally we carry identity so the
    recipient can attribute it."""
    id: str
    name: str
    is_bot: bool = True

    @classmethod
    def bot(cls, profile: str) -> "Author":
        return cls(id=f"bot:{profile}", name=profile, is_bot=True)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Message:
    """One outbound message. A2A's Message is role + parts; locally it is text
    plus the author, which is all a local fleet exchanges."""
    text: str
    author: Optional[Author] = None
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        data = {"text": self.text, "created_at": self.created_at}
        if self.author:
            data["author"] = self.author.to_dict()
        return data


@dataclass
class Task:
    """One delivery. A2A's Task carries id/state/artifacts; this mirrors it over
    the mailbox receipt so a caller has one shape to read."""
    id: str
    state: TaskState
    bot: str
    lane: Lane = DEFAULT_LANE
    session_title: str = ""
    session_id: Optional[str] = None
    message: str = ""
    reply: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[int] = None
    completed_at: Optional[int] = None

    @classmethod
    def new_id(cls) -> str:
        return uuid.uuid4().hex

    @classmethod
    def from_receipt(cls, rec: dict, bot: str) -> "Task":
        """Build a Task from a mailbox receipt (the durable record)."""
        raw = str(rec.get("status") or TaskState.PENDING.value)
        try:
            state = TaskState(raw)
        except ValueError:
            state = TaskState.PENDING
        return cls(
            id=str(rec.get("delivery_id") or rec.get("id") or ""),
            state=state,
            bot=bot,
            session_id=rec.get("session_id"),
            message=str(rec.get("message") or ""),
            reply=(str(rec["reply"]) if rec.get("reply") else None),
            error=(str(rec["error"]) if rec.get("error") else None),
            created_at=rec.get("created_at"),
            completed_at=rec.get("completed_at"),
        )

    def to_dict(self) -> dict:
        msg = self.message
        return {
            "bot": self.bot,
            "delivery_id": self.id,
            "status": self.state.value,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "message": (msg[:160] + "…") if len(msg) > 160 else msg,
            "reply": (self.reply[:400] if self.reply else None),
            "error": self.error,
        }


# ── agent cards ──────────────────────────────────────────────────────────────

@dataclass
class AgentCard:
    """A bot's declared identity and capabilities — A2A's Agent Card, downscaled.

    Read from ``<profile>/b2b-card.json``. An absent or malformed card yields a
    permissive default: the bot accepts every lane and declares nothing.
    """
    name: str = ""
    description: str = ""
    lanes: tuple[str, ...] = ()          # empty = accepts every lane
    capabilities: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Any) -> "AgentCard":
        if not isinstance(data, dict):
            return cls()
        lanes = data.get("lanes")
        caps = data.get("capabilities")
        return cls(
            name=str(data.get("name") or "").strip(),
            description=str(data.get("description") or "").strip(),
            lanes=tuple(str(x) for x in lanes) if isinstance(lanes, list) else (),
            capabilities=tuple(str(x) for x in caps) if isinstance(caps, list) else (),
            raw=data,
        )

    def accepts(self, lane: Lane) -> bool:
        """An undeclared card accepts everything; a declared one is a gate."""
        return not self.lanes or lane.value in self.lanes

    @property
    def declared(self) -> bool:
        return bool(self.lanes)

    def summary(self) -> str:
        """One-line rendering for b2b_list."""
        parts = []
        if self.description:
            parts.append(self.description)
        if self.lanes:
            parts.append(f"[lanes: {', '.join(self.lanes)}]")
        if self.capabilities:
            parts.append(f"[capabilities: {', '.join(self.capabilities)}]")
        return (" — " + "  ".join(parts)) if parts else ""
