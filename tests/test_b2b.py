#!/usr/bin/env python3
"""Tests for the bot-to-bot plugin.

Pure-logic tests: no Hermes install, no network, no live bots required.
Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent.parent
PLUGIN = PLUGIN_DIR / "__init__.py"


def _load():
    """Import the plugin the way Hermes does: as a package, so its relative
    imports (``from .b2b.tools import TOOLS``) resolve."""
    name = "b2b_under_test"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN, submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = name
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


b2b = _load()
from b2b_under_test.b2b import (cards, guard, roster, service, tools,  # noqa: E402
                                transport)
from b2b_under_test.b2b.model import Lane, Reason, TaskState, err  # noqa: E402

EXPECTED_TOOLS = {"b2b_call", "b2b_list", "b2b_send", "b2b_status",
                  "b2b_cancel", "b2b_card", "b2b_policy"}

# Every source file in the package — the no-hardcoding rule covers all of them.
SOURCES = sorted([PLUGIN, *sorted((PLUGIN_DIR / "b2b").glob("*.py"))])


class TestNoHardcodedBots(unittest.TestCase):
    """Roster discovery is derived from disk, never hardcoded."""

    def test_no_bot_names_hardcoded_in_logic(self):
        """Bot names may appear in example prose, but no logic may depend on one.

        A hardcoded dependency looks like a comparison operand — not a docstring
        example or a tool-description string.
        """
        names = {"bravo", "marnie", "jefferson", "vinny"}
        offenders = []
        for path in SOURCES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    for operand in [node.left, *node.comparators]:
                        if (isinstance(operand, ast.Constant)
                                and isinstance(operand.value, str)
                                and any(n in operand.value.lower() for n in names)):
                            offenders.append(f"{path.name}: {ast.dump(node)}")
        self.assertEqual(offenders, [], f"bot name used in a comparison: {offenders}")


class TestRoster(unittest.TestCase):
    def setUp(self):
        roster._roster_cache.clear()

    def test_roster_always_includes_default(self):
        with mock.patch.object(roster, "hermes_home", return_value=Path("/nonexistent")):
            self.assertIn("default", roster.roster())

    def test_roster_reads_profile_dirs(self):
        fake_home = Path("/fake/hermes")
        with mock.patch.object(roster, "hermes_home", return_value=fake_home), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "iterdir") as it, \
             mock.patch.object(roster, "display_name", return_value="Test Bot"), \
             mock.patch.object(Path, "is_file", return_value=True):
            it.return_value = [Path("/fake/hermes/profiles/alpha"),
                               Path("/fake/hermes/profiles/beta")]
            known = roster.roster()
        self.assertIn("alpha", known)
        self.assertIn("beta", known)

    def test_roster_cache_avoids_rescan(self):
        fake_home = Path("/fake/hermes")
        with mock.patch.object(roster, "hermes_home", return_value=fake_home), \
             mock.patch.object(Path, "is_dir", return_value=False):
            roster.roster()
            first = len(roster._roster_cache)
            roster.roster()
        self.assertEqual(len(roster._roster_cache), first)

    def test_infra_dirs_excluded(self):
        fake_home = Path("/fake/hermes")
        with mock.patch.object(roster, "hermes_home", return_value=fake_home), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "iterdir") as it, \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(roster, "display_name", return_value=""):
            it.return_value = [Path("/fake/hermes/profiles/sessions"),
                               Path("/fake/hermes/profiles/logs"),
                               Path("/fake/hermes/profiles/real")]
            known = roster.roster()
        self.assertNotIn("sessions", known)
        self.assertNotIn("logs", known)
        self.assertIn("real", known)


class TestResolve(unittest.TestCase):
    """Target resolution: profile id, display name, or title."""

    def _with_roster(self, known):
        return mock.patch.object(roster, "roster", return_value=known)

    def test_exact_profile_id(self):
        with self._with_roster({"default": "", "alpha": ""}):
            self.assertEqual(roster.resolve("alpha"), "alpha")

    def test_case_insensitive(self):
        with self._with_roster({"default": "", "alpha": ""}):
            self.assertEqual(roster.resolve("ALPHA"), "alpha")

    def test_display_name(self):
        with self._with_roster({"default": "", "alpha": "Research Buddy"}):
            self.assertEqual(roster.resolve("Research Buddy"), "alpha")

    def test_unknown_returns_none(self):
        with self._with_roster({"default": ""}):
            self.assertIsNone(roster.resolve("nobody"))

    def test_empty_returns_none(self):
        with self._with_roster({"default": ""}):
            self.assertIsNone(roster.resolve("   "))


class TestSign(unittest.TestCase):
    """Messages carry sender attribution."""

    def test_signature_shape(self):
        with mock.patch.object(roster, "self_profile", return_value="default"):
            out = service._sign(["bravo"], "hello")
        for token in ("default", "bravo", "hello"):
            self.assertIn(token, out)

    def test_multiple_targets(self):
        with mock.patch.object(roster, "self_profile", return_value="default"):
            out = service._sign(["bravo", "marnie"], "hi")
        self.assertIn("bravo", out)
        self.assertIn("marnie", out)


class TestCallValidation(unittest.TestCase):
    """b2b_call argument handling — no delivery attempted."""

    def setUp(self):
        roster._roster_cache.clear()

    def test_missing_message(self):
        self.assertIn("error", tools.b2b_call({"target": "bravo"}))

    def test_missing_target(self):
        self.assertIn("error", tools.b2b_call({"message": "hi"}))

    def test_message_too_long(self):
        out = tools.b2b_call({"target": "bravo", "message": "x" * 20000})
        self.assertIn("error", out)
        self.assertIn("too_large", out)

    def test_unknown_lane_rejected(self):
        out = tools.b2b_call({"target": "bravo", "message": "hi", "session": "bogus"})
        self.assertIn("bad_lane", out)

    def test_unknown_target_lists_known(self):
        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"):
            out = tools.b2b_call({"target": "nobody", "message": "hi"})
        self.assertIn("bad_target", out)
        self.assertIn("alpha", out)      # known bots listed to help the caller

    def test_cannot_message_self(self):
        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"):
            out = tools.b2b_call({"target": "default", "message": "hi"})
        self.assertIn("bad_target", out)

    def _fanout(self, target):
        seen = []

        def fake_one(pid, msg, lane, session_name, wait, policy):
            seen.append(pid)
            return f"ok:{pid}"

        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": "", "beta": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"), \
             mock.patch.object(service, "_one", side_effect=fake_one):
            tools.b2b_call({"target": target, "message": "hi"})
        return seen

    def test_fanout_star_excludes_self(self):
        self.assertEqual(sorted(self._fanout("*")), ["alpha", "beta"])

    def test_comma_list_resolves(self):
        self.assertEqual(sorted(self._fanout("alpha, beta")), ["alpha", "beta"])

    def test_duplicate_targets_deduped(self):
        self.assertEqual(self._fanout("alpha, alpha"), ["alpha"])

    def test_default_lane_is_bot_chat(self):
        """The default lane must be 'bot' — the canonical Bot Chat."""
        captured = {}

        def fake_one(pid, msg, lane, session_name, wait, policy):
            captured["lane"] = lane
            captured["wait"] = wait
            return "ok"

        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"), \
             mock.patch.object(service, "_one", side_effect=fake_one):
            tools.b2b_call({"target": "alpha", "message": "hi"})
        self.assertIs(captured["lane"], Lane.BOT)
        self.assertFalse(captured["wait"])      # async is the default

    def test_wait_mode_selected(self):
        captured = {}

        def fake_one(pid, msg, lane, session_name, wait, policy):
            captured["wait"] = wait
            return "ok"

        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"), \
             mock.patch.object(service, "_one", side_effect=fake_one):
            tools.b2b_call({"target": "alpha", "message": "hi", "mode": "wait"})
        self.assertTrue(captured["wait"])


class TestLanes(unittest.TestCase):
    """Lane parsing and the existing-lane contract."""

    def test_lane_parse(self):
        self.assertIs(Lane.parse("bot"), Lane.BOT)
        self.assertIs(Lane.parse("NEW"), Lane.NEW)
        self.assertIs(Lane.parse("existing"), Lane.EXISTING)
        self.assertIs(Lane.parse(None), Lane.BOT)     # default
        self.assertIsNone(Lane.parse("bogus"))

    def test_existing_lane_needs_a_live_session_or_a_name(self):
        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"), \
             mock.patch.object(roster, "any_live", return_value=False):
            out = tools.b2b_call({"target": "alpha", "message": "hi", "session": "existing"})
        self.assertIn("bad_session", out)

    def test_existing_lane_without_live_owner_refuses_rather_than_creating(self):
        with mock.patch.object(roster, "pick_live", return_value=(None, "none")):
            out = service._one("alpha", "hi", Lane.EXISTING, "", True,
                               guard.Policy({}))
        self.assertIn("no_live_owner", out)


class TestTaskState(unittest.TestCase):
    """The lifecycle enum mirrors the mailbox's own states."""

    def test_terminal_states(self):
        self.assertEqual(sorted(s.value for s in TaskState.terminal()),
                         ["cancelled", "failed", "settled"])

    def test_live_states(self):
        self.assertEqual(sorted(s.value for s in TaskState.live()),
                         ["claimed", "queued"])

    def test_is_terminal(self):
        self.assertTrue(TaskState.DONE.is_terminal)
        self.assertFalse(TaskState.PENDING.is_terminal)


class TestGuardrails(unittest.TestCase):
    """Policy gates. A refused call spawns nothing."""

    def test_defaults_are_permissive(self):
        p = guard.Policy({})
        self.assertIsNone(p.allowed_targets)
        self.assertEqual(p.denied_targets, frozenset())
        self.assertEqual(p.max_message_chars, guard.DEFAULT_MAX_CHARS)

    def test_size_cap_cannot_exceed_hard_limit(self):
        p = guard.Policy({"max_message_chars": 10_000_000})
        self.assertEqual(p.max_message_chars, guard.HARD_MAX_CHARS)

    def test_oversize_rejected(self):
        from b2b_under_test.b2b.model import B2BError
        p = guard.Policy({})
        with self.assertRaises(B2BError) as ctx:
            p.check_message("x" * (guard.DEFAULT_MAX_CHARS + 1))
        self.assertEqual(ctx.exception.reason, Reason.TOO_LARGE)

    def test_denied_target_always_wins(self):
        from b2b_under_test.b2b.model import B2BError
        p = guard.Policy({"allowed_targets": ["alpha"], "denied_targets": ["alpha"]})
        with self.assertRaises(B2BError):
            p.check_target("alpha")

    def test_allowed_targets_is_a_whitelist(self):
        from b2b_under_test.b2b.model import B2BError
        p = guard.Policy({"allowed_targets": ["alpha"]})
        p.check_target("alpha")                     # permitted
        with self.assertRaises(B2BError):
            p.check_target("beta")

    def test_lane_allowlist(self):
        from b2b_under_test.b2b.model import B2BError
        p = guard.Policy({"allowed_lanes": ["bot"]})
        p.check_lane(Lane.BOT)
        with self.assertRaises(B2BError):
            p.check_lane(Lane.NEW)

    def test_fanout_can_be_disabled(self):
        from b2b_under_test.b2b.model import B2BError
        p = guard.Policy({"allow_fanout": False})
        with self.assertRaises(B2BError):
            p.check_fanout(["alpha", "beta"])
        p.check_fanout(["alpha"])                   # a single target is fine


class TestCards(unittest.TestCase):
    """AgentCard semantics: undeclared = permissive, declared = a gate."""

    def test_absent_card_accepts_every_lane(self):
        card = cards.read("no-such-profile-xyz")
        for lane in Lane:
            self.assertTrue(card.accepts(lane))
        self.assertFalse(card.declared)

    def test_declared_card_gates_lanes(self):
        from b2b_under_test.b2b.model import AgentCard
        card = AgentCard.from_dict({"lanes": ["new"]})
        self.assertTrue(card.accepts(Lane.NEW))
        self.assertFalse(card.accepts(Lane.BOT))
        self.assertTrue(card.declared)

    def test_malformed_card_is_permissive(self):
        from b2b_under_test.b2b.model import AgentCard
        self.assertTrue(AgentCard.from_dict("not a dict").accepts(Lane.BOT))
        self.assertTrue(AgentCard.from_dict({"lanes": "oops"}).accepts(Lane.BOT))

    def test_declared_lane_refuses_the_call(self):
        from b2b_under_test.b2b.model import AgentCard
        with mock.patch.object(roster, "roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(roster, "self_profile", return_value="default"), \
             mock.patch.object(cards, "read",
                               return_value=AgentCard.from_dict({"lanes": ["new"]})):
            out = tools.b2b_call({"target": "alpha", "message": "hi"})
        self.assertIn("lane_unsupported", out)


class TestErrors(unittest.TestCase):
    """Structured error payloads."""

    def test_error_shape(self):
        payload = json.loads(err(Reason.BAD_TARGET, "nope"))
        self.assertEqual(payload["reason"], "bad_target")
        self.assertIn("error", payload)
        self.assertIn("meaning", payload)

    def test_every_reason_has_a_meaning(self):
        from b2b_under_test.b2b.model import MEANINGS
        for reason in Reason:
            self.assertIn(reason, MEANINGS, f"{reason.value} has no human meaning")


class TestLauncherFallback(unittest.TestCase):
    """The launcher resolves even when the private Hermes API is unavailable."""

    def test_falls_back_when_private_api_missing(self):
        real_import = __import__

        def blocked(name, *a, **kw):
            if name == "tools.bot_relay":
                raise ImportError("simulated rename")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", side_effect=blocked):
            resolved = transport.hermes_cli()
        self.assertTrue(resolved)
        self.assertIsInstance(resolved, str)

    def test_never_raises(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsInstance(transport.hermes_cli(), str)


class TestRegistration(unittest.TestCase):
    """register() wires all declared tools into the declared toolset."""

    def test_all_tools_registered(self):
        registered = {}

        class FakeCtx:
            def register_tool(self, *, name, toolset, handler, description, schema, emoji=""):
                registered[name] = toolset

        b2b.register(FakeCtx())
        self.assertEqual(set(registered), EXPECTED_TOOLS)
        self.assertTrue(all(ts == "bot-to-bot" for ts in registered.values()))

    def test_manifest_matches_registrations(self):
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        declared = set(re.findall(r"^\s+-\s+(b2b_\w+)", manifest, re.M))
        self.assertEqual(declared, EXPECTED_TOOLS)

    def test_schemas_are_valid(self):
        class FakeCtx:
            def __init__(self):
                self.schemas = []

            def register_tool(self, *, name, toolset, handler, description, schema, emoji=""):
                self.schemas.append(schema)

        ctx = FakeCtx()
        b2b.register(ctx)
        self.assertEqual(len(ctx.schemas), len(EXPECTED_TOOLS))
        for schema in ctx.schemas:
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("properties", params)
            if params.get("required"):
                # Every declared requirement must name a real property.
                for req in params["required"]:
                    self.assertIn(req, params["properties"],
                                  f"{schema['name']} requires unknown property {req!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
