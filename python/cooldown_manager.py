# python/cooldown_manager.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional


@dataclass
class CooldownConfig:
    win_minutes: int = 10
    loss_minutes: int = 45
    global_minutes: int = 3
    loss_streak_threshold: int = 2
    loss_streak_minutes: int = 120  # 2 hours
    dd_pause_pct: float = 3.0
    dd_pause_minutes: int = 120     # 2 hours
    dd_stop_pct: float = 5.0        # stop trading for day


@dataclass
class CooldownState:
    global_block_until: Optional[datetime] = None
    per_symbol_block_until: Dict[str, datetime] = field(default_factory=dict)
    symbol_loss_streak: Dict[str, int] = field(default_factory=dict)
    dd_pause_until: Optional[datetime] = None
    dd_stop_for_day: bool = False
    equity_peak: Optional[float] = None
    equity_peak_time: Optional[datetime] = None


class CooldownManager:
    def __init__(self, cfg: dict):
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
        return datetime.utcnow()

    def can_enter(self, symbol: str) -> tuple[bool, str]:
        now = self._now()

        if self.state.dd_stop_for_day:
            return False, "DD_STOP_DAY"

        if self.state.dd_pause_until and now < self.state.dd_pause_until:
            return False, f"DD_PAUSE_UNTIL_{self.state.dd_pause_until.isoformat()}"

        if self.state.global_block_until and now < self.state.global_block_until:
            return False, f"GLOBAL_COOLDOWN_UNTIL_{self.state.global_block_until.isoformat()}"

        until = self.state.per_symbol_block_until.get(symbol)
        if until and now < until:
            return False, f"SYMBOL_COOLDOWN_UNTIL_{until.isoformat()}"

        return True, "OK"

    def on_exit(self, symbol: str, pnl: float):
        """Call this whenever a trade closes (EXIT_RECORDED)."""
        now = self._now()

        # Global cooldown after ANY close
        self.state.global_block_until = now + timedelta(minutes=self.cfg.global_minutes)

        # Per-symbol cooldown
        if pnl > 0:
            self.state.symbol_loss_streak[symbol] = 0
            self.state.per_symbol_block_until[symbol] = now + timedelta(minutes=self.cfg.win_minutes)
        else:
            # loss or BE counts as "negative regime" for throttling
            self.state.symbol_loss_streak[symbol] = self.state.symbol_loss_streak.get(symbol, 0) + 1
            streak = self.state.symbol_loss_streak[symbol]

            base_until = now + timedelta(minutes=self.cfg.loss_minutes)

            # If loss streak hits threshold, extend block
            if streak >= self.cfg.loss_streak_threshold:
                streak_until = now + timedelta(minutes=self.cfg.loss_streak_minutes)
                self.state.per_symbol_block_until[symbol] = max(base_until, streak_until)
            else:
                self.state.per_symbol_block_until[symbol] = base_until

    def update_equity_peak_and_check_dd(self, equity: float) -> tuple[bool, str]:
        """
        Update today equity peak. If drawdown from peak exceeds thresholds,
        trigger pause or stop.
        """
        now = self._now()

        if self.state.equity_peak is None or equity > self.state.equity_peak:
            self.state.equity_peak = float(equity)
            self.state.equity_peak_time = now
            return True, "PEAK_UPDATED"

        peak = float(self.state.equity_peak)
        if peak <= 0:
            return True, "NO_PEAK"

        dd_pct = ((peak - float(equity)) / peak) * 100.0

        # Stop trading for day
        if dd_pct >= self.cfg.dd_stop_pct:
            self.state.dd_stop_for_day = True
            return False, f"DD_STOP_DAY_{dd_pct:.2f}%"

        # Pause trading temporarily
        if dd_pct >= self.cfg.dd_pause_pct:
            self.state.dd_pause_until = now + timedelta(minutes=self.cfg.dd_pause_minutes)
            return False, f"DD_PAUSE_{dd_pct:.2f}%"

        return True, "DD_OK"

    def diagnostics(self) -> dict:
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