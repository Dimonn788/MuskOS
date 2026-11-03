#!/usr/bin/env python3
"""musklayout — switch Plasma 6 panel layouts (MuskOS presets).

Usage:
    musklayout list
    musklayout apply <name>        # e.g. "win11-classic" / "Win11 Classic"
    musklayout current
    musklayout restore [N]         # roll back N backups (default 1)
    musklayout validate            # parse every shipped JSON + check Plasma 6 keys

Plasma 6 facts this script relies on:
  * org.kde.PlasmaShell.reparseConfiguration DOES NOT EXIST (Plasma 5 API).
    It is never called here — that call was what corrupted the panel before.
  * Changing ~/.config/plasma-org.kde.plasma.desktop-appletsrc requires the
    shell to re-read it => plasmashell restart (systemctl --user, else
    killall + `plasmashell --replace`).  Config files are backed up first and
    written atomically, so a failed reload can always be restored.
  * Panel geometry keys live in [Containments][<id>]: Location enum is
    0=none 1=up 2=down 3=left 4=right; alignment 1=leading 2=center 3=trailing;
    hideMode 0=auto 1=manual 2=autohide 3=dodge 4=dodge-windows.
  * Applets hang off ContainmentShells/Applets with plugin ids like
    org.kde.plasma.icontasks; widgets need an empty [Applets][n][Configuration]
    group or Plasma treats them as unconfigured garbage.

Right-click menu hook ("Настроить панель" -> "Шаблоны MuskOS") invokes
`musklayout apply <name>`.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# --- paths from __file__, cwd-independent ------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent                  # .../scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent                       # repo root
REPO_LAYOUTS = PROJECT_ROOT / "system-tweaks" / "plasma" / "layouts"

HOME = Path(os.environ.get("MUSKHOME", str(Path.home())))
LAYOUT_DIR = Path(os.environ.get("MUSKDATA", str(
    next((d for d in (REPO_LAYOUTS,
                      HOME / ".local" / "share" / "muskos" / "layouts",
                      Path("/usr/share/muskos/layouts")) if d.is_dir()),
         REPO_LAYOUTS))))
APPLETSRC = HOME / ".config" / "plasma-org.kde.plasma.desktop-appletsrc"
BACKUP_DIR = HOME / ".local" / "share" / "muskos" / "layout-backups"

LOC_MAP = {"top": 1, "bottom": 2, "left": 3, "right": 4}      # Plasma 6 enum
ALIGN_MAP = {"leading": 1, "left": 1, "start": 1,
             "center": 2, "middle": 2,
             "trailing": 3, "right": 3, "end": 3}
HIDE_MAP = {"none": 0, "auto": 1, "autohide": 2, "dodged": 3,
            "dodge": 3, "dodge-windows": 4}

VALID_PLUGINS = {
    "org.kde.plasma.kickoff", "org.kde.plasma.icontasks", "org.kde.plasma.taskmanager",
    "org.kde.plasma.systemtray", "org.kde.plasma.digitalclock", "org.kde.plasma.pager",
    "org.kde.plasma.showdesktop", "org.kde.plasma.globalmenu",
    "org.kde.plasma.lock_launcher", "org.kde.plasma.kwinswitch",
    "org.kde.desktopcontainment",
}


# --------------------------------------------------------------------------- helpers
def _run(cmd: list[str], timeout: int = 30) -> int:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 127


def plasmashell_running() -> bool:
    return _run(["pgrep", "-x", "plasmashell"]) == 0


def available() -> dict[str, tuple[Path, dict]]:
    out: dict[str, tuple[Path, dict]] = {}
    if not LAYOUT_DIR.is_dir():
        return out
    for f in sorted(LAYOUT_DIR.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        out[data["layout"]] = (f, data)
    return out


def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def find_layout(want: str) -> tuple[str, dict] | None:
    layouts = available()
    wn = normalize(want)
    for key, (_, spec) in layouts.items():
        if normalize(key) == wn or normalize(Path(_).stem.replace("muskos-", "")) == wn:
            return key, spec
    return None


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".musktmp")
    tmp.write_text(text, encoding="utf-8")
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- renderer
def render_appletsrc(spec: dict) -> str:
    """Emit a minimal-but-valid Plasma 6 appletsrc for the given panel spec."""
    chunks: list[str] = []
    containment_id = 1
    applet_id = 0
    for p in spec["panels"]:
        loc = LOC_MAP[p["location"]]
        align = ALIGN_MAP.get(str(p.get("alignment", "center")).lower(), 2)
        hide = HIDE_MAP.get(str(p.get("hideMode", "none")).lower(), 0)
        cid = containment_id
        lines = [
            f"[Containments][{cid}]",
            "Action=Add",
            "Desktop=-1",
            "Screen=0",
            "FormFactor=2",                     # MediaPanel (task bar)
            f"Location={loc}",
            f"alignment={align}",
            f"hideMode={hide}",
            f"floating={'true' if p.get('floating') else 'false'}",
            "offset=0,0",
            "maximumSize=120,120" if loc in (3, 4) else "maximumSize=1200,1200",
        ]
        if "height" in p and loc in (1, 2):
            lines.append(f"minimumSize=0,{p['height']}")
        if "width" in p and loc in (3, 4):
            lines.append(f"minimumSize={p['width']},0")
        ext: list[str] = []
        for wid, opts in p.get("widgets", {}).items():
            applet_id += 1
            aid = applet_id
            lines += [
                f"[Containments][{cid}][Applets][{aid}]",
                "immutability=1",
                f"Plugin={wid}",
            ]
            lines.append(f"[Containments][{cid}][Applets][{aid}][Configuration]")
            lines.append("immutability=1")
            for k, v in opts.items():
                if isinstance(v, bool):
                    v = "true" if v else "false"
                lines.append(f"{k}={v}")
            lines.append(f"[Containments][{cid}][Applets][{aid}][Configuration][ExtentsItems]")
            lines.append("immutability=1")
            lines.append(f"[Containments][{cid}][Applets][{aid}][Geometry]")
            lines.append("immutability=1")
            ext.append(f"Applet-Extra::plugins+={wid}")
        lines.append(f"[Containments][{cid}][General]")
        lines.append("immutability=1")
        for e in ext:
            lines.append(e)
        chunks.append("\n".join(lines))
        containment_id += 1

    theme = spec.get("theme", {})
    header = [
        "[General]",
        "immutability=1",
        "lastPackage=com.muskos.accent",
        "",
        f"[Containments][{len(spec['panels']) + 1}]",   # desktop containment
        "DefaultItemIndex=-1",
        "FormFactor=0",
        "IsModified=true",
        "Location=0",
        "Plugin=org.kde.desktopcontainment",
        f"wallpaperplugin={theme.get('wallpaperPlugin', 'org.kde.image')}",
        f"[Containments][{len(spec['panels']) + 1}][ConfigDialog]",
        "DialogOpen=false",
    ]
    return "\n".join(header) + "\n\n" + "\n\n".join(chunks) + "\n"


def validate_spec(name: str, spec: dict) -> list[str]:
    errs: list[str] = []
    if "layout" not in spec or "panels" not in spec:
        errs.append(f"{name}: missing 'layout'/'panels'")
        return errs
    for i, p in enumerate(spec["panels"]):
        loc = p.get("location")
        if loc not in LOC_MAP:
            errs.append(f"{name}: panel {i} bad location {loc!r}")
        for wid in p.get("widgets", {}):
            if wid not in VALID_PLUGINS:
                errs.append(f"{name}: panel {i} unknown Plasma 6 plugin {wid!r}")
    return errs


# --------------------------------------------------------------------------- reload
def reload_shell() -> str:
    """Make Plasma 6 re-read appletsrc. No reparseConfiguration (doesn't exist)."""
    exe = shutil.which("plasmashell")
    if exe and shutil.which("systemctl") and _run(
            ["systemctl", "--user", "is-active", "-q", "plasmashell.service"],
            timeout=10) == 0 and _run(
            ["systemctl", "--user", "restart", "plasmashell.service"], timeout=40) == 0:
        print("  ↻ plasmashell перезапущен через systemctl --user")
        return "systemctl"
    if not exe:
        print("  ! plasmashell не найден — новый макет применится при следующем входе",
              file=sys.stderr)
        return "offline"
    _run(["killall", "-q", "plasmashell"], timeout=10)
    time.sleep(2)
    env = dict(os.environ)
    try:
        subprocess.Popen([exe, "--replace", "--no-splash"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=env)
    except OSError as exc:
        print(f"  ! не удалось перезапустить plasmashell: {exc}", file=sys.stderr)
        return "failed"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if plasmashell_running():
            print("  ↻ plasmashell перезапущен (--replace), панель перечитана")
            return "replace"
        time.sleep(0.5)
    return "failed"


# --------------------------------------------------------------------------- commands
def cmd_list(_: argparse.Namespace) -> int:
    for name, (path, spec) in available().items():
        print(f"  {name:<14} ({path.name})  {spec.get('description','')}")
    return 0


LAYOUT_STATE = HOME / ".config" / "muskos" / "layout.conf"


def cmd_current(_: argparse.Namespace) -> int:
    if LAYOUT_STATE.exists():
        cp = configparser.ConfigParser()
        cp.read(LAYOUT_STATE, encoding="utf-8")
        sec = cp["layout"] if cp.has_section("layout") else {}
        print(sec.get("layout", "(пусто)"))
        return 0
    print("(нет сохранённого макета)")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    found = find_layout(args.name)
    if found is None:
        sys.exit(f"musklayout: неизвестный шаблон {args.name!r}. "
                 f"Доступно: {', '.join(available())}")
    key, spec = found
    errs = validate_spec(key, spec)
    if errs:
        sys.exit("musklayout: шаблон несовместим с Plasma 6:\n  " + "\n  ".join(errs))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = None
    if APPLETSRC.exists():
        backup = BACKUP_DIR / f"{stamp}-{key.replace(' ', '_')}.appletsrc"
        shutil.copy2(APPLETSRC, backup)
        print(f"  бэкап: {backup}")

    txt = render_appletsrc(spec)
    atomic_write(APPLETSRC, txt)
    _write_state(LAYOUT_STATE,
                 "[layout]\n"
                 f"layout={key}\n"
                 "source=musklayout\n"
                 f"backup={backup or ''}\n")

    result = reload_shell()
    if result == "failed" and backup is not None:
        shutil.copy2(backup, APPLETSRC)
        print("  ! plasmashell не поднялся — appletsrc откачен из бэкапа", file=sys.stderr)
        return 1
    print(f"✅ Layout '{key}' applied ({result}).")
    return 0


def _write_state(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def cmd_restore(args: argparse.Namespace) -> int:
    backups = sorted(BACKUP_DIR.glob("*.appletsrc"))
    if not backups:
        sys.exit("musklayout: нечего восстанавливать (пустой каталог бэкапов)")
    idx = max(0, len(backups) - 1 - (args.n or 0))
    src = backups[idx]
    atomic_write(APPLETSRC, src.read_text(encoding="utf-8"))
    print(f"restored {src.name}")
    reload_shell()
    return 0


def cmd_validate(_: argparse.Namespace) -> int:
    total_errs: list[str] = []
    for name, (path, spec) in available().items():
        errs = validate_spec(name, spec)
        rendered = render_appletsrc(spec)
        if "[Containments][1]" not in rendered:
            errs.append(f"{name}: renderer produced no containment")
        status = "OK" if not errs else "FAIL"
        print(f"  {status}  {name:<14} {path.name}")
        total_errs += errs
    if total_errs:
        print("\n".join(total_errs), file=sys.stderr)
        return 1
    print("Все шаблоны совместимы с Plasma 6.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="musklayout", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("current").set_defaults(fn=cmd_current)
    a = sub.add_parser("apply")
    a.add_argument("name")
    a.set_defaults(fn=cmd_apply)
    r = sub.add_parser("restore")
    r.add_argument("n", nargs="?", type=int, default=0, help="how far back (0=latest)")
    r.set_defaults(fn=cmd_restore)
    sub.add_parser("validate").set_defaults(fn=cmd_validate)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
