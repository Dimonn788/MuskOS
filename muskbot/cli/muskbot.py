#!/usr/bin/env python3
"""muskbot — user-facing CLI for the MuskOS AI assistant.

    muskbot status                       # mode, tier verdict, model, health
    muskbot setup cloud --provider openrouter [--key ...]
    muskbot setup local [--model auto|nano-qwen25-3b|...]
    muskbot setup off
    muskbot pull [model_id]              # mirror-first GGUF download via ollama
    muskbot ask "переведи: hello world"  # works in cloud & local modes
    muskbot wait-ready --timeout 120     # used by systemd ExecStartPre
    muskbot doctor                       # RAM/disk/network sanity report

Backend notes: llama.cpp + ollama are implementation details; the user only
sees "Локальный мозг". Chat UI also exists as a Plasma widget (see gui/).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import muskbot_core as core  # noqa: E402

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434")


def cfg_path() -> Path:
    return Path(os.environ.get("MUSKBOT_CONFIG", str(core.CONFIG_PATH)))


def load_cfg() -> core.MuskBotConfig:
    return core.MuskBotConfig.load(cfg_path())


def save_cfg(cfg: core.MuskBotConfig) -> None:
    cfg.save(cfg_path())


# ---------------------------------------------------------------- ollama glue
def ollama_api(path: str, payload: dict | None = None, timeout: float = 10.0):
    url = f"http://{OLLAMA_HOST}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ollama_up() -> bool:
    try:
        ollama_api("/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- commands
def cmd_status(_: argparse.Namespace) -> int:
    cfg = load_cfg()
    ram = core.ram_gib()
    tier, verdict = core.tier_for_ram(ram)
    print(f"Режим: {cfg.mode}")
    if cfg.mode == core.MODE_CLOUD:
        print(f"Провайдер: {cfg.provider}  ключ: {'задан' if cfg.api_key else 'НЕ задан (введите при первом запуске)'}")
    elif cfg.mode == core.MODE_LOCAL:
        m = core.model_by_id(cfg.model) if cfg.model else core.default_model_for_ram(ram)
        print(f"Железо: {ram:.0f} ГБ ОЗУ -> {verdict}")
        print(f"Модель: {m.id if m else '—'}")
        print(f"ollama: {'online' if ollama_up() else 'offline (systemctl --user status muskbot-local)'}")
    else:
        print("Демонов нет, телеметрии нет. Чистая система.")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    cfg.mode = args.mode
    if args.mode == core.MODE_CLOUD:
        cfg.provider = args.provider
        if args.key:
            cfg.api_key = args.key
    elif args.mode == core.MODE_LOCAL:
        ram = core.ram_gib()
        errs = core.validate_choice(
            core.MuskBotConfig(mode="local", model=args.model), ram)
        if errs:
            for e in errs:
                print("🔴", e, file=sys.stderr)
            print("→ Вариант «Облако» доступен на любом железе: "
                  "`muskbot setup cloud`", file=sys.stderr)
            return 1
        cfg.model = args.model or core.default_model_for_ram(ram).id
    else:
        cfg.telemetry = False
    save_cfg(cfg)
    print(f"OK: режим '{cfg.mode}' сохранён в {cfg_path()} (права 0600)")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    if cfg.mode != core.MODE_LOCAL:
        print("muskbot pull имеет смысл только в режиме local", file=sys.stderr)
        return 1
    ram = core.ram_gib()
    model = core.model_by_id(args.model) if args.model else core.default_model_for_ram(ram)
    ok, why = core.can_fit(model, ram)
    if not ok:
        print("🔴", why, file=sys.stderr)
        return 1
    url = model.download_url()          # our mirror first — speed matters
    print(f"⬇  {model.id} ({model.quant}) ~{model.disk_gib} ГБ\n   {url}")
    if not ollama_up():
        print("ollama не отвечает; качаем GGUF напрямую с зеркала…", file=sys.stderr)
        dest = Path.home() / ".local/share/muskbot/models"
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / model.gguf_file
        urllib.request.urlretrieve(url, target)
        print(f"Сохранено: {target}")
        return 0
    # hand to ollama under its canonical name so llama.cpp backend loads it
    name = f"{model.hf_repo.split('/')[-1]}:{model.quant}"
    try:
        ollama_api("/api/pull", {"name": name}, timeout=3600)
        print(f"ollama готов: {name}")
    except urllib.error.URLError as e:
        print(f"не удалось через ollama ({e}); fallback: curl с зеркала", file=sys.stderr)
        subprocess.run(["curl", "-fL", "--retry", "3", "-o",
                        f"/tmp/{model.gguf_file}", url], check=False)
        return 1
    return 0


def _ask_cloud(cfg: core.MuskBotConfig, prompt: str) -> str:
    endpoints = {
        "openrouter": ("https://openrouter.ai/api/v1/chat/completions",
                       "openai/gpt-4o-mini"),
        "openai": ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
        "anthropic": ("https://api.anthropic.com/v1/messages", "claude-3-5-haiku-latest"),
        "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                   "gemini-2.0-flash"),
    }
    url, default_model = endpoints[cfg.provider]
    body = json.dumps({"model": default_model,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode())
    return out["choices"][0]["message"]["content"]


def _ask_local(prompt: str, model_id: str) -> str:
    ram = core.ram_gib()
    m = core.model_by_id(model_id) if model_id else core.default_model_for_ram(ram)
    name = f"{m.hf_repo.split('/')[-1]}:{m.quant}"
    out = ollama_api("/api/generate", {"model": name, "prompt": prompt,
                                       "stream": False}, timeout=600)
    return out["response"]


def cmd_ask(args: argparse.Namespace) -> int:
    cfg = load_cfg()
    if cfg.mode == core.MODE_OFF:
        print("MuskBot выключен. Включите: Параметры системы → ИИ "
              "(или `muskbot setup cloud|local`)", file=sys.stderr)
        return 1
    try:
        answer = (_ask_cloud(cfg, args.prompt) if cfg.mode == core.MODE_CLOUD
                  else _ask_local(args.prompt, cfg.model))
        print(answer)
        return 0
    except Exception as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2


def cmd_wait_ready(args: argparse.Namespace) -> int:
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if ollama_up():
            return 0
        time.sleep(1)
    print("ollama так и не поднялся", file=sys.stderr)
    return 1


def cmd_doctor(_: argparse.Namespace) -> int:
    ram = core.ram_gib()
    disk = core.free_disk_gib("/")
    tier, verdict = core.tier_for_ram(ram)
    print(f"ОЗУ: {ram:.1f} ГБ | Диск свободно: {disk:.1f} ГБ")
    print(verdict)
    for m in core.MODELS:
        ok, why = core.can_fit(m, ram, "/")
        mark = "✅" if ok else "❌"
        print(f"  {mark} {m.id:<28} {m.quant:<8} {m.disk_gib:>5.1f} ГБ  {'' if ok else why}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="muskbot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    st = sub.add_parser("setup")
    st.add_argument("mode", choices=["cloud", "local", "off"])
    st.add_argument("--provider", default="openrouter",
                    choices=list(core.CLOUD_PROVIDERS))
    st.add_argument("--key", default="", help="API key (stored 0600)")
    st.add_argument("--model", default="", help="local model id or '' = auto by RAM")
    st.set_defaults(fn=cmd_setup)
    pl = sub.add_parser("pull")
    pl.add_argument("model", nargs="?", default="")
    pl.set_defaults(fn=cmd_pull)
    ak = sub.add_parser("ask")
    ak.add_argument("prompt")
    ak.set_defaults(fn=cmd_ask)
    wr = sub.add_parser("wait-ready")
    wr.add_argument("--timeout", type=int, default=120)
    wr.set_defaults(fn=cmd_wait_ready)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
