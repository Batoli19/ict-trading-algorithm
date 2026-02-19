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
from typing import Optional, List
from datetime import datetime

logger = logging.getLogger("TRAIL")


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
        
    def get_pip_size(self, symbol: str) -> float:
        if "JPY" in symbol:
            return 0.01
        if symbol in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol:
            return 0.1
        return 0.0001
    
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
                        candles_m5: List[dict], spread_pips: float = 0) -> Optional[float]:
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