#!/usr/bin/env python3
"""Switch Hermes (and Claude Code) between local 9router and remote smart-router.

Usage:
  python switch_router.py status                 # show current router
  python switch_router.py smart [KEY] [--check]  # switch to smart-router
  python switch_router.py 9router [--check]      # switch back to local 9router

Options:
  --target {hermes,claude,both}   which client config to patch (default: both)

Hermes side (config.yaml + .env):
  - Ensures a custom_providers entry named 'smart-router' exists
    (base_url http://10.236.102.86:8320/v1).
  - Sets model.provider to custom:smart-router and model.default to
    claude-router-main; saves the previous values to router_state.json
    so '9router' restores them exactly.
  - Writes the API key to Hermes .env under HERMES_CUSTOM_SMART_ROUTER_API_KEY
    (secrets stay in .env, never in config.yaml).

Key sources for smart switch (priority): CLI arg > SMART_ROUTER_KEY env >
saved state file > previously stored value in Hermes .env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

# ── Constants ───────────────────────────────────────────────────────────────
SETTINGS = Path.home() / ".claude" / "settings.json"

SMART_BASE = "http://10.236.102.86:8320"
SMART_V1 = SMART_BASE + "/v1"
NINE_V1 = "http://127.0.0.1:20128/v1"

SMART_PROVIDER_NAME = "smart-router"
SMART_MODEL_DEFAULT = "claude-router-main"
SMART_KEY_ENV = "HERMES_CUSTOM_SMART_ROUTER_API_KEY"

SMART_MODELS = {
    "ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-router-review",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-router-critical",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-router-engineering",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-router-fast",
}


# ── Hermes paths ────────────────────────────────────────────────────────────
def get_hermes_home() -> Path:
    """Resolve the Hermes home directory (profile-safe)."""
    p = os.environ.get("HERMES_HOME")
    if p:
        path = Path(p)
        if path.is_dir():
            return path
        if path.parent.is_dir():
            return path.parent
    appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
    for c in (Path(appdata) / "hermes", Path.home() / ".hermes"):
        if c.is_dir():
            return c
    return Path(appdata) / "hermes"


HERMES_HOME = get_hermes_home()
HERMES_CFG = HERMES_HOME / "config.yaml"
HERMES_ENV = HERMES_HOME / ".env"
STATE_FILE = HERMES_HOME / "router_state.json"


# ── Small helpers ───────────────────────────────────────────────────────────
def _backup(src: Path) -> Path | None:
    if not src.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = src.with_name(f"{src.name}.bak-{stamp}")
    shutil.copy2(src, dst)
    return dst


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False,
                  allow_unicode=True, width=4096)
    os.replace(tmp, path)


def read_settings() -> dict:
    if not SETTINGS.exists():
        raise SystemExit(f"Missing settings file: {SETTINGS}")
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {SETTINGS}: {exc}")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(data: dict) -> None:
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── .env management (Hermes secrets live here) ─────────────────────────────
def env_get(var: str) -> str | None:
    if not HERMES_ENV.exists():
        return None
    pat = re.compile(rf"^{re.escape(var)}=(.*)$")
    for line in HERMES_ENV.read_text(encoding="utf-8").splitlines():
        m = pat.match(line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return None


def env_set(var: str, value: str) -> None:
    lines: list[str] = []
    if HERMES_ENV.exists():
        lines = HERMES_ENV.read_text(encoding="utf-8").splitlines()
    pat = re.compile(rf"^{re.escape(var)}=")
    replaced = False
    for i, line in enumerate(lines):
        if pat.match(line.strip()):
            lines[i] = f"{var}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{var}={value}")
    _backup(HERMES_ENV)
    tmp = HERMES_ENV.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, HERMES_ENV)


# ── Key resolution ──────────────────────────────────────────────────────────
def resolve_key(cli_key: str | None) -> str:
    key = cli_key or os.environ.get("SMART_ROUTER_KEY") or os.environ.get("SMART_ROUTER_KEY_ENV")
    if not key:
        key = load_state().get("smart_key")
    if not key:
        key = env_get(SMART_KEY_ENV)
    if not key:
        try:
            key = (read_settings().get("env") or {}).get("ANTHROPIC_AUTH_TOKEN")
        except SystemExit:
            key = None
    if not key or not str(key).startswith("sru-"):
        raise SystemExit(
            "Smart-router key not found. Options:\n"
            "  python switch_router.py smart <KEY>\n"
            "  set SMART_ROUTER_KEY environment variable\n"
            f"  or put {SMART_KEY_ENV}=sru-... in {HERMES_ENV}"
        )
    return str(key)


# ── Custom-provider entry helpers ───────────────────────────────────────────
def find_provider(cfg: dict, name: str) -> dict | None:
    for entry in cfg.get("custom_providers") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


# ── Switch: smart-router ────────────────────────────────────────────────────
def switch_smart(key: str, target: str) -> None:
    if target in ("both", "claude"):
        if not SETTINGS.exists():
            print(f"[Claude] skipped (missing {SETTINGS})")
        else:
            data = read_settings()
            env = data.setdefault("env", {})
            env["ANTHROPIC_BASE_URL"] = SMART_BASE
            env["ANTHROPIC_AUTH_TOKEN"] = key
            for name, model in SMART_MODELS.items():
                env[name] = model
            data["model"] = SMART_MODEL_DEFAULT
            _backup(SETTINGS)
            tmp = SETTINGS.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            os.replace(tmp, SETTINGS)
            print(f"[Claude] → smart-router ({SMART_BASE})")

    if target in ("both", "hermes"):
        if not HERMES_CFG.exists():
            print(f"[Hermes] skipped (missing {HERMES_CFG})")
            return

        cfg = _read_yaml(HERMES_CFG)
        model = cfg.setdefault("model", {})

        # Save current profile for exact restore (first time only,
        # unless last switch was back to 9router).
        state = load_state()
        if state.get("active") != "smart-router":
            state["hermes"] = {
                "provider": model.get("provider"),
                "default": model.get("default"),
                "fallback": cfg.get("fallback_providers"),
            }
            state["active"] = "smart-router"

        # Create/update the named custom provider entry.
        entry = find_provider(cfg, SMART_PROVIDER_NAME)
        if entry is None:
            entry = {"name": SMART_PROVIDER_NAME}
            cfg.setdefault("custom_providers", []).append(entry)
        entry["base_url"] = SMART_V1
        entry["key_env"] = SMART_KEY_ENV
        entry["model"] = SMART_MODEL_DEFAULT
        entry["models"] = {m: {} for m in (entry.get("models") or {
            "claude-router-main": {},
            "claude-router-fast": {},
            "claude-router-engineering": {},
            "claude-router-review": {},
            "claude-router-critical": {},
        })} or {
            "claude-router-main": {},
            "claude-router-fast": {},
            "claude-router-engineering": {},
            "claude-router-review": {},
            "claude-router-critical": {},
        }

        model["provider"] = f"custom:{SMART_PROVIDER_NAME}"
        model["default"] = SMART_MODEL_DEFAULT
        model.pop("base_url", None) if not model.get("base_url") else None

        # Fallback chain follows the same provider.
        fb = cfg.get("fallback_providers")
        if isinstance(fb, dict) or isinstance(fb, list):
            items = fb.values() if isinstance(fb, dict) else fb
            for item in items:
                if isinstance(item, dict) and str(item.get("provider", "")).startswith("custom:"):
                    item["provider"] = f"custom:{SMART_PROVIDER_NAME}"

        _backup(HERMES_CFG)
        _write_yaml(HERMES_CFG, cfg)
        env_set(SMART_KEY_ENV, key)
        state["smart_key"] = key
        save_state(state)
        print(f"[Hermes] → smart-router ({SMART_V1}), model={SMART_MODEL_DEFAULT}")
        print(f"[Hermes] key stored as {SMART_KEY_ENV} in {HERMES_ENV}")
        print("[Hermes] Restart the gateway/session for changes to apply.")


# ── Switch: 9router ─────────────────────────────────────────────────────────
def switch_9router(target: str) -> None:
    if target in ("both", "claude"):
        if not SETTINGS.exists():
            print(f"[Claude] skipped (missing {SETTINGS})")
        else:
            data = read_settings()
            env = data.setdefault("env", {})
            env["ANTHROPIC_BASE_URL"] = NINE_V1
            state = load_state()
            saved = state.get("claude") or {}
            for name, model in saved.get("env", {}).items():
                env[name] = model
            if saved.get("model"):
                data["model"] = saved["model"]
            _backup(SETTINGS)
            tmp = SETTINGS.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
            os.replace(tmp, SETTINGS)
            print(f"[Claude] → 9router ({NINE_V1})")

    if target in ("both", "hermes"):
        if not HERMES_CFG.exists():
            print(f"[Hermes] skipped (missing {HERMES_CFG})")
            return

        cfg = _read_yaml(HERMES_CFG)
        model = cfg.setdefault("model", {})
        state = load_state()
        saved = state.get("hermes") or {}

        model["provider"] = saved.get("provider") or "custom:9router"
        model["default"] = saved.get("default") or "chat-all"
        if saved.get("fallback") is not None:
            cfg["fallback_providers"] = saved["fallback"]
        else:
            fb = cfg.get("fallback_providers")
            if isinstance(fb, dict) or isinstance(fb, list):
                items = fb.values() if isinstance(fb, dict) else fb
                for item in items:
                    if isinstance(item, dict) and str(item.get("provider", "")).startswith("custom:"):
                        item["provider"] = "custom:9router"

        _backup(HERMES_CFG)
        _write_yaml(HERMES_CFG, cfg)
        state["active"] = "9router"
        save_state(state)
        print(f"[Hermes] → 9router ({NINE_V1}), model={model['default']}")
        print("[Hermes] Restart the gateway/session for changes to apply.")


# ── Status ──────────────────────────────────────────────────────────────────
def hermes_active() -> tuple[str, str, str]:
    """Return (active, provider, model) for Hermes."""
    if not HERMES_CFG.exists():
        return "missing", "", ""
    cfg = _read_yaml(HERMES_CFG)
    model = cfg.get("model") or {}
    provider = str(model.get("provider") or "")
    default = str(model.get("default") or "")
    if "smart-router" in provider:
        entry = find_provider(cfg, SMART_PROVIDER_NAME) or {}
        active = "smart-router" if SMART_V1 in str(entry.get("base_url", "")) else "smart-router(no entry)"
    elif "9router" in provider:
        active = "9router"
    else:
        active = "unknown"
    return active, provider, default


def status() -> None:
    # Claude
    if SETTINGS.exists():
        env = read_settings().get("env", {})
        base = env.get("ANTHROPIC_BASE_URL", "")
        if base.rstrip("/") == NINE_V1.rstrip("/"):
            c_active = "9router"
        elif base.rstrip("/") == SMART_BASE.rstrip("/"):
            c_active = "smart-router"
        else:
            c_active = "unknown"
        print(f"[Claude] active={c_active} base={base} model={read_settings().get('model', '')}")
    else:
        print(f"[Claude] missing: {SETTINGS}")

    # Hermes
    active, provider, default = hermes_active()
    print(f"[Hermes] active={active} provider={provider} model={default}")
    key = env_get(SMART_KEY_ENV)
    print(f"[Hermes] {SMART_KEY_ENV}: {'set' if key else 'NOT SET'}")
    print(f"[Hermes] config: {HERMES_CFG}")

    # Gateway reachability (best-effort)
    try:
        with urlopen(SMART_BASE + "/health/live", timeout=5) as r:
            print(f"[gateway] {SMART_BASE}/health/live: HTTP {r.status}")
    except Exception as exc:
        print(f"[gateway] unreachable ({exc})")
    try:
        with urlopen(NINE_V1.rstrip('/v1') + "/", timeout=5) as r:
            print(f"[9router] HTTP {r.status}")
    except HTTPError as exc:
        print(f"[9router] HTTP {exc.code}")
    except Exception as exc:
        print(f"[9router] unreachable ({exc})")


# ── Check (E2E probe) ───────────────────────────────────────────────────────
def check_smart(key: str) -> int:
    """Send one OpenAI-format request through /v1/chat/completions."""
    body = json.dumps({
        "model": SMART_MODEL_DEFAULT,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Reply with exactly: ROUTER_OK"}],
    }).encode()
    req = Request(SMART_V1 + "/chat/completions", data=body, method="POST",
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {key}"})
    try:
        with urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        print(f"[check] HTTP {r.status} model={d.get('model')} content={content!r}")
        return 0
    except HTTPError as exc:
        print(f"[check] HTTP {exc.code}: {exc.read().decode()[:200]}")
        return 1
    except Exception as exc:
        print(f"[check] failed: {exc}")
        return 1


def check_9router() -> int:
    try:
        with urlopen(NINE_V1.rstrip('/v1') + "/", timeout=10) as r:
            print(f"[check] 9router HTTP {r.status}")
        return 0
    except HTTPError as exc:
        print(f"[check] 9router HTTP {exc.code}")
        return 0  # any response means the proxy is alive
    except Exception as exc:
        print(f"[check] 9router unreachable: {exc}")
        return 1


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")

    smart = sub.add_parser("smart", help="Use remote smart-router")
    smart.add_argument("smart_key", nargs="?", default=None)
    smart.add_argument("--target", choices=["hermes", "claude", "both"], default="both")
    smart.add_argument("--check", action="store_true", help="E2E test after switching")

    nine = sub.add_parser("9router", help="Use local 9router")
    nine.add_argument("--target", choices=["hermes", "claude", "both"], default="both")
    nine.add_argument("--check", action="store_true")

    args = parser.parse_args()

    if args.command == "status":
        status()
        return 0
    if args.command == "smart":
        key = resolve_key(args.smart_key)
        switch_smart(key, target=args.target)
        return args_check(args, key)
    if args.command == "9router":
        switch_9router(target=args.target)
        return check_9router() if args.check else 0
    return 2


def args_check(args, key: str | None) -> int:
    if getattr(args, "check", False) and key:
        return check_smart(key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
