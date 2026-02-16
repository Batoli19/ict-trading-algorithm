"""
Risk Manager
─────────────
Handles all risk logic:
  • Position sizing based on % risk per trade
  • Daily loss limit enforcement
  • Max open trades guard
  • Trailing stop management
  • Trade journal entry
"""

import logging
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("RISK")


@dataclass
class TradeRecord:
    ticket:     int
    symbol:     str
    direction:  str
    volume:     float
    entry:      float
    sl:         float
    tp:         float
    open_time:  datetime
    close_time: Optional[datetime] = None
    close_price: Optional[float]  = None
    pnl:        float = 0.0
    setup_type: str   = ""
    reason:     str   = ""


class RiskManager:
    def __init__(self, config: dict):
        self.cfg       = config["risk"]
        self.journal:  list[TradeRecord] = []
        self._today:   date = datetime.utcnow().date()
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0

    # ── Daily Reset ────────────────────────────────────────────────────────────
    def _check_reset(self):
        today = datetime.utcnow().date()
        if today != self._today:
            logger.info(f"📅  New day {today} — resetting daily counters. "
                        f"Yesterday P&L: {self._daily_pnl:+.2f}")
            self._today       = today
            self._daily_pnl   = 0.0
            self._daily_trades = 0

    # ── Guards ─────────────────────────────────────────────────────────────────
    def can_trade(self, open_positions: list, account_balance: float) -> tuple[bool, str]:
        self._check_reset()

        # Max open trades
        if len(open_positions) >= self.cfg["max_open_trades"]:
            return False, f"Max open trades reached ({self.cfg['max_open_trades']})"

        # Max daily trades
        if self._daily_trades >= self.cfg["max_daily_trades"]:
            return False, f"Max daily trades reached ({self.cfg['max_daily_trades']})"

        # Daily loss limit
        max_loss = account_balance * (self.cfg["max_daily_loss_pct"] / 100)
        if self._daily_pnl <= -max_loss:
            return False, (f"Daily loss limit hit: {self._daily_pnl:.2f} / "
                           f"-{max_loss:.2f} ({self.cfg['max_daily_loss_pct']}%)")

        return True, "OK"

    # ── Position Sizing ────────────────────────────────────────────────────────
    def calculate_lot_size(self, symbol: str, entry: float, sl: float,
                            account_balance: float,
                            account_currency: str = "USD") -> float:
        """
        Risk-based lot size calculation.
        Formula: lot = (balance × risk%) / (SL_pips × pip_value_per_lot)
        """
        risk_amount = account_balance * (self.cfg["risk_per_trade_pct"] / 100)
        sl_distance = abs(entry - sl)

        if sl_distance == 0:
            logger.warning(f"SL distance is 0 for {symbol} — defaulting to 0.01 lot")
            return 0.01

        # Pip value per standard lot (approximations)
        pip_values = {
            "EURUSD": 10.0,  "GBPUSD": 10.0,  "AUDUSD": 10.0,
            "USDJPY": 9.1,   "EURJPY": 9.1,
            "XAUUSD": 100.0, "US30":   1.0,    "NAS100": 1.0,
            "SPX500": 1.0,
        }
        pip_size = self._get_pip_size(symbol)
        sl_pips  = sl_distance / pip_size
        pv       = pip_values.get(symbol, 10.0)

        raw_lot = risk_amount / (sl_pips * pv)

        # Round to broker-standard increments
        lot = self._round_lot(raw_lot)
        lot = max(0.01, min(lot, 10.0))  # Hard clamp 0.01 – 10.0

        logger.info(f"💰  {symbol} | Risk: ${risk_amount:.2f} | SL: {sl_pips:.1f} pips | "
                    f"Lot size: {lot}")
        return lot

    def _get_pip_size(self, symbol: str) -> float:
        if "JPY" in symbol:
            return 0.01
        if symbol in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol:
            return 0.1
        return 0.0001

    def _round_lot(self, lot: float) -> float:
        if lot < 0.1:
            return round(lot * 100) / 100  # 0.01 increments
        if lot < 1.0:
            return round(lot * 10) / 10    # 0.1 increments
        return round(lot)                   # 1.0 increments

    # ── Trailing Stop ─────────────────────────────────────────────────────────
    def get_trailing_sl(self, position: dict, current_price: float) -> Optional[float]:
        """
        Returns a new SL price if trailing stop should be moved, else None.
        """
        if not self.cfg.get("trailing_stop", False):
            return None

        trail_dist = self._get_pip_size(position["symbol"]) * self.cfg["trailing_stop_pips"]
        current_sl = position["sl"]

        if position["type"] == "BUY":
            new_sl = current_price - trail_dist
            if new_sl > current_sl + trail_dist * 0.5:  # Only move if meaningful
                return round(new_sl, 5)

        elif position["type"] == "SELL":
            new_sl = current_price + trail_dist
            if new_sl < current_sl - trail_dist * 0.5:
                return round(new_sl, 5)

        return None

    # ── Journal ────────────────────────────────────────────────────────────────
    def record_open(self, trade: dict, setup_type: str = "", reason: str = ""):
        self._daily_trades += 1
        record = TradeRecord(
            ticket    = trade["ticket"],
            symbol    = trade["symbol"],
            direction = trade["type"],
            volume    = trade["volume"],
            entry     = trade["price"],
            sl        = trade["sl"],
            tp        = trade["tp"],
            open_time = trade["time"],
            setup_type = setup_type,
            reason    = reason,
        )
        self.journal.append(record)
        logger.info(f"📝  Trade #{trade['ticket']} recorded | "
                    f"{trade['type']} {trade['volume']} {trade['symbol']}")

    def record_close(self, ticket: int, close_price: float, pnl: float):
        self._daily_pnl += pnl
        for r in self.journal:
            if r.ticket == ticket:
                r.close_time  = datetime.utcnow()
                r.close_price = close_price
                r.pnl         = pnl
                emoji = "✅" if pnl > 0 else "❌"
                logger.info(f"{emoji}  Trade #{ticket} closed | P&L: {pnl:+.2f} | "
                            f"Daily P&L: {self._daily_pnl:+.2f}")
                return

    # ── Stats ──────────────────────────────────────────────────────────────────
    def get_stats(self) -> dict:
        closed = [r for r in self.journal if r.close_time is not None]
        if not closed:
            return {"trades": 0, "winrate": 0, "total_pnl": 0, "daily_pnl": self._daily_pnl}

        wins   = [r for r in closed if r.pnl > 0]
        losses = [r for r in closed if r.pnl < 0]
        total  = sum(r.pnl for r in closed)

        avg_win  = sum(r.pnl for r in wins)  / len(wins)  if wins   else 0
        avg_loss = sum(r.pnl for r in losses)/ len(losses) if losses else 0
        expectancy = (len(wins)/len(closed) * avg_win) + (len(losses)/len(closed) * avg_loss) if closed else 0

        return {
            "trades":      len(closed),
            "wins":        len(wins),
            "losses":      len(losses),
            "winrate":     round(len(wins) / len(closed) * 100, 1),
            "total_pnl":   round(total, 2),
            "daily_pnl":   round(self._daily_pnl, 2),
            "avg_win":     round(avg_win, 2),
            "avg_loss":    round(avg_loss, 2),
            "expectancy":  round(expectancy, 2),
            "daily_trades": self._daily_trades,
        }
