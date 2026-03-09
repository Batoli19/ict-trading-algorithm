"""
Hybrid Gate — Session-Based Entry Filter
══════════════════════════════════════════
Additional entry filtering layer used when the bot is in "hybrid" mode.

What is Hybrid Mode?
    Normal mode:  Bot takes every signal that passes the sniper filter.
    Hybrid mode:  Bot adds extra restrictions on top — cooldowns after closes,
                  limits per kill zone, and re-entry blocking.

    Think of it as the difference between a fully automated algo and a
    "semi-automatic" system that mimics a disciplined manual trader.

What the Hybrid Gate checks:
    1. RR minimum:       Reject signals below a minimum risk-reward ratio
    2. Post-close cooldown: Pause after ANY trade close (separate from
                           the CooldownManager's cooldowns)
    3. Re-entry blocking: Prevent re-entering the same direction + setup
                          on the same symbol (avoids "same thesis" trap)
    4. Daily caps:        Max trades per day (total, per symbol, per KZ)

Configuration (settings.json → "hybrid"):
    "hybrid": {
        "enabled": true,
        "min_rr": 1.5,
        "cooldown_after_close_seconds": 300,
        "cooldown_after_loss_seconds": 600,
        "cooldown_after_win_seconds": 180,
        "block_reentry_same_direction": true,
        "reentry_requires_new_setup": true,
        "max_trades_per_day_total": 6,
        "max_trades_per_symbol_per_day": 2,
        "max_trades_per_killzone_per_symbol": 1
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple


@dataclass
class GateDecision:
    """
    Result of the hybrid gate check.
    allowed=True  → trade may proceed
    allowed=False → trade is blocked, reason explains why
    """
    allowed: bool
    reason: str = "OK"


class HybridGate:
    """
    Hybrid (Sniper + Session Momentum) entry gate.

    This gate sits between the sniper filter and the actual order placement.
    If hybrid mode is enabled, every signal must pass through this gate
    after passing the sniper filter.

    Flow: ICTStrategy → SniperFilter → HybridGate → Order Placement

    The gate tracks state internally:
        - Per-symbol cooldown timers after trade closes
        - Last trade direction per symbol (for re-entry blocking)
        - Trade counts per day/symbol/KZ from the memory database
    """

    def __init__(self, cfg: dict, memory):
        """
        Args:
            cfg:    Full config dict (reads the "hybrid" section)
            memory: TradingMemoryDB instance for counting today's trades
        """
        self.cfg = cfg
        self.memory = memory

        # Track per-symbol cooldowns (set after each trade close)
        self._cooldown_until_by_symbol: Dict[str, datetime] = {}

        # Track last closed trade per symbol (direction + setup type)
        # Used for re-entry blocking logic
        self._last_close_by_symbol: Dict[str, Dict] = {}

    def on_trade_closed(self, symbol: str, pnl: float, direction: Optional[str], setup_type: Optional[str]):
        """
        Called by the engine when a trade closes. Sets cooldown timers
        and records the trade's direction/setup for re-entry blocking.

        The cooldown duration depends on whether the trade was a win or loss:
            - Loss → cooldown_after_loss_seconds (longer to avoid revenge trading)
            - Win  → cooldown_after_win_seconds (shorter, market may still be trending)
            - BE   → cooldown_after_close_seconds (neutral)

        Args:
            symbol:     The instrument that was closed (e.g. "EURUSD")
            pnl:        Trade P&L (positive=win, negative=loss, zero=BE)
            direction:  "BUY" or "SELL"
            setup_type: Which ICT setup was used (e.g. "FVG", "STOP_HUNT")
        """
        hcfg = self.cfg.get("hybrid", {})
        now = datetime.utcnow()

        # Get configured cooldown durations (in seconds)
        cd_close = int(hcfg.get("cooldown_after_close_seconds", 0))  # Any close
        cd_loss = int(hcfg.get("cooldown_after_loss_seconds", 0))    # Loss-specific
        cd_win = int(hcfg.get("cooldown_after_win_seconds", 0))      # Win-specific

        # Pick the appropriate cooldown based on trade outcome
        if pnl < 0:
            cooldown = max(cd_close, cd_loss)     # Use the longer of the two
        elif pnl > 0:
            cooldown = max(cd_close, cd_win)
        else:
            cooldown = cd_close  # Break-even uses generic close cooldown

        # Set the cooldown timer
        if cooldown > 0:
            self._cooldown_until_by_symbol[symbol] = now + timedelta(seconds=cooldown)

        # Record what just closed (for re-entry blocking)
        self._last_close_by_symbol[symbol] = {
            "time": now,
            "pnl": pnl,
            "direction": direction,
            "setup_type": setup_type,
        }

    def _cooldown_active(self, symbol: str) -> Tuple[bool, str]:
        """Check if the hybrid cooldown is still active for this symbol."""
        until = self._cooldown_until_by_symbol.get(symbol)
        if not until:
            return False, ""
        now = datetime.utcnow()
        if now < until:
            return True, f"COOLDOWN_ACTIVE until={until.isoformat()}"
        return False, ""

    def _counts_ok(self, symbol: str, kz_name: str) -> Tuple[bool, str]:
        """
        Check if daily trade count limits have been reached.

        Uses the TradingMemoryDB to count trades taken today.
        Three separate caps are checked:
            1. Total trades today across all symbols
            2. Trades today on this specific symbol
            3. Trades today on this symbol in this kill zone
        """
        hcfg = self.cfg.get("hybrid", {})

        # Get the configured limits (0 = unlimited)
        max_total = int(hcfg.get("max_trades_per_day_total", 0))
        max_sym = int(hcfg.get("max_trades_per_symbol_per_day", 0))
        max_kzsym = int(hcfg.get("max_trades_per_killzone_per_symbol", 0))

        # Query the memory database for today's counts
        total_today = self.memory.count_trades_today_total()
        sym_today = self.memory.count_trades_today_symbol(symbol)
        kzsym_today = self.memory.count_trades_today_symbol_kz(symbol, kz_name)

        if max_total > 0 and total_today >= max_total:
            return False, f"MAX_DAILY_TOTAL reached {total_today}/{max_total}"
        if max_sym > 0 and sym_today >= max_sym:
            return False, f"MAX_DAILY_SYMBOL reached {sym_today}/{max_sym}"
        if max_kzsym > 0 and kzsym_today >= max_kzsym:
            return False, f"MAX_KZ_SYMBOL reached {kzsym_today}/{max_kzsym}"

        return True, "OK"

    def allow_entry(
        self,
        symbol: str,
        kz_name: str,
        direction: str,
        setup_type: str,
        rr: float,
        confidence: float,
    ) -> GateDecision:
        """
        Main entry point — decide whether to allow a trade entry.

        This is called by the engine after the sniper filter has passed.
        Checks are applied in this order:
            1. Is hybrid mode enabled? (if not, always allow)
            2. Does the signal meet the minimum RR?
            3. Is the symbol in a post-close cooldown?
            4. Would this be a re-entry on the same direction + setup?
            5. Have daily trade caps been reached?

        Args:
            symbol:     Instrument (e.g. "EURUSD")
            kz_name:    Current kill zone (e.g. "LONDON_OPEN")
            direction:  Signal direction ("BUY" or "SELL")
            setup_type: ICT setup type (e.g. "FVG", "STOP_HUNT")
            rr:         Risk-reward ratio of the signal
            confidence: Signal confidence score (0.0-1.0)

        Returns:
            GateDecision with allowed=True/False and reason.
        """
        hcfg = self.cfg.get("hybrid", {})

        # If hybrid mode is disabled, always allow
        if not hcfg.get("enabled", False):
            return GateDecision(True, "HYBRID_DISABLED")

        # Check 1: Minimum RR threshold
        min_rr = float(hcfg.get("min_rr", 0.0))
        if rr < min_rr:
            return GateDecision(False, f"RR_TOO_LOW rr={rr:.2f} min={min_rr:.2f}")

        # Check 2: Post-close cooldown
        active, reason = self._cooldown_active(symbol)
        if active:
            return GateDecision(False, reason)

        # Check 3: Re-entry blocking
        # If the last trade on this symbol was the same direction AND setup,
        # block re-entry (prevents the "same thesis" trap where the bot
        # keeps entering the same losing idea)
        if hcfg.get("block_reentry_same_direction", True):
            last = self._last_close_by_symbol.get(symbol)
            if last and last.get("direction") == direction:
                if hcfg.get("reentry_requires_new_setup", True) and last.get("setup_type") == setup_type:
                    return GateDecision(False, "REENTRY_BLOCKED same_direction_and_setup")

        # Check 4: Daily trade count limits
        ok, reason = self._counts_ok(symbol, kz_name)
        if not ok:
            return GateDecision(False, reason)

        return GateDecision(True, "OK")
