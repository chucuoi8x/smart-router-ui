"""AI-BOX catalog parsing, classification, and promotion policy."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


FAMILIES = ("deepseek", "qwen", "glm", "kimi")
PRICE_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
# new-api billing: total quota = ratio * (input_tokens + completion_ratio * output_tokens)
# and money = quota / QUOTA_PER_USD. Per million tokens this yields:
#   input  $/M = model_ratio * 1e6 / QUOTA_PER_USD
#   output $/M = model_ratio * completion_ratio * 1e6 / QUOTA_PER_USD
QUOTA_PER_USD = 500000.0  # new-api default quota units per USD


def _as_float(value: Any) -> float | None:
    """Coerce a numeric/string value to float, or return None if not numeric."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _newapi_base_name(name: str) -> str:
    return re.sub(r"\[[^\]]+\]$", "", name.strip().lower())


def discover_public_model_names_from_newapi(entries: Iterable[dict[str, Any]]) -> set[str]:
    """Public model IDs confirmed by the new-api /api/pricing payload (no auth)."""
    found: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_name = entry.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            continue
        found.add(model_name.strip().lower())
        found.add(_newapi_base_name(model_name))
    return found


def parse_newapi_pricing(
    entries: Iterable[dict[str, Any]],
    quota_per_usd: float = QUOTA_PER_USD,
) -> dict[str, dict[str, float | None]]:
    """Convert per-token new-api ratios to USD per million tokens.

    Only per-token (quota_type == 0) entries are converted; per-call entries
    (quota_type == 1) have no per-token price and are omitted.
    """
    factor = 1_000_000.0 / float(quota_per_usd) if quota_per_usd else 0.0
    prices: dict[str, dict[str, float | None]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_name = entry.get("model_name")
        if not isinstance(model_name, str) or not model_name.strip():
            continue
        if int(entry.get("quota_type", 0)) != 0:
            continue
        ratio = _as_float(entry.get("model_ratio"))
        if ratio is None:
            continue
        completion = _as_float(entry.get("completion_ratio"))
        input_price = ratio * factor
        output_price = input_price * completion if completion is not None else None
        entry_prices = {"input_per_million": input_price, "output_per_million": output_price}
        prices[model_name.strip().lower()] = entry_prices
        prices[_newapi_base_name(model_name)] = entry_prices
    return prices


@dataclass(frozen=True)
class ModelRecord:
    name: str
    runtime: bool
    public: bool
    prices: dict[str, float | None]
    family: str | None
    classification: str
    denied: bool
    cheap_eligible: bool
    engineering_eligible: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "runtime": self.runtime,
            "public": self.public,
            "verified": self.runtime and self.public,
            "prices": self.prices,
            "family": self.family,
            "classification": self.classification,
            "denied": self.denied,
            "cheap_eligible": self.cheap_eligible,
            "engineering_eligible": self.engineering_eligible,
            "reason": self.reason,
        }


def normalize_model_name(value: str) -> str:
    """Normalize catalog names while preserving provider model IDs."""
    return value.strip().strip("`'\" ,;:()")


def model_family(model: str, families: Iterable[str] = FAMILIES) -> str | None:
    lower = model.lower()
    for family in families:
        if re.search(rf"(?:^|[-_/]){re.escape(family)}(?:[0-9]|[-_/]|$)", lower):
            return family
    return None


def is_denied(model: str, patterns: Iterable[str]) -> bool:
    lower = model.lower()
    return any(fnmatch.fnmatch(lower, pattern.lower()) for pattern in patterns)


def discover_public_model_names(text: str) -> set[str]:
    """Find public model IDs conservatively from docs/pricing text."""
    found: set[str] = set()
    for match in re.finditer(
        r"\b(?:deepseek|qwen|glm|kimi)(?:[-_/][A-Za-z0-9][A-Za-z0-9._/-]*|[0-9][A-Za-z0-9._/-]*)", text, re.IGNORECASE
    ):
        name = normalize_model_name(match.group(0))
        if len(name) >= 4 and not name.endswith(("/", ".")):
            found.add(name)
    return found


def _price_in_window(window: str, labels: tuple[str, ...]) -> float | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"\b(?:{label_pattern})\b[^0-9$]{{0,90}}\$\s*({PRICE_NUMBER})",
        window,
        re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_model_price(model: str, pricing_text: str, docs_text: str = "") -> dict[str, float | None]:
    """Extract explicit input/output prices; unknown is never treated as zero."""
    sources = []
    for text in (pricing_text, docs_text):
        lower = text.lower()
        start = 0
        while True:
            index = lower.find(model.lower(), start)
            if index < 0:
                break
            sources.append(text[max(0, index - 240) : index + len(model) + 360])
            start = index + len(model)
    input_price = output_price = None
    for window in sources:
        input_price = input_price if input_price is not None else _price_in_window(
            window, ("input", "prompt", "in")
        )
        output_price = output_price if output_price is not None else _price_in_window(
            window, ("output", "completion", "out")
        )
        if input_price is not None and output_price is not None:
            break
    return {"input_per_million": input_price, "output_per_million": output_price}


def _numeric_major(model: str) -> int | None:
    match = re.search(r"qwen\s*([0-9]+)", model.lower())
    return int(match.group(1)) if match else None


def classify_model(model: str, policy: dict[str, Any]) -> tuple[str, str | None, bool, str]:
    """Return class, family, denied flag, and a human-readable reason."""
    deny_patterns = policy.get("global_deny_patterns", [])
    if is_denied(model, deny_patterns):
        return "unknown", model_family(model), True, "global deny pattern"

    family = model_family(model)
    cheap = policy.get("cheap", {})
    engineering = policy.get("engineering", {})
    cheap_families = {str(x).lower() for x in cheap.get("allowed_families", [])}
    engineering_families = {str(x).lower() for x in engineering.get("allowed_families", [])}
    lower = model.lower()

    if family == "qwen" and (_numeric_major(model) or 0) >= 4 and "max" in lower:
        return "critical_candidate", family, False, "new qwen max requires manual critical approval"
    if family in cheap_families and any(marker.lower() in lower for marker in cheap.get("preferred_markers", [])):
        return "cheap", family, False, "family and cheap marker matched"
    if family in engineering_families:
        return "engineering", family, False, "allowed engineering family matched"
    return "unknown", family, False, "family or marker is not allowlisted"


def _within_price(prices: dict[str, float | None], policy: dict[str, Any]) -> tuple[bool, str]:
    input_price = prices.get("input_per_million")
    output_price = prices.get("output_per_million")
    if input_price is None or output_price is None:
        return False, "price unknown"
    if input_price > float(policy.get("max_input_price_per_million", 0)):
        return False, "input price exceeds ceiling"
    if output_price > float(policy.get("max_output_price_per_million", 0)):
        return False, "output price exceeds ceiling"
    return True, "price within ceiling"


def _has_public_confirmation(model: str, public_names: set[str]) -> bool:
    lower = model.lower()
    if lower in public_names:
        return True
    base = re.sub(r"\[[^\]]+\]$", "", lower)
    return base in public_names or any(
        re.sub(r"\[[^\]]+\]$", "", public_name) == base for public_name in public_names
    )


def build_records(
    runtime_models: Iterable[str],
    docs_text: str,
    pricing_text: str,
    policy: dict[str, Any],
    newapi_entries: Iterable[dict[str, Any]] | None = None,
    quota_per_usd: float = QUOTA_PER_USD,
) -> tuple[list[ModelRecord], set[str]]:
    runtime = {normalize_model_name(x) for x in runtime_models if normalize_model_name(x)}
    public_names = {x.lower() for x in discover_public_model_names(docs_text + "\n" + pricing_text)}
    price_override: dict[str, dict[str, float | None]] = {}
    if newapi_entries:
        public_names |= discover_public_model_names_from_newapi(newapi_entries)
        price_override = parse_newapi_pricing(newapi_entries, quota_per_usd)
    records: list[ModelRecord] = []
    for name in sorted(runtime, key=str.lower):
        public = _has_public_confirmation(name, public_names)
        prices = price_override.get(name.lower()) or parse_model_price(name, pricing_text, docs_text)
        classification, family, denied, reason = classify_model(name, policy)
        verified = public
        cheap_ok = False
        engineering_ok = False
        if verified and not denied and classification == "cheap":
            cheap_ok, price_reason = _within_price(prices, policy.get("cheap", {}))
            if not cheap_ok:
                reason = price_reason
        if verified and not denied and classification == "engineering":
            engineering_ok, price_reason = _within_price(prices, policy.get("engineering", {}))
            if not engineering_ok:
                reason = price_reason
        if not verified:
            reason = "runtime-only; no public confirmation"
        records.append(
            ModelRecord(
                name=name,
                runtime=True,
                public=public,
                prices=prices,
                family=family,
                classification=classification,
                denied=denied,
                cheap_eligible=cheap_ok,
                engineering_eligible=engineering_ok,
                reason=reason,
            )
        )
    return records, public_names


def _cheap_score(record: ModelRecord, family_priority: dict[str, int]) -> tuple[int, int, float, str]:
    lower = record.name.lower()
    marker_bonus = 1 if "flash" in lower else 0
    price = record.prices.get("output_per_million")
    return (
        family_priority.get(record.family or "", 0),
        marker_bonus,
        -(price if price is not None else 999999.0),
        record.name.lower(),
    )


def select_routes(records: Iterable[ModelRecord], policy: dict[str, Any]) -> dict[str, list[str]]:
    records = list(records)
    cheap_policy = policy.get("cheap", {})
    engineering_policy = policy.get("engineering", {})
    cheap = sorted(
        (r for r in records if r.cheap_eligible),
        key=lambda r: _cheap_score(r, {"deepseek": 4, "qwen": 3, "glm": 2, "kimi": 1}),
        reverse=True,
    )[: int(cheap_policy.get("max_candidates", 3))]

    engineering_manual = [str(x).lower() for x in engineering_policy.get("manual_preference", [])]
    critical_manual = [
        str(x).lower() for x in policy.get("critical_review", {}).get("manual_preference", [])
    ]
    family_score = {"glm": 4, "deepseek": 3, "qwen": 2, "kimi": 1}
    engineering = sorted(
        (r for r in records if r.engineering_eligible),
        key=lambda r: (
            1 if r.name.lower() in engineering_manual else 0,
            family_score.get(r.family or "", 0),
            -(r.prices.get("output_per_million") or 999999.0),
            r.name.lower(),
        ),
        reverse=True,
    )[: int(engineering_policy.get("max_candidates", 3))]

    critical = [
        r.name
        for r in records
        if r.public and not r.denied and r.classification == "critical_candidate"
    ]
    preferred_critical = [
        r.name for r in records if r.public and not r.denied and r.name.lower() in critical_manual
    ]
    return {
        "cheap": [r.name for r in cheap],
        "engineering": [r.name for r in engineering],
        "critical_review": preferred_critical[:1],
        "critical_candidates": critical,
    }


def sync_catalog(
    config: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    fetch_text: Callable[[str], str] | None = None,
    fetch_json: Callable[[str, dict[str, str] | None], Any] | None = None,
    token: str | None = None,
) -> int:
    """Fetch AI-BOX catalog, persist last-known-good state, return record count.

    Network functions are injectable for deterministic tests. Any sync failure
    leaves existing state untouched and returns zero.
    """
    config = config or _load_runtime_catalog_config()
    policy = policy or _load_runtime_policy()
    if not token:
        token = os.getenv("AIBOX_API_KEY")
    if not token:
        return 0

    fetch_text = fetch_text or _http_text
    fetch_json = fetch_json or _http_json
    state_path = Path(str(config.get("state_file", "state/aibox-catalog.json")))
    previous_path = Path(str(config.get("previous_state_file", "state/aibox-catalog.previous.json")))
    generated_path = Path(str(config.get("generated_routes_file", "state/aibox-routes.generated.yaml")))
    try:
        docs_text = fetch_text(str(config.get("docs_url", "https://api.ai-box.vn/docs")))
        pricing_text = fetch_text(str(config.get("pricing_url", "https://api.ai-box.vn/pricing")))
        pricing_payload = fetch_json(
            str(config.get("pricing_api_url", "https://api.ai-box.vn/api/pricing")), None
        )
        entries = pricing_payload.get("data", []) if isinstance(pricing_payload, dict) else pricing_payload
        if not isinstance(entries, list):
            entries = []
        models_payload = fetch_json(
            str(config.get("models_url", "https://api.ai-box.vn/v1/models")),
            {"Authorization": "Bearer " + token},
        )
        raw_models = models_payload.get("data", models_payload) if isinstance(models_payload, dict) else models_payload
        if not isinstance(raw_models, list):
            return 0
        runtime_models = [str(item.get("id")) for item in raw_models if isinstance(item, dict) and item.get("id")]
        if not runtime_models:
            return 0
        records, public_names = build_records(
            runtime_models,
            docs_text,
            pricing_text,
            policy,
            newapi_entries=entries,
            quota_per_usd=float(config.get("quota_per_usd", QUOTA_PER_USD)),
        )
        if not records:
            return 0
        selected = select_routes(records, policy)
        timestamp = datetime.now(UTC).isoformat()
        warnings = []
        if any(r.reason == "price unknown" for r in records):
            warnings.append("some model prices are unknown; they were not auto-promoted")
        state = state_from_records(records, runtime_models, public_names, selected, timestamp, warnings)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists():
            previous_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(state_path, previous_path)
        _atomic_json_file(state_path, state)
        _atomic_text_file(generated_path, _generated_routes_yaml(selected))
        return len(records)
    except Exception:
        return 0


def _http_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "smart-router-catalog/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"catalog text HTTP {response.status}")
        return response.read().decode("utf-8", "replace")


def _http_json(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers=headers or {"User-Agent": "smart-router-catalog/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"catalog JSON HTTP {response.status}")
        return json.loads(response.read().decode("utf-8", "replace"))


def _atomic_json_file(path: Path, value: Any) -> None:
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _atomic_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _generated_routes_yaml(selected: dict[str, list[str]]) -> str:
    lines = []
    for route_id, key in (
        ("claude-router-aibox-cheap", "cheap"),
        ("claude-router-aibox-engineering", "engineering"),
        ("claude-router-aibox-review", "critical_review"),
    ):
        lines.extend([route_id + ":", "  strategy: priority", "  generated: true", "  candidates:"])
        for model in selected.get(key, []):
            lines.extend(["    - upstream: aibox", f"      model: {model}"])
    return "\n".join(lines) + "\n"


def _load_runtime_catalog_config() -> dict[str, Any]:
    return {
        "docs_url": "https://api.ai-box.vn/docs",
        "pricing_url": "https://api.ai-box.vn/pricing",
        "pricing_api_url": "https://api.ai-box.vn/api/pricing",
        "models_url": "https://api.ai-box.vn/v1/models",
        "quota_per_usd": QUOTA_PER_USD,
        "state_file": "state/aibox-catalog.json",
        "previous_state_file": "state/aibox-catalog.previous.json",
        "generated_routes_file": "state/aibox-routes.generated.yaml",
    }


def _load_runtime_policy() -> dict[str, Any]:
    return {
        "cheap": {"allowed_families": ["deepseek", "qwen"], "preferred_markers": ["flash", "lite"], "max_input_price_per_million": 0.05, "max_output_price_per_million": 0.10, "max_candidates": 3},
        "engineering": {"allowed_families": ["glm", "deepseek", "qwen", "kimi"], "preferred_markers": ["pro", "max", "code"], "max_input_price_per_million": 0.20, "max_output_price_per_million": 0.60, "max_candidates": 3},
        "critical_review": {"manual_preference": ["qwen3.8-max"]},
        "global_deny_patterns": ["*image*", "*embedding*", "*audio*", "*tts*", "*rerank*"],
    }


def state_from_records(
    records: Iterable[ModelRecord],
    runtime_models: Iterable[str],
    public_names: set[str],
    selected_routes: dict[str, list[str]],
    timestamp: str,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    records = list(records)
    runtime = {normalize_model_name(x) for x in runtime_models}
    verified = [r.name for r in records if r.runtime and r.public]
    runtime_only = [r.name for r in records if r.runtime and not r.public]
    docs_only = sorted(public_names - {x.lower() for x in runtime})
    unclassified = [r.name for r in records if r.classification == "unknown" and not r.denied]
    prices = {r.name: r.prices for r in records}
    return {
        "last_successful_sync": timestamp,
        "verified_models": verified,
        "runtime_only": runtime_only,
        "docs_only": docs_only,
        "unclassified_runtime_models": unclassified,
        "prices": prices,
        "selected_routes": selected_routes,
        "critical_candidates": selected_routes.get("critical_candidates", []),
        "discovered": [r.as_dict() for r in records],
        "warnings": warnings or [],
    }
