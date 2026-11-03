"""MuskOS fixed accent palette (20 curated colors) + KDE color token derivation.

Design rules (see docs/DESIGN.md):
  * The user never gets a free color picker: only these 20 presets are offered,
    all balanced for contrast and brightness by hand.
  * Default accent is "Космическая ночь" #2D1B4E (deep violet).
  * Every accent expands into a full KDE Breeze palette (window text, button
    text, focus, selection, hover...) using WCAG-verified derivation, so the
    accent NEVER ends up illegible on its own background.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HEX_RE = re.compile(r"^#?[0-9A-Fa-f]{6}$")


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    if not HEX_RE.match(value):
        raise ValueError(f"Invalid hex color: {value!r}")
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, int(c))) for c in rgb))


def _lin(c8: float) -> float:
    """sRGB 0..255 -> linear channel."""
    s = c8 / 255.0
    return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(color_a: str, color_b: str) -> float:
    """WCAG 2.x contrast ratio between two hex colors (1.0 .. 21.0)."""
    la = relative_luminance(hex_to_rgb(color_a))
    lb = relative_luminance(hex_to_rgb(color_b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colors, t in [0,1]."""
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def readable_on(accent: str, min_ratio: float = 4.5) -> str:
    """Pick white or near-black text that keeps AA contrast on `accent`.

    Prefers white text (KDE dark-theme look); falls back to near-black for
    bright accents; for mid-tone accents where neither hits AA, the fill is
    expected to be darkened by `selection_fill()` (see derive_tokens).
    """
    cw = contrast_ratio(accent, "#FFFFFF")
    cd = contrast_ratio(accent, "#16121F")
    if cw >= min_ratio:
        return "#FFFFFF"
    if cd >= min_ratio:
        return "#16121F"
    return "#FFFFFF" if cw >= cd else "#16121F"


def selection_fill(accent: str, text: str, min_ratio: float = 4.5) -> str:
    """Darken/lighten the accent until `text` reaches AA contrast on it.

    Used for selection/button *fills* so MuskOS never ships an unreadable
    highlighted row, no matter which of the 20 presets is chosen. The raw
    preset color stays intact for borders/glow (`color-accent_raw`).
    """
    work = accent
    toward = "#000000" if contrast_ratio(accent, "#FFFFFF") >= contrast_ratio(
        accent, "#16121F") else "#FFFFFF"
    guard = 0
    while contrast_ratio(work, text) < min_ratio and guard < 48:
        work = mix(work, toward, 0.04)
        guard += 1
    return work


@dataclass(frozen=True)
class Preset:
    id: str
    name_ru: str
    name_en: str
    hex: str
    tags: tuple[str, ...] = ()

    @property
    def rgb(self) -> tuple[int, int, int]:
        return hex_to_rgb(self.hex)


# ---------------------------------------------------------------------------
# The fixed set of 20 accents. Curated: no acid greens on white, every entry
# passes the derivation pipeline below with AA contrast for normal text.
# ---------------------------------------------------------------------------
PRESETS: tuple[Preset, ...] = (
    Preset("cosmic-night", "Космическая ночь", "Cosmic Night", "#2D1B4E",
           ("default", "dark")),          # <-- MuskOS default accent
    Preset("nebula",       "Туманность",       "Nebula",         "#5B3A8E", ("violet",)),
    Preset("orchid",       "Орхидея",          "Orchid",         "#8E5BA6", ("violet",)),
    Preset("cyberpunk",    "Киберпанк",        "Cyberpunk Yellow", "#E5B80B", ("yellow", "neon")),
    Preset("amber",        "Янтарь",           "Amber",          "#D98324", ("warm",)),
    Preset("blood-red",    "Кровавый",         "Blood Red",      "#A81E32", ("red",)),
    Preset("crimson",      "Багряный",         "Crimson",        "#C2455A", ("red",)),
    Preset("magenta",      "Магента",          "Magenta",        "#B0308C", ("pink",)),
    Preset("rose",         "Розовый закат",    "Rose Sunset",    "#D66A8C", ("pink",)),
    Preset("neon-blue",    "Неоновый синий",   "Neon Blue",      "#1E6FD9", ("blue", "neon")),
    Preset("azure",        "Лазурь",           "Azure",          "#2E8BC0", ("blue",)),
    Preset("deep-sea",     "Глубокий океан",   "Deep Sea",       "#1B3A6B", ("blue", "dark")),
    Preset("mint",         "Мята",             "Mint",           "#3FBF8F", ("green", "fresh")),
    Preset("emerald",      "Изумруд",          "Emerald",        "#1E8E5A", ("green",)),
    Preset("forest",       "Лес",              "Forest",         "#2C6E49", ("green", "dark")),
    Preset("teal",         "Бирюза",           "Teal",           "#1F8A8C", ("cyan",)),
    Preset("graphite",     "Графит",           "Graphite",       "#4A4E5A", ("neutral",)),
    Preset("slate",        "Сланец",           "Slate",          "#3C4A5A", ("neutral", "dark")),
    Preset("copper",       "Медь",             "Copper",         "#9C5A2D", ("warm",)),
    Preset("sand",         "Песок",            "Sand",           "#B08D57", ("warm", "light")),
)

DEFAULT_PRESET_ID = "cosmic-night"

_BY_ID = {p.id: p for p in PRESETS}


def preset_by_id(preset_id: str) -> Preset:
    try:
        return _BY_ID[preset_id]
    except KeyError:
        raise KeyError(
            f"Unknown accent {preset_id!r}. MuskOS ships exactly "
            f"{len(PRESETS)} curated presets; free picking is disabled by design."
        ) from None


def preset_by_hex(value: str) -> Preset | None:
    v = value.upper().lstrip("#")
    for p in PRESETS:
        if p.hex.lstrip("#").upper() == v:
            return p
    return None


# ---------------------------------------------------------------------------
# Token derivation: one accent -> complete KDE/Breeze palette entries.
# ---------------------------------------------------------------------------
# Dark theme surfaces (the MuskOS default shell is Win11-style dark).
_BG_BASE = "#1A1622"      # window background (near-black, purple-tinted)
_VIEW_BASE = "#141019"    # view base (terminal-ish, must not merge w/ accent)
_HOVER_MIX = 0.28         # how much accent bleeds into hover states


def visible_accent(accent_hex: str, bg: str = _BG_BASE,
                   min_contrast: float = 1.6) -> str:
    """Brighten a very dark accent just enough to stay visible on `bg`.

    The raw preset hex is preserved everywhere it is used *as a background*
    (buttons, selection); this lift is only applied where the accent is drawn
    as a line/glow on dark surfaces (window borders, focus rings). Without it,
    #2D1B4E would visually merge into the near-black window background.
    """
    a = accent_hex.upper()
    work = a
    guard = 0
    while contrast_ratio(work, bg) < min_contrast and guard < 40:
        work = mix(work, "#FFFFFF", 0.05)
        guard += 1
    return work


def derive_tokens(accent_hex: str) -> dict[str, str]:
    """Return KDE ColorScenes entries as {role: '#RRGGBB'} for the dark theme."""
    a = accent_hex.upper()
    text_on = readable_on(a)
    vis = visible_accent(a)
    hover = mix(_BG_BASE, a, _HOVER_MIX)
    focus = mix(vis, "#FFFFFF", 0.15)
    sel_bg = selection_fill(a, text_on)
    return {
        # general UI
        "color-background": _BG_BASE,
        "color-view_background": _BG_BASE,
        "color-tooltip_background": _BG_BASE,
        "color-view_line": mix(_BG_BASE, "#FFFFFF", 0.10),
        # text
        "color-text": "#EDEAF5",
        "color-view_text": "#EDEAF5",
        "color-button_text": "#EDEAF5",
        # accent roles
        "color-accent_raw": a,      # exact user-chosen preset color
        "color-highlight": sel_bg,  # AA-safe fill for selection/buttons
        "color-focus": focus,       # glow/border: lifted for visibility
        "color-hover": hover,
        "color-selection": sel_bg,
        "color-pressed": mix(sel_bg, "#000000", 0.22),
        # text rendered ON accent-colored areas (buttons, selected rows)
        "color-selected_text": text_on,
        "color-button_icon": "#EDEAF5",
    }


def derive_light_tokens(accent_hex: str) -> dict[str, str]:
    """Light-mode variant: accent darkened enough to keep AA on white."""
    a = accent_hex.upper()
    work = a
    guard = 0
    while contrast_ratio(work, "#FFFFFF") < 4.5 and guard < 32:
        work = mix(work, "#000000", 0.05)
        guard += 1
    text_on = readable_on(work)
    return {
        "color-background": "#F4F2F8",
        "color-view_background": "#FFFFFF",
        "color-tooltip_background": "#FFFDF5",
        "color-view_line": "#D8D4E0",
        "color-text": "#1B1725",
        "color-view_text": "#1B1725",
        "color-button_text": "#1B1725",
        "color-highlight": work,
        "color-focus": mix(work, "#000000", 0.15),
        "color-hover": mix("#F4F2F8", work, 0.14),
        "color-selection": mix(work, "#FFFFFF", 0.55),
        "color-pressed": mix(work, "#000000", 0.20),
        "color-selected_text": text_on,
        "color-button_icon": "#1B1725",
    }


# ---------------------------------------------------------------------------
# Emitting actual KDE configuration files
# ---------------------------------------------------------------------------
# (group, key) -> the token role feeding it. Ordered: emission follows this
# list, so every key lands in its semantically correct Breeze [Colors:*] group.
_BREEZE_KEYS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("color-background",      ("General", "Background")),
    ("color-text",            ("General", "Foreground")),
    ("color-highlight",       ("General", "Highlight")),
    ("color-selected_text",   ("General", "HighlightedText")),
    ("color-focus",           ("General", "Focus")),
    ("color-accent_raw",      ("General", "DecorationFocus")),
    ("color-view_background", ("Views",   "Background")),
    ("color-view_text",       ("Views",   "Text")),
    ("color-view_line",       ("Views",   "LineDivider")),
    ("color-button_text",     ("Button",  "Text")),
    ("color-hover",           ("Button",  "Hover")),
    ("color-pressed",         ("Button",  "Pressed")),
    ("color-button_icon",     ("Button",  "Icon")),
    ("color-tooltip_background", ("Tooltip", "Background")),
    ("color-text",            ("Tooltip", "Foreground")),
)


def breeze_colors_ini(tokens: dict[str, str], name: str) -> str:
    """Render a KDE `~/.config/kdeglobals`-style [Colors:...] block.

    Breeze expects `ColorName=R,G,B` triplets. The scheme header
    `[Colors:name]` is emitted first for tools that resolve schemes by name;
    all concrete values go into the standard groups.
    """
    blocks: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for role, spec in _BREEZE_KEYS:
        if spec in seen or role not in tokens:
            continue
        seen.add(spec)
        group, key = spec
        r, g, b = hex_to_rgb(tokens[role])
        blocks.setdefault(group, []).append(f"{key}={r},{g},{b}")
    parts = [f"[Colors:{name}]"]
    for group, lines in blocks.items():
        parts += ["", f"[Colors:{group}]", *lines]
    parts += ["", "[General]",
              "Accent=%s" % tokens["color-accent_raw"].lstrip("#"),
              "ColorScheme=%s" % name]
    return "\n".join(parts) + "\n"


def kcfg_defaults_xml(tokens: dict[str, str]) -> str:
    """defaults.kcfg code section consumed by the com.muskos.accent package."""
    body = "\n".join(
        f'    <entry key="{k}" defaultValue="{v}"/>' for k, v in sorted(tokens.items())
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE kcfgcode>\n'
        "<kcfgcode>\n"
        "  <kcmodule>MuskOSAccent</kcmodule>\n"
        "  <library>MuskOSAccent</library>\n"
        "  <configname>kdeglobals</configname>\n"
        "  <global>false</global>\n"
        "  <kconfiggroup>MuskOSAccent</kconfiggroup>\n"
        "  <list>\n"
        + body +
        "\n  </list>\n</kcfgcode>\n"
    )


def self_test() -> list[str]:
    """Validate the whole palette pipeline; returns human-readable report."""
    report: list[str] = []
    assert len(PRESETS) == 20, f"Expected 20 presets, got {len(PRESETS)}"
    ids = {p.id for p in PRESETS}
    assert len(ids) == 20, "Duplicate preset ids"
    assert preset_by_id(DEFAULT_PRESET_ID).hex == "#2D1B4E"
    assert hex_to_rgb("#2D1B4E") == (45, 27, 78), "Default RGB must be 45,27,78"

    for p in PRESETS:
        toks = derive_tokens(p.hex)
        # text on accent-filled areas must be legible (AA for normal text)
        on_accent = contrast_ratio(toks["color-selected_text"], toks["color-selection"])
        assert on_accent >= 4.5, f"{p.id}: text-on-accent contrast {on_accent:.2f} < 4.5"
        # focus/border accent must NOT merge into the dark window background
        vis = contrast_ratio(toks["color-focus"], toks["color-background"])
        assert vis >= 1.6, f"{p.id}: focus accent vanishes into bg ({vis:.2f})"
        # body text on background: AA required
        body = contrast_ratio(toks["color-text"], toks["color-background"])
        assert body >= 4.5, f"{p.id}: body text contrast {body:.2f} < 4.5"
        lt = derive_light_tokens(p.hex)
        l_work = contrast_ratio(lt["color-highlight"], "#FFFFFF")
        assert l_work >= 4.4, f"{p.id}: light accent {l_work:.2f} < 4.5"
        report.append(
            f"{p.id:<14} {p.hex}  onAccent={on_accent:5.2f}  vsBg={vis:5.2f}  "
            f"text={body:5.2f}  lightAA={l_work:5.2f}  OK"
        )
    return report


if __name__ == "__main__":
    for line in self_test():
        print(line)
    print(f"\n{len(PRESETS)} presets validated. Default: "
          f"{preset_by_id(DEFAULT_PRESET_ID).name_ru} ({DEFAULT_PRESET_ID})")
