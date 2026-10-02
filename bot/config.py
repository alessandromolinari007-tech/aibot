"""Caricamento configurazione + override validati scritti dall'AI Coach."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_DIR = ROOT / "state"
OVERRIDES_PATH = STATE_DIR / "overrides.json"


def get_path(cfg: dict, dotted: str) -> Any:
    node = cfg
    for part in dotted.split("."):
        node = node[part]
    return node


def set_path(cfg: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def clamp_value(value: float, bounds: dict) -> float | int:
    v = min(max(float(value), float(bounds["min"])), float(bounds["max"]))
    return int(round(v)) if bounds.get("integer") else round(v, 4)


def load_overrides(path: Path = OVERRIDES_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("active", {})
    except (json.JSONDecodeError, OSError) as exc:
        # Self-healing: file corrotto => si ignora e si riparte dalla base.
        log.error("overrides.json illeggibile (%s): uso la configurazione base", exc)
        return {}


def apply_overrides(base: dict, overrides: dict) -> dict:
    """Applica solo override di chiavi whitelisted e sempre dentro i limiti."""
    cfg = copy.deepcopy(base)
    tunable = cfg["coach"]["tunable"]
    for key, value in overrides.items():
        if key not in tunable:
            log.warning("Override ignorato, chiave non modificabile: %s", key)
            continue
        set_path(cfg, key, clamp_value(value, tunable[key]))
    return cfg


def resolve_prop_firm(cfg: dict) -> dict:
    """Applica il profilo prop firm scelto (consistency, drawdown, DLL)."""
    cfg = copy.deepcopy(cfg)
    name = cfg.get("prop_firm", "none")
    profile = cfg.get("prop_firm_profiles", {}).get(name)
    if profile is None:
        profile = {
            "consistency": {"enabled": False, "threshold": 1.0},
            "account_drawdown": {"mode": "static", "max_drawdown_pct": 10.0},
            "daily_loss": {"pct": 5.0},
        }
    cfg["consistency"].update(profile["consistency"])
    cfg["account_drawdown"] = dict(profile["account_drawdown"])
    cfg["risk"]["daily_loss_limit_pct"] = profile["daily_loss"]["pct"]
    return cfg


def load_config(path: Path = CONFIG_PATH, with_overrides: bool = True) -> dict:
    base = resolve_prop_firm(yaml.safe_load(path.read_text()))
    if not with_overrides:
        return base
    return apply_overrides(base, load_overrides())
