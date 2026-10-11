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
python -m unittest discover -s tests -v      # 47 pure-logic tests
```

Then, in a chat:

> call b2b_list

You should see every profile on the machine.

---

## Tools

| Tool | Purpose |
|---|---|
| `b2b_call` | message one or more bots, on a chosen lane |
| `b2b_list` | the roster, with live state and capability cards |
| `b2b_send` | deliver **as** a bot, on its own token, to a channel |
| `b2b_status` | read a delivery's lifecycle state |
| `b2b_cancel` | cancel a delivery that has not yet settled |
| `b2b_card` | read/write a bot's capability card |
| `b2b_policy` | show the resolved guardrails |

### `b2b_call` — message bots

| Arg | Type | Notes |
|---|---|---|
| `target` | string | A bot name, `"bravo, marnie"` for several, or `"*"` / `"all"` for the fleet |
| `message` | string | What to send (max 16 000 chars) |
| `mode` | `"async"` \| `"wait"` | Default `async`. See below. |
| `session` | `"bot"` \| `"new"` \| `"existing"` | **Default `bot`** — the target's canonical Bot Chat. Opt in to a different session only when you mean it. |
| `session_name` | string | Session title for `session="new"` or `session="existing"` |

**`mode="async"` (default)** — fire-and-forget. Hands the message to a tracked background
process and returns a `queued` acknowledgement immediately. The recipient's reply arrives later
as a completion notification that wakes the sender, attributed to that bot. This is how Bot
Mode's `message_agent` behaves.

**`mode="wait"`** — blocks and returns the reply directly. Use this on surfaces that cannot
receive an async completion: cron jobs, one-shot CLI runs, the API server.

> **Do not wait or poll after an async call.** The notification carries the reply. If your
> session reports `notify_on_complete: false`, it cannot receive async replies — re-issue with
> `mode="wait"`.

### `b2b_send` — send *as* a bot

```
b2b_send(target="bravo", channel="telegram:123456789", message="hello")
```

### `b2b_status` / `b2b_cancel` — the task lifecycle

Every delivery is a durable task. The states mirror A2A's and the mailbox's own:

```
queued ──► claimed ──► settled
   │          │    └─► failed
   └──────────┴──────► cancelled
```

Receipts are permanent and survive a crash; a crashed consumer leaves an inspectable unknown
outcome rather than permission to re-run the input. `b2b_status` reads one task by id (or a
bot's recent ones); `b2b_cancel` cancels a task that has not yet settled.

### `b2b_card` — capability cards

A bot declares itself in `<profile>/b2b-card.json` — A2A's Agent Card, downscaled:

```json
{
  "name": "marnie",
  "description": "diary, journal, and dream-journal keeper",
  "lanes": ["bot", "new"],
  "capabilities": ["notion-write", "journal-capture"]
}
```

**No card means every lane is accepted.** Declaring `lanes` turns the card into a gate: a
delivery on a lane the bot did not declare is refused, and the refusal names what it does
accept.

### `b2b_policy` — guardrails

Policy lives in `<hermes-home>/b2b-policy.json` (optional; absent = permissive):

```json
{
  "allowed_targets": ["marnie", "jefferson"],
  "denied_targets": ["vinny"],
  "allowed_lanes": ["bot", "existing"],
  "max_message_chars": 16000,
  "allow_fanout": true
}
```

`denied_targets` always wins. A hard ceiling of 64 000 chars cannot be raised by policy.
**A refused call spawns nothing** — every gate runs before any delivery.

## How it works

Each module owns exactly one concern; nothing else does that job.

```
b2b/model.py      vocabulary — Message, Task, TaskState, AgentCard, Lane, errors
b2b/guard.py      guardrails — size cap, allowed/denied targets, allowed lanes
b2b/ledger.py     TaskStore — the durable task lifecycle
b2b/transport.py  delivery — the live-owner lane and the named-session lane
b2b/roster.py     discovery — who exists, what they declare, what is live
b2b/cards.py      AgentCard read/write
b2b/service.py    the service — lane choice, fan-out, the card gate
b2b/tools.py      tool schemas + handlers (thin adapters only)
```

`__init__.py` is a shim: it imports the tool table and registers it.

### Lanes

| `session=` | Reaches | Works on |
|---|---|---|
| `bot` *(default)* | the target's canonical Bot Chat | every surface |
| `new` | a fresh session (named, or timestamped) | every surface |
| `existing` | a pre-existing session — the live one if open, else the named one | live lane: desktop/TUI only |

On the default lane the service **prefers a live owner when one exists**. That matters: the
mailbox correlates each reply to its delivery id, so a reply can never be mistaken for another
turn's. A CLI turn into a session that is already live would interleave with that session's
queue and return an uncorrelated answer, so the CLI path is the *fallback* — for bots with no
live session, such as Telegram-gateway bots.

### Design notes

- **The default lane is the Bot Chat.** A caller who says nothing about sessions gets the
  canonical Bot Chat. Nothing here mints a scratch session unless asked.
- **Roster is derived.** The plugin scans `<hermes-home>/profiles/` and accepts any directory
  carrying a `config.yaml` or `profile.yaml`. No bot names exist in the code — a test enforces
  this by walking the AST.
- **5-second roster cache.** Repeat calls skip the filesystem scan.
- **Graceful degradation.** The launcher path resolves through Hermes' own private resolver when
  available, then falls back to the published `hermes` binary, then `PATH`.
- **Structured errors.** Failures return `{"error", "reason", "meaning"}` with a stable reason
  code (`bad_target`, `bad_message`, `bad_lane`, `bad_session`, `no_live_owner`, `too_large`,
  `denied`, `lane_unsupported`, `spawn_failed`, `async_unsupported`, `not_found`,
  `not_cancellable`).

## Updating every bot (plugins are per-profile)

**There is no shared install and no automatic propagation.** Each profile keeps
its own copy of the plugin files under `<profile>/plugins/bot-to-bot/`. Updating
the default profile does **not** update the other bots — you must copy the files
into each one.

The plugin ships a script for exactly this. The default profile is the source:

```bash
python scripts/sync_profiles.py                # report only — shows who is behind
python scripts/sync_profiles.py --apply        # copy, with a timestamped backup each
python scripts/sync_profiles.py --apply --install   # also add it to profiles lacking it
```

It backs up whatever was there to `<profile>/backups/<plugin>-<timestamp>` before
replacing it, so a bad update is one `mv` away from being undone.

> The backup deliberately lives **outside** `plugins/`. Hermes treats any directory
> containing a `plugin.yaml` as a plugin, so a `<name>-backup` sitting next to the real
> one is listed as a second plugin carrying the *old* version and description.

### Then restart the bot

**A running bot keeps the code it loaded at startup.** Copying the files does not
affect a process that is already running — restart it (or start a new session in
that profile) to pick up the new version. Verify with:

```bash
hermes -p <profile> plugins list | grep bot-to-bot
```

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
- **The live lane is desktop/TUI only.** The poller that consumes mailbox deliveries runs in
  `tui_gateway.session_notifications`. A Telegram-gateway bot has no live owner, so it takes
  the CLI fallback on the default lane.
- **A live Bot Chat can be mid-turn.** A blocking `mode="wait"` call may have to wait for the
  session's current turn to finish before yours is claimed.
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
