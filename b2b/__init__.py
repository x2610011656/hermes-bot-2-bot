"""B2B — bot-to-bot messaging for a local Hermes fleet.

A downscaled A2A architecture, all local:

    model.py      vocabulary — Message, Task, TaskState, AgentCard, errors
    guard.py      guardrails — allowed lanes, size caps, target policy
    ledger.py     TaskStore — the durable task lifecycle (queued→claimed→terminal)
    transport.py  delivery — live-owner lane + named-session lane
    roster.py     discovery — which bots exist, their cards, their live sessions
    cards.py      AgentCard load/save
    service.py    the service — lanes, fan-out, card gate; what the tools call
    tools.py      tool schemas + handlers

Nothing here reimplements Bot Mode: the transport reuses Hermes' own
``tools.bot_live_delivery`` mailbox and its ``hermes -p <bot> chat`` argv.
"""
from __future__ import annotations

__all__ = ["register"]


def register(ctx) -> None:
    """Register the b2b toolset — no config gate: local profiles are always reachable."""
    from .tools import TOOLS

    for name, (handler, description, properties, required) in TOOLS.items():
        parameters: dict = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(
            name=name, toolset="bot-to-bot", handler=handler, description=description,
            schema={"name": name, "description": description, "parameters": parameters},
            emoji="🤝",
        )
