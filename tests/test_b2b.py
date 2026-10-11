#!/usr/bin/env python3
"""Tests for the bot-to-bot plugin.

Pure-logic tests: no Hermes install, no network, no live bots required.
Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent / "__init__.py"


def _load():
    """Import the plugin module from its file path (it is not an installed package)."""
    spec = importlib.util.spec_from_file_location("b2b_under_test", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


b2b = _load()


class TestRoster(unittest.TestCase):
    """Roster discovery is derived from disk, never hardcoded."""

    def setUp(self):
        b2b._roster_cache.clear()

    def test_no_bot_names_hardcoded_in_source(self):
        """Bot names may appear in example prose, but no logic may depend on one.

        A hardcoded dependency looks like a comparison, dict lookup, or string
        literal assignment — not a docstring example.
        """
        import ast
        tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
        names = {"bravo", "marnie", "jefferson"}
        # Collect every string literal used as a COMPARISON operand (not a docstring/description).
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for operand in [node.left, *node.comparators]:
                    if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                        if any(n in operand.value.lower() for n in names):
                            offenders.append(ast.dump(node))
        self.assertEqual(offenders, [], f"bot name used in a comparison: {offenders}")

    def test_roster_always_includes_default(self):
        with mock.patch.object(b2b, "_hermes_home", return_value=Path("/nonexistent")):
            roster = b2b._roster()
        self.assertIn("default", roster)

    def test_roster_reads_profile_dirs(self):
        fake_home = Path("/fake/hermes")
        with mock.patch.object(b2b, "_hermes_home", return_value=fake_home), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "iterdir") as it, \
             mock.patch.object(b2b, "_display_name", return_value="Test Bot"), \
             mock.patch.object(Path, "is_file", return_value=True):
            it.return_value = [Path("/fake/hermes/profiles/alpha"),
                               Path("/fake/hermes/profiles/beta")]
            roster = b2b._roster()
        self.assertIn("alpha", roster)
        self.assertIn("beta", roster)

    def test_roster_cache_avoids_rescan(self):
        fake_home = Path("/fake/hermes")
        calls = []
        with mock.patch.object(b2b, "_hermes_home", return_value=fake_home), \
             mock.patch.object(Path, "is_dir", return_value=False):
            b2b._roster()
            first = len(b2b._roster_cache)
            b2b._roster()   # second call should hit the cache
        self.assertEqual(len(b2b._roster_cache), first)

    def test_infra_dirs_excluded(self):
        fake_home = Path("/fake/hermes")
        with mock.patch.object(b2b, "_hermes_home", return_value=fake_home), \
             mock.patch.object(Path, "is_dir", return_value=True), \
             mock.patch.object(Path, "iterdir") as it, \
             mock.patch.object(Path, "is_file", return_value=True), \
             mock.patch.object(b2b, "_display_name", return_value=""):
            it.return_value = [Path("/fake/hermes/profiles/sessions"),
                               Path("/fake/hermes/profiles/logs"),
                               Path("/fake/hermes/profiles/real")]
            roster = b2b._roster()
        self.assertNotIn("sessions", roster)
        self.assertNotIn("logs", roster)
        self.assertIn("real", roster)


class TestResolve(unittest.TestCase):
    """Target resolution: profile id, display name, or title."""

    def setUp(self):
        b2b._roster_cache.clear()

    def _with_roster(self, roster):
        return mock.patch.object(b2b, "_roster", return_value=roster)

    def test_exact_profile_id(self):
        with self._with_roster({"default": "", "alpha": ""}):
            self.assertEqual(b2b._resolve("alpha"), "alpha")

    def test_case_insensitive(self):
        with self._with_roster({"default": "", "alpha": ""}):
            self.assertEqual(b2b._resolve("ALPHA"), "alpha")

    def test_display_name(self):
        with self._with_roster({"default": "", "alpha": "Research Buddy"}):
            self.assertEqual(b2b._resolve("Research Buddy"), "alpha")

    def test_unknown_returns_none(self):
        with self._with_roster({"default": ""}):
            self.assertIsNone(b2b._resolve("nobody"))

    def test_empty_returns_none(self):
        with self._with_roster({"default": ""}):
            self.assertIsNone(b2b._resolve("   "))


class TestSign(unittest.TestCase):
    """Messages carry sender attribution."""

    def test_signature_shape(self):
        with mock.patch.object(b2b, "_self_profile", return_value="default"):
            out = b2b._sign(["bravo"], "hello")
        self.assertIn("default", out)
        self.assertIn("bravo", out)
        self.assertIn("hello", out)

    def test_multiple_targets(self):
        with mock.patch.object(b2b, "_self_profile", return_value="default"):
            out = b2b._sign(["bravo", "marnie"], "hi")
        self.assertIn("bravo", out)
        self.assertIn("marnie", out)


class TestCallValidation(unittest.TestCase):
    """b2b_call argument handling — no delivery attempted."""

    def setUp(self):
        b2b._roster_cache.clear()

    def test_missing_message(self):
        out = b2b.b2b_call({"target": "bravo"})
        self.assertIn("error", out)

    def test_missing_target(self):
        out = b2b.b2b_call({"message": "hi"})
        self.assertIn("error", out)

    def test_message_too_long(self):
        out = b2b.b2b_call({"target": "bravo", "message": "x" * 20000})
        self.assertIn("error", out)
        self.assertIn("bad_message", out)

    def test_unknown_target_lists_known(self):
        with mock.patch.object(b2b, "_roster", return_value={"default": "", "alpha": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"):
            out = b2b.b2b_call({"target": "nobody", "message": "hi"})
        self.assertIn("bad_target", out)
        self.assertIn("alpha", out)      # known bots listed to help the caller

    def test_cannot_message_self(self):
        with mock.patch.object(b2b, "_roster", return_value={"default": "", "alpha": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"):
            out = b2b.b2b_call({"target": "default", "message": "hi"})
        self.assertIn("bad_target", out)

    def test_fanout_star_excludes_self(self):
        seen = []

        def fake_lane(pid, msg, wait):
            seen.append(pid)
            return f"ok:{pid}"

        with mock.patch.object(b2b, "_roster",
                               return_value={"default": "", "alpha": "", "beta": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"), \
             mock.patch.object(b2b, "_run_lane", side_effect=fake_lane):
            b2b.b2b_call({"target": "*", "message": "hi"})
        self.assertEqual(sorted(seen), ["alpha", "beta"])

    def test_comma_list_resolves(self):
        seen = []

        def fake_lane(pid, msg, wait):
            seen.append(pid)
            return f"ok:{pid}"

        with mock.patch.object(b2b, "_roster",
                               return_value={"default": "", "alpha": "", "beta": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"), \
             mock.patch.object(b2b, "_run_lane", side_effect=fake_lane):
            b2b.b2b_call({"target": "alpha, beta", "message": "hi"})
        self.assertEqual(sorted(seen), ["alpha", "beta"])

    def test_duplicate_targets_deduped(self):
        seen = []

        def fake_lane(pid, msg, wait):
            seen.append(pid)
            return f"ok:{pid}"

        with mock.patch.object(b2b, "_roster",
                               return_value={"default": "", "alpha": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"), \
             mock.patch.object(b2b, "_run_lane", side_effect=fake_lane):
            b2b.b2b_call({"target": "alpha, alpha", "message": "hi"})
        self.assertEqual(seen, ["alpha"])

    def test_wait_mode_selected(self):
        captured = {}

        def fake_lane(pid, msg, wait):
            captured["wait"] = wait
            return "ok"

        with mock.patch.object(b2b, "_roster", return_value={"default": "", "alpha": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"), \
             mock.patch.object(b2b, "_run_lane", side_effect=fake_lane):
            b2b.b2b_call({"target": "alpha", "message": "hi", "mode": "wait"})
        self.assertTrue(captured["wait"])

    def test_async_is_default(self):
        captured = {}

        def fake_lane(pid, msg, wait):
            captured["wait"] = wait
            return "ok"

        with mock.patch.object(b2b, "_roster", return_value={"default": "", "alpha": ""}), \
             mock.patch.object(b2b, "_self_profile", return_value="default"), \
             mock.patch.object(b2b, "_run_lane", side_effect=fake_lane):
            b2b.b2b_call({"target": "alpha", "message": "hi"})
        self.assertFalse(captured["wait"])


class TestErrors(unittest.TestCase):
    """Structured error payloads."""

    def test_error_shape(self):
        import json
        payload = json.loads(b2b._err("bad_target", "nope"))
        self.assertEqual(payload["reason"], "bad_target")
        self.assertIn("error", payload)
        self.assertIn("meaning", payload)

    def test_unknown_reason_still_structured(self):
        import json
        payload = json.loads(b2b._err("mystery", "nope"))
        self.assertEqual(payload["reason"], "mystery")


class TestLauncherFallback(unittest.TestCase):
    """The launcher resolves even when the private Hermes API is unavailable."""

    def test_falls_back_when_private_api_missing(self):
        # Simulate `tools.bot_relay` being absent/renamed by blocking the import.
        real_import = __import__

        def blocked(name, *a, **kw):
            if name == "tools.bot_relay":
                raise ImportError("simulated rename")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", side_effect=blocked):
            resolved = b2b._hermes_cli()
        self.assertTrue(resolved)          # never raises, always returns something
        self.assertIsInstance(resolved, str)

    def test_never_raises(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsInstance(b2b._hermes_cli(), str)


class TestRegistration(unittest.TestCase):
    """register() wires all declared tools into the declared toolset."""

    def test_all_tools_registered(self):
        registered = {}

        class FakeCtx:
            def register_tool(self, *, name, toolset, handler, description, schema, emoji=""):
                registered[name] = toolset

        b2b.register(FakeCtx())
        self.assertEqual(set(registered), {"b2b_call", "b2b_list", "b2b_send"})
        self.assertTrue(all(ts == "bot-to-bot" for ts in registered.values()))

    def test_manifest_matches_registrations(self):
        import re
        manifest = (PLUGIN.parent / "plugin.yaml").read_text(encoding="utf-8")
        declared = set(re.findall(r"^\s+-\s+(b2b_\w+)", manifest, re.M))
        self.assertEqual(declared, {"b2b_call", "b2b_list", "b2b_send"})

    def test_schemas_are_valid(self):
        class FakeCtx:
            def __init__(self):
                self.schemas = []

            def register_tool(self, *, name, toolset, handler, description, schema, emoji=""):
                self.schemas.append(schema)

        ctx = FakeCtx()
        b2b.register(ctx)
        self.assertEqual(len(ctx.schemas), 3)
        for schema in ctx.schemas:
            # Hermes' plugin schema shape: {name, description, parameters:{type, properties, required}}
            self.assertIn("name", schema)
            self.assertIn("description", schema)
            params = schema["parameters"]
            self.assertEqual(params["type"], "object")
            self.assertIn("properties", params)
            # A no-argument tool (b2b_list) may declare none; tools that take input must.
            if params["properties"]:
                self.assertIn("required", params, f"{schema['name']} has properties but no required list")


if __name__ == "__main__":
    unittest.main(verbosity=2)
