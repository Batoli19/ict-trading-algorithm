"""Config loader — reads and validates settings.json"""

import json
from pathlib import Path


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config not found at: {path}")

    with open(path, "r") as f:
        config = json.load(f)

    _validate(config)
    return config


def _validate(cfg: dict):
    required_keys = ["mt5", "pairs", "risk", "ict", "news"]
    for key in required_keys:
        if key not in cfg:
            raise ValueError(f"Missing required config key: '{key}'")

    if not cfg["pairs"]:
        raise ValueError("No trading pairs specified in config.")

    risk = cfg["risk"]
    if risk["risk_per_trade_pct"] > 5:
        raise ValueError("risk_per_trade_pct too high (max 5%). Protect your account!")

    if risk["max_daily_loss_pct"] > 10:
        raise ValueError("max_daily_loss_pct > 10% is dangerous. Reduce it.")
