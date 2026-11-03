#!/usr/bin/env python3
"""muskaccent — MuskOS accent switcher.

One click on a preset circle -> the whole desktop (panel, windows, buttons)
repaints instantly. No color picker by design: only the 20 curated presets.

Usage:
    muskaccent list
    muskaccent show                    # current accent + derived tokens
    muskaccent apply <preset-id|#RRGGBB> [--dry-run]
    muskaccent preview <preset-id>     # writes ColorScenes entry only (for
                                       # the Calamares "Мозги и Стиль" step)
    muskaccent daemon                  # watch config change and re-apply live

What `apply` does, in order:
  1. Validates the id against the fixed palette (unknown colors are rejected).
  2. Derives the full token set (WCAG-checked) and writes:
       ~/.config/kdeglobals                     (Breeze [Colors:*] groups)
       ~/.config/plasma-org.kde.plasma.desktop-appletsrc  (AccentColor=)
       ~/.local/share/dlt-plasmo/dockerc        (window decorations)
  3. Registers/refreshes a ColorScheme "MuskOS-Accent" via kconf_update-style
     merge (idempotent).
  4. Runs `muskbottweaks apply` to push the tokens into Plasma QML objects
     without restarting the shell (instant repaint).
  5. Persists the choice in ~/.config/muskos/accent.conf for the daemon and
     for first-login re-application.
"""

from __future__ import annotations

import argparse
import configparser
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from palette import (  # noqa: E402
    DEFAULT_PRESET_ID, PRESETS, breeze_colors_ini, contrast_ratio,
    derive_light_tokens, derive_tokens, hex_to_rgb, preset_by_hex,
    preset_by_id, rgb_to_hex,
)

HOME = Path(os.environ.get("MUSKACCENT_HOME", str(Path.home())))
CFG_DIR = HOME / ".config"
MUSK_DIR = HOME / ".config" / "muskos"
STATE_FILE = MUSK_DIR / "accent.conf"
KDEGLOBALS = CFG_DIR / "kdeglobals"
APPLETSRC = CFG_DIR / "plasma-org.kde.plasma.desktop-appletsrc"
DOCKERC = HOME / ".local" / "share" / "dlt-plasmo" / "dockerc"


# --------------------------------------------------------------------------- helpers
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _run(cmd: list[str], check: bool = False) -> int:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if check and res.returncode != 0:
            print(f"! {' '.join(cmd)} failed: {res.stderr.strip()}", file=sys.stderr)
        return res.returncode
    except FileNotFoundError:
        print(f"(skip) {cmd[0]} not installed — file state updated only", file=sys.stderr)
        return 127
    except subprocess.TimeoutExpired:
        print(f"(skip) {cmd[0]} timed out", file=sys.stderr)
        return 124


def current_state() -> dict[str, str]:
    if STATE_FILE.exists():
        cp = configparser.ConfigParser()
        cp.read(STATE_FILE, encoding="utf-8")
        return dict(cp["accent"]) if cp.has_section("accent") else {}
    return {}


def save_state(preset_id: str, accent_hex: str, mode: str) -> None:
    _write(STATE_FILE,
           "[accent]\n"
           f"preset={preset_id}\n"
           f"accent={accent_hex}\n"
           f"mode={mode}\n"
           f"source=muskaccent\n")


# --------------------------------------------------------------------------- writers
def _ini_sections(text: str) -> dict[str, list[tuple[str, str]]]:
    """Parse an INI file into {section: [(key, value), ...]} preserving case."""
    out: dict[str, list[tuple[str, str]]] = {}
    cur = None
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(("#", ";")):
            continue
        if ln.startswith("[") and ln.endswith("]"):
            cur = ln[1:-1]
            out.setdefault(cur, [])
        elif "=" in ln and cur is not None:
            k, v = ln.split("=", 1)
            out[cur].append((k.strip(), v.strip()))
    return out


def write_kdeglobals(tokens: dict[str, str], scheme_name: str = "MuskOS-Accent") -> None:
    """Merge our [Colors:*]/[General] keys into kdeglobals, keeping other keys.

    Line-based merge (not configparser) so Breeze's exact key casing —
    `Background`, `Highlight`, `DecorationFocus`... — survives.
    Idempotent: re-applying replaces only our own sections' overlapping keys.
    """
    ours = _ini_sections(breeze_colors_ini(tokens, scheme_name))
    existing_text = KDEGLOBALS.read_text(encoding="utf-8") if KDEGLOBALS.exists() else ""
    merged = _ini_sections(existing_text)
    for sec, entries in ours.items():
        target = merged.setdefault(sec, [])
        for k, v in entries:
            for i, (ek, _) in enumerate(target):
                if ek == k:
                    target[i] = (k, v)
                    break
            else:
                target.append((k, v))
    lines: list[str] = []
    for sec, entries in merged.items():
        lines.append(f"[{sec}]")
        lines += [f"{k}={v}" for k, v in entries]
        lines.append("")
    _write(KDEGLOBALS, "\n".join(lines).rstrip() + "\n")


def write_appletsrc_accent(accent_hex: str) -> None:
    """Plasma 6 reads AccentColor from plasmashellrc / appletsrc contexts."""
    r, g, b = hex_to_rgb(accent_hex)
    shellrc = CFG_DIR / "plasmashellrc"
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str          # Plasma expects exact key case
    if shellrc.exists():
        cp.read(shellrc, encoding="utf-8")
    if not cp.has_section("General"):
        cp.add_section("General")
    cp.set("General", "AccentColor", f"{r},{g},{b}")
    cp.set("General", "ForcedColors", "true")
    with open(shellrc, "w", encoding="utf-8") as f:
        cp.write(f)
    if APPLETSRC.exists():
        txt = APPLETSRC.read_text(encoding="utf-8")
        import re as _re
        if _re.search(r"^AccentColor=.*$", txt, _re.M):
            txt = _re.sub(r"^AccentColor=.*$", f"AccentColor={r},{g},{b}", txt, flags=_re.M)
        _write(APPLETSRC, txt)


def write_dockerc(accent_hex: str) -> None:
    """Window decoration color (dlt-dock-theme / dlt-plasmo)."""
    _write(DOCKERC, f"[Color]\nactive_color_1={accent_hex.lstrip('#')}\n"
                    f"active_color_2={accent_hex.lstrip('#')}\n"
                    f"inactive_color_1=#231F2E\ninactive_color_2=#231F2E\n")


# --------------------------------------------------------------------------- commands
def cmd_list(_: argparse.Namespace) -> int:
    for p in PRESETS:
        mark = "*" if p.id == DEFAULT_PRESET_ID else " "
        star = " (default)" if p.id == DEFAULT_PRESET_ID else ""
        print(f"{mark} {p.id:<14} {p.hex}  {p.name_ru}/{p.name_en}{star}")
    return 0


def _resolve(value: str):
    if value.startswith("#") or len(value) == 6:
        p = preset_by_hex(value)
        if p is None:
            raise SystemExit(
                f"muskaccent: {value!r} is not one of the 20 approved MuskOS "
                "presets. Free color picking is disabled by design — see "
                "`muskaccent list`.")
        return p
    return preset_by_id(value)


def cmd_apply(args: argparse.Namespace) -> int:
    if getattr(args, "from_state", False):
        st = current_state()
        value = st.get("accent") or preset_by_id(DEFAULT_PRESET_ID).hex
        mode = st.get("mode", "dark")
        preset_hint = st.get("preset", value)
    else:
        value, mode, preset_hint = args.preset, args.mode, args.preset
    p = _resolve(value) if not (value and value.startswith("#")
                                and preset_by_hex(value)) else preset_by_hex(value)
    if p is None:  # state held a raw hex that maps to a preset by definition
        p = _resolve(preset_hint)
    dark = mode == "dark"
    tokens = derive_tokens(p.hex) if dark else derive_light_tokens(p.hex)
    print(f"Applying accent '{p.name_ru}' ({p.id}) {p.hex} in {mode} mode")
    if args.dry_run:
        for k, v in sorted(tokens.items()):
            print(f"  {k:<24} {v}")
        return 0
    save_state(p.id, p.hex, mode)
    write_kdeglobals(tokens)
    write_appletsrc_accent(p.hex)
    write_dockerc(p.hex)
    # instant repaint without restarting Plasma
    rc = _run(["muskbottweaks", "apply"])
    if rc != 0:
        # graceful fallback: classic qdbus refresh
        _run(["qdbus", "--release", "org.kde.KWin", "/KWin",
              "org.kde.KWin.reconfigure"])
        _run(["qdbus", "org.kde.plasmashell", "/PlasmaShell",
              "org.kde.PlasmaShell.reparseConfiguration"])
    print("Done. Panel, window borders and buttons repainted.")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Calamares step 5 hook: emit ColorScenes JSON on stdout, touch nothing."""
    import json
    p = _resolve(args.preset)
    tokens = derive_tokens(p.hex)
    print(json.dumps({"preset": p.id, "hex": p.hex, "tokens": tokens},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_show(_: argparse.Namespace) -> int:
    st = current_state()
    if not st:
        st = {"preset": DEFAULT_PRESET_ID,
              "accent": preset_by_id(DEFAULT_PRESET_ID).hex, "mode": "dark"}
        print("(no state yet — showing factory default)")
    print(f"preset={st['preset']}  accent={st['accent']}  mode={st.get('mode','dark')}")
    for k, v in sorted(derive_tokens(st["accent"]).items()):
        print(f"  {k:<24} {v}")
    return 0


def cmd_daemon(_: argparse.Namespace) -> int:
    """Re-apply accent whenever the state file changes (live preview loop)."""
    import time
    last = None
    print("muskaccent daemon: watching", STATE_FILE, "(Ctrl+C to stop)")
    while True:
        try:
            mtime = STATE_FILE.stat().st_mtime
        except FileNotFoundError:
            mtime = None
        if mtime is not None and mtime != last:
            last = mtime
            st = current_state()
            cmd_apply(argparse.Namespace(preset=st["accent"], mode=st.get("mode", "dark"),
                                         dry_run=False))
        time.sleep(0.5)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="muskaccent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="show the 20 approved presets").set_defaults(fn=cmd_list)
    sub.add_parser("show", help="current accent + derived tokens").set_defaults(fn=cmd_show)
    a = sub.add_parser("apply", help="apply preset system-wide")
    a.add_argument("preset", nargs="?", help="preset id or #RRGGBB (must be a preset)")
    a.add_argument("--from-state", action="store_true",
                   help="re-apply the persisted choice (login service)")
    a.add_argument("--mode", choices=["dark", "light"], default="dark")
    a.add_argument("--dry-run", action="store_true")
    a.set_defaults(fn=cmd_apply)
    pv = sub.add_parser("preview", help="JSON token dump for Calamares preview")
    pv.add_argument("preset")
    pv.set_defaults(fn=cmd_preview)
    sub.add_parser("daemon", help="watch state file and re-apply").set_defaults(fn=cmd_daemon)
    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    raise SystemExit(args.fn(args))
