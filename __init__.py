"""Bot-to-Bot (B2B) — bot-to-bot messaging for a local Hermes fleet.

A downscaled A2A architecture, all local. The plugin root is a shim; the
implementation lives in the ``b2b`` package beside it:

    b2b/model.py      vocabulary — Message, Task, TaskState, AgentCard, errors
    b2b/guard.py      guardrails — allowed lanes, size caps, target policy
    b2b/ledger.py     TaskStore — the durable task lifecycle
    b2b/transport.py  delivery — live-owner lane + named-session lane
    b2b/roster.py     discovery — which bots exist, their cards, live sessions
    b2b/cards.py      AgentCard read/write
    b2b/service.py    the service — lanes, fan-out, card gate
    b2b/tools.py      tool schemas + handlers

Design rules this package holds to:

- **The default lane is the target's Bot Chat.** ``session='new'`` and
  ``session='existing'`` are opt-in, passed only when the user asks for them.
- **Nothing reimplements Bot Mode.** The transport reuses Hermes' own
  ``tools.bot_live_delivery`` mailbox and its ``hermes -p <bot> chat`` argv.
- **One module owns each concern.** Delivery logic is in transport; policy is in
  guard; the lifecycle is in ledger; only service decides what to do.
- **Guardrails fail closed.** A refused call spawns nothing.
"""
from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:
    """Register the b2b toolset — no config gate: local profiles are always reachable."""
    from .b2b.tools import TOOLS

    for name, (handler, description, properties, required) in TOOLS.items():
        parameters: dict = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(
            name=name, toolset="bot-to-bot", handler=handler, description=description,
            schema={"name": name, "description": description, "parameters": parameters},
            emoji="🤝",
        )
