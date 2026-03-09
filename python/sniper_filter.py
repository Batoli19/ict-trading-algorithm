"""
Sniper Filter — High-Precision Trade Entry Gate
═══════════════════════════════════════════════
Active when execution.profile = "SNIPER" or "PROP_CHALLENGE".
This is the STRICTEST quality filter in the pipeline — it sits between
ICTStrategy (signal generation) and actual order execution.

Checks (in order):
    1. R:R floor        — Hard minimum reward-to-risk ratio (e.g. 1.3:1)
    2. SL cap           — Maximum stop-loss in pips (with optional soft cap + buffer)
    3. Entry distance   — How far price has moved since the setup formed
    4. Premium/Discount — Must buy in discount zone, sell in premium zone
    5. Displacement     — Requires a strong candle body (ATR-based) confirming direction
    6. Chop detection   — Blocks entries in low-volatility ranging markets
    7. Reversal gate    — Extra conditions required to trade against HTF bias
    8. Kill zone enforcement — Only trade during London Open, NY Open, London Close
    9. Per-symbol/KZ limits — One trade per symbol per kill zone window

Each check returns (pass/fail, reason, SniperMetrics) for full audit logging.
The SniperMetrics dataclass carries all computed values downstream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("SNIPER")


@dataclass
class SniperMetrics:
    sl_pips: float = 0.0
    rr: float = 0.0
    confidence: float = 0.0
    entry_distance_pips: float = 0.0
    in_discount_premium: bool = True
    risk_scale: float = 1.0
    killzone: str = "NONE"
    override: str = ""
    sl_soft_cap: bool = False
    sl_cap: float = 0.0
    sl_soft_buffer: float = 0.0
    htf_bias: str = "NEUTRAL"
    setup_type: str = "UNKNOWN"
    sweep_detected: bool = False
    displacement_strength: float = 0.0
    structure_shift_detected: bool = False
    market_state: str = "TREND"
    entry_mode: str = "CONFIRMATION"
    entry_price: float = 0.0
    displacement_confirmed: bool = False
    is_reversal: bool = False
    skip_reason: str = ""

    def __getitem__(self, key: str):
        return getattr(self, key)

    def __setitem__(self, key: str, value):
        setattr(self, key, value)


class SniperFilter:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.exec_cfg = cfg.get("execution", {})
        self.hybrid_cfg = cfg.get("hybrid", {})
        self._symbol_kz_window_counts: Dict[tuple[str, str], int] = {}
        self._last_setup_by_symbol_kz: Dict[tuple[str, str], str] = {}

    def enabled(self) -> bool:
        profile = str(self.exec_cfg.get("profile", "normal")).strip().upper()
        return profile in ("SNIPER", "PROP_CHALLENGE")

    def _cfg_for_symbol(self, symbol: str) -> dict:
        sym = str(symbol or "").upper()
        base = dict(self.exec_cfg)
        per_symbol_all = self.exec_cfg.get("per_symbol", {})
        per_symbol = per_symbol_all.get(sym, {}) if isinstance(per_symbol_all, dict) else {}
        merged = {**base, **per_symbol}

        global_sl = base.get("max_sl_pips", 0.0)
        if isinstance(global_sl, dict):
            merged["max_sl_pips"] = self._num(global_sl.get(sym, 0.0), 0.0)
        else:
            merged["max_sl_pips"] = self._num(global_sl, 0.0)
        if "max_sl_pips" in per_symbol:
            merged["max_sl_pips"] = self._num(per_symbol.get("max_sl_pips"), merged["max_sl_pips"])

        global_dist = base.get("max_entry_distance_pips", 0.0)
        if isinstance(global_dist, dict):
            merged["max_entry_distance_pips"] = self._num(global_dist.get(sym, 0.0), 0.0)
        else:
            merged["max_entry_distance_pips"] = self._num(global_dist, 0.0)
        if "max_entry_distance_pips" in per_symbol:
            merged["max_entry_distance_pips"] = self._num(
                per_symbol.get("max_entry_distance_pips"), merged["max_entry_distance_pips"]
            )

        merged["allow_ob_premium_override"] = bool(
            per_symbol.get("allow_ob_premium_override", base.get("ob_override_not_in_discount_premium", False))
        )
        return merged

    def _soft_sl_cfg_for_symbol(self, symbol: str) -> dict:
        sym = str(symbol or "").upper()
        block = self.exec_cfg.get("soft_sl_cap", {})
        if not isinstance(block, dict):
            return {"enabled": False}
        default_cfg = block.get("default", {}) if isinstance(block.get("default", {}), dict) else {}
        per_cfg_all = block.get("per_symbol", {}) if isinstance(block.get("per_symbol", {}), dict) else {}
        per_cfg = per_cfg_all.get(sym, {}) if isinstance(per_cfg_all.get(sym, {}), dict) else {}
        out = {**default_cfg, **per_cfg}
        out["enabled"] = bool(block.get("enabled", False))
        return out

    def _pip_size(self, symbol: str) -> float:
        s = str(symbol or "").upper()
        if "JPY" in s:
            return 0.01
        if s in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in s or "GOLD" in s:
            return 0.1
        return 0.0001

    def _num(self, x, default: float = 0.0) -> float:
        try:
            return float(x)
        except Exception:
            return float(default)

    def _as_candles(self, candles) -> List[dict]:
        return candles if isinstance(candles, list) else []

    def _signal_fields(self, signal) -> Tuple[float, float, float, str, str]:
        entry = self._num(getattr(signal, "entry", None), 0.0)
        sl = self._num(getattr(signal, "sl", None), 0.0)
        tp = self._num(getattr(signal, "tp", None), 0.0)
        direction_raw = getattr(getattr(signal, "direction", None), "value", getattr(signal, "direction", ""))
        direction = str(direction_raw).upper()
        setup_raw = getattr(getattr(signal, "setup_type", None), "value", getattr(signal, "setup_type", ""))
        setup = str(setup_raw).upper()
        return entry, sl, tp, direction, setup

    def compute_sl_pips(self, signal, symbol: str) -> float:
        entry, sl, _, _, _ = self._signal_fields(signal)
        pip = self._pip_size(symbol)
        if pip <= 0:
            return 0.0
        return abs(entry - sl) / pip

    def compute_rr(self, signal) -> float:
        entry, sl, tp, _, _ = self._signal_fields(signal)
        risk = abs(entry - sl)
        if risk <= 0:
            return 0.0
        return abs(tp - entry) / risk

    def compute_entry_distance(self, signal, symbol: str, candles_m5: List[dict]) -> Optional[float]:
        entry, _, _, _, _ = self._signal_fields(signal)
        ref = getattr(signal, "trigger_price", None)
        if ref is None:
            ref = getattr(signal, "zone_price", None)
        if ref is None and candles_m5:
            try:
                ref = candles_m5[-1].get("close")
            except Exception:
                ref = None
        if ref is None:
            return None
        pip = self._pip_size(symbol)
        if pip <= 0:
            return None
        return abs(entry - self._num(ref, entry)) / pip

    def _check_discount_premium(self, signal, candles_m15: List[dict], buffer_pct: float) -> bool:
        if len(candles_m15) < 10:
            return True
        entry, _, _, direction, _ = self._signal_fields(signal)
        recent = candles_m15[-50:] if len(candles_m15) > 50 else candles_m15
        highs = [self._num(c.get("high")) for c in recent if isinstance(c, dict)]
        lows = [self._num(c.get("low")) for c in recent if isinstance(c, dict)]
        if not highs or not lows:
            return True
        hi = max(highs)
        lo = min(lows)
        span = hi - lo
        if span <= 0:
            return True
        pos = (entry - lo) / span
        buy_max = 0.30 + max(0.0, buffer_pct)
        sell_min = 0.70 - max(0.0, buffer_pct)
        if direction == "BUY":
            return pos <= buy_max
        if direction == "SELL":
            return pos >= sell_min
        return True

    def _atr(self, candles: List[dict], period: int) -> Optional[float]:
        if len(candles) < max(3, period + 1):
            return None
        period = max(1, int(period))
        recent = candles[-(period + 1):]
        trs = []
        for i in range(1, len(recent)):
            c = recent[i]
            p = recent[i - 1]
            high = self._num(c.get("high"))
            low = self._num(c.get("low"))
            prev_close = self._num(p.get("close"))
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)
        if not trs:
            return None
        return sum(trs) / len(trs)

    def _displacement_strength(self, candles_m5: List[dict], atr_period: int) -> float:
        if len(candles_m5) < 8:
            return 0.0
        atr = self._atr(candles_m5, atr_period)
        if not atr or atr <= 0:
            return 0.0
        last = candles_m5[-1]
        body = abs(self._num(last.get("close")) - self._num(last.get("open")))
        return body / atr

    def _check_displacement(
        self,
        signal,
        candles_m5: List[dict],
        atr_period: int,
        atr_mult: float,
        require_close_beyond_structure: bool = True,
    ) -> bool:
        if len(candles_m5) < 8:
            return True
        atr = self._atr(candles_m5, atr_period)
        if not atr or atr <= 0:
            return True

        entry, _, _, direction, _ = self._signal_fields(signal)
        last = candles_m5[-1]
        open_ = self._num(last.get("open"))
        close = self._num(last.get("close"), entry)
        body = abs(close - open_)
        if body < float(atr_mult) * atr:
            return False

        prev = candles_m5[-6:-1]
        highs = [self._num(c.get("high")) for c in prev if isinstance(c, dict)]
        lows = [self._num(c.get("low")) for c in prev if isinstance(c, dict)]
        if not highs or not lows:
            return True
        if not require_close_beyond_structure:
            return True
        margin = atr * 0.05
        if direction == "BUY":
            return close >= max(highs) + margin
        if direction == "SELL":
            return close <= min(lows) - margin
        return True

    def _detect_liquidity_sweep(self, signal, candles_m5: List[dict], lookback: int = 24) -> bool:
        if len(candles_m5) < max(10, lookback):
            return False
        _, _, _, direction, _ = self._signal_fields(signal)
        tol_mult = 0.08
        atr = self._atr(candles_m5, 14) or 0.0
        tol = max(atr * tol_mult, 1e-9)
        recent = candles_m5[-lookback:]
        last = recent[-1]
        prev = recent[:-1]
        highs = [self._num(c.get("high")) for c in prev]
        lows = [self._num(c.get("low")) for c in prev]
        if not highs or not lows:
            return False
        eqh = max(highs)
        eql = min(lows)
        if direction == "BUY":
            return self._num(last.get("low")) < (eql - tol) and self._num(last.get("close")) > eql
        if direction == "SELL":
            return self._num(last.get("high")) > (eqh + tol) and self._num(last.get("close")) < eqh
        return False

    def _detect_mss(self, signal, candles_m5: List[dict], lookback: int = 18) -> bool:
        if len(candles_m5) < max(10, lookback):
            return False
        _, _, _, direction, _ = self._signal_fields(signal)
        last = candles_m5[-1]
        prev = candles_m5[-lookback:-1]
        prev_high = max(self._num(c.get("high")) for c in prev)
        prev_low = min(self._num(c.get("low")) for c in prev)
        close = self._num(last.get("close"))
        if direction == "BUY":
            return close > prev_high
        if direction == "SELL":
            return close < prev_low
        return False

    def _htf_bias_single(self, candles: List[dict]) -> str:
        if len(candles) < 25:
            return "NEUTRAL"
        closes = [self._num(c.get("close")) for c in candles[-25:]]
        last = closes[-1]
        sma_fast = sum(closes[-8:]) / 8.0
        sma_slow = sum(closes[-21:]) / 21.0
        if last > sma_fast > sma_slow:
            return "BULLISH"
        if last < sma_fast < sma_slow:
            return "BEARISH"
        return "NEUTRAL"

    def _combined_htf_bias(self, candles_h1: List[dict], candles_h4: List[dict]) -> str:
        b1 = self._htf_bias_single(candles_h1)
        b4 = self._htf_bias_single(candles_h4)
        if b1 == b4 and b1 != "NEUTRAL":
            return b1
        if b4 != "NEUTRAL":
            return b4
        return b1

    def _is_direction_conflict(self, direction: str, htf_bias: str) -> bool:
        if htf_bias == "NEUTRAL":
            return False
        return (direction == "BUY" and htf_bias == "BEARISH") or (direction == "SELL" and htf_bias == "BULLISH")

    def _market_state(self, candles_m5: List[dict], chop_cfg: dict) -> str:
        if len(candles_m5) < 30:
            return "RANGE"
        lookback = int(self._num(chop_cfg.get("lookback", 24), 24))
        atr_period = int(self._num(chop_cfg.get("atr_period", 14), 14))
        atr = self._atr(candles_m5, atr_period) or 0.0
        last_close = self._num(candles_m5[-1].get("close"))
        if last_close <= 0:
            return "RANGE"
        atr_pct = (atr / last_close) * 100.0 if atr > 0 else 0.0
        min_atr_pct = self._num(chop_cfg.get("min_atr_pct", 0.03), 0.03)
        recent = candles_m5[-lookback:]
        hi = max(self._num(c.get("high")) for c in recent)
        lo = min(self._num(c.get("low")) for c in recent)
        band_pct = ((hi - lo) / last_close) * 100.0 if last_close > 0 else 0.0
        max_band_pct = self._num(chop_cfg.get("max_band_pct", 0.25), 0.25)
        overlap = self._chop_overlap_pct(candles_m5, lookback) or 1.0
        max_overlap = self._num(chop_cfg.get("max_overlap_pct", 0.70), 0.70)
        if atr_pct < min_atr_pct and band_pct < max_band_pct:
            return "RANGE"
        if overlap > max_overlap:
            return "RANGE"
        return "TREND"

    def _session_phase(self, now_utc: Optional[datetime] = None) -> str:
        now = now_utc or datetime.now(UTC).replace(tzinfo=None)
        h = int(now.hour)
        if 0 <= h < 6:
            return "ASIA"
        if 6 <= h < 7 or 11 <= h < 12:
            return "PRE_SESSION"
        if 7 <= h < 10:
            return "LONDON"
        if 12 <= h < 15:
            return "NY"
        return "OFF_SESSION"

    def _range_extreme_ok(self, signal, candles_m5: List[dict], lookback: int = 30) -> bool:
        if len(candles_m5) < max(10, lookback):
            return False
        _, _, _, direction, _ = self._signal_fields(signal)
        recent = candles_m5[-lookback:]
        hi = max(self._num(c.get("high")) for c in recent)
        lo = min(self._num(c.get("low")) for c in recent)
        span = hi - lo
        if span <= 0:
            return False
        close = self._num(candles_m5[-1].get("close"))
        pos = (close - lo) / span
        if direction == "BUY":
            return pos <= 0.25
        if direction == "SELL":
            return pos >= 0.75
        return False



    def _entry_distance_limit_pips(self, symbol: str) -> float:
        cfg = self.cfg.get("entry_distance_limit", {})
        if not isinstance(cfg, dict):
            return 0.0
        return self._num(cfg.get(str(symbol or "").upper(), 0.0), 0.0)

    def _chop_overlap_pct(self, candles_m5: List[dict], lookback: int) -> Optional[float]:
        look = candles_m5[-lookback:] if len(candles_m5) >= lookback else candles_m5
        if len(look) < 3:
            return None
        overlap_sum = 0.0
        total_range = 0.0
        for i in range(1, len(look)):
            cur = look[i]
            prv = look[i - 1]
            ch = self._num(cur.get("high"))
            cl = self._num(cur.get("low"))
            ph = self._num(prv.get("high"))
            pl = self._num(prv.get("low"))
            total_range += max(0.0, ch - cl)
            overlap = min(ch, ph) - max(cl, pl)
            if overlap > 0:
                overlap_sum += overlap
        if total_range <= 0:
            return None
        return overlap_sum / total_range

    def _window_id(self, killzone: str, now_utc: Optional[datetime] = None) -> str:
        now = now_utc or datetime.now(UTC).replace(tzinfo=None)
        return f"{now.date().isoformat()}:{str(killzone or 'NONE').upper()}"

    def _sl_cap_for_symbol(self, cfg: dict, symbol: str) -> float:
        sym = str(symbol or "").upper()
        cap_cfg = cfg.get("max_sl_pips", 0.0)
        if isinstance(cap_cfg, dict):
            return self._num(cap_cfg.get(sym, 0.0), 0.0)
        return self._num(cap_cfg, 0.0)

    def _soft_sl_settings(self, cfg: dict, symbol: str) -> dict:
        sym = str(symbol or "").upper()
        sl_cap = self._sl_cap_for_symbol(cfg, sym)
        soft = cfg.get("soft_sl_cap", False)
        out = {
            "enabled": False,
            "sl_cap": sl_cap,
            "soft_buffer": 0.0,
            "hard_mult": 1.0,
            "risk_scale": 1.0,
        }

        if isinstance(soft, bool):
            out["enabled"] = soft
            out["soft_buffer"] = max(0.0, sl_cap * self._num(cfg.get("soft_sl_cap_allow_pct", 0.0), 0.0))
            out["hard_mult"] = 1.0 + max(0.0, self._num(cfg.get("soft_sl_cap_allow_pct", 0.0), 0.0))
            out["risk_scale"] = max(0.0, min(1.0, self._num(cfg.get("soft_sl_cap_risk_scale", 1.0), 1.0)))
            return out

        if isinstance(soft, dict):
            sym_soft = self._soft_sl_cfg_for_symbol(sym)
            out["enabled"] = bool(sym_soft.get("enabled", False))
            out["sl_cap"] = self._num(sym_soft.get("max_sl_pips", sl_cap), sl_cap)
            out["soft_buffer"] = max(0.0, self._num(sym_soft.get("soft_buffer_pips", 0.0), 0.0))
            out["hard_mult"] = max(1.0, self._num(sym_soft.get("hard_reject_multiplier", 2.0), 2.0))
            return out

        return out

    def register_entry(self, symbol: str, killzone: str, setup_type: str):
        if not self.enabled():
            return
        sym = str(symbol).upper()
        win = self._window_id(killzone)
        key = (sym, win)
        self._symbol_kz_window_counts[key] = self._symbol_kz_window_counts.get(key, 0) + 1
        self._last_setup_by_symbol_kz[key] = str(setup_type or "").upper()

    def evaluate(
        self,
        signal,
        symbol: str,
        candles_m5,
        candles_m15,
        candles_h4,
        candles_h1=None,
        killzone: str = "NONE",
        in_killzone: bool = False,
    ) -> tuple[bool, str, SniperMetrics]:
        metrics = SniperMetrics()
        if not self.enabled():
            return True, "OK", metrics

        cfg = self._cfg_for_symbol(symbol)
        sym = str(symbol).upper()
        c5 = self._as_candles(candles_m5)
        c15 = self._as_candles(candles_m15)
        c1h = self._as_candles(candles_h1)
        c4h = self._as_candles(candles_h4)
        _, _, _, direction, setup = self._signal_fields(signal)
        metrics.setup_type = setup

        if setup in ("FVG", "ORDER_BLOCK"):
            metrics.entry_mode = "LIMIT"
            limit_mid = getattr(signal, "zone_midpoint", None)
            if limit_mid is not None:
                original_entry = self._num(getattr(signal, "entry", 0.0))
                original_sl = self._num(getattr(signal, "sl", 0.0))
                original_tp = self._num(getattr(signal, "tp", 0.0))
                original_risk = abs(original_entry - original_sl)
                original_rr = abs(original_tp - original_entry) / original_risk if original_risk > 0 else 2.0
                
                signal.entry = limit_mid
                setattr(signal, "trigger_price", c5[-1].get("close") if c5 else limit_mid)
                metrics.entry_price = limit_mid
                new_risk = abs(limit_mid - original_sl)
                if direction == "BUY":
                    signal.tp = limit_mid + (new_risk * original_rr)
                else:
                    signal.tp = limit_mid - (new_risk * original_rr)
            else:
                metrics.entry_mode = "CONFIRMATION"

        metrics.sl_pips = self.compute_sl_pips(signal, sym)
        metrics.rr = self.compute_rr(signal)
        metrics.confidence = self._num(getattr(signal, "confidence", 0.0), 0.0)
        metrics.killzone = str(killzone or "NONE")
        metrics.risk_scale = 1.0
        metrics.htf_bias = self._combined_htf_bias(c1h, c4h)
        metrics.is_reversal = self._is_direction_conflict(direction, metrics.htf_bias)
        disp_atr_mult = self._num(cfg.get("min_displacement_atr_mult", 0.8), 0.8)
        disp_require_close = bool(cfg.get("require_close_beyond_structure", True))
        atr_period = int(self._num(cfg.get("atr_period", 14), 14))
        displacement_confirmed = self._check_displacement(
            signal,
            c5,
            atr_period,
            disp_atr_mult,
            require_close_beyond_structure=disp_require_close,
        )
        metrics.displacement_confirmed = displacement_confirmed
        metrics.displacement_strength = self._displacement_strength(c5, atr_period)
        metrics.structure_shift_detected = self._detect_mss(signal, c5)
        metrics.sweep_detected = self._detect_liquidity_sweep(signal, c5)
        chop_cfg = {
            "enabled": bool(cfg.get("avoid_chop", False)),
            "lookback": int(self._num(cfg.get("chop_lookback", 24), 24)),
            "atr_period": int(self._num(cfg.get("chop_atr_period", 14), 14)),
            "min_atr_pct": self._num(cfg.get("chop_min_atr_pct", 0.03), 0.03),
            "max_band_pct": self._num(cfg.get("chop_max_band_pct", 0.25), 0.25),
            "max_overlap_pct": self._num(cfg.get("max_overlap_pct", 0.70), 0.70),
            "strict_in_asia_pre_session": bool(cfg.get("chop_strict_in_asia_pre_session", True)),
            "asia_min_atr_pct": self._num(cfg.get("chop_asia_min_atr_pct", 0.04), 0.04),
            "asia_max_band_pct": self._num(cfg.get("chop_asia_max_band_pct", 0.20), 0.20),
            "asia_max_overlap_pct": self._num(cfg.get("chop_asia_max_overlap_pct", 0.60), 0.60),
            "allow_range_reversal_extremes_only": bool(cfg.get("chop_allow_range_reversal_extremes_only", True)),
        }
        phase = self._session_phase()
        if bool(chop_cfg.get("strict_in_asia_pre_session", True)) and phase in ("ASIA", "PRE_SESSION"):
            chop_cfg = {
                **chop_cfg,
                "min_atr_pct": self._num(chop_cfg.get("asia_min_atr_pct", self._num(chop_cfg.get("min_atr_pct", 0.03), 0.03) * 1.35)),
                "max_band_pct": self._num(chop_cfg.get("asia_max_band_pct", self._num(chop_cfg.get("max_band_pct", 0.25), 0.25) * 0.85)),
                "max_overlap_pct": self._num(chop_cfg.get("asia_max_overlap_pct", self._num(chop_cfg.get("max_overlap_pct", 0.70), 0.70) * 0.9)),
            }
        metrics.market_state = self._market_state(c5, chop_cfg)

        # Low confidence no longer hard-blocks execution; keep for downstream sizing/context.
        min_conf = self._num(cfg.get("min_confidence", 0.0))

        rr_hard_floor = self._num(cfg.get("rr_hard_floor", 1.3), 1.3)
        if metrics.rr < rr_hard_floor:
            metrics.skip_reason = "LOW_RR_HARD_FLOOR"
            return False, "LOW_RR_HARD_FLOOR", metrics

        min_rr = self._num(cfg.get("min_rr", 0.0))
        rr_epsilon = max(0.0, self._num(cfg.get("rr_epsilon", 0.0), 0.0))
        if metrics.rr + rr_epsilon < min_rr:
            metrics.skip_reason = "LOW_RR"
            return False, "LOW_RR", metrics

        sl_soft = self._soft_sl_settings(cfg, sym)
        soft_enabled = bool(sl_soft.get("enabled", False))
        sl_cap = self._num(sl_soft.get("sl_cap", 0.0), 0.0)
        soft_buffer = self._num(sl_soft.get("soft_buffer", 0.0), 0.0)
        hard_mult = self._num(sl_soft.get("hard_mult", 1.0), 1.0)
        soft_risk_scale = self._num(sl_soft.get("risk_scale", 1.0), 1.0)
        if sl_cap > 0:
            if soft_enabled:
                hard_limit = sl_cap * max(1.0, hard_mult)
                if metrics.sl_pips > hard_limit:
                    metrics.skip_reason = "SL_TOO_WIDE_PIPS"
                    return False, "SL_TOO_WIDE_PIPS", metrics
                if metrics.sl_pips > sl_cap:
                    if metrics.sl_pips <= (sl_cap + max(0.0, soft_buffer)):
                        metrics["sl_soft_cap"] = True
                        metrics["sl_cap"] = sl_cap
                        metrics["sl_soft_buffer"] = soft_buffer
                        if soft_risk_scale < 1.0:
                            metrics["risk_scale"] = soft_risk_scale
                    else:
                        metrics.skip_reason = "SL_TOO_WIDE_PIPS"
                        return False, "SL_TOO_WIDE_PIPS", metrics
            else:
                if metrics.sl_pips > sl_cap:
                    metrics.skip_reason = "SL_TOO_WIDE_PIPS"
                    return False, "SL_TOO_WIDE_PIPS", metrics

        max_sl_usd = self._num(cfg.get("max_sl_usd", 0.0), 0.0)
        if max_sl_usd > 0:
            # USD risk cap is enforced during lot sizing in RiskManager where account context exists.
            pass

        max_entry_dist = self._entry_distance_limit_pips(sym)
        if max_entry_dist <= 0:
            max_entry_dist = self._num(cfg.get("max_entry_distance_pips", 0.0), 0.0)



        dist = self.compute_entry_distance(signal, sym, c5)
        metrics.entry_distance_pips = self._num(dist, 0.0) if dist is not None else 0.0
        if max_entry_dist > 0 and dist is not None and dist > max_entry_dist:
            metrics.skip_reason = "LATE_ENTRY"
            return False, "LATE_ENTRY", metrics

        require_discount_premium = bool(
            cfg.get(
                "require_discount_premium",
                bool(cfg.get("soft_discount_premium", False)) or bool(cfg.get("dp_strict", False)),
            )
        )
        if require_discount_premium:
            buffer_pct = self._num(cfg.get("discount_premium_buffer_pct", 0.0), 0.0)
            discount_ok = self._check_discount_premium(signal, c15, buffer_pct)
            metrics.in_discount_premium = bool(discount_ok)
            if not discount_ok:
                _, _, _, _, setup_type = self._signal_fields(signal)
                ob_override_enabled = bool(cfg.get("allow_ob_premium_override", False))
                ob_override_min_conf = self._num(cfg.get("ob_override_min_confidence", 0.85), 0.85)
                ob_override_need_disp = bool(cfg.get("ob_override_requires_displacement", True))
                ob_override_ok = (
                    setup_type == "ORDER_BLOCK"
                    and ob_override_enabled
                    and metrics.confidence >= ob_override_min_conf
                    and ((not ob_override_need_disp) or displacement_confirmed)
                )
                if ob_override_ok:
                    metrics["override"] = "OB_PREMIUM_OVERRIDE"
                else:
                    soft_dp = bool(cfg.get("soft_discount_premium", False))
                    dp_min_conf = self._num(cfg.get("dp_min_conf_if_not_dp", min_conf), min_conf)
                    soft_dp_ok = soft_dp and (
                        metrics.confidence >= dp_min_conf or displacement_confirmed
                    )
                    if not soft_dp_ok:
                        metrics.skip_reason = "NOT_IN_DISCOUNT_PREMIUM"
                        return False, "NOT_IN_DISCOUNT_PREMIUM", metrics
        else:
            metrics.in_discount_premium = True

        if bool(cfg.get("require_displacement", False)):
            if not displacement_confirmed:
                metrics.skip_reason = "NO_DISPLACEMENT"
                return False, "NO_DISPLACEMENT", metrics
        elif not displacement_confirmed:
            logger.info(f"INFO: NOT_ENFORCED_DISPLACEMENT: symbol={sym} setup={setup}")
            logger.info(f"RELAXED_GATE: displacement_not_required symbol={sym} setup={setup}")

        if bool(cfg.get("avoid_chop", False)) and metrics.market_state == "RANGE":
            allow_range_reversal = (
                bool(chop_cfg.get("allow_range_reversal_extremes_only", True))
                and metrics.is_reversal
                and metrics.sweep_detected
                and metrics.structure_shift_detected
                and self._range_extreme_ok(signal, c5, int(self._num(chop_cfg.get("lookback", 24), 24)))
            )
            if not allow_range_reversal:
                metrics.skip_reason = "RANGE_CONDITION"
                return False, "RANGE_CONDITION", metrics
        elif bool(cfg.get("avoid_chop", False)):
            lookback = int(self._num(cfg.get("chop_lookback", 30), 30))
            max_overlap = self._num(cfg.get("max_overlap_pct", 0.65), 0.65)
            overlap_pct = self._chop_overlap_pct(c5, lookback)
            if overlap_pct is not None and overlap_pct > max_overlap:
                metrics.skip_reason = "CHOP_MARKET"
                return False, "CHOP_MARKET", metrics
        else:
            if metrics.market_state == "RANGE":
                logger.info(f"INFO: NOT_ENFORCED_CHOP_FILTER: symbol={sym} setup={setup} market_state=RANGE")
                logger.info(f"RELAXED_GATE: chop_not_blocking symbol={sym} setup={setup}")

        reversal_gate_enabled = bool(cfg.get("reversal_gate_enabled", False))
        if metrics.is_reversal:
            if reversal_gate_enabled:
                gate_conditions = [
                    metrics.sweep_detected,
                    displacement_confirmed,
                    metrics.structure_shift_detected,
                ]
                min_required = int(self._num(cfg.get("reversal_gate_min_conditions_required", 2), 2))
                if bool(cfg.get("reversal_gate_require_sweep", False)) and not metrics.sweep_detected:
                    metrics.skip_reason = "NO_SWEEP_FOR_REVERSAL"
                    return False, "NO_SWEEP_FOR_REVERSAL", metrics
                if bool(cfg.get("reversal_gate_require_mss", False)) and not metrics.structure_shift_detected:
                    metrics.skip_reason = "NO_MSS_FOR_REVERSAL"
                    return False, "NO_MSS_FOR_REVERSAL", metrics
                if sum(1 for x in gate_conditions if x) < max(1, min_required):
                    metrics.skip_reason = "REVERSAL_GATE_FAILED"
                    return False, "REVERSAL_GATE_FAILED", metrics
            else:
                logger.info(f"INFO: NOT_ENFORCED_REVERSAL_GATE: symbol={sym} setup={setup}")
                logger.info(f"RELAXED_GATE: reversal_gate_disabled symbol={sym} setup={setup}")

        htf_ctrl = self.cfg.get("htf_bias_control", {})
        if not isinstance(htf_ctrl, dict):
            htf_ctrl = {}
        if bool(htf_ctrl.get("enabled", False)) and metrics.is_reversal:
            if bool(htf_ctrl.get("block_on_strong_conflict", False)) and metrics.htf_bias != "NEUTRAL":
                metrics.skip_reason = "HTF_CONFLICT"
                return False, "HTF_CONFLICT", metrics
            if bool(htf_ctrl.get("reduce_risk_on_conflict", False)):
                metrics.risk_scale *= 0.5

        ict_kz_cfg = self.cfg.get("ict", {}).get("kill_zones", {})
        trade_only_kz = bool(ict_kz_cfg.get("trade_only_in_kill_zones", False))
        if trade_only_kz and not in_killzone:
            metrics.skip_reason = "KILLZONE_LIMIT"
            return False, "KILLZONE_LIMIT", metrics
        if trade_only_kz:
            allowed_kz = self.hybrid_cfg.get("allowed_kill_zones", ["LONDON_OPEN", "NY_OPEN", "LONDON_CLOSE"])
            allowed_set = {str(x).upper() for x in allowed_kz}
            if allowed_set and str(killzone or "NONE").upper() not in allowed_set:
                metrics.skip_reason = "KILLZONE_LIMIT"
                return False, "KILLZONE_LIMIT", metrics

        win = self._window_id(killzone)
        key = (sym, win)
        if bool(cfg.get("one_trade_per_symbol_per_killzone", False)):
            if self._symbol_kz_window_counts.get(key, 0) >= 1:
                metrics.skip_reason = "KILLZONE_LIMIT"
                return False, "KILLZONE_LIMIT", metrics

        if bool(cfg.get("reentry_requires_new_setup", False)):
            _, _, _, _, setup = self._signal_fields(signal)
            last_setup = self._last_setup_by_symbol_kz.get(key)
            if last_setup and last_setup == setup:
                metrics.skip_reason = "KILLZONE_LIMIT"
                return False, "KILLZONE_LIMIT", metrics

        return True, "OK", metrics


if __name__ == "__main__":
    dummy_cfg = {
        "hybrid": {"allowed_kill_zones": ["LONDON_OPEN", "NY_OPEN", "LONDON_CLOSE"]},
        "execution": {
            "profile": "PROP_CHALLENGE",
            "min_confidence": 0.65,
            "min_rr": 2.0,
            "rr_epsilon": 0.02,
            "soft_discount_premium": True,
            "dp_min_conf_if_not_dp": 0.72,
            "ob_override_not_in_discount_premium": True,
            "ob_override_min_confidence": 0.85,
            "ob_override_requires_displacement": True,
            "soft_sl_cap": True,
            "soft_sl_cap_allow_pct": 0.15,
            "soft_sl_cap_risk_scale": 0.5,
            "discount_premium_buffer_pct": 0.0,
            "require_displacement": True,
            "atr_period": 14,
            "min_displacement_atr_mult": 0.8,
            "avoid_chop": True,
            "chop_lookback": 30,
            "max_overlap_pct": 0.95,
            "enforce_killzones": True,
            "one_trade_per_symbol_per_killzone": False,
            "reentry_requires_new_setup": True,
            "per_symbol": {
                "XAUUSD": {
                    "min_rr": 2.0,
                    "max_sl_pips": 60,
                    "max_entry_distance_pips": 500.0,
                    "require_discount_premium": True,
                    "allow_ob_premium_override": True,
                }
            },
        },
    }

    class _Dir:
        value = "BUY"

    class _Setup:
        value = "ORDER_BLOCK"

    class _Signal:
        direction = _Dir()
        setup_type = _Setup()
        entry = 2350.0
        sl = 2349.0
        tp = 2351.99
        confidence = 0.90

    # Build candles so displacement passes but discount/premium for BUY fails.
    candles_m5 = []
    px = 2330.0
    for i in range(40):
        o = px
        if i == 39:
            c = px + 3.0
        else:
            c = px + (0.2 if i % 2 else -0.05)
        h = max(o, c) + 0.3
        l = min(o, c) - 0.2
        candles_m5.append({"open": o, "high": h, "low": l, "close": c})
        px = c

    candles_m15 = []
    px2 = 2320.0
    for i in range(60):
        o = px2
        c = px2 + (0.5 if i % 2 else 0.2)
        h = max(o, c) + 0.4
        l = min(o, c) - 0.3
        candles_m15.append({"open": o, "high": h, "low": l, "close": c})
        px2 = c

    f = SniperFilter(dummy_cfg)
    ok, reason, m = f.evaluate(_Signal(), "XAUUSD", candles_m5, candles_m15, [], "NY_OPEN", True)
    print("SNIPER_PROP_TEST", ok, reason, m["override"], round(m.rr, 4), round(m.confidence, 2), m.risk_scale)
