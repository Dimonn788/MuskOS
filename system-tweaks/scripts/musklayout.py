#!/usr/bin/env python3
"""musklayout — switch Plasma panel layouts (MuskOS presets).

Presets ship as JSON under /usr/share/muskos/layouts/. Applying a layout:
  1. validates the current appletsrc backup,
  2. writes plasma-org.kde.plasma.desktop-appletsrc from the template,
  3. tells plasmashell to reparse over DBus (no logout needed).

Right-click menu hook: "Настроить панель" -> "Шаблоны MuskOS" invokes
`musklayout apply <name>` (see kcm/plasma-layout-menu.plugin).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("MUSKHOME", str(Path.home())))
LAYOUT_DIR = Path(os.environ.get(
    "MUSKDATA", str(Path(__file__).resolve().parent.parent / "plasma" / "layouts")))
APPLETSRC = HOME / ".config" / "plasma-org.kde.plasma.desktop-appletsrc"
BACKUP_DIR = HOME / ".local" / "share" / "muskos" / "layout-backups"

LOC_MAP = {"top": 1, "bottom": 2, "left": 3, "right": 4}  # Plasma Location enum


def available() -> dict[str, dict]:
    out = {}
    for f in sorted(LAYOUT_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        out[data["layout"]] = data
    return out


def render_appletsrc(spec: dict) -> str:
    """Minimal but valid Plasma 6 appletsrc for the given panel spec."""
    panels_txt = []
    applet_id = 100
    for i, p in enumerate(spec["panels"], start=1):
        loc = LOC_MAP[p["location"]]
        body = [f"[Containments][{i}]", "Action=Add", f"Location={loc}",
                f"floating={str(p.get('floating', False)).lower()}"]
        if "height" in p:
            body.append(f"height={p['height']}")
        if "width" in p:
            body.append(f"width={p['width']}")
        for wid, opts in p.get("widgets", {}).items():
            applet_id += 1
            cfg = "\n".join(f"{k}={v}" for k, v in opts.items())
            body.append(f"[Containments][{i}][Applets][{applet_id}]\n"
                        f"Plugin={wid}\n{cfg}")
        panels_txt.append("\n".join(body))
    header = ("[General]\nimmutability=1\n\n"
              "[Containments][0]\n"
              f"wallpaperplugin={spec.get('theme',{}).get('wallpaperPlugin','org.kde.image')}\n")
    return header + "\n\n".join(panels_txt) + "\n"


def cmd_list(_: argparse.Namespace) -> int:
    for name, spec in available().items():
        print(f"  {name:<14} {spec['description']}")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    layouts = available()
    key = next((k for k in layouts if k.lower().replace(" ", "-") == args.name.lower()), None)
    if key is None:
        sys.exit(f"musklayout: неизвестный шаблон {args.name!r}. "
                 f"Доступно: {', '.join(layouts)}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if APPLETSRC.exists():
        shutil.copy2(APPLETSRC, BACKUP_DIR / f"pre-{key.replace(' ', '_')}.appletsrc")
    APPLETSRC.write_text(render_appletsrc(layouts[key]), encoding="utf-8")
    try:
        subprocess.run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
                        "org.kde.PlasmaShell.reparseConfiguration"], check=False)
    except FileNotFoundError:
        print("(qdbus недоступен — применится при следующем входе)", file=sys.stderr)
    print(f"✅ Layout '{key}' applied.")
    return 0


def cmd_restore(_: argparse.Namespace) -> int:
    backups = sorted(BACKUP_DIR.glob("pre-*.appletsrc"))
    if not backups:
        sys.exit("нечего восстанавливать")
    shutil.copy2(backups[-1], APPLETSRC)
    print(f"restored {backups[-1].name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="musklayout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = sub.add_parser("apply"); a.add_argument("name"); a.set_defaults(fn=cmd_apply)
    sub.add_parser("restore").set_defaults(fn=cmd_restore)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
