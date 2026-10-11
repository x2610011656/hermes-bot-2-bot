#!/usr/bin/env python3
"""Sync the bot-to-bot plugin from one profile to every other profile.

Hermes plugins are PER-PROFILE: each profile keeps its own copy of the plugin
files under ``<profile>/plugins/<name>/``. There is no shared install and no
automatic propagation — updating the default profile does NOT update the bots.
This script is that propagation.

The default profile is the canonical source. Every other profile that already
has the plugin gets a copy of it (with a timestamped backup of what was there).
Profiles that do not have the plugin are reported and skipped unless
``--install`` is given.

Usage:
    python scripts/sync_profiles.py                 # report only (dry run)
    python scripts/sync_profiles.py --apply         # copy, with backups
    python scripts/sync_profiles.py --apply --install   # also add to profiles lacking it

After syncing, a profile's running bot must be RESTARTED to load the new code:
a live process keeps the version it imported at startup.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

PLUGIN_NAME = "bot-to-bot"
# Directories that are build artefacts or repo-only, never part of an install.
SKIP = {"__pycache__", "legacy", ".git", ".github"}


def hermes_home() -> Path:
    """The Hermes home. Respects HERMES_HOME, else the platform default."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes"
    return Path.home() / ".hermes"


def plugin_dir(home: Path, profile: str) -> Path:
    """Where a profile's copy of the plugin lives. 'default' is the home itself."""
    return (home / "plugins" / PLUGIN_NAME if profile == "default"
            else home / "profiles" / profile / "plugins" / PLUGIN_NAME)


def profiles(home: Path) -> list[str]:
    """Every profile on this install, 'default' first."""
    out = ["default"]
    pdir = home / "profiles"
    if pdir.is_dir():
        for entry in sorted(pdir.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "config.yaml").is_file() or (entry / "profile.yaml").is_file():
                out.append(entry.name)
    return out


def version_of(path: Path) -> str:
    """The plugin version declared in plugin.yaml, or '?'."""
    manifest = path / "plugin.yaml"
    if not manifest.is_file():
        return "?"
    for line in manifest.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return "?"


def copy_tree(src: Path, dst: Path) -> int:
    """Replace dst with a copy of src. Returns the file count written."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP, "*.pyc"))
    return sum(len(files) for _, _, files in os.walk(dst))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually copy (default is a dry run)")
    ap.add_argument("--install", action="store_true",
                    help="also install into profiles that do not have the plugin")
    ap.add_argument("--source", default="default",
                    help="profile to copy FROM (default: 'default')")
    args = ap.parse_args(argv)

    home = hermes_home()
    src = plugin_dir(home, args.source)
    if not (src / "plugin.yaml").is_file():
        print(f"ERROR: no plugin at {src}")
        return 2

    src_ver = version_of(src)
    print(f"Hermes home : {home}")
    print(f"Source      : {args.source} (v{src_ver})")
    print(f"Mode        : {'APPLY' if args.apply else 'dry run (pass --apply to copy)'}")
    print()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    changed = skipped = missing = 0

    for profile in profiles(home):
        if profile == args.source:
            continue
        dst = plugin_dir(home, profile)
        have = (dst / "plugin.yaml").is_file()

        if not have and not args.install:
            print(f"  {profile:<12} —  no plugin (use --install to add it)")
            missing += 1
            continue

        old_ver = version_of(dst) if have else "(absent)"
        if have and old_ver == src_ver:
            print(f"  {profile:<12} ok already v{old_ver}")
            skipped += 1
            continue

        if not args.apply:
            print(f"  {profile:<12} -> would update {old_ver} -> v{src_ver}")
            changed += 1
            continue

        # Back up whatever is there before replacing it.
        if have:
            backup = dst.with_name(f"{PLUGIN_NAME}-{stamp}-backup")
            if backup.exists():
                shutil.rmtree(backup)
            shutil.copytree(dst, backup)
            note = f"backup: {backup.name}"
        else:
            note = "new install"

        n = copy_tree(src, dst)
        print(f"  {profile:<12} ok updated {old_ver} -> v{src_ver}  ({n} files, {note})")
        changed += 1

    print()
    print(f"{changed} changed, {skipped} already current, {missing} without the plugin")
    if args.apply and changed:
        print("\nRESTART each updated bot to load the new code — a running process "
              "keeps the version it imported at startup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
