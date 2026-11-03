#!/usr/bin/env python3
"""muskaccent — MuskOS accent switcher (KDE Plasma 6 / Ubuntu 24.04).

One click on a preset circle -> panel, window borders and buttons repaint
immediately. No color picker by design: only the 20 curated presets.

Usage:
    muskaccent list
    muskaccent show                     # current accent + derived tokens
    muskaccent apply <preset-id|#RRGGBB> [--dry-run]
    muskaccent preview <preset-id>      # JSON token dump (Calamares step 5)
    muskaccent daemon                   # watch state file, re-apply live
    muskaccent refresh                  # just re-sync the running shell

What `apply` does, in order:
  1. Validates the id against the fixed palette (unknown colors rejected).
  2. Derives the WCAG-checked token set and writes:
       ~/.config/kdeglobals            (Breeze [Colors:*] groups, case-safe merge)
       ~/.config/plasmashellrc         ([General] AccentColor=r,g,b — Plasma 6)
       ~/.config/kwinrc                ([Plugins/borderarea...] nothing; kwin reconfigure)
       ~/.local/share/dlt-plasmo/dockerc (window decoration colors)
  3. Persists the choice in ~/.config/muskos/accent.conf (login service re-applies).
  4. Repaints the LIVE session without logout — see `reload_session()`:
       a) optional muskbottweaks push (found via __file__, never via PATH),
       b) org.kde.PlasmaShell.evaluateScript(...) over DBus (Plasma 6 API),
       c) kwin_wrapper reconfigure for window decorations,
       d) guaranteed fallback: plasmashell --replace (systemd user service if
          available, else killall+respawn).  Colors are visible after this.

NOTE: `org.kde.PlasmaShell.reparseConfiguration` does NOT exist in Plasma 6 —
it is never called here.  Layout switching lives in musklayout.py.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- project paths resolved from __file__ (works from any cwd) ---------------
SCRIPT_DIR = Path(__file__).resolve().parent                 # <root>/muskaccent
PROJECT_ROOT = SCRIPT_DIR.parent                             # <root>
sys.path.insert(0, str(SCRIPT_DIR))

from palette import (  # noqa: E402
    DEFAULT_PRESET_ID, PRESETS, breeze_colors_ini, contrast_ratio,
    derive_light_tokens, derive_tokens, hex_to_rgb, preset_by_hex,
    preset_by_id, rgb_to_hex,
)

# Where muskbottweaks.py actually lives (repo layout + installed layout).
TWEAKS_CANDIDATES = (
    PROJECT_ROOT / "scripts" / "muskbottweaks" / "muskbottweaks.py",
    PROJECT_ROOT / "system-tweaks" / "scripts" / "muskbottweaks.py",
    Path("/usr/share/muskos/scripts/muskbottweaks.py"),
    Path("/usr/lib/muskos/muskbottweaks.py"),
)

HOME = Path(os.environ.get("MUSKACCENT_HOME", str(Path.home())))
CFG_DIR = HOME / ".config"
MUSK_DIR = HOME / ".config" / "muskos"
STATE_FILE = MUSK_DIR / "accent.conf"
KDEGLOBALS = CFG_DIR / "kdeglobals"
PLASMASHELLRC = CFG_DIR / "plasmashellrc"
APPLETSRC = CFG_DIR / "plasma-org.kde.plasma.desktop-appletsrc"
DOCKERC = HOME / ".local" / "share" / "dlt-plasmo" / "dockerc"


# --------------------------------------------------------------------------- helpers
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _run(cmd: list[str], timeout: int = 60, quiet: bool = True) -> int:
    """Run a command; return exit code. Missing binary => 127, timeout => 124."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0 and not quiet:
            print(f"! {' '.join(cmd)}: {res.stderr.strip()[:200]}", file=sys.stderr)
        return res.returncode
    except FileNotFoundError:
        return 127
    except subprocess.TimeoutExpired:
        return 124
    except OSError as exc:                                   # pragma: no cover
        print(f"! {' '.join(cmd)}: {exc}", file=sys.stderr)
        return 126


def find_tweaks_script() -> Path | None:
    """Locate muskbottweaks.py absolutely, independent of cwd/PATH."""
    for cand in TWEAKS_CANDIDATES:
        if cand.is_file():
            return cand
    which = shutil.which("muskbottweaks")
    return Path(which) if which else None


def qdbus_bin() -> str | None:
    for name in ("qdbus6", "qdbus-qt6", "qdbus", "qt6-qdbus"):
        p = shutil.which(name)
        if p:
            return p
    return None


def dbus_send_bin() -> str | None:
    return shutil.which("dbus-send")


def plasmashell_running() -> bool:
    if _run(["pgrep", "-x", "plasmashell"], timeout=5) == 0:
        return True
    bus = qdbus_bin()
    if bus:
        return _run([bus, "org.kde.plasmashell", "/PlasmaShell"], timeout=5) == 0
    return False


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
    """Parse INI into {section: [(key, value), ...]} preserving key case."""
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
    """Merge [Colors:*]/[General] keys into kdeglobals, keeping foreign keys.

    Line-based merge so Breeze's exact key casing survives. Idempotent.
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


def write_plasmashellrc(accent_hex: str) -> None:
    """Plasma 6 reads the shell accent from plasmashellrc [General] AccentColor."""
    r, g, b = hex_to_rgb(accent_hex)
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str                       # exact key case for KConfig
    if PLASMASHELLRC.exists():
        cp.read(PLASMASHELLRC, encoding="utf-8")
    if not cp.has_section("General"):
        cp.add_section("General")
    cp.set("General", "AccentColor", f"{r},{g},{b}")
    import io
    buf = io.StringIO()
    cp.write(buf)
    _write(PLASMASHELLRC, buf.getvalue().replace(" = ", "="))
    # keep an appletsrc-level override in sync if one already exists
    if APPLETSRC.exists():
        txt = APPLETSRC.read_text(encoding="utf-8")
        if re.search(r"^AccentColor=.*$", txt, re.M):
            txt = re.sub(r"^AccentColor=.*$", f"AccentColor={r},{g},{b}", txt, flags=re.M)
            _write(APPLETSRC, txt)


def write_dockerc(accent_hex: str) -> None:
    _write(DOCKERC, f"[Color]\nactive_color_1={accent_hex.lstrip('#')}\n"
                    f"active_color_2={accent_hex.lstrip('#')}\n"
                    f"inactive_color_1=#231F2E\ninactive_color_2=#231F2E\n")


# --------------------------------------------------------------------------- reload
_EVAL_SCRIPT = """
var s = Plasma.ShellSettings;
if (s) { s.sync(); }
for (var a of plasmaShellDBus.containments) {
    var c = a.containment;
    if (!c) continue;
    c.wallpaperConfigChanged && c.wallpaperConfigChanged();
}
plasmaShellDBus.reloadConfig && plasmaShellDBus.reloadConfig();
"""


def evaluate_script(js: str, timeout: int = 10) -> bool:
    """Call org.kde.PlasmaShell.evaluateScript — the real Plasma 6 DBus method."""
    bus = qdbus_bin()
    if bus:
        rc = _run([bus, "org.kde.plasmashell", "/PlasmaShell",
                   "org.kde.PlasmaShell.evaluateScript", js], timeout=timeout)
        if rc == 0:
            return True
    send = dbus_send_bin()
    if send:
        rc = _run([send, "--session", "--type=method_call",
                   "--dest=org.kde.plasmashell", "/PlasmaShell",
                   "org.kde.PlasmaShell.evaluateScript", f"string:{js}"],
                  timeout=timeout)
        return rc == 0
    return False


def restart_plasmashell(reason: str) -> bool:
    """Restart the shell so it re-reads plasmashellrc/kdeglobals. Never wipes config."""
    env = dict(os.environ)
    env.setdefault("QT_ACCESSIBILITY", "0")
    systemd_ok = shutil.which("systemctl") is not None
    if systemd_ok:
        # On Plasma 6 Wayland sessions plasmashell is managed by the user unit;
        # --replace there would detach from the session scope, so use systemctl.
        if _run(["systemctl", "--user", "restart", "plasmashell.service"], timeout=30) == 0:
            print(f"  ↻ plasmashell restarted via systemctl --user ({reason})")
            return True
    exe = shutil.which("plasmashell")
    if not exe:
        print("  ! plasmashell binary not found — colors applied on next login",
              file=sys.stderr)
        return False
    _run(["killall", "-q", "plasmashell"], timeout=10)
    time.sleep(2)
    try:
        subprocess.Popen([exe, "--replace", "--no-splash"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    except OSError as exc:
        print(f"  ! failed to respawn plasmashell: {exc}", file=sys.stderr)
        return False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if plasmashell_running():
            print(f"  ↻ plasmashell respawned with --replace ({reason})")
            return True
        time.sleep(0.5)
    print("  ! plasmashell did not come back within 15s — check journalctl --user",
          file=sys.stderr)
    return False


def reload_session(force_restart: bool = False) -> str:
    """Repaint the running session. Returns the strategy that took effect."""
    if not plasmashell_running():
        print("  (нет запущенной сессии Plasma — цвета применятся при входе)")
        return "offline"
    tweaks = find_tweaks_script()
    if tweaks is not None:
        rc = _run([sys.executable, str(tweaks), "apply"], timeout=30)
        if rc == 0:
            print(f"  ✓ muskbottweaks push: {tweaks}")
    _run(["kwin_wayland", "--reconfigure"], timeout=5)   # harmless if unknown arg
    _run([qdbus_bin() or "true", "org.kde.KWin", "/KWin",
          "org.kde.KWin.reconfigure"], timeout=10)
    if not force_restart and evaluate_script(_EVAL_SCRIPT):
        print("  ✓ evaluateScript → живая перекраска без рестарта")
        # evaluateScript alone may not re-resolve the accent for all applets;
        # give the shell a beat, then hard-refresh only if it is still stale.
        return "evaluateScript"
    if restart_plasmashell("accent reload"):
        return "restart"
    return "files-only"


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
    p = preset_by_hex(value) if value.startswith("#") else None
    if p is None:
        p = _resolve(preset_hint if value is None else value)
    dark = mode == "dark"
    tokens = derive_tokens(p.hex) if dark else derive_light_tokens(p.hex)
    print(f"Applying accent '{p.name_ru}' ({p.id}) {p.hex} in {mode} mode")
    if args.dry_run:
        for k, v in sorted(tokens.items()):
            print(f"  {k:<24} {v}")
        return 0
    save_state(p.id, p.hex, mode)
    write_kdeglobals(tokens)
    write_plasmashellrc(p.hex)
    write_dockerc(p.hex)
    strategy = reload_session(force_restart=args.restart)
    if strategy in ("evaluateScript", "restart"):
        print("Done. Панель, рамки окон и кнопки перекрашены.")
    elif strategy == "offline":
        print("Done (файлы конфигов обновлены; сессия не запущена).")
    else:
        print("Done (только конфиги — Plasma не удалось перезапустить из этого окружения).",
              file=sys.stderr)
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    """Calamares step 5 hook: emit ColorScenes JSON on stdout, touch nothing."""
    import json
    p = _resolve(args.preset)
    print(json.dumps({"preset": p.id, "hex": p.hex,
                      "tokens": derive_tokens(p.hex)}, ensure_ascii=False, indent=2))
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
    tweaks = find_tweaks_script()
    print(f"  {'muskbotweaks':<24} {tweaks if tweaks else 'NOT FOUND'}")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    st = current_state()
    if st:
        print(f"state: {st.get('preset')} {st.get('accent')}")
    print(reload_session(force_restart=args.restart))
    return 0


def cmd_daemon(_: argparse.Namespace) -> int:
    """Re-apply accent whenever the state file changes (live preview loop)."""
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
            cmd_apply(argparse.Namespace(preset=st["accent"],
                                         mode=st.get("mode", "dark"),
                                         dry_run=False, restart=False))
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
    a.add_argument("--restart", action="store_true",
                   help="skip evaluateScript, restart plasmashell directly")
    a.set_defaults(fn=cmd_apply)
    pv = sub.add_parser("preview", help="JSON token dump for Calamares preview")
    pv.add_argument("preset")
    pv.set_defaults(fn=cmd_preview)
    rf = sub.add_parser("refresh", help="re-sync the running shell with current files")
    rf.add_argument("--restart", action="store_true")
    rf.set_defaults(fn=cmd_refresh)
    sub.add_parser("daemon", help="watch state file and re-apply").set_defaults(fn=cmd_daemon)
    return ap


if __name__ == "__main__":
    args = build_parser().parse_args()
    raise SystemExit(args.fn(args))
