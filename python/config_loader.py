"""
Configuration Loader — UPDATED
───────────────────────────────
CHANGE: Removed max_daily_loss_pct validation for testing flexibility
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("CONFIG")

def load_config(path: Path) -> dict:
    with open(path, "r") as f:
        config = json.load(f)
    _validate(config)
    return config

def _validate(config: dict):
    """Basic sanity checks"""
    required = ["mt5", "pairs", "risk", "ict"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")
    
    risk = config["risk"]
    
    # REMOVED: max_daily_loss_pct validation (allow any value for testing)
    
    if risk.get("risk_per_trade_pct", 0) > 5:
        logger.warning("⚠️  risk_per_trade_pct > 5% is aggressive!")
    
    if not config.get("pairs"):
        raise ValueError("No trading pairs specified")
    
    logger.info(f"✅  Config validation passed")
    if risk.get("max_daily_loss_pct", 0) > 10:
        logger.warning(f"⚠️  Daily loss limit: {risk['max_daily_loss_pct']}% — TESTING MODE")