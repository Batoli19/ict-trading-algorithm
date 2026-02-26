"""
ICT 2022 Trailing Stop Strategy
─────────────────────────────────
Based on ICT's 2022 Free Mentorship concepts:

BEARISH TRADES:
  • Use intermediate highs as trail points
  • Don't trail SL above the last intermediate high
  • Take 50% profit at 50% to TP
  • Take 25% profit at 75% to TP
  • Trail remaining 25% using intermediate highs

BULLISH TRADES:
  • Use intermediate lows as trail points
  • Don't trail SL below the last intermediate low
  • Same partial profit structure

SPREAD COMPENSATION:
  • Add spread buffer to TP (so TP hits despite spread)
  • Subtract spread buffer from SL (so SL doesn't get hit by spread)
"""

import logging
from dataclasses import dataclass
from typing import Optional, List
from datetime import datetime

logger = logging.getLogger("TRAIL")


@dataclass
class TpMissProtectionCfg:
    enabled: bool = True
    near_tp_pips: float = 2.0
    lock_pct: float = 0.90
    min_lock_pips: float = 1.0
    spread_guard: bool = True
    max_spread_pips: float = 3.0
    once_per_position: bool = False


class ICT2022TrailingStop:
    def __init__(self, config: dict, mt5_connector):
        self.cfg = config.get("risk", {})
        self.mt5 = mt5_connector
        self.enabled = self.cfg.get("trailing_stop", True)
        
        # Partial profit levels
        self.partial_50_pct = 0.50  # Take 50% at halfway
        self.partial_75_pct = 0.25  # Take 25% at 75% to TP
        
        # Track partial closures
        self._partials_taken = {}  # {ticket: {"50%": bool, "75%": bool}}
        self._tp_protection_state = {}  # {ticket: {"tp_protect_applied": bool}}

        tp_cfg = self.cfg.get("tp_miss_protection", {})
        fallback_near_pips = float(self.cfg.get("tp_miss_protection_pips", 2.0) or 2.0)
        self.tp_miss_cfg = TpMissProtectionCfg(
            enabled=bool(tp_cfg.get("enabled", True)),
            near_tp_pips=float(tp_cfg.get("near_tp_pips", fallback_near_pips) or fallback_near_pips),
            lock_pct=float(tp_cfg.get("lock_pct", 0.90) or 0.90),
            min_lock_pips=float(tp_cfg.get("min_lock_pips", 1.0) or 1.0),
            spread_guard=bool(tp_cfg.get("spread_guard", True)),
            max_spread_pips=float(tp_cfg.get("max_spread_pips", 3.0) or 3.0),
            once_per_position=bool(tp_cfg.get("once_per_position", False)),
        )
        
    def get_pip_size(self, symbol: str) -> float:
        if "JPY" in symbol:
            return 0.01
        if symbol in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol:
            return 0.1
        return 0.0001

    def _pip_size_from_symbol_info(self, symbol: str) -> float:
        info = None
        if hasattr(self.mt5, "get_symbol_info"):
            info = self.mt5.get_symbol_info(symbol)
        if info:
            point = float(info.get("point", 0.0) or 0.0)
            digits = int(info.get("digits", 0) or 0)
            if point > 0:
                return point * 10 if digits in (3, 5) else point
        return self.get_pip_size(symbol)

    def _pips_to_price(self, symbol: str, pips: float) -> float:
        return pips * self._pip_size_from_symbol_info(symbol)

    def apply_tp_miss_protection(self, position: dict, bid: float, ask: float) -> Optional[float]:
        cfg = self.tp_miss_cfg
        if not cfg.enabled:
            return None

        tp = float(position.get("tp", 0.0) or 0.0)
        if tp <= 0:
            return None

        symbol = position["symbol"]
        ticket = int(position["ticket"])
        pos_type = str(position["type"]).upper()
        entry = float(position["open_price"])
        current_sl_raw = position.get("sl")
        current_sl = float(current_sl_raw) if current_sl_raw else 0.0

        pip_price = self._pips_to_price(symbol, 1.0)
        if pip_price <= 0:
            return None

        spread_pips = (ask - bid) / pip_price
        if cfg.spread_guard and cfg.max_spread_pips > 0 and spread_pips > cfg.max_spread_pips:
            return None

        if pos_type == "BUY":
            remaining = tp - bid
            total = tp - entry
            if total <= 0:
                return None
            if remaining > self._pips_to_price(symbol, cfg.near_tp_pips):
                return None

            desired_sl = entry + (total * cfg.lock_pct)
            min_improve = self._pips_to_price(symbol, cfg.min_lock_pips)
            if desired_sl <= current_sl + min_improve:
                return None

            buffer = self._pips_to_price(symbol, 0.2)
            desired_sl = min(desired_sl, bid - buffer)
            if desired_sl <= current_sl + min_improve:
                return None
        elif pos_type == "SELL":
            remaining = ask - tp
            total = entry - tp
            if total <= 0:
                return None
            if remaining > self._pips_to_price(symbol, cfg.near_tp_pips):
                return None

            desired_sl = entry - (total * cfg.lock_pct)
            current_sl = float(current_sl_raw) if current_sl_raw else 10**9
            min_improve = self._pips_to_price(symbol, cfg.min_lock_pips)
            if desired_sl >= current_sl - min_improve:
                return None

            buffer = self._pips_to_price(symbol, 0.2)
            desired_sl = max(desired_sl, ask + buffer)
            if desired_sl >= current_sl - min_improve:
                return None
        else:
            return None

        state = self._tp_protection_state.setdefault(ticket, {})
        if cfg.once_per_position and state.get("tp_protect_applied"):
            return None
        state["tp_protect_applied"] = True
        return desired_sl
    
    def find_intermediate_high(self, candles: List[dict]) -> Optional[float]:
        """
        Find the most recent intermediate high (swing high)
        = highest point in last 5-10 candles
        """
        if len(candles) < 5:
            return None
        
        recent = candles[-10:]  # Last 10 candles
        highs = [c["high"] for c in recent]
        
        # Find the swing high (local maximum)
        swing_high = max(highs[-5:])  # Highest in last 5 candles
        
        return swing_high
    
    def find_intermediate_low(self, candles: List[dict]) -> Optional[float]:
        """
        Find the most recent intermediate low (swing low)
        """
        if len(candles) < 5:
            return None
        
        recent = candles[-10:]
        lows = [c["low"] for c in recent]
        
        swing_low = min(lows[-5:])
        
        return swing_low
    
    def calculate_progress_to_tp(self, entry: float, current: float, tp: float) -> float:
        """
        Calculate % progress from entry to TP
        """
        total_distance = abs(tp - entry)
        current_distance = abs(current - entry)
        
        if total_distance == 0:
            return 0
        
        progress = (current_distance / total_distance) * 100
        return min(progress, 100)
    
    def take_partial_profit(self, position: dict, percentage: float, reason: str):
        """
        Close a percentage of the position
        """
        ticket = position["ticket"]
        symbol = position["symbol"]
        volume = position["volume"]
        
        # Track partials
        if ticket not in self._partials_taken:
            self._partials_taken[ticket] = {"50%": False, "75%": False}
        
        # Check if already taken
        if self._partials_taken[ticket].get(reason, False):
            return  # Already took this partial
        
        # Calculate partial volume
        partial_volume = round(volume * percentage, 2)
        
        if partial_volume < 0.01:
            logger.debug(f"Partial volume too small ({partial_volume}), skipping")
            return
        
        # Close partial via MT5
        logger.info(f"💰  Taking {percentage*100:.0f}% profit on {symbol} #{ticket} | "
                    f"Closing {partial_volume} of {volume} lots | Reason: {reason}")
        
        # In real implementation, you'd call MT5 to close partial
        # For now, just mark it as taken
        self._partials_taken[ticket][reason] = True
    
    def get_trailing_sl(self, position: dict, current_price: float,
                        candles_m5: List[dict], spread_pips: float = 0,
                        bid: Optional[float] = None, ask: Optional[float] = None) -> Optional[float]:
        """
        ICT 2022 trailing logic:
          1. Take partials at 50% and 75% to TP
          2. Trail using intermediate highs/lows
          3. Never trail past market structure
          4. Move to break-even after 30% to TP
        """
        if not self.enabled:
            return None
        
        symbol = position["symbol"]
        ticket = position["ticket"]
        pos_type = position["type"]
        entry = position["open_price"]
        current_sl = position["sl"]
        tp = position["tp"]
        volume = position["volume"]

        pip_size = self.get_pip_size(symbol)

        spread_price = pip_size * spread_pips
        bid_price = float(bid) if bid is not None else float(current_price - (spread_price / 2.0))
        ask_price = float(ask) if ask is not None else float(current_price + (spread_price / 2.0))

        tp_protect_sl = self.apply_tp_miss_protection(position, bid_price, ask_price)
        if tp_protect_sl is not None:
            logger.info(f"TP_MISS_PROTECT | {symbol} #{ticket} | {current_sl:.5f} -> {tp_protect_sl:.5f}")
            return round(tp_protect_sl, 5)
        
        # Calculate progress to TP
        progress = self.calculate_progress_to_tp(entry, current_price, tp)
        
        # PARTIAL PROFITS
        if progress >= 50 and volume > 0.01:
            self.take_partial_profit(position, self.partial_50_pct, "50%")
        
        if progress >= 75 and volume > 0.01:
            self.take_partial_profit(position, self.partial_75_pct, "75%")
        
        # BREAK-EVEN at 30% to TP
        if progress >= 30 and ((pos_type == "BUY" and current_sl < entry) or 
                                (pos_type == "SELL" and current_sl > entry)):
            # Add spread compensation to break-even
            if pos_type == "BUY":
                be_level = entry + (pip_size * (spread_pips + 2))
                logger.info(f"✅  {symbol} #{ticket} at 30% to TP — moving to break-even + spread")
                return round(be_level, 5)
            else:
                be_level = entry - (pip_size * (spread_pips + 2))
                logger.info(f"✅  {symbol} #{ticket} at 30% to TP — moving to break-even + spread")
                return round(be_level, 5)
        
        # INTERMEDIATE HIGH/LOW TRAILING
        if pos_type == "BUY":
            # Find intermediate low (don't trail below it)
            swing_low = self.find_intermediate_low(candles_m5)
            
            if swing_low:
                # Trail just below the swing low (with spread buffer)
                trail_level = swing_low - (pip_size * (spread_pips + 3))
                
                # Only move if it's above current SL and meaningful
                if trail_level > current_sl + (pip_size * 3):
                    logger.info(f"📈  {symbol} #{ticket} trailing to intermediate low {swing_low:.5f} "
                                f"(SL: {trail_level:.5f})")
                    return round(trail_level, 5)
        
        elif pos_type == "SELL":
            # Find intermediate high (don't trail above it)
            swing_high = self.find_intermediate_high(candles_m5)
            
            if swing_high:
                # Trail just above the swing high (with spread buffer)
                trail_level = swing_high + (pip_size * (spread_pips + 3))
                
                # Only move if it's below current SL and meaningful
                if trail_level < current_sl - (pip_size * 3):
                    logger.info(f"📉  {symbol} #{ticket} trailing to intermediate high {swing_high:.5f} "
                                f"(SL: {trail_level:.5f})")
                    return round(trail_level, 5)
        
        return None
    
    def remove_position_tracking(self, ticket: int):
        """Clean up when position closes"""
        if ticket in self._partials_taken:
            del self._partials_taken[ticket]
        if ticket in self._tp_protection_state:
            del self._tp_protection_state[ticket]
