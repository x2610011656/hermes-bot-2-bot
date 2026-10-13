"""Tools — the agent-facing surface. Schemas and handlers, nothing else.

Every handler is a thin adapter: parse arguments, call the service, turn a
B2BError into the structured refusal shape. No delivery logic lives here.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import cards, guard, roster, service
from .model import DEFAULT_LANE, B2BError, Lane, Reason, err

logger = logging.getLogger(__name__)


def _guard(fn: Callable[[], str]) -> str:
    """Run a handler, converting a refusal into its JSON payload."""
    try:
        return fn()
    except B2BError as exc:
        return exc.to_json()
    except Exception as exc:  # pragma: no cover — a handler must never raise
        logger.error("b2b tool failed: %s", exc, exc_info=True)
        return err(Reason.SPAWN_FAILED, f"Unexpected failure: {exc}")


def _trusted_sender() -> str:
    """Who is really running this turn, from TRUSTED runtime context only.

    This replaces the old ``_true_sender`` which accepted a caller-supplied
    ``sender``/``from`` argument as proof of identity. That was a spoofing hole:
    any bot could pass sender='marnie' and the message would be attributed to
    marnie. Identity is not something a caller gets to assert.

    We resolve it from the one source that cannot be forged by the caller:
    the profile this process is actually running under. Under the multiplexed
    gateway that reads 'default' for every bot, which we CANNOT disambiguate —
    so we return '' (unknown) rather than guess. Callers then label the message
    'a local bot' / omit attribution instead of asserting a possibly-wrong name.

    There is deliberately NO argument-based override. If a future Hermes version
    exposes a trusted per-turn identity, resolve it here — never from args.
    """
    me = roster.self_profile()
    # 'default' here means the multiplexer's shared mask, not a real identity:
    # every profile's tool call resolves to it, so it proves nothing about who
    # is actually calling. Return unknown rather than mislabel everyone 'default'.
    return "" if me == "default" else me


# ── handlers ─────────────────────────────────────────────────────────────────

def b2b_call(args: dict) -> str:
    """Message one or more bots. Default lane is the target's Bot Chat."""
    def run() -> str:
        raw_target = str(args.get("target") or args.get("bot") or args.get("agent") or "").strip()
        message = str(args.get("message") or args.get("msg") or "").strip()
        mode = str(args.get("mode") or "async").strip().lower()
        lane = Lane.parse(args.get("session") or args.get("lane"))
        session_name = str(args.get("session_name") or args.get("session_title") or "").strip()

        if not raw_target or not message:
            raise B2BError(Reason.BAD_MESSAGE, "Both 'target' and 'message' are required.")
        if lane is None:
            raise B2BError(Reason.BAD_LANE,
                           "Unknown session lane. Use 'bot', 'new', or 'existing'.")
        if lane is Lane.EXISTING and not session_name and not roster.any_live():
            raise B2BError(Reason.BAD_SESSION)

        return service.call(
            target=raw_target, message=message, lane=lane,
            session_name=session_name,
            sender=_trusted_sender(),
            wait=mode in ("wait", "block", "sync", "blocking"),
        )
    return _guard(run)


def b2b_list(args: dict | None = None) -> str:
    """The roster: every reachable bot, its live state, and its card."""
    return _guard(service.list_bots)


def b2b_send(args: dict) -> str:
    """Send a message AS a bot (its own profile and token) to a channel."""
    return _guard(lambda: service.send_as(
        target=str(args.get("target") or args.get("bot") or "").strip(),
        channel=str(args.get("channel") or args.get("to") or "").strip(),
        message=str(args.get("message") or "").strip(),
        sender=_trusted_sender(),
    ))


def b2b_status(args: dict) -> str:
    """Read a delivery's lifecycle state, or a bot's recent deliveries."""
    def run() -> str:
        try:
            limit = int(args.get("limit") or 10)
        except (TypeError, ValueError):
            limit = 10
        return service.status(
            delivery_id=str(args.get("delivery_id") or args.get("id") or "").strip(),
            target=str(args.get("target") or args.get("bot") or "").strip(),
            limit=limit,
        )
    return _guard(run)


def b2b_cancel(args: dict) -> str:
    """Cancel a delivery that has not yet reached a terminal state."""
    return _guard(lambda: service.cancel(
        delivery_id=str(args.get("delivery_id") or args.get("id") or "").strip(),
        target=str(args.get("target") or args.get("bot") or "").strip(),
    ))


def b2b_card(args: dict) -> str:
    """Read or write a bot's capability card (AgentCard)."""
    def run() -> str:
        pid = roster.resolve(str(args.get("target") or args.get("bot") or "").strip())
        if not pid:
            raise B2BError(Reason.BAD_TARGET,
                           f"No bot named '{args.get('target') or args.get('bot')}'.")
        action = str(args.get("action") or "get").strip().lower()

        if action == "get":
            card = cards.read(pid)
            return _card_json(pid, card)
        if action == "set":
            lanes = args.get("lanes")
            caps = args.get("capabilities")
            if isinstance(lanes, list):
                unknown = [x for x in lanes if str(x).lower() not in cards.valid_lanes()]
                if unknown:
                    raise B2BError(
                        Reason.BAD_LANE,
                        f"Unknown lane(s): {', '.join(map(str, unknown))}. "
                        f"Valid: {', '.join(cards.valid_lanes())}.",
                    )
            path = cards.write(
                pid,
                name=str(args.get("name") or "").strip(),
                description=str(args.get("description") or "").strip(),
                lanes=lanes if isinstance(lanes, list) else None,
                capabilities=caps if isinstance(caps, list) else None,
            )
            return _card_json(pid, cards.read(pid), written=str(path))
        if action == "clear":
            removed = cards.remove(pid)
            return _card_json(pid, cards.read(pid), removed=removed)
        raise B2BError(Reason.BAD_MESSAGE, f"Unknown action '{action}'. Use get/set/clear.")
    return _guard(run)


def _card_json(pid: str, card, **extra: Any) -> str:
    import json
    data = {
        "bot": pid,
        "name": card.name,
        "description": card.description,
        "lanes": list(card.lanes) or "all",
        "capabilities": list(card.capabilities),
        "accepts_every_lane": not card.declared,
    }
    data.update(extra)
    return json.dumps(data, indent=1, ensure_ascii=False)


def b2b_policy(args: dict | None = None) -> str:
    """Show the resolved guardrail policy for this install."""
    import json
    return _guard(lambda: json.dumps(
        guard.load(roster.hermes_home()).describe(), indent=1, ensure_ascii=False))


# ── schemas ──────────────────────────────────────────────────────────────────

TOOLS: dict[str, tuple] = {
    "b2b_call": (
        b2b_call,
        "Message one or more bots (Hermes profiles) and get their replies — bot-to-bot "
        "messaging that works from ANY session, including regular Sessions mode where Bot "
        "Mode's message_agent is unavailable. DEFAULT session lane is the target's canonical "
        "Bot Chat; pass session='new' or session='existing' ONLY when the user explicitly "
        "asks for a different session. FIRE-AND-FORGET by default: returns a 'queued' ack "
        "and the reply arrives later as a completion notification — do NOT wait or poll. "
        "Pass mode='wait' only on surfaces that can't receive async replies (cron/one-shot). "
        "target: a bot name, comma-separated names ('bravo, marnie'), or '*'/'all'. "
        "Use b2b_list to see reachable bots.",
        {
            "target": {"type": "string", "description": "Bot(s): profile id, display name, or Bot Mode title. Comma-separate for several; '*' or 'all' for every bot."},
            "message": {"type": "string", "description": "The message to send."},
            "mode": {"type": "string", "enum": ["async", "wait"], "description": "'async' (default) = fire-and-forget, reply via notification. 'wait' = block and return the reply (cron/one-shot only)."},
            "session": {"type": "string", "enum": ["bot", "new", "existing"], "description": "Session lane. 'bot' (DEFAULT) = the target's Bot Chat. 'new' = a fresh session. 'existing' = a pre-existing session (the live one if open, else the named one)."},
            "session_name": {"type": "string", "description": "Session title for session='new' or session='existing'."},
        },
        ["target", "message"],
    ),
    "b2b_list": (
        b2b_list,
        "List every bot reachable via b2b_call: profile id, friendly name, live-session state, "
        "and the bot's capability card (description, accepted lanes, capabilities).",
        {},
        [],
    ),
    "b2b_send": (
        b2b_send,
        "Send a message AS a bot (from that bot's own profile and token) to a channel — "
        "e.g. deliver from bravo to telegram:<chat_id>.",
        {
            "target": {"type": "string", "description": "Bot to send as: profile id, display name, or title."},
            "channel": {"type": "string", "description": "Destination, e.g. 'telegram:123456789'."},
            "message": {"type": "string", "description": "Message body."},
        },
        ["target", "channel", "message"],
    ),
    "b2b_status": (
        b2b_status,
        "Read a delivery's lifecycle state (queued/claimed/settled/failed/cancelled). Pass "
        "delivery_id for one receipt, or target for that bot's recent deliveries. Use to "
        "check whether a fire-and-forget message was delivered, and what it answered.",
        {
            "delivery_id": {"type": "string", "description": "Exact receipt id."},
            "target": {"type": "string", "description": "Bot name; lists its recent receipts when delivery_id is omitted."},
            "limit": {"type": "integer", "description": "How many recent receipts to list (default 10)."},
        },
        [],
    ),
    "b2b_cancel": (
        b2b_cancel,
        "Cancel a delivery that has not yet been claimed or settled. No effect on a delivery "
        "that already reached a terminal state.",
        {
            "delivery_id": {"type": "string", "description": "The receipt id to cancel."},
            "target": {"type": "string", "description": "Bot owning the receipt (searched fleet-wide if omitted)."},
        },
        ["delivery_id"],
    ),
    "b2b_card": (
        b2b_card,
        "Read or write a bot's capability card: what it does, and which session lanes it "
        "accepts. action='get' reads, 'set' writes, 'clear' removes. A bot with no card "
        "accepts every lane; declaring lanes makes the card a gate on incoming deliveries.",
        {
            "target": {"type": "string", "description": "The bot whose card to read or write."},
            "action": {"type": "string", "enum": ["get", "set", "clear"], "description": "'get' (default) reads; 'set' writes; 'clear' removes the card."},
            "name": {"type": "string", "description": "Card: the bot's declared name."},
            "description": {"type": "string", "description": "Card: what this bot is for."},
            "lanes": {"type": "array", "items": {"type": "string"}, "description": "Card: session lanes this bot accepts ('bot','new','existing'). Omit to accept every lane."},
            "capabilities": {"type": "array", "items": {"type": "string"}, "description": "Card: what this bot can do."},
        },
        ["target"],
    ),
    "b2b_policy": (
        b2b_policy,
        "Show the resolved guardrail policy for this install (allowed/denied targets, "
        "allowed lanes, message size cap, fan-out).",
        {},
        [],
    ),
}
