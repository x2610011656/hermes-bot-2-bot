"""TaskStore — the durable task lifecycle, wrapping Hermes' own mailbox.

The mailbox (``tools.bot_live_delivery``) already implements exactly the
semantics a task ledger needs: a private record advancing queued -> claimed ->
terminal under a process-shared lock, with permanent receipts that survive a
crash and never execute the same input twice.

This module does NOT reimplement that. It is the one place that talks to it, so
every other module depends on a small local interface instead of Hermes
internals:

    admit(...)      create a pending task (idempotent by id)
    read(...)       inspect one task without waiting
    recent(...)     newest-first tasks for a bot
    cancel(...)     cancel a still-live task
    wait(...)       block for a terminal receipt (bounded)

If Hermes' mailbox API moves, only this file changes.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from .model import Author, Lane, Reason, Task, TaskState, B2BError

logger = logging.getLogger(__name__)

DELIVERY_DIR = ("runtime", "bot_live_delivery")


def _mailbox():
    """The mailbox module. Imported lazily so a missing dependency surfaces as a
    clean refusal, not an import-time crash of the whole plugin."""
    try:
        from tools import bot_live_delivery as live  # type: ignore
        return live
    except Exception as exc:  # pragma: no cover — defensive
        logger.error("bot_live_delivery unavailable: %s", exc)
        return None


class TaskStore:
    """One profile's delivery ledger."""

    def __init__(self, profile_home: Path):
        self.home = Path(profile_home)

    # ── write ────────────────────────────────────────────────────────────────

    def admit(self, owner: dict, message: str, *, task_id: Optional[str] = None,
              author: Optional[Author] = None) -> str:
        """Create (or idempotently re-read) a pending task pinned to ``owner``.

        Returns the task id. Raises B2BError if the mailbox is unavailable.
        """
        live = _mailbox()
        if live is None:
            raise B2BError(Reason.SPAWN_FAILED, "The delivery mailbox is unavailable.")
        key = task_id or Task.new_id()
        try:
            live.deliver_to_live_owner(
                self.home, owner, message, delivery_id=key,
                author=(author.to_dict() if author else None),
            )
        except Exception as exc:
            raise B2BError(Reason.SPAWN_FAILED, f"Could not admit the delivery: {exc}")
        return key

    def cancel(self, task_id: str) -> TaskState:
        """Cancel a live task. Refuses once the task is terminal."""
        live = _mailbox()
        if live is None:
            raise B2BError(Reason.SPAWN_FAILED, "The delivery mailbox is unavailable.")
        current = self.read(task_id)
        if current is None:
            raise B2BError(Reason.NOT_FOUND, f"No delivery receipt '{task_id}'.")
        if current.state.is_terminal:
            raise B2BError(
                Reason.NOT_CANCELLABLE,
                f"Delivery {task_id} is already '{current.state.value}'.",
            )
        try:
            rec = live.cancel_queued_delivery(
                self.home, task_id,
                error="Cancelled by the sender.", reason="cancelled_by_sender",
            )
        except Exception as exc:
            raise B2BError(Reason.SPAWN_FAILED, f"Cancel failed: {exc}")
        return Task.from_receipt(rec or {}, current.bot).state

    # ── read ─────────────────────────────────────────────────────────────────

    def read(self, task_id: str, bot: str = "") -> Optional[Task]:
        """One task by id, or None. Never raises."""
        live = _mailbox()
        if live is None:
            return None
        try:
            rec = live.read_delivery_result(self.home, task_id)
        except Exception:
            return None
        return Task.from_receipt(rec, bot) if rec is not None else None

    def recent(self, bot: str = "", limit: int = 10) -> list[Task]:
        """Newest-first tasks for this profile. Never raises."""
        root = self.home.joinpath(*DELIVERY_DIR)
        if not root.is_dir():
            return []
        tasks: list[Task] = []
        for path in root.glob("*.json"):
            try:
                rec = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if isinstance(rec, dict):
                tasks.append(Task.from_receipt(rec, bot))
        tasks.sort(key=lambda t: t.created_at or 0, reverse=True)
        return tasks[:limit]

    def has_mailbox(self) -> bool:
        """Cheap pre-check: no mailbox directory means nothing was ever admitted."""
        live = _mailbox()
        if live is None:
            return False
        try:
            return bool(live.has_mailbox(self.home))
        except Exception:
            return False

    def wait(self, task_id: str, timeout: float, bot: str = "") -> Task:
        """Block until the task is terminal or ``timeout`` lapses.

        Returns the last state read — still pending when the budget lapsed, so
        the caller can report honestly rather than claiming a delivery landed.
        """
        live = _mailbox()
        if live is None:
            raise B2BError(Reason.SPAWN_FAILED, "The delivery mailbox is unavailable.")
        try:
            rec = live.await_delivery(self.home, task_id, timeout)
        except Exception as exc:
            raise B2BError(Reason.SPAWN_FAILED, f"Waiting on the delivery failed: {exc}")
        if rec is None:
            raise B2BError(Reason.NOT_FOUND, f"No delivery receipt '{task_id}'.")
        return Task.from_receipt(rec, bot)
