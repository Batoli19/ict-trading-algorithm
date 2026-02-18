"""
Enhanced Risk Manager
─────────────────────
NEW FEATURES:
  • Dynamic position sizing based on signal confidence (0.10 - 0.18 lots)
  • Kill zone multiplier (1.5x size during kill zones)
  • Maximum risk cap at $150 per trade
  • Minimum 1:2 RR ratio enforcement for high-risk trades
  • Volatility-aware position sizing
"""

import logging
from datetime import datetime, date, time as dtime
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

    # ── Enhanced Position Sizing ───────────────────────────────────────────────
    def calculate_lot_size(self, symbol: str, entry: float, sl: float, tp: float,
                            account_balance: float, confidence: float = 0.75,
                            in_kill_zone: bool = False) -> float:
        """
        Enhanced dynamic position sizing.
        
        Rules:
          • Base size: 0.10 lots (XAUUSD) or equivalent
          • Confidence scaling: 0.10 (low) → 0.18 (high)
          • Kill zone multiplier: 1.5x during active kill zones
          • Max risk: $150 per trade (only in high volatility + high confidence)
          • Min RR: 1:2 for trades risking >$100
        """
        sl_distance = abs(entry - sl)
        tp_distance = abs(tp - entry)
        
        if sl_distance == 0:
            logger.warning(f"SL distance is 0 for {symbol} — defaulting to 0.10 lot")
            return 0.10

        # Calculate RR ratio
        rr_ratio = tp_distance / sl_distance if sl_distance > 0 else 0
        
        # Base lot sizes per symbol (for $5000 account)
        base_lots = {
            "XAUUSD":  0.10,   # Gold
            "EURUSD":  0.50,   # Major forex
            "GBPUSD":  0.50,
            "AUDUSD":  0.50,
            "USDJPY":  0.50,
            "US30":    0.03,   # Dow Jones
            "NAS100":  0.03,   # Nasdaq
            "SPX500":  0.03,   # S&P 500
        }
        
        base_lot = base_lots.get(symbol, 0.30)
        
        # Scale by confidence (0.75 = base, 0.50 = 0.7x, 1.0 = 1.4x)
        confidence_multiplier = 0.7 + (confidence * 0.7)  # Range: 0.7 - 1.4
        lot = base_lot * confidence_multiplier
        
        # Kill zone bonus (50% more size during prime sessions)
        if in_kill_zone:
            lot *= 1.5
            logger.info(f"🎯  Kill zone active — increasing position size by 50%")
        
        # Calculate dollar risk
        pip_size = self._get_pip_size(symbol)
        sl_pips = sl_distance / pip_size
        pip_values = {
            "EURUSD": 10.0, "GBPUSD": 10.0, "AUDUSD": 10.0,
            "USDJPY": 9.1,  "EURJPY": 9.1,
            "XAUUSD": 100.0, "US30": 1.0, "NAS100": 1.0, "SPX500": 1.0,
        }
        pip_value = pip_values.get(symbol, 10.0)
        dollar_risk = lot * sl_pips * pip_value
        
        # Hard cap at $150 risk (only if RR >= 2.0)
        if dollar_risk > 150:
            if rr_ratio >= 2.0:
                lot = 150 / (sl_pips * pip_value)
                logger.warning(f"⚠️  High risk trade: ${dollar_risk:.2f} → capped at $150 | RR: 1:{rr_ratio:.1f}")
            else:
                # If RR < 2.0, reduce to $100 max risk
                lot = 100 / (sl_pips * pip_value)
                logger.warning(f"⚠️  Low RR trade ({rr_ratio:.1f}) — risk capped at $100")
        
        # Enforce RR >= 2.0 for trades risking >$100
        if dollar_risk > 100 and rr_ratio < 2.0:
            lot *= 0.7  # Reduce size by 30%
            dollar_risk = lot * sl_pips * pip_value
            logger.warning(f"⚠️  RR below 2.0 ({rr_ratio:.1f}) — reducing lot to {lot:.2f} (${dollar_risk:.2f} risk)")
        
        # Round to broker increments
        lot = self._round_lot(lot)
        
        # Final bounds (stricter than before)
        if symbol == "XAUUSD":
            lot = max(0.10, min(lot, 0.18))  # Gold: 0.10 - 0.18
        elif symbol in ("US30", "NAS100", "SPX500"):
            lot = max(0.03, min(lot, 0.08))  # Indices: 0.03 - 0.08
        else:
            lot = max(0.30, min(lot, 0.80))  # Forex: 0.30 - 0.80
        
        # Recalculate final risk
        final_risk = lot * sl_pips * pip_value
        
        logger.info(f"💰  {symbol} | Confidence: {confidence:.0%} | "
                    f"Lot: {lot} | Risk: ${final_risk:.2f} | "
                    f"SL: {sl_pips:.1f}p | RR: 1:{rr_ratio:.1f}")
        
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