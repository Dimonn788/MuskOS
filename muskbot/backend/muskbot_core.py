#!/usr/bin/env python3
"""MuskBot — MuskOS AI assistant core.

Single source of truth for the "Мозги" logic:
  * three modes: cloud | local | off            (Calamares step 5, KCM "ИИ")
  * RAM-based local model tiers: Nano / Mid / Heavy (+ 🔴 blocked < 8 GiB)
  * model catalog with quantizations & size budgets
  * mirror-aware download URLs (GitHub/HuggingFace mirror first)
  * systemd unit rendering (ollama hidden from the user)

Used by: Calamares custom module, muskbot CLI, system-settings KCM, daemon.
No third-party deps — stdlib only, import-safe anywhere.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

MODE_CLOUD, MODE_LOCAL, MODE_OFF = "cloud", "local", "off"

# ---------------------------------------------------------------- RAM -> tier
NANO_MIN_GIB, MID_MIN_GIB, HEAVY_MIN_GIB = 8, 16, 32

GGUF_MIRROR = "https://mirror.muskos.org/llama/{model}/resolve/main/{file}"
HF_UPSTREAM = "https://huggingface.co/{model}/resolve/main/{file}"


def _gguf_file(hf_repo: str, quant: str) -> str:
    """Canonical GGUF filename inside a repo, e.g. Qwen2.5-3B-Instruct-Q4_K_M.gguf."""
    base = hf_repo.split("/")[-1]
    for suffix in ("-GGUF", "-gguf"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}-{quant}.gguf"


def ram_gib() -> float:
    """Total physical RAM in GiB (reads /proc/meminfo; overridable for tests)."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return 0.0


@dataclass(frozen=True)
class ModelSpec:
    id: str
    tier: str                 # Nano | Mid | Heavy
    names: tuple[str, ...]    # acceptable aliases (HF repo ids)
    quant: str                # llama.cpp quant tag
    disk_gib: float           # download size
    vram_gib: float           # runtime footprint (llama.cpp backend)
    min_ram_gib: int
    mirrors: tuple[str, ...] = ()
    hf_repo: str = ""

    def download_url(self, template: str = GGUF_MIRROR) -> str:
        """Mirror-first URL: our GitHub/HF mirror beats upstream throughput."""
        return template.format(model=self.hf_repo or self.id,
                               file=_gguf_file(self.hf_repo or self.id, self.quant))

    @property
    def gguf_file(self) -> str:
        return _gguf_file(self.hf_repo or self.id, self.quant)


GGUF_MIRROR = "https://mirror.muskos.org/llama/{model}/resolve/main/{file}"
HF_UPSTREAM = "https://huggingface.co/{model}/resolve/main/{file}"


def _gguf_file(hf_repo: str, quant: str) -> str:
    """Canonical GGUF filename inside a repo, e.g. Qwen2.5-3B-Instruct-Q4_K_M.gguf."""
    base = hf_repo.split("/")[-1]
    for suffix in ("-GGUF", "-gguf"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}-{quant}.gguf"

MODELS: tuple[ModelSpec, ...] = (
    # ---- Nano: 8..15 GiB RAM, ~2-3 GiB on disk/RAM, fast, decent at scripts
    ModelSpec("nano-qwen25-3b", "Nano", ("Qwen2.5-3B-Instruct",),
              "Q4_K_M", 2.0, 2.5, NANO_MIN_GIB,
              mirrors=(GGUF_MIRROR,), hf_repo="Qwen/Qwen2.5-3B-Instruct-GGUF"),
    ModelSpec("nano-phi3-mini", "Nano", ("Phi-3-mini-4k-instruct",),
              "Q4_K_M", 2.3, 2.6, NANO_MIN_GIB,
              mirrors=(GGUF_MIRROR,), hf_repo="microsoft/Phi-3-mini-4k-instruct-gguf"),
    # ---- Mid: 16..31 GiB RAM, golden middle
    ModelSpec("mid-llama3-8b", "Mid", ("Llama-3-8B-Instruct",),
              "Q5_K_M", 5.3, 6.5, MID_MIN_GIB,
              mirrors=(GGUF_MIRROR,), hf_repo="Meta-Llama-3-8B-Instruct-GGUF"),
    ModelSpec("mid-mistral-nemo-12b", "Mid", ("Mistral-Nemo-12B-Instruct",),
              "Q5_K_M", 8.1, 9.0, MID_MIN_GIB,
              mirrors=(GGUF_MIRROR,), hf_repo="Mistral-Nemo-Instruct-2407-GGUF"),
    # ---- Heavy: >= 32 GiB RAM
    ModelSpec("heavy-qwen25-32b", "Heavy", ("Qwen2.5-32B-Instruct",),
              "Q4_K_M", 19.0, 21.0, HEAVY_MIN_GIB,
              mirrors=(GGUF_MIRROR,), hf_repo="Qwen2.5-32B-Instruct-GGUF"),
    ModelSpec("heavy-llama3-70b-abliterated", "Heavy",
              ("Llama-3-70B-Abliterated",),
              "Q3_K_M", 32.0, 34.0, 48,   # only if it actually fits
              mirrors=(GGUF_MIRROR,), hf_repo="Llama-3-70B-Abliterated-GGUF"),
)

_BY_ID = {m.id: m for m in MODELS}


def model_by_id(model_id: str) -> ModelSpec:
    return _BY_ID[model_id]


def tier_for_ram(ram: float) -> tuple[str | None, str]:
    """Map installed RAM to a tier per spec. Returns (tier|None, verdict_ru)."""
    if ram < NANO_MIN_GIB:
        return None, ("🔴 Недоступно: для локального режима нужно минимум 8 ГБ ОЗУ. "
                      "Выберите «Облако».")
    if ram < MID_MIN_GIB:
        return "Nano", (f"🟢 Nano (доступно при {ram:.0f} ГБ): Qwen2.5-3B или Phi-3-mini, "
                        "квант Q4_K_M, ~2-3 ГБ.")
    if ram < HEAVY_MIN_GIB:
        return "Mid", (f"🟡 Mid (доступно при {ram:.0f} ГБ): Llama-3-8B или Mistral-Nemo-12B, "
                       "квант Q5_K_M, ~6-9 ГБ.")
    return "Heavy", (f"🔵 Heavy (доступно при {ram:.0f} ГБ): Qwen2.5-32B (Q4_K_M, ~20-24 ГБ); "
                     "Llama-3-70B-Abliterated только если влезет в Q3.")


def models_for_ram(ram: float | None = None) -> list[ModelSpec]:
    ram = ram_gib() if ram is None else ram
    tier, _ = tier_for_ram(ram)
    if tier is None:
        return []
    out = [m for m in MODELS if m.tier == tier]
    if tier == "Heavy":
        out = [m for m in out if m.min_ram_gib <= ram] or out[:1]
    return out


def default_model_for_ram(ram: float | None = None) -> ModelSpec | None:
    ms = models_for_ram(ram)
    return ms[0] if ms else None


# ---------------------------------------------------------------- free space
def free_disk_gib(path: str | Path = "/") -> float:
    usage = shutil.disk_usage(str(path))
    return usage.free / 1024 ** 3


def can_fit(model: ModelSpec, ram: float | None = None,
            disk_path: str | Path = "/") -> tuple[bool, str]:
    ram = ram_gib() if ram is None else ram
    if ram < model.min_ram_gib:
        return False, f"{model.id}: нужно ≥{model.min_ram_gib} ГБ ОЗУ, есть {ram:.0f}"
    free = free_disk_gib(disk_path)
    if free < model.disk_gib * 1.15:  # 15% headroom
        return False, (f"{model.id}: на диске свободно {free:.1f} ГБ, "
                       f"требуется ≥{model.disk_gib * 1.15:.1f} ГБ")
    return True, "ok"


# ---------------------------------------------------------------- config I/O
CLOUD_PROVIDERS = ("openrouter", "openai", "anthropic", "gemini")


@dataclass
class MuskBotConfig:
    mode: str = MODE_OFF                  # cloud | local | off
    provider: str = "openrouter"          # cloud only
    api_key: str = ""                     # cloud only, stored 0600
    model: str = ""                       # local model id; "" => auto by RAM
    telemetry: bool = False               # Variant В guarantees zero daemons
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        path.chmod(0o600)                  # API key at rest must not be world-readable

    @classmethod
    def load(cls, path: Path) -> "MuskBotConfig":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


CONFIG_PATH = Path.home() / ".config" / "muskos" / "muskbot.json"


# ---------------------------------------------------------------- systemd bits
UNIT_LOCAL = """\
[Unit]
Description=MuskBot local brain (ollama, llama.cpp backend) - hidden from user
Documentation=https://muskos.org/muskbot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_MODELS=/var/lib/muskbot/models
Environment=OLLAMA_KEEP_ALIVE=15m
ExecStart=/usr/bin/ollama serve
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=muskbot.target
"""

UNIT_CHAT = """\
[Unit]
Description=MuskBot chat bridge (starts after ollama is healthy)
After=muskbot-local.service
Requires=muskbot-local.service

[Service]
Type=simple
ExecStartPre=/usr/bin/muskbot wait-ready --timeout 120
ExecStart=/usr/lib/muskos/muskbot/muskbot-chatd
Restart=on-failure

[Install]
WantedBy=muskbot.target
"""

TARGET = """\
[Unit]
Description=MuskOS AI subsystem (enable only when mode != off)
"""


def render_units(mode: str) -> dict[str, str]:
    """Which systemd units exist for a given mode. Variant В => nothing."""
    if mode == MODE_OFF:
        return {}
    if mode == MODE_CLOUD:
        return {"muskbot-cloud.service": UNIT_CHAT.replace(
            "After=muskbot-local.service\nRequires=muskbot-local.service",
            "After=network-online.target")}
    return {"muskbot.target": TARGET,
            "muskbot-local.service": UNIT_LOCAL,
            "muskbot-chat.service": UNIT_CHAT}


# ---------------------------------------------------------------- validation
def validate_choice(cfg: MuskBotConfig, ram: float | None = None,
                    disk_path: str | Path = "/") -> list[str]:
    """Return human-readable errors (empty list == choice is installable)."""
    ram = ram_gib() if ram is None else ram
    errs: list[str] = []
    if cfg.mode == MODE_CLOUD:
        if cfg.provider not in CLOUD_PROVIDERS:
            errs.append(f"Неизвестный провайдер {cfg.provider!r}")
        # key may arrive post-install (first run), so it's a warning-level gap
    elif cfg.mode == MODE_LOCAL:
        tier, verdict = tier_for_ram(ram)
        if tier is None:
            errs.append(verdict)
            return errs
        model = model_by_id(cfg.model) if cfg.model else default_model_for_ram(ram)
        if model is None:
            errs.append("Нет подходящей модели для этого железа")
            return errs
        if model.tier != tier:
            errs.append(f"Модель {model.id} относится к уровню {model.tier}, "
                        f"а ваше железо тянет только {tier}")
        ok, why = can_fit(model, ram, disk_path)
        if not ok:
            errs.append(why)
    elif cfg.mode == MODE_OFF:
        if cfg.telemetry:
            errs.append("Режим «Выкл» несовместим с телеметрией")
    else:
        errs.append(f"Неизвестный режим {cfg.mode!r}")
    return errs


if __name__ == "__main__":
    r = ram_gib()
    tier, verdict = tier_for_ram(r)
    print(json.dumps({
        "ram_gib": round(r, 1), "tier": tier, "verdict": verdict,
        "models": [asdict(m) for m in models_for_ram(r)],
        "units_for_local": sorted(render_units(MODE_LOCAL)),
    }, ensure_ascii=False, indent=2))
