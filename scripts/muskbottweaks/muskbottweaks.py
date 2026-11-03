#!/usr/bin/env python3
"""muskbottweaks — live-tweak Plasma 6 QML objects over KUserFeedback DBus.

Ported and trimmed from the upstream project (github.com/nschloe/muskbottweaks,
GPL-3.0) for MuskOS: we keep only the plumbing needed to repaint the shell
*instantly* when the accent changes, without restarting plasmashell.

Requires: qdbus (qt6-tools), running KDE Plasma 6 session.

CLI:
    muskbottweaks apply                    # push current accent into all
                                           # panels/widgets/containment
    muskbottweaks set <prop> <value>       # raw property set on every object
    muskbottweaks dump                     # list tweakable DBus paths
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DBUS_LIST = ["qdbus", "org.kde.plasmashell", "/Modules"]


def _qdbus(args: list[str], stdin: str | None = None) -> str:
    cmd = ["qdbus", "--silent"] + args
    res = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"qdbus failed: {' '.join(args)}")
    return res.stdout


def module_paths() -> list[str]:
    """All /Modules/<name> DBus objects of the running plasmashell."""
    try:
        out = _qdbus(DBUS_LIST)
    except FileNotFoundError:
        sys.exit("muskbottweaks: qdbus not found (install qt6-tools-dbus-platform)")
    except RuntimeError as e:
        sys.exit(f"muskbottweaks: plasmashell not reachable: {e}")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def read_color_tokens() -> dict[str, str]:
    """Load derived tokens written by `muskaccent apply` state."""
    accent_conf = Path.home() / ".config" / "muskos" / "accent.conf"
    if not accent_conf.exists():
        sys.exit("muskbottweaks: no accent state; run `muskaccent apply <preset>` first")
    preset_hex = ""
    for ln in accent_conf.read_text(encoding="utf-8").splitlines():
        if ln.startswith("accent="):
            preset_hex = ln.split("=", 1)[1].strip()
    if not preset_hex:
        sys.exit("muskbottweaks: corrupt accent.conf")
    here = Path(__file__).resolve().parent.parent / "muskaccent"
    sys.path.insert(0, str(here))
    from palette import derive_tokens  # noqa: PLC0415
    return derive_tokens(preset_hex)


def set_property(path: str, prop: str, value: str) -> bool:
    try:
        _qdbus([f"org.kde.plasmashell", path, "setProperty", prop, value])
        return True
    except RuntimeError:
        return False


def cmd_apply(_: argparse.Namespace) -> int:
    tokens = read_color_tokens()
    accent = tokens["color-accent_raw"]
    hover = tokens["color-hover"]
    hits = 0
    for p in module_paths():
        # PanelBackgroundItem / containment style hooks understand these:
        for prop, val in (("accentColor", accent), ("highlightColor", accent),
                          ("hoverColor", hover)):
            if set_property(p, prop, val):
                hits += 1
    print(f"muskbottweaks: pushed accent {accent} to {hits} live properties")
    # ask each module to re-evaluate its bindings (upstream 'apply' does this)
    for p in module_paths():
        set_property(p, "forceRefresh", "true")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    n = sum(set_property(p, args.prop, args.value) for p in module_paths())
    print(f"set {args.prop}={args.value} on {n} objects")
    return 0


def cmd_dump(_: argparse.Namespace) -> int:
    print(json.dumps(module_paths(), indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="muskbottweaks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("apply").set_defaults(fn=cmd_apply)
    s = sub.add_parser("set")
    s.add_argument("prop")
    s.add_argument("value")
    s.set_defaults(fn=cmd_set)
    sub.add_parser("dump").set_defaults(fn=cmd_dump)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
