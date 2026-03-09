"""
Cooldown Manager — Post-Trade Throttling System
═════════════════════════════════════════════════
Prevents overtrading by enforcing waiting periods after trade exits
and equity drawdowns.

Why cooldowns matter:
    After a losing trade, the natural instinct is to "revenge trade" — jump
    back in immediately to recover. This usually leads to more losses.
    The cooldown manager forces the bot to pause after losses, giving the
    market time to establish new structure.

Cooldown types:
    1. GLOBAL cooldown:     Brief pause after ANY trade close (win or loss)
    2. PER-SYMBOL cooldown: Longer pause on the specific symbol after a loss
    3. LOSS STREAK cooldown: Extended pause if consecutive losses on one symbol
    4. EQUITY DD pause:     Temporary halt if equity drops too much from peak
    5. EQUITY DD stop:      Full stop for the day if drawdown is severe

Configuration (settings.json → "risk"):
    "risk": {
        "cooldown_win_minutes": 10,       ← wait 10 min after a win
        "cooldown_loss_minutes": 45,      ← wait 45 min after a loss
        "cooldown_global_minutes": 3,     ← wait 3 min after any close
        "loss_streak_threshold": 2,       ← 2 consecutive losses triggers streak
        "loss_streak_minutes": 120,       ← 2-hour pause after streak
        "equity_dd_pause_pct": 3.0,       ← pause trading at 3% drawdown
        "equity_dd_pause_minutes": 120,   ← how long to pause
        "equity_dd_stop_pct": 5.0         ← stop for the day at 5% drawdown
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional


# ═══════════════════════════════════════════════════════════════════════════
# CooldownConfig — stores the cooldown thresholds from settings.json
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CooldownConfig:
    """
    Configuration for all cooldown timers, loaded from settings.json.
    These are the thresholds that determine when and how long to pause.
    """
    win_minutes: int = 10           # Pause after a winning trade
    loss_minutes: int = 45          # Pause after a losing trade
    global_minutes: int = 3         # Brief pause after ANY trade close
    loss_streak_threshold: int = 2  # How many consecutive losses trigger a streak
    loss_streak_minutes: int = 120  # How long to pause after a loss streak (2 hours)
    dd_pause_pct: float = 3.0       # Equity drawdown % that triggers a pause
    dd_pause_minutes: int = 120     # How long the drawdown pause lasts
    dd_stop_pct: float = 5.0        # Equity drawdown % that stops trading for the day


# ═══════════════════════════════════════════════════════════════════════════
# CooldownState — tracks the current state of all cooldown timers
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CooldownState:
    """
    Live state tracking for all cooldown timers.
    Updated every time a trade closes or equity changes.
    """
    global_block_until: Optional[datetime] = None            # When global cooldown expires
    per_symbol_block_until: Dict[str, datetime] = field(default_factory=dict)  # Per-symbol expiry
    symbol_loss_streak: Dict[str, int] = field(default_factory=dict)  # Consecutive losses per symbol
    dd_pause_until: Optional[datetime] = None                # When equity DD pause expires
    dd_stop_for_day: bool = False                            # True = no more trading today
    equity_peak: Optional[float] = None                      # Highest equity seen today
    equity_peak_time: Optional[datetime] = None              # When the peak was recorded


# ═══════════════════════════════════════════════════════════════════════════
# CooldownManager — the main class that enforces all cooldown rules
# ═══════════════════════════════════════════════════════════════════════════

class CooldownManager:
    """
    Manages post-trade cooldowns to prevent overtrading and revenge trading.

    Called by the engine at two key points:
        1. can_enter(symbol) — before every trade entry attempt
        2. on_exit(symbol, pnl) — after every trade close

    And periodically:
        3. update_equity_peak_and_check_dd(equity) — on every scan loop

    Example flow:
        Trade closes with -$50 loss on EURUSD
        → on_exit("EURUSD", -50) is called
        → Sets global cooldown for 3 minutes (no trades on any pair)
        → Sets EURUSD cooldown for 45 minutes
        → If 2nd consecutive loss on EURUSD → extends cooldown to 2 hours
    """

    def __init__(self, cfg: dict):
        """
        Initialize the cooldown manager from the bot's config.

        Args:
            cfg: Full config dict (reads from the "risk" section)
        """
        r = cfg.get("risk", {})
        self.cfg = CooldownConfig(
            win_minutes=int(r.get("cooldown_win_minutes", 10)),
            loss_minutes=int(r.get("cooldown_loss_minutes", 45)),
            global_minutes=int(r.get("cooldown_global_minutes", 3)),
            loss_streak_threshold=int(r.get("loss_streak_threshold", 2)),
            loss_streak_minutes=int(r.get("loss_streak_minutes", 120)),
            dd_pause_pct=float(r.get("equity_dd_pause_pct", 3.0)),
            dd_pause_minutes=int(r.get("equity_dd_pause_minutes", 120)),
            dd_stop_pct=float(r.get("equity_dd_stop_pct", 5.0)),
        )
        self.state = CooldownState()

    def _now(self) -> datetime:
        """Get current UTC time. Extracted for testability."""
        return datetime.utcnow()

    def can_enter(self, symbol: str) -> tuple[bool, str]:
        """
        Check if a trade entry is allowed for the given symbol.

        This is the main gatekeeper called before every entry attempt.
        It checks all cooldown conditions in priority order:
            1. Equity DD stop (full day stop)
            2. Equity DD pause (temporary pause)
            3. Global cooldown (brief pause after any trade)
            4. Per-symbol cooldown (longer pause for specific symbol)

        Args:
            symbol: The instrument to check (e.g. "EURUSD")

        Returns:
            Tuple of (allowed: bool, reason: str).
            If not allowed, reason explains which cooldown is active.
        """
        now = self._now()

        # Priority 1: Full day stop (equity drawdown too severe)
        if self.state.dd_stop_for_day:
            return False, "DD_STOP_DAY"

        # Priority 2: Temporary equity drawdown pause
        if self.state.dd_pause_until and now < self.state.dd_pause_until:
            return False, f"DD_PAUSE_UNTIL_{self.state.dd_pause_until.isoformat()}"

        # Priority 3: Global cooldown (affects ALL symbols)
        if self.state.global_block_until and now < self.state.global_block_until:
            return False, f"GLOBAL_COOLDOWN_UNTIL_{self.state.global_block_until.isoformat()}"

        # Priority 4: Per-symbol cooldown (affects only this symbol)
        until = self.state.per_symbol_block_until.get(symbol)
        if until and now < until:
            return False, f"SYMBOL_COOLDOWN_UNTIL_{until.isoformat()}"

        return True, "OK"

    def on_exit(self, symbol: str, pnl: float):
        """
        Called whenever a trade closes (win, loss, or break-even).
        Sets the appropriate cooldown timers based on the outcome.

        Args:
            symbol: The instrument that was closed (e.g. "EURUSD")
            pnl:    The trade's profit/loss in account currency
                    Positive = win, negative = loss, zero = break-even
        """
        now = self._now()

        # Always set a brief global cooldown after any close
        # This prevents rapid-fire trading across all pairs
        self.state.global_block_until = now + timedelta(minutes=self.cfg.global_minutes)

        if pnl > 0:
            # ─── WIN: reset loss streak, apply win cooldown ───────────
            self.state.symbol_loss_streak[symbol] = 0
            self.state.per_symbol_block_until[symbol] = now + timedelta(minutes=self.cfg.win_minutes)
        else:
            # ─── LOSS or BREAK-EVEN: track streak, apply loss cooldown ─
            # Increment consecutive loss counter for this symbol
            self.state.symbol_loss_streak[symbol] = self.state.symbol_loss_streak.get(symbol, 0) + 1
            streak = self.state.symbol_loss_streak[symbol]

            # Base cooldown after a loss
            base_until = now + timedelta(minutes=self.cfg.loss_minutes)

            # If loss streak hits threshold, apply extended cooldown
            # E.g., 2 consecutive losses on EURUSD → 2-hour pause instead of 45 min
            if streak >= self.cfg.loss_streak_threshold:
                streak_until = now + timedelta(minutes=self.cfg.loss_streak_minutes)
                self.state.per_symbol_block_until[symbol] = max(base_until, streak_until)
            else:
                self.state.per_symbol_block_until[symbol] = base_until

    def update_equity_peak_and_check_dd(self, equity: float) -> tuple[bool, str]:
        """
        Update the equity high-water mark and check for drawdown breaches.

        Called on every scan loop (every 10 seconds) by the engine.
        Tracks the highest equity seen and calculates current drawdown:

            Drawdown % = (peak - current_equity) / peak × 100

        If drawdown exceeds thresholds:
            - 3% drawdown → pause trading for 2 hours
            - 5% drawdown → stop trading for the entire day

        Args:
            equity: Current account equity (balance + unrealized P&L)

        Returns:
            Tuple of (ok: bool, status: str).
            ok=False means a drawdown threshold was breached.
        """
        now = self._now()

        # Update peak if we have a new high
        if self.state.equity_peak is None or equity > self.state.equity_peak:
            self.state.equity_peak = float(equity)
            self.state.equity_peak_time = now
            return True, "PEAK_UPDATED"

        peak = float(self.state.equity_peak)
        if peak <= 0:
            return True, "NO_PEAK"

        # Calculate drawdown percentage from peak
        dd_pct = ((peak - float(equity)) / peak) * 100.0

        # Check severe drawdown → stop for the day
        if dd_pct >= self.cfg.dd_stop_pct:
            self.state.dd_stop_for_day = True
            return False, f"DD_STOP_DAY_{dd_pct:.2f}%"

        # Check moderate drawdown → temporary pause
        if dd_pct >= self.cfg.dd_pause_pct:
            self.state.dd_pause_until = now + timedelta(minutes=self.cfg.dd_pause_minutes)
            return False, f"DD_PAUSE_{dd_pct:.2f}%"

        return True, "DD_OK"

    def diagnostics(self) -> dict:
        """
        Return current cooldown state as a dict for the dashboard API.
        Shows all active cooldowns, loss streaks, and equity peak info.
        """
        s = self.state
        return {
            "global_block_until": s.global_block_until.isoformat() if s.global_block_until else None,
            "dd_pause_until": s.dd_pause_until.isoformat() if s.dd_pause_until else None,
            "dd_stop_for_day": bool(s.dd_stop_for_day),
            "equity_peak": s.equity_peak,
            "equity_peak_time": s.equity_peak_time.isoformat() if s.equity_peak_time else None,
            "symbol_blocks": {k: v.isoformat() for k, v in s.per_symbol_block_until.items()},
            "symbol_loss_streak": dict(s.symbol_loss_streak),
        }