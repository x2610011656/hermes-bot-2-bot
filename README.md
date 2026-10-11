# bot-to-bot

**Bot-to-bot messaging for Hermes Agent — from any session, without creating new sessions.**

Hermes Bot Mode ships a `message_agent` tool so your bots can message each other. It has one
catch: it only exists inside a Bot's canonical **Bot Chat**. In a regular session — Sessions
mode, a CLI run, a cron job — the tool simply isn't there.

`bot-to-bot` (B2B) adds an equivalent that works **anywhere**, while reusing Bot Mode's own
delivery machinery rather than reimplementing it.

```
┌─────────────────────────────────────────────────────────────────┐
│  b2b_call(target="bravo", message="status?")                    │
│                                                                 │
│   ┌── fire-and-forget ──────────────────────────────────────┐   │
│   │  returns {"status":"queued"} immediately                │   │
│   │  ...later: completion notification carries the reply    │   │
│   └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Read this first: what this is (and isn't)

**It is** a personal-grade bridge that reuses Hermes' own transport to let any profile reach any
other profile on the same machine, from any session.

**It is not** a peer of the built-in Bot Mode feature, and it is not affiliated with or
endorsed by Nous Research.

### ⚠️ It routes around a deliberate security gate

Bot Mode deliberately restricts `message_agent` to canonical Bot Chats. That restriction was not
an oversight — it was added after the older renderer/shell-based handoff paths produced real
bugs (shell quoting/injection, verbatim message forwarding, cross-connection misrouting). See
[hermes-agent#94018](https://github.com/NousResearch/hermes-agent/issues/94018) for the full
history; a structured replacement for regular sessions is still an open request upstream.

This plugin is one possible shape of that replacement. It:

- never builds a shell command out of bot names, titles, or message text;
- writes message bodies to temp files, never into an argv or a shell string;
- validates targets against the live roster before delivering;
- never forwards your text verbatim — the calling agent composes its own message.

If you do not understand and accept the above, **do not install this.**

---

## Requirements

| Requirement | Why |
|---|---|
| Hermes Agent with Bot Mode machinery (`tools/bot_live_delivery`, `tools/bot_relay`) | The delivery lanes |
| At least two profiles on the machine | There is no one to message otherwise |
| Python 3.10+ | `str \| None` syntax |

Tested on Hermes Agent v0.21.6 (Windows, git install). Should work on Linux/macOS — the code has
no platform-specific paths, but it has not been tested there.

---

## Install

Copy the plugin directory into your Hermes home:

```bash
# from this repo's directory
cp -r . "$HERMES_HOME/plugins/bot-to-bot"
```

Or, on a git install, point at it directly. Then enable it **per profile** — this is the step
everyone misses:

```bash
hermes plugins enable bot-to-bot                      # for the default profile
hermes -p bravo plugins enable bot-to-bot             # repeat for EVERY bot you want to reach from
hermes -p marnie plugins enable bot-to-bot
```

**Why per profile?** Plugin enablement is per-profile state. Installing the plugin makes it
*available*; enabling it in a profile makes its tools *appear* in that profile's sessions.

Restart the gateway (or start a new session) and the tools register.

### Verify

```bash
hermes plugins validate "$HERMES_HOME/plugins/bot-to-bot"
```

Then, in a chat:

> call b2b_list

You should see every profile on the machine.

---

## Tools

### `b2b_call` — message bots

| Arg | Type | Notes |
|---|---|---|
| `target` | string | A bot name, `"bravo, marnie"` for several, or `"*"` / `"all"` for the fleet |
| `message` | string | What to send (max 16 000 chars) |
| `mode` | `"async"` \| `"wait"` | Default `async`. See below. |

**`mode="async"` (default)** — fire-and-forget. Hands the message to a tracked background
process and returns a `queued` acknowledgement immediately. The recipient's reply arrives later
as a completion notification that wakes the sender, attributed to that bot. This is how Bot
Mode's `message_agent` behaves.

**`mode="wait"`** — blocks and returns the reply directly. Use this on surfaces that cannot
receive an async completion: cron jobs, one-shot CLI runs, the API server.

> **Do not wait or poll after an async call.** The notification carries the reply. If your
> session reports `notify_on_complete: false`, it cannot receive async replies — re-issue with
> `mode="wait"`.

### `b2b_list` — who can I reach?

Lists every profile on the machine with its friendly name and whether its canonical Bot Chat is
currently held open by a live session.

### `b2b_send` — send *as* a bot

Sends a message from a specific bot's own profile and credentials to a channel:

```
b2b_send(target="bravo", channel="telegram:123456789", message="hello")
```

---

## How it works

Two delivery lanes, tried in order:

**1. Live owner.** If the target's canonical Bot Chat is open somewhere (Desktop, another
terminal), the message is handed to that live session through Hermes' own owner-pinned mailbox
(`tools.bot_live_delivery`) — the same mechanism `message_agent` uses. **No new session.**

**2. Canonical Bot Chat relay.** If no live owner is present, the message is delivered into the
target's canonical `Bot Chat` via a one-shot CLI turn — the same argv Bot Mode itself runs. The
title is the fixed canonical `"Bot Chat"`, so every delivery lands in the *same* forever-chat
thread. **No new session.**

The roster is discovered from disk (`<hermes-home>/profiles/`), not hardcoded. Create a profile
and it appears; delete one and it stops appearing.

---

## Design notes

- **No new sessions.** Both lanes target the canonical Bot Chat. Nothing here mints a scratch
  session per message.
- **Roster is derived.** The plugin scans `<hermes-home>/profiles/` and accepts any directory
  carrying a `config.yaml` or `profile.yaml`. No bot names exist in the code.
- **5-second roster cache.** Repeat calls skip the filesystem scan. A profile created in the last
  5 seconds may not appear yet.
- **Graceful degradation.** The launcher path resolves through Hermes' own private resolver when
  available, then falls back to the published `hermes` binary, then `PATH`. If a future Hermes
  release moves the private function, this plugin keeps working instead of crashing.
- **Structured errors.** Failures return `{"error", "reason", "meaning"}` with a stable reason
  code (`bad_target`, `bad_message`, `spawn_failed`, `async_unsupported`).

## Known limitations

- **Same machine only.** Bots on other hosts are not reachable. Bot Mode's peer/Desktop relay is
  not replicated here.
- **Not an async equivalent in every surface.** On cron, one-shot CLI, and API-server runs the
  session cannot receive a completion notification; `b2b_call` reports this and you should use
  `mode="wait"`.
- **Depends on Hermes internals.** The delivery lanes use `tools.bot_relay` and
  `tools.bot_live_delivery`. These are internal modules and may change between Hermes releases.
  The launcher lookup degrades safely; the lanes themselves would need updating if those modules
  move.
- **Fan-out is sequential.** Messaging N bots takes N delivery attempts in sequence.
- **First call in a fresh session may miss the tool.** Hermes defers newly-registered plugin
  tools; the first turn or two in a brand-new session may not see them, after which they are
  present.

---

## Troubleshooting

### "There's no `b2b_call` tool in this session"

By far the most common report. The plugin is installed, but its tools appear **per profile**, and
enablement is per-profile state. Check both:

```bash
# 1. Is the plugin installed at all?
hermes plugins list | grep bot-to-bot

# 2. Is it enabled for THIS profile?
hermes -p <profile> plugins list | grep bot-to-bot
```

If step 2 shows `not enabled`, run:

```bash
hermes -p <profile> plugins enable bot-to-bot
```

Then **start a new session** in that profile — the toolset list is read at session start.

### The bot still says it has no `b2b_call` after enabling

Hermes **defers** newly-registered plugin tools. The very first turn or two in a brand-new
session may not see them, after which they are present. Ask again in a later turn before
concluding anything is broken.

### A different profile can't be reached even though it's listed

The roster (who exists) and enablement (who has the tools) are separate:

- **Roster** is discovered from disk automatically — any profile in `<hermes-home>/profiles/`.
- **Enablement** is manual, per profile.

So bot A can *see* bot B in `b2b_list` while bot B has no tools of its own. That is expected.
Enable the plugin in B too if B should be able to initiate messages.

### `notify_on_complete: false` in the result

That session cannot receive an async completion — cron jobs, one-shot CLI runs, and API-server
sessions are the usual causes. Re-issue the call with `mode="wait"` to block for the reply
instead.

### Delivery fails with "could not reach the Bot Chat"

The target's canonical Bot Chat is held open by another surface and no live-owner handoff was
available. Retry later, or close that Bot Chat tab.

---

## Uninstall

```bash
hermes -p <profile> plugins disable bot-to-bot   # repeat per profile
rm -rf "$HERMES_HOME/plugins/bot-to-bot"
```

Nothing is written outside the plugin directory except temp message files under
`<hermes-home>/cache/scratch/`, which are removed after each delivery.

---

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with Nous Research. "Hermes Agent" and "Bot Mode" are their work; this is an
independent plugin that targets their plugin API.
