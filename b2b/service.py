"""Service — the one place that decides how a call is executed.

Everything above this module is mechanism (vocabulary, guardrails, ledger,
transport, discovery). This is policy: which lane, which targets, what order,
and what the caller gets back.

    call(...)     message one or more bots, on a chosen lane
    list_bots()   the roster, with cards and live state
    status(...)   read the task ledger
    cancel(...)   cancel a live task
    send_as(...)  deliver AS a bot, on its own token, to a channel
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

from . import cards, guard, roster, transport
from .ledger import TaskStore
from .model import (DEFAULT_LANE, Author, B2BError, Lane, MEANINGS, Reason,
                    err)

logger = logging.getLogger(__name__)


# ── targeting ────────────────────────────────────────────────────────────────

def _resolve_targets(raw: str, policy: guard.Policy) -> list[str]:
    """Turn the caller's target string into canonical profile ids.

    '*' / 'all' fans out to the whole fleet except this profile. Named targets
    accept a profile id, a display name, or a Bot Mode title.
    """
    known = roster.roster()
    me = roster.self_profile()

    # Fan-out to every bot except self (never message yourself in a broadcast).
    if raw.strip().lower() in ("*", "all"):
        pids = [p for p in sorted(known) if p != me]
        if not pids:
            raise B2BError(Reason.BAD_TARGET, "No other bots to reach.")
        policy.check_fanout(pids)
        for pid in pids:
            policy.check_target(pid)
        return pids

    # Explicitly named targets — a profile id, display name, or title. Self is
    # allowed here on purpose: naming 'default' explicitly is a deliberate
    # reach, not a loop. Only fan-out above excludes self.
    pids: list[str] = []
    for part in raw.split(","):
        pid = roster.resolve(part)
        if not pid:
            raise B2BError(
                Reason.BAD_TARGET,
                f"No bot named '{part.strip()}'. Known: {', '.join(sorted(known))}",
            )
        if pid not in pids:
            pids.append(pid)
    if not pids:
        raise B2BError(Reason.BAD_TARGET, "No targets after resolving the roster.")
    policy.check_fanout(pids)
    for pid in pids:
        policy.check_target(pid)
    return pids


def _apply_cards(pids: Iterable[str], lane: Lane) -> tuple[list[str], list[str]]:
    """Split targets into (allowed, declined) by their declared cards."""
    allowed, declined = [], []
    for pid in pids:
        card = cards.read(pid)
        if card.accepts(lane):
            allowed.append(pid)
        else:
            declined.append(f"{pid} (accepts: {', '.join(card.lanes)})")
    return allowed, declined


# ── the call ─────────────────────────────────────────────────────────────────

def call(*, target: str, message: str, lane: Lane = DEFAULT_LANE,
         session_name: str = "", sender: str = "", wait: bool = False,
         policy: Optional[guard.Policy] = None) -> str:
    """Message one or more bots and return the caller-facing result.

    The lane decides the mechanism; everything else is the same on every lane.
    ``sender`` is the real originating profile, threaded from the tool call so
    attribution never falls back to the multiplexer's shared 'default' mask.
    Failures are per-target, so one unreachable bot does not lose the others.
    """
    policy = policy or guard.load(roster.hermes_home())

    # Guardrails first — a refused call spawns nothing.
    policy.check_message(message)
    policy.check_lane(lane)

    pids = _resolve_targets(target, policy)
    pids, declined = _apply_cards(pids, lane)
    if declined and not pids:
        raise B2BError(
            Reason.LANE_UNSUPPORTED,
            f"No target accepts session='{lane.value}'. Declined by: {'; '.join(declined)}",
        )

    signed = _sign(pids, message, sender)
    out = [_one(pid, signed, lane, session_name, wait, policy) for pid in pids]
    if declined:
        out.append(err(Reason.LANE_UNSUPPORTED,
                       f"Skipped (lane not accepted): {'; '.join(declined)}"))
    return "\n\n".join(out)


def _sign(pids: list[str], message: str, sender: str = "") -> str:
    """Attribution prefix so recipients know who is talking (parity with
    Bot Mode's ``message_agent``).

    ``sender`` is threaded from the call. When it is empty (unknown under the
    multiplexer), the prefix states the message is from an unnamed local bot
    rather than asserting a wrong identity — a wrong "from" is worse than an
    honest "unnamed".
    """
    who = f"{sender} (@{sender})" if sender else "a local bot"
    return f"Message from 🤖 {who} → {', '.join(pids)}: {message}"


def _one(pid: str, message: str, lane: Lane, session_name: str,
         wait: bool, policy: guard.Policy) -> str:
    """Execute one delivery for one bot, on the chosen lane."""
    if lane is Lane.EXISTING:
        owner, note = roster.pick_live(pid, session_name)
        if owner is not None:
            return _via_live(pid, owner, message, wait)
        if not session_name:
            # No live session and nothing named: say so plainly instead of
            # silently creating a session the caller did not ask for.
            return err(
                Reason.NO_LIVE_OWNER,
                f"{MEANINGS[Reason.NO_LIVE_OWNER]} Pass session_name= to target a named "
                f"session instead, or use session='bot'.",
            )
        return _via_named(pid, session_name, message, wait, policy)

    if lane is Lane.NEW:
        title = session_name or f"B2B {_stamp()}"
        return _via_named(pid, title, message, wait, policy)

    # Lane.BOT — the target's canonical Bot Chat, and the only lane that works on
    # every surface. Prefer the LIVE owner when one exists: the mailbox
    # correlates the reply to the delivery id, so a reply can never be mistaken
    # for another turn's. A CLI turn into a session that is already live would
    # interleave with its queue and return an uncorrelated answer, so that path
    # is the FALLBACK, for bots with no live session (Telegram-gateway bots).
    owner, _ = roster.pick_live(pid, "Bot Chat")
    if owner is not None:
        return _via_live(pid, owner, message, wait)
    return _via_named(pid, "Bot Chat", message, wait, policy)


def _via_live(pid: str, owner: dict, message: str, wait: bool = True) -> str:
    """Lane on a live session: admit, then wait for the correlated reply (or ack)."""
    store = TaskStore(roster.profile_dir(pid))
    try:
        task_id = transport.live_owner(
            pid, owner, message, author=Author.bot(roster.self_profile()))
    except B2BError as exc:
        return exc.to_json()

    if not wait:
        return transport.ack(pid, _session_label(pid, owner),
                             delivery_id=task_id)

    try:
        task = transport.wait_live(pid, task_id, transport.LIVE_WAIT_SECONDS)
    except B2BError as exc:
        return exc.to_json()

    label = _session_label(pid, owner)
    if task.state.value == "settled":
        return f"[b2b → {pid} · {label}]\n{task.reply or '(no reply)'}"
    if not task.state.is_terminal:
        return (f"[b2b → {pid} · {label}]\nMessage handed to the live session; "
                f"reply not yet settled: {task.state.value}. (id {task_id})")
    return (f"[b2b → {pid} · {label}] {task.state.value}: "
            f"{task.error or '(no detail)'} (id {task_id})")


def _session_label(pid: str, owner: dict) -> str:
    """A human label for a live session (its title, else 'live session')."""
    title = roster.session_title(pid, str(owner.get("session_id") or ""))
    return f"{title} (live)" if title else "live session"


def _via_named(pid: str, title: str, message: str, wait: bool,
               policy: guard.Policy) -> str:
    """Named-session lane: block for the reply, or hand it to a background process.

    The async path hands the turn to a tracked background process. This lane is
    for targets with no live owner (e.g. Telegram-gateway bots), so there is no
    mailbox owner to admit a durable receipt to — the durable handle IS the
    background process id, and the ack says so instead of implying a receipt
    exists (the old code minted a fresh Task.new_id() that was never written
    anywhere, so b2b_status could only ever report it not_found).
    """
    if wait:
        ok, out = transport.run_named(pid, title, message)
        if ok:
            return f"[b2b → {pid} · {title}]\n{out}"
        return f"[b2b → {pid} · {title}] Error: could not reach that session. {out}"

    try:
        parsed = transport.spawn_named(pid, title, message, policy.max_message_chars)
    except B2BError as exc:
        return exc.to_json()
    return transport.ack(pid, title, spawn=parsed)


def _stamp() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M")


# ── read paths ───────────────────────────────────────────────────────────────

def list_bots() -> str:
    """The roster: every bot, its live state, and its card."""
    known = roster.roster()
    lines = [roster.card_line(pid, name) for pid, name in sorted(known.items())]
    body = "\n".join(lines) if lines else "  (none found)"
    return f"Bots reachable via b2b_call:\n{body}"


def status(*, delivery_id: str = "", target: str = "", limit: int = 10) -> str:
    """One task by id, or a bot's recent tasks. Fleet-wide when no target given."""
    if delivery_id:
        for pid in roster.roster():
            task = TaskStore(roster.profile_dir(pid)).read(delivery_id, bot=pid)
            if task is not None:
                return json.dumps(task.to_dict(), indent=1, ensure_ascii=False)
        raise B2BError(Reason.NOT_FOUND, f"No delivery receipt '{delivery_id}' on any profile.")

    if target:
        pid = roster.resolve(target)
        if not pid:
            raise B2BError(Reason.BAD_TARGET, f"No bot named '{target}'.")
        pids = [pid]
    else:
        pids = sorted(roster.roster())

    rows: list[dict] = []
    for pid in pids:
        rows.extend(t.to_dict() for t in TaskStore(roster.profile_dir(pid)).recent(pid, limit))
    if not rows:
        return "No delivery receipts found."
    return json.dumps(rows, indent=1, ensure_ascii=False)


def cancel(*, delivery_id: str, target: str = "") -> str:
    """Cancel a live task (no effect once terminal)."""
    if not delivery_id:
        raise B2BError(Reason.BAD_MESSAGE, "'delivery_id' is required.")
    if target:
        pid = roster.resolve(target)
        if not pid:
            raise B2BError(Reason.BAD_TARGET, f"No bot named '{target}'.")
        pids = [pid]
    else:
        pids = sorted(roster.roster())

    for pid in pids:
        store = TaskStore(roster.profile_dir(pid))
        if store.read(delivery_id, bot=pid) is None:
            continue
        state = store.cancel(delivery_id)
        return json.dumps({"cancelled": state.value == "cancelled",
                           "delivery_id": delivery_id, "bot": pid,
                           "status": state.value})
    raise B2BError(Reason.NOT_FOUND, f"No delivery receipt '{delivery_id}' on any profile.")


def send_as(*, target: str, channel: str, message: str, sender: str = "") -> str:
    """Deliver AS a bot (its own profile and token) to a channel.

    ``sender`` is the profile that triggered the send, threaded from the tool
    call so attribution reflects the real origin, not ambient process state.
    """
    if not (target and channel and message):
        raise B2BError(Reason.BAD_MESSAGE,
                       "'target', 'channel', and 'message' are all required.")
    pid = roster.resolve(target)
    if not pid:
        raise B2BError(Reason.BAD_TARGET, f"No bot named '{target}'.")
    ok, out = transport.run_as(pid, channel, message, sender=sender)
    if ok:
        return f"[b2b send as {pid} → {channel}]{f' (triggered by {sender})' if sender else ''}\n{out or 'sent.'}"
    raise B2BError(Reason.SPAWN_FAILED, f"Send as '{pid}' failed — {out}")
