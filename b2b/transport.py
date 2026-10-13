"""Transport — the two ways a message reaches a bot. Nothing else spawns.

    live_owner(...)   hand a message to an already-open session (no new session)
    named_session(...) run one turn in a named session (creates it if missing)

Both reuse Hermes' own machinery rather than reimplementing it: the live lane
goes through ``tools.bot_live_delivery`` (the owner-pinned mailbox Bot Mode
uses), and the named-session lane runs the exact argv Bot Mode's own delivery
runs (``hermes -p <bot> chat -c <title> -Q``).

The named-session lane has two flavours, identical except for who waits:
``run`` blocks for the reply, ``spawn`` hands it to a tracked background process
whose completion notification carries the reply back.
"""
from __future__ import annotations

import json
import logging
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from .model import Author, Reason, B2BError
from . import roster

logger = logging.getLogger(__name__)

LIVE_WAIT_SECONDS = 300.0     # bounded so a stalled owner can't hang a caller
TURN_TIMEOUT_SECONDS = 240
_COMPLETION_HEADROOM = 2000


def hermes_cli() -> str:
    """Resolve the hermes launcher, degrading gracefully.

    Preference order:
      1. Hermes' own resolver (``tools.bot_relay._hermes_cli``) — most correct,
         but a private function, so it may move between versions.
      2. The published launcher beside the Hermes home (``<home>/bin/hermes``).
      3. ``shutil.which("hermes")`` on PATH.
      4. Bare ``"hermes"`` as a last resort.
    Never raises: a missing launcher surfaces later as a clean spawn failure.
    """
    try:
        from tools.bot_relay import _hermes_cli as _internal  # type: ignore
        resolved = _internal()
        if resolved:
            return resolved
    except Exception:
        logger.debug("bot_relay._hermes_cli unavailable; falling back", exc_info=True)

    import shutil
    name = "hermes.exe" if sys.platform == "win32" else "hermes"
    published = roster.hermes_home() / "bin" / name
    if published.is_file():
        return str(published)
    return shutil.which("hermes") or "hermes"


def turn_argv(pid: str, title: str, query_file: str) -> list[str]:
    """One turn in a named session of a profile (creates the session if absent).

    ``--in ~`` keeps the session scoped to the profile's own workspace, and
    ``--create-if-missing`` is what makes a named lane able to create a session
    without the caller doing anything else.
    """
    return [hermes_cli(), "-p", pid, "chat", "--in", "~", "-c", title,
            "--create-if-missing", "-Q", "--query-file", query_file]


def _write_query(message: str) -> str:
    """Write the message to a scratch file. The message never touches a shell,
    so quotes, ``$(...)`` and backticks are preserved verbatim."""
    scratch = roster.hermes_home() / "cache" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="b2b_", suffix=".txt", dir=str(scratch))
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write(message)
    return path


# ── lane 1: a live session ───────────────────────────────────────────────────

def live_owner(pid: str, owner: dict, message: str,
               author: Optional[Author] = None) -> str:
    """Admit a message to a specific live session and wait for its reply.

    Returns the task id (the mailbox receipt). The caller reads the receipt for
    the outcome — this function only admits and waits.
    """
    from .ledger import TaskStore

    store = TaskStore(roster.profile_dir(pid))
    task_id = store.admit(owner, message, author=author)
    return task_id


def wait_live(pid: str, task_id: str, timeout: float = LIVE_WAIT_SECONDS):
    """Block for a live-lane task's terminal receipt."""
    from .ledger import TaskStore

    return TaskStore(roster.profile_dir(pid)).wait(task_id, timeout, bot=pid)


# ── lane 2: a named session ──────────────────────────────────────────────────

def run_named(pid: str, title: str, message: str,
              timeout: int = TURN_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """Run one turn in a named session and block for the reply."""
    qf = _write_query(message)
    try:
        cmd = turn_argv(pid, title, qf)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                  encoding="utf-8", errors="replace")
            out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
            return proc.returncode == 0, out
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s waiting for '{pid}'."
    finally:
        Path(qf).unlink(missing_ok=True)


def spawn_named(pid: str, title: str, message: str,
                max_chars: int = 16000) -> dict:
    """Hand one turn to a tracked background process; the reply arrives later as
    a completion notification. Returns the parsed spawn result.

    The query file is deliberately NOT deleted here: the background process reads
    it after this returns. Scratch files are pruned by Hermes on a 24h cycle.
    """
    from tools.terminal_tool import terminal_tool  # type: ignore

    qf = _write_query(message)
    argv = turn_argv(pid, title, qf)
    cmd_str = " ".join(shlex.quote(c) for c in argv)
    raw = terminal_tool(cmd_str, background=True, notify_on_complete=True,
                        _completion_output_chars=max_chars + _COMPLETION_HEADROOM)
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = {}
    if parsed.get("error"):
        Path(qf).unlink(missing_ok=True)   # nothing started; the file is ours to clean
        raise B2BError(Reason.SPAWN_FAILED,
                       f"Delivery to {pid} failed to start: {parsed['error']}")
    if parsed.get("notify_on_complete") is False:
        Path(qf).unlink(missing_ok=True)
        raise B2BError(
            Reason.ASYNC_UNSUPPORTED,
            f"Message queued for {pid}, but this session can't receive the async "
            f"reply. Retry with mode='wait' to block for it.",
        )
    return parsed


def ack(pid: str, title: str, *, delivery_id: str = "", spawn: Optional[dict] = None) -> str:
    """The fire-and-forget acknowledgement.

    Two shapes, honestly distinguished:

    - ``delivery_id`` (live-owner lane): a real durable mailbox receipt exists
      and is retrievable via b2b_status(delivery_id=...).
    - ``spawn`` (named-session lane, for no-live-owner bots like Telegram
      gateways): there is no mailbox owner to settle a receipt, so there is no
      delivery id. The real, inspectable handle is the background ``process_id``.

    The old code minted a fresh Task.new_id() on the named lane that was never
    written anywhere, so b2b_status could only ever report it not_found. Now each
    shape returns the handle that actually exists and says which it is.
    """
    if delivery_id:
        return json.dumps({
            "status": "queued",
            "to": pid,
            "session": title,
            "delivery_id": delivery_id,
            "durable_receipt": True,
            "queued_at": int(time.time()),
            "detail": (
                f"Message queued for {pid} in '{title}': a durable receipt exists. "
                f"Do NOT wait or poll; the reply arrives as the completion "
                f"notification. Track it with b2b_status(delivery_id='{delivery_id}')."
            ),
        })

    spawn = spawn or {}
    proc_id = str(spawn.get("process_id") or spawn.get("session_id") or "")
    return json.dumps({
        "status": "queued",
        "to": pid,
        "session": title,
        "process_id": proc_id,
        "queued_at": int(time.time()),
        "durable_receipt": False,
        "detail": (
            f"Message queued for {pid} in '{title}': this hands the turn to a "
            f"background delivery process (no live owner to settle a mailbox "
            f"receipt, so there is no delivery id). Do NOT wait or poll; the reply "
            f"arrives as the completion notification for process "
            f"{proc_id or '(pending)'}. Inspect via the process id, not b2b_status."
        ),
    })


# ── outbound as a bot ────────────────────────────────────────────────────────

def run_as(pid: str, channel: str, message: str, *,
           sender: str = "", timeout: int = 120) -> tuple[bool, str]:
    """Deliver a message AS a bot — its own profile and token, to a channel.

    ``-p <profile>`` is what makes it send as THAT bot; without it, ``hermes
    send`` would use the active profile's token instead.

    ``sender`` is the profile that actually triggered the send. It is threaded
    through from the tool call (never derived from ambient process state), and
    when present the subject states BOTH the trigger and the sending bot — so a
    message can never be mislabeled as originating from the bot it merely rode
    out on. Falls back to a neutral subject when the sender is unknown.
    """
    subject = f"b2b: {sender} via {pid}" if sender else f"b2b via {pid}"
    cmd = [hermes_cli(), "-p", pid, "send", "--to", channel, "-s", subject, message]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace")
        out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
        return proc.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s."
    except Exception as exc:
        return False, str(exc)
