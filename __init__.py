"""Bot-to-Bot (B2B) — lean bot-to-bot messaging from ANY Hermes session.

Mirrors Hermes Bot Mode's ``message_agent`` as closely as a plugin can, using
the SAME supported machinery, not a reimplementation:

- **Fire-and-forget by default.** ``b2b_call`` hands the message to a tracked
  background process via ``terminal_tool(background=True, notify_on_complete=True)``
  — the exact async path ``tools/bot_mode_dm.py`` uses. It returns a ``queued``
  ack (a dispatch hand-off, not a delivery receipt) and the turn ends. The
  recipient's reply arrives later as a completion notification that wakes the
  sender, attributed to that bot. ``mode="wait"`` opts into a blocking call for
  surfaces that can't receive async completions (cron, one-shot, api_server).
- **Same delivery plane.** Lane 1 (target Bot Chat is live) uses Hermes' own
  owner-pinned mailbox ``tools.bot_live_delivery``. Lane 2 (no live owner) uses
  the same ``hermes -p <bot> chat -c "Bot Chat" -Q`` argv Bot Mode uses. Both
  land in the target's canonical forever-chat — no new sessions.
- **Attribution + roster parity.** Messages are signed with the sender; targets
  accept a profile id, display name, or Bot Mode title, comma-separated for
  several, or ``*``/``all`` for the fleet.

This routes around Bot Mode's canonical-"Bot Chat"-only gate (open as issue
#94018). It is a personal substitute that reuses Bot Mode's own transport, not
a peer of the built-in feature.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["register"]

BOT_CHAT_TITLE = "Bot Chat"
_TOOLSET = "bot-to-bot"
_LIVE_WAIT_SECONDS = 300          # bounded so a stalled owner can't hang the wait mode
_MESSAGE_MAX_CHARS = 16000
_ROSTER_TTL_SECONDS = 5.0         # roster changes are rare; cache briefly

# Structured reasons, aligned with tools/bot_failure_reasons.py where they map.
_REASONS = {
    "bad_target": "No such bot on this install.",
    "bad_message": "Message missing or too long.",
    "target_busy": "That bot's Bot Chat is open on another surface right now.",
    "spawn_failed": "The delivery process could not be started.",
    "async_unsupported": "This session can't receive an async reply; use mode='wait'.",
}

# ── roster cache ─────────────────────────────────────────────────────────────
_roster_cache: dict[str, tuple[float, dict[str, str]]] = {}


def _hermes_home() -> Path:
    from hermes_constants import get_process_hermes_home  # type: ignore
    return Path(str(get_process_hermes_home()))


def _roster() -> dict[str, str]:
    """{profile_id: display_name} for every live profile (default + named), cached."""
    home = _hermes_home()
    key = str(home)
    now = time.monotonic()
    hit = _roster_cache.get(key)
    if hit and (now - hit[0]) < _ROSTER_TTL_SECONDS:
        return hit[1]

    out: dict[str, str] = {"default": ""}
    profiles = home / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if not entry.is_dir() or entry.name in {"sessions", "logs"}:
                continue
            if (entry / "config.yaml").is_file() or (entry / "profile.yaml").is_file():
                out[entry.name] = _display_name(entry)
    _roster_cache[key] = (now, out)
    return out


def _display_name(profile_dir: Path) -> str:
    try:
        import yaml  # type: ignore
        raw = profile_dir / "profile.yaml"
        if not raw.is_file():
            return ""
        data = yaml.safe_load(raw.read_text(encoding="utf-8-sig", errors="replace")) or {}
        ui = data.get("ui_meta") if isinstance(data, dict) else None
        bots = ui.get("hermes-bots") if isinstance(ui, dict) else None
        title = (bots or {}).get("title", "") if isinstance(bots, dict) else ""
        return str(title or data.get("display_name") or "").strip()
    except Exception:
        return ""


def _profile_dir(pid: str) -> Path:
    home = _hermes_home()
    return home if pid == "default" else home / "profiles" / pid


def _resolve(target: str) -> Optional[str]:
    """Accept a profile id, a display name, or a Bot Mode title -> canonical profile id."""
    target = target.strip()
    if not target:
        return None
    roster = _roster()
    if target in roster:
        return target
    low = target.lower()
    for pid, name in roster.items():
        if pid.lower() == low or (name and low == name.lower()):
            return pid
    return None


def _self_profile() -> str:
    home = _hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def _sign(pids: list[str], message: str) -> str:
    """Attribution prefix so recipients know who's talking (parity with message_agent)."""
    me = _self_profile()
    return f"Message from 🤖 {me} (@{me}) → {', '.join(pids)}: {message}"


# ── delivery lanes ────────────────────────────────────────────────────────────

def _deliver_live(pid: str, message: str) -> tuple[str, str]:
    """Hand the message to the target bot's LIVE canonical Bot Chat owner.
    Returns (status, reply_or_note). No new session is created either way."""
    from tools import bot_live_delivery as live  # type: ignore

    pdir = _profile_dir(pid)
    owner = live.find_canonical_live_owner(pdir)
    if owner is None:
        return "no_live_owner", ""
    delivery_id = uuid.uuid4().hex
    author = {"id": f"bot:{_self_profile()}", "name": _self_profile(), "is_bot": True}
    live.deliver_to_live_owner(pdir, owner, message, delivery_id=delivery_id, author=author)
    record = live.await_delivery(pdir, delivery_id, _LIVE_WAIT_SECONDS)
    if record is None:
        return "pending", "Message delivered to the live Bot Chat; still awaiting its reply."
    return record.get("status", "?"), record.get("reply") or record.get("error") or ""


def _hermes_cli() -> str:
    """Resolve the hermes launcher, degrading gracefully.

    Preference order:
      1. Hermes' own resolver (``tools.bot_relay._hermes_cli``) — most correct, but a
         private function, so it may move between versions.
      2. The published launcher beside the Hermes home (``<home>/bin/hermes[.exe]``).
      3. ``shutil.which("hermes")`` on PATH.
      4. Bare ``"hermes"`` as a last resort.
    Never raises: a missing launcher surfaces later as a clean spawn failure, not a crash.
    """
    try:
        from tools.bot_relay import _hermes_cli as _internal  # type: ignore
        resolved = _internal()
        if resolved:
            return resolved
    except Exception:
        logger.debug("bot_relay._hermes_cli unavailable; using fallback resolution", exc_info=True)

    import shutil
    import sys
    name = "hermes.exe" if sys.platform == "win32" else "hermes"
    published = _hermes_home() / "bin" / name
    if published.is_file():
        return str(published)
    return shutil.which("hermes") or "hermes"


def _deliver_bot_chat(pid: str, message: str, timeout: int = 240) -> tuple[bool, str]:
    """Deliver into the target's canonical 'Bot Chat' (forever-chat). No live owner:
    a one-shot turn runs there, landing in the SAME thread every time."""
    import tempfile
    fd, qf = tempfile.mkstemp(prefix="b2b_", suffix=".txt",
                              dir=str(_hermes_home() / "cache" / "scratch"))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(message)
        cmd = [_hermes_cli(), "-p", pid, "chat", "--in", "~", "-c", BOT_CHAT_TITLE,
               "--create-if-missing", "-Q", "--query-file", qf]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
            out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
            return proc.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s waiting for '{pid}'."
    finally:
        Path(qf).unlink(missing_ok=True)


def _run_lane(pid: str, message: str, wait: bool) -> str:
    """Execute the delivery for one bot. `wait` True -> block and return the reply.
    `wait` False -> hand to a background delivery process (async) and return an ack."""
    if not wait:
        # Async: build the same one-shot CLI command the background runner executes,
        # write the message to a temp file, and spawn it via terminal_tool exactly as
        # bot_mode_dm does. The completion notification carries the reply back.
        return _deliver_async(pid, message)
    # Blocking path (cron/one-shot surfaces, or explicit mode='wait').
    try:
        status, body = _deliver_live(pid, message)
    except Exception as exc:
        logger.debug("b2b live delivery failed for %s", pid, exc_info=True)
        status, body = "error", str(exc)

    if status == "settled":
        return f"[b2b → {pid} · live Bot Chat]\n{body or '(no reply)'}"
    if status in ("pending", "claimed", "queued", "ambiguous"):
        return (f"[b2b → {pid} · live Bot Chat]\nMessage handed to the live session; "
                f"reply not yet settled: {status}. {body}".strip())
    ok, out = _deliver_bot_chat(pid, message)
    if ok:
        return f"[b2b → {pid} · Bot Chat]\n{out}"
    return f"[b2b → {pid}] Error: could not reach the Bot Chat without a new session. {out}"


def _deliver_async(pid: str, message: str) -> str:
    """Spawn a tracked background delivery process and return a `queued` ack.
    The reply arrives as a completion notification — same shape as message_agent.

    Uses the CLI Bot-Chat relay (``hermes -p <pid> chat -c "Bot Chat" -Q``), the
    same argv Bot Mode's own delivery runs. This is the reliable, proven lane
    (wait mode exercises the identical command); it lands in the target's
    canonical forever-chat and never mints a new session.
    """
    import tempfile
    import shlex
    from tools.terminal_tool import terminal_tool  # type: ignore

    cli = _hermes_cli()
    fd, qf = tempfile.mkstemp(prefix="b2b_", suffix=".txt",
                              dir=str(_hermes_home() / "cache" / "scratch"))
    try:
        with open(fd, "w", encoding="utf-8") as f:
            f.write(message)

        argv = [cli, "-p", pid, "chat", "--in", "~", "-c", BOT_CHAT_TITLE,
                "--create-if-missing", "-Q", "--query-file", qf]

        # terminal_tool takes a string command; quote safely (no shell metachars injected).
        cmd_str = " ".join(shlex.quote(c) for c in argv)
        raw = terminal_tool(cmd_str, background=True, notify_on_complete=True,
                            _completion_output_chars=_MESSAGE_MAX_CHARS + 2000)
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        if parsed.get("error"):
            return _err("spawn_failed", f"Delivery to {pid} failed to start: {parsed['error']}")
        if parsed.get("notify_on_complete") is False:
            return _err("async_unsupported",
                        f"Message queued for {pid}, but this session can't receive the async "
                        f"reply. Retry with mode='wait' to block for it.")
        proc_id = parsed.get("session_id") or ""
        return json.dumps({
            "status": "queued", "to": pid, "delivery_id": uuid.uuid4().hex,
            "process_id": proc_id, "queued_at": int(time.time()),
            "detail": (f"Message queued for {pid}: this acknowledges the hand-off to a "
                       f"background delivery process, not a delivery receipt — do NOT wait or "
                       f"poll. Finish your turn; the completion notification carries the reply "
                       f"(relay it, attributed to {pid}) or the delivery failure."),
        })
    except Exception as exc:
        logger.error("b2b async spawn failed: %s", exc, exc_info=True)
        return _err("spawn_failed", f"Delivery to {pid} could not be started: {exc}")


def _err(reason: str, detail: str) -> str:
    return json.dumps({"error": detail, "reason": reason,
                       "meaning": _REASONS.get(reason, "")})


# ── tools ─────────────────────────────────────────────────────────────────────

def b2b_call(args: dict) -> str:
    """Message one or more bots. Fire-and-forget by default; mode='wait' to block.

    args:
      target   — a bot name (profile id / display name / title). Comma-separate for
                 several: "bravo, marnie". '*' or 'all' = every bot except yourself.
      message  — what to send
      mode     — 'async' (default): queue and return an ack; the reply arrives as a
                 completion notification. 'wait': block and return the reply now
                 (use on cron/one-shot surfaces that can't receive async completions).
    """
    raw_target = str(args.get("target") or args.get("bot") or args.get("agent") or "").strip()
    message = str(args.get("message") or args.get("msg") or "").strip()
    mode = str(args.get("mode") or "async").strip().lower()
    if not raw_target or not message:
        return _err("bad_message", "Both 'target' and 'message' are required.")
    if len(message) > _MESSAGE_MAX_CHARS:
        return _err("bad_message", f"Message too long ({len(message)} > {_MESSAGE_MAX_CHARS}).")

    roster = _roster()
    me = _self_profile()
    wait = mode in ("wait", "block", "sync", "blocking")

    if raw_target.lower() in ("*", "all"):
        pids = [p for p in sorted(roster) if p != me]
        if not pids:
            return _err("bad_target", "No other bots to reach.")
    else:
        pids = []
        for part in raw_target.split(","):
            pid = _resolve(part)
            if not pid:
                return _err("bad_target",
                            f"No bot named '{part.strip()}'. Known: {', '.join(sorted(roster))}")
            if pid != me and pid not in pids:
                pids.append(pid)
        if not pids:
            return _err("bad_target", "No targets after resolving the roster.")

    signed = _sign(pids, message)
    if len(pids) == 1:
        return _run_lane(pids[0], signed, wait=wait)
    return "\n\n".join(_run_lane(p, signed, wait=wait) for p in pids)


def b2b_list(args: dict | None = None) -> str:
    """List every bot reachable via b2b_call (profile id, friendly name, live Bot Chat state)."""
    lines = []
    for pid, name in sorted(_roster().items()):
        label = pid + (f" ({name})" if name and name.lower() != pid.lower() else "")
        try:
            from tools import bot_live_delivery as live  # type: ignore
            live_now = "live" if live.find_canonical_live_owner(_profile_dir(pid)) else "idle"
        except Exception:
            live_now = "?"
        lines.append(f"  - {label}  [Bot Chat: {live_now}]")
    return "Bots reachable via b2b_call:\n" + ("\n".join(lines) if lines else "  (none found)")


def b2b_send(args: dict) -> str:
    """Send a message AS a bot (from that bot's own profile/token) to a channel.

    args:
      target   — profile id / display name / Bot Mode title to send as
      channel  — e.g. 'telegram:123456789'
      message  — the message body
    """
    target = str(args.get("target") or args.get("bot") or "").strip()
    channel = str(args.get("channel") or args.get("to") or "").strip()
    message = str(args.get("message") or "").strip()
    if not (target and channel and message):
        return _err("bad_message", "'target', 'channel', and 'message' are all required.")
    pid = _resolve(target)
    if not pid:
        return _err("bad_target", f"No bot named '{target}'.")
    cmd = [_hermes_cli(), "-p", pid, "send", "--to", channel, "-s", f"b2b from {pid}", message]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return f"[b2b send as {pid} → {channel}]\n{out or 'sent.'}" if proc.returncode == 0 \
            else _err("spawn_failed", f"Send as '{pid}' failed — {out}")
    except subprocess.TimeoutExpired:
        return _err("spawn_failed", f"Send as '{pid}' timed out.")


_TOOLS = {
    "b2b_call": (
        b2b_call,
        "Message one or more bots (Hermes profiles) and get their replies — bot-to-bot "
        "messaging that works from ANY session, including regular Sessions mode where Bot "
        "Mode's message_agent is unavailable. FIRE-AND-FORGET by default (like Bot Mode's "
        "message_agent): returns a 'queued' ack immediately and the reply arrives later as a "
        "completion notification — do NOT wait or poll. Pass mode='wait' only on surfaces that "
        "can't receive async replies (cron/one-shot), to block for the reply. Never creates a "
        "new session: each message lands in the target's canonical Bot Chat (the live session "
        "when open). target: a bot name, comma-separated names ('bravo, marnie'), or '*'/'all'. "
        "Use b2b_list to see reachable bots.",
        {
            "target": {"type": "string", "description": "Bot(s): profile id, display name, or Bot Mode title. Comma-separate for several; '*' or 'all' for every bot."},
            "message": {"type": "string", "description": "The message to send."},
            "mode": {"type": "string", "enum": ["async", "wait"], "description": "'async' (default) = fire-and-forget, reply via notification. 'wait' = block and return the reply (cron/one-shot only)."},
        },
        ["target", "message"],
    ),
    "b2b_list": (
        b2b_list,
        "List every bot reachable via b2b_call (profile id, friendly name, live Bot Chat state).",
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
}


def register(ctx) -> None:
    """Register the b2b toolset — no config gate: local profiles are always reachable."""
    for name, (handler, description, properties, required) in _TOOLS.items():
        parameters: dict = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        ctx.register_tool(
            name=name, toolset=_TOOLSET, handler=handler, description=description,
            schema={"name": name, "description": description, "parameters": parameters},
            emoji="🤝",
        )
