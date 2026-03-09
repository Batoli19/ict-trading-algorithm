"""
Trailing Manager — Structure-Based Stop Loss Management
════════════════════════════════════════════════════════
Moves stop-loss levels for open positions using ICT market structure
concepts rather than simple pip-based trailing.

Trailing methods (highest priority wins):
    1. BREAKEVEN (BE):  Move SL to entry + buffer after sufficient profit
                        (triggered by pip threshold OR R-multiple)
    2. SWING TRAIL:     Trail behind fractal swing lows (BUY) or swing highs (SELL)
                        using M1 or M5 candles with configurable fractal params
    3. ORDER BLOCK (OB):Trail behind the last opposing candle before impulse
    4. TP MISS PROTECT: Lock in 90% of profit when price gets within 2 pips of TP

Safety checks:
    - SL can only tighten (never widen) — enforced by floor/ceiling logic
    - Market validation ensures new SL respects broker stops_level/freeze_level
    - Per-symbol config allows tuning fractal sensitivity for different instruments

Each position is tracked via TrailingState (keyed by ticket) to remember
the last applied SL and prevent regression.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("TRAIL")


@dataclass
class TrailingState:
    last_sl: float = 0.0


class StructureTrailingManager:
    def __init__(self, config: dict):
        self.root_cfg = config or {}
        self.tf_cfg = self.root_cfg.get("timeframes", {}) if isinstance(self.root_cfg.get("timeframes", {}), dict) else {}
        self.scalping_cfg = self.root_cfg.get("scalping", {}) if isinstance(self.root_cfg.get("scalping", {}), dict) else {}
        self.risk_cfg = self.root_cfg.get("risk", {}) if isinstance(self.root_cfg.get("risk", {}), dict) else {}

        self.cfg = (
            self.root_cfg.get("trailing_structure", {})
            if isinstance(self.root_cfg.get("trailing_structure", {}), dict)
            else {}
        )
        self.enabled = bool(self.cfg.get("enabled", True))
        self._state: Dict[int, TrailingState] = {}

        self._base_defaults = {
            "fractal_left": int(self.cfg.get("fractal_left", 2)),
            "fractal_right": int(self.cfg.get("fractal_right", 2)),
            "swing_buffer_pips": float(self.cfg.get("swing_buffer_pips", 1.0)),
            "min_swing_pips": float(self.cfg.get("min_swing_pips", 2.0)),
            "min_swing_atr_mult": float(self.cfg.get("min_swing_atr_mult", 0.2)),
            "atr_period": int(self.cfg.get("atr_period", 14)),
            "allow_ob_trail": bool(self.cfg.get("allow_ob_trail", True)),
            "ob_min_impulse_atr_mult": float(self.cfg.get("ob_min_impulse_atr_mult", 0.8)),
            "be_enabled": bool(self.cfg.get("be_enabled", True)),
            "be_min_profit_pips": float(self.cfg.get("be_min_profit_pips", 6.0)),
            "be_trigger_r_multiple": float(self.cfg.get("be_trigger_r_multiple", 0.6)),
            "be_buffer_pips": float(self.cfg.get("be_buffer_pips", 0.8)),
            "lock_be_as_floor": bool(self.cfg.get("lock_be_as_floor", True)),
            "swing_tf": str(self.cfg.get("swing_tf", "M1")).upper(),
            "trailing_tf": str(self.cfg.get("trailing_tf", self.tf_cfg.get("trigger", "M5"))).upper(),
        }

        self._symbol_defaults = {
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
            # Gold needs much larger values — 1 pip = $0.10, ATR ~200+ pips/day
            "XAUUSD": {
                "fractal_left": 3,
                "fractal_right": 3,
                "swing_buffer_pips": 5.0,
                "min_swing_pips": 10.0,
                "min_swing_atr_mult": 0.3,
                "allow_ob_trail": True,
                "ob_min_impulse_atr_mult": 1.0,
                "be_enabled": True,
                "be_min_profit_pips": 30.0,
                "be_trigger_r_multiple": 0.8,
                "be_buffer_pips": 3.0,
                "swing_tf": "M5",
                "trailing_tf": "M5",
            },
        }

    def _sym_cfg(self, symbol: str) -> dict:
        sym = str(symbol or "").upper()
        per_all = self.cfg.get("per_symbol", {}) if isinstance(self.cfg.get("per_symbol", {}), dict) else {}
        per_sym = per_all.get(sym, {}) if isinstance(per_all.get(sym, {}), dict) else {}
        merged = {**self._base_defaults, **self._symbol_defaults.get(sym, {}), **per_sym}
        merged["swing_tf"] = str(merged.get("swing_tf", "M1")).upper()
        merged["trailing_tf"] = str(merged.get("trailing_tf", self.tf_cfg.get("trigger", "M5"))).upper()
        return merged

    def select_trailing_timeframe(self, position: dict, default_tf: str = "M5") -> str:
        symbol = str(position.get("symbol", "")).upper()
        sym_cfg = self._sym_cfg(symbol)
        fallback = str(sym_cfg.get("trailing_tf", default_tf or "M5")).upper()
        if not bool(self.scalping_cfg.get("enabled", False)):
            return fallback
        comment = str(position.get("comment", "")).upper()
        if "M1" in comment or "SCALP" in comment:
            return "M1"
        return fallback

    def _pip_size(self, symbol: str, symbol_info: Optional[dict] = None) -> float:
        info = symbol_info or {}
        point = float(info.get("point", 0.0) or 0.0)
        digits = int(info.get("digits", 0) or 0)
        if point > 0:
            return point * 10.0 if digits in (3, 5) else point
        s = str(symbol or "").upper()
        if "JPY" in s:
            return 0.01
        if s in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in s or "GOLD" in s:
            return 0.1
        return 0.0001

    def _atr(self, candles: List[dict], period: int) -> float:
        p = max(1, int(period))
        if len(candles) < p + 1:
            return 0.0
        recent = candles[-(p + 1):]
        trs: List[float] = []
        for i in range(1, len(recent)):
            cur = recent[i]
            prev = recent[i - 1]
            high = float(cur.get("high", 0.0))
            low = float(cur.get("low", 0.0))
            prev_close = float(prev.get("close", 0.0))
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(max(0.0, tr))
        return sum(trs) / len(trs) if trs else 0.0

    def _entry_index(self, candles: List[dict], entry_time) -> int:
        if not isinstance(entry_time, datetime):
            return -1
        idx = -1
        for i, candle in enumerate(candles):
            t = candle.get("time")
            if isinstance(t, datetime) and t <= entry_time:
                idx = i
        return idx

    def _swing_lows(self, candles: List[dict], left: int, right: int) -> List[dict]:
        out: List[dict] = []
        l = max(1, left)
        r = max(1, right)
        if len(candles) < l + r + 1:
            return out
        for i in range(l, len(candles) - r):
            low = float(candles[i].get("low", 0.0))
            ll = [float(candles[j].get("low", 0.0)) for j in range(i - l, i)]
            rl = [float(candles[j].get("low", 0.0)) for j in range(i + 1, i + 1 + r)]
            if ll and rl and low < min(ll) and low < min(rl):
                out.append({"index": i, "price": low})
        return out

    def _swing_highs(self, candles: List[dict], left: int, right: int) -> List[dict]:
        out: List[dict] = []
        l = max(1, left)
        r = max(1, right)
        if len(candles) < l + r + 1:
            return out
        for i in range(l, len(candles) - r):
            high = float(candles[i].get("high", 0.0))
            lh = [float(candles[j].get("high", 0.0)) for j in range(i - l, i)]
            rh = [float(candles[j].get("high", 0.0)) for j in range(i + 1, i + 1 + r)]
            if lh and rh and high > max(lh) and high > max(rh):
                out.append({"index": i, "price": high})
        return out

    def _meaningful_swing(
        self,
        candles: List[dict],
        swing: dict,
        left: int,
        right: int,
        atr_value: float,
        pip: float,
        min_swing_pips: float,
        min_swing_atr_mult: float,
        is_high: bool,
    ) -> tuple[bool, str]:
        i = int(swing.get("index", -1))
        if i < 0:
            return False, "invalid_index"
        l = max(1, left)
        r = max(1, right)
        if i - l < 0 or i + r >= len(candles):
            return False, "not_confirmed"

        if is_high:
            local_left = [float(candles[j].get("high", 0.0)) for j in range(i - l, i)]
            local_right = [float(candles[j].get("high", 0.0)) for j in range(i + 1, i + 1 + r)]
            prominence = min(float(swing.get("price", 0.0)) - max(local_left), float(swing.get("price", 0.0)) - max(local_right))
        else:
            local_left = [float(candles[j].get("low", 0.0)) for j in range(i - l, i)]
            local_right = [float(candles[j].get("low", 0.0)) for j in range(i + 1, i + 1 + r)]
            prominence = min(min(local_left) - float(swing.get("price", 0.0)), min(local_right) - float(swing.get("price", 0.0)))

        pips_thr = max(0.0, min_swing_pips) * max(pip, 0.0)
        atr_thr = max(0.0, min_swing_atr_mult) * max(atr_value, 0.0)
        if prominence < pips_thr and prominence < atr_thr:
            return False, (
                f"too_small prominence={prominence:.5f} "
                f"pips_thr={pips_thr:.5f} atr_thr={atr_thr:.5f}"
            )
        return True, f"ok prominence={prominence:.5f}"

    def _is_valid_for_market(
        self,
        side: str,
        candidate_sl: float,
        bid: float,
        ask: float,
        pip: float,
        symbol_info: Optional[dict],
    ) -> tuple[bool, str]:
        info = symbol_info or {}
        point = float(info.get("point", 0.0) or 0.0)
        stops_level = int(info.get("stops_level", 0) or 0)
        freeze_level = int(info.get("freeze_level", 0) or 0)
        min_dist = max(pip * 0.2, point * max(stops_level, freeze_level, 0))
        if side == "BUY":
            if candidate_sl >= bid - min_dist:
                return False, "stop_level_or_freeze"
            return True, "ok"
        if side == "SELL":
            if candidate_sl <= ask + min_dist:
                return False, "stop_level_or_freeze"
            return True, "ok"
        return False, "invalid_side"

    def _be_candidate(
        self,
        position: dict,
        side: str,
        current_price: float,
        pip: float,
        r_gain: float,
        sym_cfg: dict,
    ) -> tuple[Optional[float], str]:
        if not bool(sym_cfg.get("be_enabled", True)):
            return None, "disabled"
        entry = float(position.get("open_price", 0.0) or 0.0)
        if entry <= 0:
            return None, "missing_entry"

        be_min_profit_pips = float(sym_cfg.get("be_min_profit_pips", 6.0) or 6.0)
        be_trigger_r_multiple = float(sym_cfg.get("be_trigger_r_multiple", 0.6) or 0.6)
        be_buffer_pips = float(sym_cfg.get("be_buffer_pips", 0.8) or 0.8)
        profit_pips = ((current_price - entry) / pip) if side == "BUY" else ((entry - current_price) / pip)

        trigger_profit = profit_pips >= be_min_profit_pips
        trigger_r = r_gain >= be_trigger_r_multiple
        if not (trigger_profit or trigger_r):
            return (
                None,
                f"not_triggered profit_pips={profit_pips:.2f}/{be_min_profit_pips:.2f} "
                f"r_gain={r_gain:.2f}/{be_trigger_r_multiple:.2f}",
            )

        be_buffer = be_buffer_pips * pip
        candidate = entry + be_buffer if side == "BUY" else entry - be_buffer
        reason = (
            "triggered "
            f"profit_pips={profit_pips:.2f} min={be_min_profit_pips:.2f} "
            f"r_gain={r_gain:.2f} min_r={be_trigger_r_multiple:.2f} "
            f"be_buffer_pips={be_buffer_pips:.2f}"
        )
        return candidate, reason

    def _tp_miss_candidate(
        self,
        position: dict,
        bid: float,
        ask: float,
        pip: float,
    ) -> tuple[Optional[float], str]:
        tp_cfg = self.risk_cfg.get("tp_miss_protection", {}) if isinstance(self.risk_cfg.get("tp_miss_protection", {}), dict) else {}
        if not bool(tp_cfg.get("enabled", True)):
            return None, "disabled"
        tp = float(position.get("tp", 0.0) or 0.0)
        entry = float(position.get("open_price", 0.0) or 0.0)
        if tp <= 0 or entry <= 0:
            return None, "missing_entry_or_tp"

        side = str(position.get("type", "")).upper()
        near_tp_pips = float(tp_cfg.get("near_tp_pips", self.risk_cfg.get("tp_miss_protection_pips", 2.0)) or 2.0)
        lock_pct = float(tp_cfg.get("lock_pct", 0.90) or 0.90)
        min_lock_pips = float(tp_cfg.get("min_lock_pips", 1.0) or 1.0)
        min_improve = pip * max(0.0, min_lock_pips)
        current_sl = float(position.get("sl", 0.0) or 0.0)

        if side == "BUY":
            total = tp - entry
            if total <= 0:
                return None, "tp_not_above_entry"
            if (tp - bid) > (near_tp_pips * pip):
                return None, "not_near_tp"
            candidate = min(entry + (total * lock_pct), bid - (0.2 * pip))
            if candidate <= current_sl + min_improve:
                return None, "not_tightening"
            return candidate, "accepted"

        if side == "SELL":
            total = entry - tp
            if total <= 0:
                return None, "tp_not_below_entry"
            if (ask - tp) > (near_tp_pips * pip):
                return None, "not_near_tp"
            current_sl = float(position.get("sl", 10**9) or 10**9)
            candidate = max(entry - (total * lock_pct), ask + (0.2 * pip))
            if candidate >= current_sl - min_improve:
                return None, "not_tightening"
            return candidate, "accepted"

        return None, "invalid_side"

    def _find_ob_candidate(
        self,
        candles: List[dict],
        side: str,
        start_idx: int,
        atr_value: float,
        buffer: float,
        impulse_mult: float,
    ) -> tuple[Optional[dict], str]:
        if len(candles) < 4:
            return None, "not_enough_candles"
        begin = max(2, start_idx + 2, 2)
        if begin >= len(candles):
            return None, "no_candles_after_entry"

        best = None
        min_impulse = max(0.0, impulse_mult) * max(atr_value, 0.0)
        saw_pattern = False
        saw_small_impulse = False
        for i in range(begin, len(candles)):
            prev = candles[i - 1]
            cur = candles[i]
            prev_o = float(prev.get("open", 0.0))
            prev_c = float(prev.get("close", 0.0))
            prev_h = float(prev.get("high", 0.0))
            prev_l = float(prev.get("low", 0.0))
            cur_o = float(cur.get("open", 0.0))
            cur_c = float(cur.get("close", 0.0))
            body = abs(cur_c - cur_o)

            if side == "BUY":
                if not (prev_c < prev_o and cur_c > cur_o and cur_c > prev_h):
                    continue
                saw_pattern = True
                if body < min_impulse:
                    saw_small_impulse = True
                    continue
                cand_sl = prev_l - buffer
                if best is None or cand_sl > float(best["sl"]):
                    best = {"sl": cand_sl, "kind": "OB", "index": i - 1, "ref": prev_l}
            elif side == "SELL":
                if not (prev_c > prev_o and cur_c < cur_o and cur_c < prev_l):
                    continue
                saw_pattern = True
                if body < min_impulse:
                    saw_small_impulse = True
                    continue
                cand_sl = prev_h + buffer
                if best is None or cand_sl < float(best["sl"]):
                    best = {"sl": cand_sl, "kind": "OB", "index": i - 1, "ref": prev_h}

        if best is not None:
            return best, "accepted"
        if saw_small_impulse:
            return None, "impulse_below_threshold"
        if saw_pattern:
            return None, "pattern_invalid_after_filters"
        return None, "no_ob_pattern"

    def evaluate_position(
        self,
        position: dict,
        candles_m5: List[dict],
        candles_m1: List[dict],
        bid: float,
        ask: float,
        symbol_info: Optional[dict] = None,
    ) -> dict:
        result = {"new_sl": None, "reason": None, "swing_tf": "M1", "ob_tf": "M5", "r_gain": 0.0}
        if not self.enabled:
            return result

        ticket = int(position.get("ticket", 0) or 0)
        symbol = str(position.get("symbol", "")).upper()
        side = str(position.get("type", "")).upper()
        entry = float(position.get("open_price", 0.0) or 0.0)
        sl = float(position.get("sl", 0.0) or 0.0)
        tp = float(position.get("tp", 0.0) or 0.0)
        if ticket <= 0 or entry <= 0 or side not in ("BUY", "SELL"):
            return result

        sym_cfg = self._sym_cfg(symbol)
        swing_tf_cfg = str(sym_cfg.get("swing_tf", "M1")).upper()
        ob_tf_cfg = str(sym_cfg.get("trailing_tf", "M5")).upper()
        swing_source = candles_m1 if swing_tf_cfg == "M1" else candles_m5
        swing_tf_used = swing_tf_cfg
        if not swing_source:
            swing_source = candles_m5
            swing_tf_used = "M5"
        ob_source = list(candles_m5 or [])
        if not swing_source and not ob_source:
            return result

        result["swing_tf"] = swing_tf_used
        result["ob_tf"] = ob_tf_cfg if ob_source else "M5"

        current_price = bid if side == "BUY" else ask
        one_r = (entry - sl) if side == "BUY" else (sl - entry)
        r_gain = 0.0
        if one_r > 0:
            move = (current_price - entry) if side == "BUY" else (entry - current_price)
            r_gain = move / one_r
        result["r_gain"] = r_gain
        logger.info(
            f"TRAIL_EVAL: symbol={symbol} ticket={ticket} side={side} current_price={current_price:.5f} "
            f"current_sl={sl:.5f} tp={tp:.5f} r_gain={r_gain:.2f} swing_tf={result['swing_tf']} ob_tf={result['ob_tf']}"
        )

        pip = self._pip_size(symbol, symbol_info=symbol_info)
        left = int(sym_cfg.get("fractal_left", 2))
        right = int(sym_cfg.get("fractal_right", 2))
        swing_base = swing_source[:-right] if right > 0 else list(swing_source)
        ob_base = list(ob_source or [])
        atr_period = int(sym_cfg.get("atr_period", 14))
        atr_swing = self._atr(swing_base, atr_period) if len(swing_base) >= 2 else 0.0
        atr_ob = self._atr(ob_base, atr_period) if len(ob_base) >= 2 else 0.0
        entry_idx_swing = self._entry_index(swing_base, position.get("open_time"))
        entry_idx_ob = self._entry_index(ob_base, position.get("open_time"))
        buffer = pip * float(sym_cfg.get("swing_buffer_pips", 1.0))

        state = self._state.setdefault(ticket, TrailingState(last_sl=sl))
        base_floor = max(sl, state.last_sl) if side == "BUY" else min(sl, state.last_sl)
        floor_or_ceiling = base_floor
        lock_be_as_floor = bool(sym_cfg.get("lock_be_as_floor", True))
        be_sl: Optional[float] = None

        candidates: List[dict] = []

        be_sl, be_reason = self._be_candidate(
            position=position,
            side=side,
            current_price=current_price,
            pip=pip,
            r_gain=r_gain,
            sym_cfg=sym_cfg,
        )
        if be_sl is not None:
            candidates.append({"kind": "BE_PLUS", "sl": float(be_sl), "ref": float(entry), "index": len(swing_base) - 1})
            logger.info(
                f"TRAIL_BE: symbol={symbol} ticket={ticket} side={side} status=accepted sl={float(be_sl):.5f} reason={be_reason}"
            )
            if lock_be_as_floor:
                floor_or_ceiling = max(floor_or_ceiling, float(be_sl)) if side == "BUY" else min(floor_or_ceiling, float(be_sl))
        else:
            logger.info(f"TRAIL_BE: symbol={symbol} ticket={ticket} side={side} status=rejected reason={be_reason}")

        if len(swing_base) >= (left + right + 5):
            if side == "BUY":
                swings = [s for s in self._swing_lows(swing_base, left=left, right=right) if int(s.get("index", -1)) > entry_idx_swing]
                meaningful: List[dict] = []
                for sw in swings:
                    ok, reason = self._meaningful_swing(
                        swing_base,
                        sw,
                        left=left,
                        right=right,
                        atr_value=atr_swing,
                        pip=pip,
                        min_swing_pips=float(sym_cfg.get("min_swing_pips", 0.0)),
                        min_swing_atr_mult=float(sym_cfg.get("min_swing_atr_mult", 0.0)),
                        is_high=False,
                    )
                    if ok:
                        meaningful.append(sw)
                        logger.info(
                            f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=BUY status=accepted "
                            f"index={int(sw['index'])} price={float(sw['price']):.5f} reason={reason}"
                        )
                    else:
                        logger.info(
                            f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=BUY status=rejected "
                            f"index={int(sw['index'])} price={float(sw['price']):.5f} reason={reason}"
                        )
                if meaningful:
                    preferred = [sw for sw in meaningful if float(sw.get("price", 0.0)) > entry]
                    selected = (preferred or meaningful)[-1]
                    selected_reason = "higher_low_above_entry" if preferred else "fallback_last_meaningful"
                    candidates.append(
                        {"kind": "SWING", "sl": float(selected["price"]) - buffer, "ref": float(selected["price"]), "index": int(selected["index"])}
                    )
                    logger.info(
                        f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=BUY status=accepted "
                        f"selected_index={int(selected['index'])} selected_reason={selected_reason}"
                    )
                else:
                    logger.info(f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=BUY status=rejected reason=no_meaningful_swing")
            else:
                swings = [s for s in self._swing_highs(swing_base, left=left, right=right) if int(s.get("index", -1)) > entry_idx_swing]
                meaningful: List[dict] = []
                for sw in swings:
                    ok, reason = self._meaningful_swing(
                        swing_base,
                        sw,
                        left=left,
                        right=right,
                        atr_value=atr_swing,
                        pip=pip,
                        min_swing_pips=float(sym_cfg.get("min_swing_pips", 0.0)),
                        min_swing_atr_mult=float(sym_cfg.get("min_swing_atr_mult", 0.0)),
                        is_high=True,
                    )
                    if ok:
                        meaningful.append(sw)
                        logger.info(
                            f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=SELL status=accepted "
                            f"index={int(sw['index'])} price={float(sw['price']):.5f} reason={reason}"
                        )
                    else:
                        logger.info(
                            f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=SELL status=rejected "
                            f"index={int(sw['index'])} price={float(sw['price']):.5f} reason={reason}"
                        )
                if meaningful:
                    preferred = [sw for sw in meaningful if float(sw.get("price", 0.0)) < entry]
                    selected = (preferred or meaningful)[-1]
                    selected_reason = "lower_high_below_entry" if preferred else "fallback_last_meaningful"
                    candidates.append(
                        {"kind": "SWING", "sl": float(selected["price"]) + buffer, "ref": float(selected["price"]), "index": int(selected["index"])}
                    )
                    logger.info(
                        f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=SELL status=accepted "
                        f"selected_index={int(selected['index'])} selected_reason={selected_reason}"
                    )
                else:
                    logger.info(f"TRAIL_SWING: symbol={symbol} ticket={ticket} side=SELL status=rejected reason=no_meaningful_swing")
        else:
            logger.info(f"TRAIL_SWING: symbol={symbol} ticket={ticket} side={side} status=rejected reason=insufficient_data")

        if bool(sym_cfg.get("allow_ob_trail", True)):
            if not ob_base:
                logger.info(f"TRAIL_OB: symbol={symbol} ticket={ticket} side={side} status=rejected reason=no_m5_data")
            else:
                ob, ob_reason = self._find_ob_candidate(
                    ob_base,
                    side=side,
                    start_idx=entry_idx_ob,
                    atr_value=atr_ob,
                    buffer=buffer,
                    impulse_mult=float(sym_cfg.get("ob_min_impulse_atr_mult", 0.8)),
                )
                if ob:
                    candidates.append(ob)
                    logger.info(
                        f"TRAIL_OB: symbol={symbol} ticket={ticket} side={side} status=accepted "
                        f"index={int(ob['index'])} ref={float(ob['ref']):.5f} sl={float(ob['sl']):.5f}"
                    )
                else:
                    logger.info(f"TRAIL_OB: symbol={symbol} ticket={ticket} side={side} status=rejected reason={ob_reason}")
        else:
            logger.info(f"TRAIL_OB: symbol={symbol} ticket={ticket} side={side} status=rejected reason=disabled")

        tp_sl, tp_reason = self._tp_miss_candidate(position, bid=bid, ask=ask, pip=pip)
        if tp_sl is not None:
            candidates.append({"kind": "TP_MISS", "sl": float(tp_sl), "ref": float(tp), "index": len(swing_base) - 1})
            logger.info(f"TRAIL_TP_MISS: symbol={symbol} ticket={ticket} side={side} status=accepted sl={float(tp_sl):.5f}")
        else:
            logger.info(f"TRAIL_TP_MISS: symbol={symbol} ticket={ticket} side={side} status=rejected reason={tp_reason}")

        valid: List[dict] = []
        for c in candidates:
            c_sl = float(c.get("sl", 0.0))
            kind = str(c.get("kind", "SWING"))
            kind_tag = "BE" if kind == "BE_PLUS" else kind
            tighten_floor = base_floor if kind == "BE_PLUS" else floor_or_ceiling
            if side == "BUY":
                if c_sl <= tighten_floor + (pip * 0.2):
                    logger.info(
                        f"TRAIL_{kind_tag}: symbol={symbol} ticket={ticket} side={side} status=rejected "
                        f"reason=not_tightening candidate_sl={c_sl:.5f} floor={tighten_floor:.5f}"
                    )
                    continue
            else:
                if c_sl >= tighten_floor - (pip * 0.2):
                    logger.info(
                        f"TRAIL_{kind_tag}: symbol={symbol} ticket={ticket} side={side} status=rejected "
                        f"reason=not_tightening candidate_sl={c_sl:.5f} ceiling={tighten_floor:.5f}"
                    )
                    continue

            market_ok, market_reason = self._is_valid_for_market(
                side=side,
                candidate_sl=c_sl,
                bid=bid,
                ask=ask,
                pip=pip,
                symbol_info=symbol_info,
            )
            if not market_ok:
                logger.info(
                    f"TRAIL_{kind_tag}: symbol={symbol} ticket={ticket} side={side} status=rejected "
                    f"reason={market_reason} candidate_sl={c_sl:.5f}"
                )
                continue
            valid.append(c)

        if not valid:
            return result

        best = max(valid, key=lambda x: float(x["sl"])) if side == "BUY" else min(valid, key=lambda x: float(x["sl"]))
        new_sl = float(best["sl"])
        if lock_be_as_floor and be_sl is not None:
            locked = max(new_sl, float(be_sl)) if side == "BUY" else min(new_sl, float(be_sl))
            if abs(locked - new_sl) > 1e-12:
                logger.info(
                    f"TRAIL_BE: symbol={symbol} ticket={ticket} side={side} status=accepted "
                    f"reason=floor_lock_applied old={new_sl:.5f} new={locked:.5f}"
                )
            new_sl = locked

        if side == "BUY" and new_sl <= base_floor + (pip * 0.2):
            return result
        if side == "SELL" and new_sl >= base_floor - (pip * 0.2):
            return result

        final_market_ok, final_market_reason = self._is_valid_for_market(
            side=side,
            candidate_sl=new_sl,
            bid=bid,
            ask=ask,
            pip=pip,
            symbol_info=symbol_info,
        )
        if not final_market_ok:
            best_kind = str(best.get("kind", "SWING"))
            best_tag = "BE" if best_kind == "BE_PLUS" else best_kind
            logger.info(
                f"TRAIL_{best_tag}: symbol={symbol} ticket={ticket} side={side} "
                f"status=rejected reason={final_market_reason} candidate_sl={new_sl:.5f}"
            )
            return result

        state.last_sl = new_sl
        result["new_sl"] = round(new_sl, 5)
        result["reason"] = str(best.get("kind", "SWING"))
        return result

    def get_trailing_sl(
        self,
        position: dict,
        current_price: float,
        candles_m5: List[dict],
        candles_m1: Optional[List[dict]] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
        symbol_info: Optional[dict] = None,
    ) -> Optional[float]:
        side = str(position.get("type", "")).upper()
        bid_price = float(bid if bid is not None else current_price)
        ask_price = float(ask if ask is not None else current_price)
        if side == "BUY" and ask is None:
            ask_price = bid_price
        if side == "SELL" and bid is None:
            bid_price = ask_price
        res = self.evaluate_position(
            position=position,
            candles_m5=candles_m5,
            candles_m1=list(candles_m1 or candles_m5),
            bid=bid_price,
            ask=ask_price,
            symbol_info=symbol_info,
        )
        return res["new_sl"]

    def remove_position_tracking(self, ticket: int):
        self._state.pop(int(ticket), None)
