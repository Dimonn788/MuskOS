# -*- coding: utf-8 -*-
"""
Calamares "muskbrains" module — Step 5 «Мозги и Стиль».

Left column : AI mode (Облако / Локальный мозг / Выкл) with live hardware
              requirements pulled from muskbot_core (RAM tier verdicts).
Right column: accent color grid — exactly the 20 curated presets, click to
              preview instantly on the installer background.

exec():     runs in the *target* system context; writes muskbot config and a
            muskaccent bootstrap so the first login applies the chosen accent.
ui()        returns a QtQuick interface definition (QML string) that Calamares
            loads when built with Qt6 support.
"""

from libcalamares import getGlobalStorage, setGlobalStorage, canChroot, chrootExecute
import os
import json

# --- mirror of muskbot/backend/muskbot_core.py tiers (kept in sync by CI test
#     tests/test_calamares_sync.py which imports both and diffs the tables) ---
TIERS = [
    ("cloud",  "Облако (Cloud API)",
     "Слабый ПК + интернет. OpenRouter/OpenAI/Claude/Gemini. Ключ при первом "
     "запуске или подписка через магазин MuskOS."),
    ("local",  "Локальный мозг (Local LLM)",
     "Параноики и мощные ПК. llama.cpp + ollama скрыты от юзера; модели с нашего "
     "зеркала. Требует ≥8 ГБ ОЗУ (Nano), ≥16 (Mid), ≥32 (Heavy)."),
    ("off",    "Выкл",
     "Никаких демонов, никаких телеметрий. Чистая система."),
]

ACCENT_PRESETS = [  # (id, ru-name, hex) — must equal muskaccent/palette.py PRESETS
    ("cosmic-night", "Космическая ночь", "#2D1B4E"),
    ("nebula", "Туманность", "#5B3A8E"),
    ("orchid", "Орхидея", "#8E5BA6"),
    ("cyberpunk", "Киберпанк", "#E5B80B"),
    ("amber", "Янтарь", "#D98324"),
    ("blood-red", "Кровавый", "#A81E32"),
    ("crimson", "Багряный", "#C2455A"),
    ("magenta", "Магента", "#B0308C"),
    ("rose", "Розовый закат", "#D66A8C"),
    ("neon-blue", "Неоновый синий", "#1E6FD9"),
    ("azure", "Лазурь", "#2E8BC0"),
    ("deep-sea", "Глубокий океан", "#1B3A6B"),
    ("mint", "Мята", "#3FBF8F"),
    ("emerald", "Изумруд", "#1E8E5A"),
    ("forest", "Лес", "#2C6E49"),
    ("teal", "Бирюза", "#1F8A8C"),
    ("graphite", "Графит", "#4A4E5A"),
    ("slate", "Сланец", "#3C4A5A"),
    ("copper", "Медь", "#9C5A2D"),
    ("sand", "Песок", "#B08D57"),
]


def ram_gib_host() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


def local_verdict(ram: float):
    """Same thresholds as muskbot_core.tier_for_ram — single source of truth."""
    if ram < 8:
        return None, "🔴 Недоступно (<8 ГБ). Берите «Облако»."
    if ram < 16:
        return "nano-qwen25-3b", "🟢 Nano: Qwen2.5-3B / Phi-3-mini, Q4_K_M, ~2-3 ГБ."
    if ram < 32:
        return "mid-llama3-8b", "🟡 Mid: Llama-3-8B / Mistral-Nemo-12B, Q5_K_M, ~6-9 ГБ."
    return "heavy-qwen25-32b", "🔵 Heavy: Qwen2.5-32B (~20-24 ГБ); 70B в Q3 если влезет."


def pretty_name():
    return "Мозги и Стиль"


def description():
    return "Выбор ИИ-режима MuskBot и акцентного цвета MuskOS"


def run_muskbrains(job=None):
    """exec() entry point (called inside the installer process)."""
    ram = ram_gib_host()
    mode = getGlobalStorage().get("muskbot_mode", "off")
    accent = getGlobalStorage().get("muskos_accent", "#2D1B4E")
    provider = getGlobalStorage().get("muskbot_provider", "openrouter")

    model = ""
    if mode == "local":
        tier_ok, _ = local_verdict(ram)
        if tier_ok is None:
            # UI should have blocked this; hard guard anyway
            mode = "cloud"
        else:
            model = tier_ok

    cfg = {"mode": mode, "provider": provider, "api_key": "",
           "model": model, "telemetry": False}
    # stage files into the target system
    target = getGlobalStorage().get("ROOT_MOUNTPOINT", "/tmp/root")
    os.makedirs(os.path.join(target, ".config/muskos"), exist_ok=True)
    with open(os.path.join(target, ".config/muskos/muskbot.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(os.path.join(target, ".config/muskos/muskbot.json"), 0o600)
    with open(os.path.join(target, ".config/muskos/installer-choice.json"), "w") as f:
        json.dump({"ai_mode": mode, "accent": accent, "ram_gib": round(ram, 1)}, f)
    # first-login autostart: apply accent + enable systemd bits for chosen mode
    boot = (
        "#!/bin/sh\n"
        f"muskaccent apply {accent}\n"
        "systemctl --user mask muskbot-local.service muskbot-chat.service >/dev/null 2>&1 || true\n"
        "systemctl --user preset muskbot.target 2>/dev/null || true\n"
    )
    if mode == "local":
        boot += "systemctl --user enable --now muskbot.target\nmuskbot pull &\n"
    elif mode == "cloud":
        boot += "systemctl --user enable --now muskbot-cloud.service\n"
    p = os.path.join(target, "etc/profile.d/muskos-firstlogin.sh")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(boot)
    os.chmod(p, 0o755)
    return None


def ui():
    """QtQuick page: radios left, 20 color circles right, live preview."""
    presets_json = json.dumps(
        [{"id": i, "name": n, "hex": h} for i, n, h in ACCENT_PRESETS],
        ensure_ascii=False)
    return f"""
import QtQuick 2.15
import QtQuick.Controls 2.15
import org.kde.calamares 2.0 as Calamares

Item {{
    id: page
    property string aiMode: "off"
    property string accent: "#2D1B4E"
    property real ramGiB: {ram_gib_host():.1f}
    property string verdict: "{local_verdict(ram_gib_host())[1]}"
    property var presets: {presets_json}

    RowLayout {{
        anchors.fill: parent; spacing: 24
        ColumnLayout {{                          // LEFT: brains
            Layout.preferredWidth: page.width * 0.5
            spacing: 8
            Label {{ text: "Интеллект системы"; font.bold: true; font.pixelSize: 18 }}
            Button {{ text: "☁️  Облако (Cloud API)"
                checkable: true; checked: page.aiMode === "cloud"
                onClicked: {{ page.aiMode = "cloud"
                             Calamares.GlobalStorage.insert("muskbot_mode", "cloud") }} }}
            Button {{ text: "🧠  Локальный мозг (Local LLM)"
                enabled: page.ramGiB >= 8
                checkable: true; checked: page.aiMode === "local"
                onClicked: {{ page.aiMode = "local"
                             Calamares.GlobalStorage.insert("muskbot_mode", "local") }} }}
            Label {{ text: page.verdict; wrapMode: Text.WordWrap
                     color: page.ramGiB >= 8 ? "#3FBF8F" : "#A81E32" }}
            Button {{ text: "⏻  Выкл (чистая система)"
                checkable: true; checked: page.aiMode === "off"
                onClicked: {{ page.aiMode = "off"
                             Calamares.GlobalStorage.insert("muskbot_mode", "off") }} }}
        }}
        ColumnLayout {{                          // RIGHT: style grid
            Layout.preferredWidth: page.width * 0.5
            spacing: 8
            Label {{ text: "Акцентный цвет (20 готовых пресетов)"; font.bold: true
                     font.pixelSize: 18 }}
            GridLayout {{
                columns: 5; rowSpacing: 14; columnSpacing: 14
                Repeater {{
                    model: page.presets
                    delegate: Rectangle {{
                        width: 44; height: 44; radius: 22
                        color: modelData.hex
                        border.width: page.accent === modelData.hex ? 4 : 1
                        border.color: "#EDEAF5"
                        MouseArea {{ anchors.fill: parent
                            onClicked: {{
                                page.accent = modelData.hex
                                Calamares.GlobalStorage.insert("muskos_accent", modelData.hex)
                                // instant preview: repaint installer chrome
                                Calamares.Branding.setSidebarColor(modelData.hex)
                            }} }}
                        ToolTip.text: modelData.name + " · " + modelData.hex
                        ToolTip.visible: hovered
                    }}
                }}
            }}
            Label {{ text: "Никакого колорпикера: палитра сбалансирована по контрасту."
                     opacity: 0.7 }}
        }}
    }}
    Rectangle {{                                 // live preview strip
        anchors.bottom: parent.bottom; width: parent.width; height: 64
        color: page.accent
        Label {{ anchors.centerIn: parent; text: "Предпросмотр: панель • окна • кнопки"
                 color: "#FFFFFF" }}
    }}
}}
"""
