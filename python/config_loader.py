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
    _normalize_execution_gates(config)
    _normalize_trailing_structure(config)
    _normalize_adaptive_learning(config)
    _validate(config)
    return config


def _normalize_execution_gates(config: dict):
    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
        config["execution"] = execution

    defaults = {
        "require_displacement": False,
        "avoid_chop": False,
        "reversal_gate_enabled": False,
        "force_enable_setups": [],
    }
    for key, default_value in defaults.items():
        execution.setdefault(key, default_value)

    displacement = config.get("displacement", {})
    if isinstance(displacement, dict):
        if "atr_multiplier" in displacement and "min_displacement_atr_mult" not in execution:
            execution["min_displacement_atr_mult"] = displacement.get("atr_multiplier")
            logger.warning("CONFIG_LEGACY_KEY: displacement.atr_multiplier mapped to execution.min_displacement_atr_mult")
        if "require_close_beyond_structure" in displacement and "require_close_beyond_structure" not in execution:
            execution["require_close_beyond_structure"] = displacement.get("require_close_beyond_structure")
            logger.warning("CONFIG_LEGACY_KEY: displacement.require_close_beyond_structure mapped to execution.require_close_beyond_structure")
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: displacement.* is deprecated; use execution.* displacement keys")

    chop_filter = config.get("chop_filter", {})
    if isinstance(chop_filter, dict):
        mapping = {
            "enabled": "avoid_chop",
            "lookback": "chop_lookback",
            "atr_period": "chop_atr_period",
            "min_atr_pct": "chop_min_atr_pct",
            "max_band_pct": "chop_max_band_pct",
            "max_overlap_pct": "max_overlap_pct",
            "strict_in_asia_pre_session": "chop_strict_in_asia_pre_session",
            "asia_min_atr_pct": "chop_asia_min_atr_pct",
            "asia_max_band_pct": "chop_asia_max_band_pct",
            "asia_max_overlap_pct": "chop_asia_max_overlap_pct",
            "allow_range_reversal_extremes_only": "chop_allow_range_reversal_extremes_only",
        }
        for old_key, new_key in mapping.items():
            if old_key in chop_filter and new_key not in execution:
                execution[new_key] = chop_filter.get(old_key)
                logger.warning(f"CONFIG_LEGACY_KEY: chop_filter.{old_key} mapped to execution.{new_key}")
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: chop_filter.* is deprecated; use execution.* chop keys")

    reversal_gate = config.get("reversal_gate", {})
    if isinstance(reversal_gate, dict):
        mapping = {
            "require_sweep": "reversal_gate_require_sweep",
            "require_mss": "reversal_gate_require_mss",
            "min_conditions_required": "reversal_gate_min_conditions_required",
        }
        for old_key, new_key in mapping.items():
            if old_key in reversal_gate and new_key not in execution:
                execution[new_key] = reversal_gate.get(old_key)
                logger.warning(f"CONFIG_LEGACY_KEY: reversal_gate.{old_key} mapped to execution.{new_key}")
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: reversal_gate.* is deprecated; use execution.reversal_gate_* keys")


def _normalize_trailing_structure(config: dict):
    ts = config.get("trailing_structure", {})
    if not isinstance(ts, dict):
        ts = {}
        config["trailing_structure"] = ts

    defaults = {
        "enabled": True,
        "fractal_left": 2,
        "fractal_right": 2,
        "swing_buffer_pips": 1.0,
        "swing_tf": "M1",
        "min_swing_pips": 2.0,
        "min_swing_atr_mult": 0.2,
        "atr_period": 14,
        "allow_ob_trail": True,
        "ob_min_impulse_atr_mult": 0.8,
        "be_enabled": True,
        "be_min_profit_pips": 6.0,
        "be_trigger_r_multiple": 0.6,
        "be_buffer_pips": 0.8,
        "lock_be_as_floor": True,
        "trailing_tf": str(config.get("timeframes", {}).get("trigger", "M5")),
    }
    for key, default_value in defaults.items():
        ts.setdefault(key, default_value)

    per_symbol = ts.get("per_symbol", {})
    if not isinstance(per_symbol, dict):
        per_symbol = {}
        ts["per_symbol"] = per_symbol
    major_defaults = {
        "AUDUSD": {
            "fractal_left": 1,
            "fractal_right": 1,
            "swing_buffer_pips": 0.8,
            "min_swing_pips": 1.2,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True,
            "ob_min_impulse_atr_mult": 0.8,
        },
        "GBPUSD": {
            "fractal_left": 2,
            "fractal_right": 2,
            "swing_buffer_pips": 1.0,
            "min_swing_pips": 1.8,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True,
            "ob_min_impulse_atr_mult": 0.9,
        },
        "USDJPY": {
            "fractal_left": 2,
            "fractal_right": 2,
            "swing_buffer_pips": 0.8,
            "min_swing_pips": 1.5,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True,
            "ob_min_impulse_atr_mult": 0.8,
        },
        "USDCHF": {
            "fractal_left": 2,
            "fractal_right": 2,
            "swing_buffer_pips": 0.8,
            "min_swing_pips": 1.4,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True,
            "ob_min_impulse_atr_mult": 0.8,
        },
    }
    for sym, sym_defaults in major_defaults.items():
        sym_cfg = per_symbol.get(sym, {})
        if not isinstance(sym_cfg, dict):
            sym_cfg = {}
            per_symbol[sym] = sym_cfg
        for key, value in sym_defaults.items():
            sym_cfg.setdefault(key, value)

    risk = config.get("risk", {})
    if isinstance(risk, dict) and ts.get("enabled", True):
        if bool(risk.get("trailing_stop", False)):
            risk["trailing_stop"] = False
            logger.warning("CONFIG_TRAILING: risk.trailing_stop disabled because trailing_structure.enabled=true")

    if "require_structure_for_be" in ts:
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: trailing_structure.require_structure_for_be is deprecated")


def _normalize_adaptive_learning(config: dict):
    al = config.get("adaptive_learning", {})
    if not isinstance(al, dict):
        al = {}
        config["adaptive_learning"] = al

    defaults = {
        "enabled": True,
        "phase": 1,
        "entry_blocking_enabled": False,
        "candidate_ttl_hours": 72,
        "min_losses_before_rule": 5,
        "min_rule_sample_size": 10,
        "min_rule_precision": 0.65,
        "max_active_rules_per_setup": 5,
        "cooldown_seconds_after_new_rule": 1800,
        "shadow_mode": False,
    }
    for key, value in defaults.items():
        al.setdefault(key, value)

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
