"""
Configuration Loader — Settings Validation & Normalization
═══════════════════════════════════════════════════════════
Loads settings.json and performs two critical functions:

    1. NORMALIZATION: Ensures all expected config keys exist with sensible
       defaults, even if the user didn't include them in settings.json.
       Also handles backward compatibility — if old config keys are used,
       they're mapped to the new key names with warnings.

    2. VALIDATION: Checks that critical config sections (mt5, pairs, risk, ict)
       are present and that risk values are within sane ranges. Warns about
       aggressive settings.

Why normalization matters:
    The bot has evolved over time. Config sections like "displacement"
    and "chop_filter" were moved inside "execution" for simplicity.
    The normalizer handles the migration so old configs still work.

Config sections handled:
    - execution:          Strategy execution gates (displacement, chop, reversal)
    - trailing_structure: Structure-based trailing stop parameters
    - trade_management:   Partial take-profits, giveback guard, time exits
    - adaptive_learning:  AI learning phase settings
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger("CONFIG")


def load_config(path: Path) -> dict:
    """
    Load and process the bot's configuration file.

    Steps:
        1. Read JSON from the file
        2. Normalize all sections (apply defaults, migrate old keys)
        3. Validate required sections and sane value ranges
        4. Return the fully processed config dict

    Args:
        path: Path to the settings.json file

    Returns:
        Fully normalized and validated config dictionary.

    Raises:
        ValueError: If required config sections are missing or values are invalid.
    """
    with open(path, "r") as f:
        config = json.load(f)

    # Apply normalization (defaults + backward compatibility) for each section
    _normalize_execution_gates(config)
    _normalize_trailing_structure(config)
    _normalize_trade_management(config)
    _normalize_adaptive_learning(config)

    # Validate required fields and sane value ranges
    _validate(config)

    return config


# ═══════════════════════════════════════════════════════════════════════════
# NORMALIZATION FUNCTIONS
# Each one ensures a config section exists with all expected keys.
# ═══════════════════════════════════════════════════════════════════════════

def _normalize_execution_gates(config: dict):
    """
    Normalize the "execution" section — strategy execution gates.

    These gates control which additional quality checks are applied to signals
    before they're executed:
        - require_displacement:   Require strong candle body (impulse move)
        - avoid_chop:             Block signals in choppy/ranging markets
        - reversal_gate_enabled:  Require sweep + MSS for reversal setups

    BACKWARD COMPATIBILITY:
    Old configs had these as separate top-level sections:
        "displacement": { "atr_multiplier": 1.5 }
        "chop_filter":  { "enabled": true, "lookback": 10 }
        "reversal_gate": { "require_sweep": true }

    These are now mapped into "execution":
        "execution": { "min_displacement_atr_mult": 1.5, "avoid_chop": true, ... }
    """
    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
        config["execution"] = execution

    # Set defaults for execution gates
    defaults = {
        "require_displacement": False,
        "avoid_chop": False,
        "reversal_gate_enabled": False,
        "force_enable_setups": [],  # Setups that bypass some filters
    }
    for key, default_value in defaults.items():
        execution.setdefault(key, default_value)

    # ─── Migrate old "displacement" section ───────────────────────────
    displacement = config.get("displacement")
    if isinstance(displacement, dict):
        if "atr_multiplier" in displacement and "min_displacement_atr_mult" not in execution:
            execution["min_displacement_atr_mult"] = displacement.get("atr_multiplier")
            logger.warning("CONFIG_LEGACY_KEY: displacement.atr_multiplier mapped to execution.min_displacement_atr_mult")
        if "require_close_beyond_structure" in displacement and "require_close_beyond_structure" not in execution:
            execution["require_close_beyond_structure"] = displacement.get("require_close_beyond_structure")
            logger.warning("CONFIG_LEGACY_KEY: displacement.require_close_beyond_structure mapped to execution.require_close_beyond_structure")
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: displacement.* is deprecated; use execution.* displacement keys")

    # ─── Migrate old "chop_filter" section ────────────────────────────
    # Chop filter detects ranging/sideways markets where ICT signals are unreliable
    chop_filter = config.get("chop_filter")
    if isinstance(chop_filter, dict):
        mapping = {
            "enabled": "avoid_chop",                           # Master toggle
            "lookback": "chop_lookback",                       # Bars to check for chop
            "atr_period": "chop_atr_period",                   # ATR calculation period
            "min_atr_pct": "chop_min_atr_pct",                # Min ATR as % of price
            "max_band_pct": "chop_max_band_pct",              # Max range band width
            "max_overlap_pct": "max_overlap_pct",              # Max candle overlap %
            "strict_in_asia_pre_session": "chop_strict_in_asia_pre_session",
            "asia_min_atr_pct": "chop_asia_min_atr_pct",      # Stricter in Asia session
            "asia_max_band_pct": "chop_asia_max_band_pct",
            "asia_max_overlap_pct": "chop_asia_max_overlap_pct",
            "allow_range_reversal_extremes_only": "chop_allow_range_reversal_extremes_only",
        }
        for old_key, new_key in mapping.items():
            if old_key in chop_filter and new_key not in execution:
                execution[new_key] = chop_filter.get(old_key)
                logger.warning(f"CONFIG_LEGACY_KEY: chop_filter.{old_key} mapped to execution.{new_key}")
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: chop_filter.* is deprecated; use execution.* chop keys")

    # ─── Migrate old "reversal_gate" section ──────────────────────────
    # Reversal gate requires specific conditions for reversal trades
    reversal_gate = config.get("reversal_gate")
    if isinstance(reversal_gate, dict):
        mapping = {
            "require_sweep": "reversal_gate_require_sweep",          # Must see liquidity sweep
            "require_mss": "reversal_gate_require_mss",              # Must see market structure shift
            "min_conditions_required": "reversal_gate_min_conditions_required",
        }
        for old_key, new_key in mapping.items():
            if old_key in reversal_gate and new_key not in execution:
                execution[new_key] = reversal_gate.get(old_key)
                logger.warning(f"CONFIG_LEGACY_KEY: reversal_gate.{old_key} mapped to execution.{new_key}")
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: reversal_gate.* is deprecated; use execution.reversal_gate_* keys")


def _normalize_trailing_structure(config: dict):
    """
    Normalize the "trailing_structure" section — structure-based trailing stops.

    Structure trailing uses swing highs/lows (fractals) to trail the stop-loss
    behind price structure rather than using a fixed pip distance. This allows
    profits to run further while still protecting against reversals.

    Key parameters:
        - fractal_left/right: How many bars on each side define a swing point
        - swing_buffer_pips:  Buffer added beyond the swing level
        - min_swing_pips:     Minimum distance between trail levels
        - be_trigger_r_multiple: R-multiple at which to move SL to breakeven

    Per-symbol overrides:
        Gold (XAUUSD) moves 10x more than forex, so it needs different
        fractal and buffer settings. Same for JPY pairs vs EUR pairs.
    """
    ts = config.get("trailing_structure", {})
    if not isinstance(ts, dict):
        ts = {}
        config["trailing_structure"] = ts

    # Default trailing structure parameters
    defaults = {
        "enabled": True,
        "fractal_left": 2,               # Bars to the left of a swing point
        "fractal_right": 2,              # Bars to the right of a swing point
        "swing_buffer_pips": 1.0,        # Pips of buffer beyond swing level
        "swing_tf": "M1",               # Timeframe used for swing detection
        "min_swing_pips": 2.0,          # Minimum distance to trail
        "min_swing_atr_mult": 0.2,      # Minimum swing as multiple of ATR
        "atr_period": 14,               # ATR calculation period
        "allow_ob_trail": True,          # Allow trailing to order block levels
        "ob_min_impulse_atr_mult": 0.8, # Min impulse strength for OB trail
        "be_enabled": True,              # Enable breakeven move
        "be_min_profit_pips": 6.0,      # Min profit before BE move
        "be_trigger_r_multiple": 0.6,   # R-multiple to trigger BE (0.6R = 60% of risk)
        "be_buffer_pips": 0.8,          # Buffer beyond entry for BE level
        "lock_be_as_floor": True,        # Never let trailing go below BE
        # Use the trigger timeframe from the main config, or default to M5
        "trailing_tf": str(config.get("timeframes", {}).get("trigger", "M5")),
    }
    for key, default_value in defaults.items():
        ts.setdefault(key, default_value)

    # ─── Per-symbol overrides ─────────────────────────────────────────
    # Different instruments need different trailing parameters
    per_symbol = ts.get("per_symbol", {})
    if not isinstance(per_symbol, dict):
        per_symbol = {}
        ts["per_symbol"] = per_symbol

    # Sensible defaults for major pairs with different volatility profiles
    major_defaults = {
        "AUDUSD": {
            "fractal_left": 1, "fractal_right": 1,          # Tighter fractals (less volatile)
            "swing_buffer_pips": 0.8, "min_swing_pips": 1.2,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True, "ob_min_impulse_atr_mult": 0.8,
        },
        "GBPUSD": {
            "fractal_left": 2, "fractal_right": 2,          # Wider fractals (more volatile)
            "swing_buffer_pips": 1.0, "min_swing_pips": 1.8,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True, "ob_min_impulse_atr_mult": 0.9,
        },
        "USDJPY": {
            "fractal_left": 2, "fractal_right": 2,
            "swing_buffer_pips": 0.8, "min_swing_pips": 1.5,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True, "ob_min_impulse_atr_mult": 0.8,
        },
        "USDCHF": {
            "fractal_left": 2, "fractal_right": 2,
            "swing_buffer_pips": 0.8, "min_swing_pips": 1.4,
            "min_swing_atr_mult": 0.2,
            "allow_ob_trail": True, "ob_min_impulse_atr_mult": 0.8,
        },
    }
    for sym, sym_defaults in major_defaults.items():
        sym_cfg = per_symbol.get(sym, {})
        if not isinstance(sym_cfg, dict):
            sym_cfg = {}
            per_symbol[sym] = sym_cfg
        for key, value in sym_defaults.items():
            sym_cfg.setdefault(key, value)

    # ─── Disable legacy risk.trailing_stop if structure trailing is on ─
    # The old-style trailing stop (fixed pip distance) conflicts with
    # structure-based trailing. Disable it if structure is enabled.
    risk = config.get("risk", {})
    if isinstance(risk, dict) and ts.get("enabled", True):
        if bool(risk.get("trailing_stop", False)):
            risk["trailing_stop"] = False
            logger.warning("CONFIG_TRAILING: risk.trailing_stop disabled because trailing_structure.enabled=true")

    # Warn about deprecated keys
    if "require_structure_for_be" in ts:
        logger.warning("CONFIG_LEGACY_KEY_IGNORED: trailing_structure.require_structure_for_be is deprecated")


def _normalize_adaptive_learning(config: dict):
    """
    Normalize the "adaptive_learning" section — AI learning system.

    The adaptive learning system has multiple phases:
        Phase 1: Observe — record lessons from losses (passive)
        Phase 2: Generate — create candidate rules and validate them
        Phase 3: Enforce — auto-block entries matching validated bad patterns

    Settings control how aggressive the learning system is:
        - min_losses_before_rule: Need at least 5 losses of a type before creating a rule
        - min_rule_sample_size:   Rule must have 10+ data points to validate
        - min_rule_precision:     Rule must be 65%+ accurate to activate
        - shadow_mode:            If true, rules only LOG, don't block
    """
    al = config.get("adaptive_learning", {})
    if not isinstance(al, dict):
        al = {}
        config["adaptive_learning"] = al

    defaults = {
        "enabled": True,
        "phase": 1,                          # Current learning phase (1-3)
        "entry_blocking_enabled": False,     # Phase 3: auto-block bad entries
        "candidate_ttl_hours": 72,           # Candidate rules expire after 72 hours
        "min_losses_before_rule": 5,         # Need at least 5 losses to create a rule
        "min_rule_sample_size": 10,          # Need 10+ data points to validate
        "min_rule_precision": 0.65,          # Rule must be 65%+ accurate
        "max_active_rules_per_setup": 5,     # Max 5 active rules per setup type
        "cooldown_seconds_after_new_rule": 1800,  # 30 min pause after new rule
        "shadow_mode": False,                # True = rules log only, don't block
    }
    for key, value in defaults.items():
        al.setdefault(key, value)


def _normalize_trade_management(config: dict):
    """
    Normalize the "trade_management" section — open trade management rules.

    This section controls what happens AFTER a trade is opened:

    1. PARTIALS (Partial Take-Profits):
       - TP1: Close 60% at 1.0R, move SL to breakeven
       - TP2: Close 25% at 2.0R, lock SL at 1.0R
       - Remaining 15% rides with trailing stop
       - Static TP1 USD: close $55 at TP1 (overrides R-based calc)

    2. GIVEBACK GUARD:
       - If trade reaches 1.2R but then pulls back
       - Close if it gives back more than 60% of max profit
       - Prevents winners from turning into losers

    3. TIME EXIT:
       - Close trade after 90 minutes if still open
       - Prevents trades from sitting in chop forever
    """
    tm = config.get("trade_management", {})
    if not isinstance(tm, dict):
        tm = {}
        config["trade_management"] = tm

    # ─── Partial take-profit defaults ─────────────────────────────────
    partials = tm.get("partials", {})
    if not isinstance(partials, dict):
        partials = {}
        tm["partials"] = partials

    partial_defaults = {
        "enabled": True,
        "tp1_r": 1.0,                # Close first partial at 1.0R (risk = reward)
        "tp1_close_pct": 0.60,       # Close 60% of position at TP1
        "tp1_sl_mode": "BE_PLUS",    # Move SL to breakeven + buffer after TP1
        "tp1_be_plus_r": 0.05,       # Buffer: 0.05R beyond entry
        "tp2_enabled": True,
        "tp2_r": 2.0,                # Close second partial at 2.0R
        "tp2_close_pct": 0.25,       # Close 25% of remaining position at TP2
        "tp2_sl_lock_r": 1.0,        # Lock SL at 1.0R after TP2
        "trail_only_after_tp1": True, # Only start trailing after TP1 is hit
        "use_static_tp1_usd": True,   # Use fixed USD amount for TP1 instead of R-based
        "tp1_static_usd": 55.0,       # Close $55 at TP1
        "tp1_static_usd_min": 50.0,   # Min static TP1 value
        "tp1_static_usd_max": 60.0,   # Max static TP1 value
        "min_tp2_remaining_usd": 25.0, # Min remaining value to bother with TP2
        "fallback_to_single_tp_if_small_trade": True,  # Use single TP for tiny positions
    }
    for key, value in partial_defaults.items():
        partials.setdefault(key, value)

    # Validate SL mode is one of the allowed values
    sl_mode = str(partials.get("tp1_sl_mode", "BE_PLUS")).upper().strip()
    partials["tp1_sl_mode"] = sl_mode if sl_mode in ("BE", "BE_PLUS") else "BE_PLUS"

    # ─── Giveback guard defaults ──────────────────────────────────────
    giveback = tm.get("giveback_guard", {})
    if not isinstance(giveback, dict):
        giveback = {}
        tm["giveback_guard"] = giveback
    giveback_defaults = {
        "enabled": True,
        "activate_at_r": 1.2,        # Start monitoring after 1.2R profit
        "max_giveback_pct": 0.60,    # Close if gives back 60% of max profit
    }
    for key, value in giveback_defaults.items():
        giveback.setdefault(key, value)

    # ─── Time-based exit defaults ─────────────────────────────────────
    time_exit = tm.get("time_exit", {})
    if not isinstance(time_exit, dict):
        time_exit = {}
        tm["time_exit"] = time_exit
    time_exit_defaults = {
        "enabled": False,             # Disabled by default
        "max_minutes_open": 90,       # Close after 90 minutes
    }
    for key, value in time_exit_defaults.items():
        time_exit.setdefault(key, value)

    # Retry interval for partial close orders (if broker rejects)
    tm.setdefault("partial_retry_seconds", 60)


def _validate(config: dict):
    """
    Validate the configuration for required sections and sane values.

    Checks:
        1. Required sections exist (mt5, pairs, risk, ict)
        2. Risk per trade isn't dangerously high (warns at >5%)
        3. Trading pairs list isn't empty
        4. Daily loss limit sanity check (warns at >10%)
    """
    # Check required top-level sections
    required = ["mt5", "pairs", "risk", "ict"]
    for key in required:
        if key not in config:
            raise ValueError(f"Missing required config key: {key}")

    risk = config["risk"]

    # Warn about aggressive risk settings
    if risk.get("risk_per_trade_pct", 0) > 5:
        logger.warning("⚠️  risk_per_trade_pct > 5% is aggressive!")

    # Must have at least one trading pair
    if not config.get("pairs"):
        raise ValueError("No trading pairs specified")

    logger.info(f"✅  Config validation passed")

    # Warn about high daily loss limits (likely testing mode)
    if risk.get("max_daily_loss_pct", 0) > 10:
        logger.warning(f"⚠️  Daily loss limit: {risk['max_daily_loss_pct']}% — TESTING MODE")
