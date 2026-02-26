"""
MT5 Connector
─────────────
Handles all communication with MetaTrader 5:
  • Connection management (auto-reconnect)
  • Market data retrieval (OHLCV, tick, spread)
  • Order placement / modification / closure
  • Account info & position tracking
"""

import time
import logging
from datetime import datetime
from typing import Optional
import MetaTrader5 as mt5

logger = logging.getLogger("MT5")

# ─── Timeframe map ─────────────────────────────────────────────────────────────
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}


class MT5Connector:
    def __init__(self, config: dict):
        self.cfg         = config["mt5"]
        self.connected   = False
        self._retry_max  = 5
        self._retry_wait = 3  # seconds

    # ── Connection ─────────────────────────────────────────────────────────────
    def connect(self) -> bool:
        for attempt in range(1, self._retry_max + 1):
            logger.info(f"Connecting to MT5 (attempt {attempt}/{self._retry_max})...")
            if mt5.initialize(
                login    = self.cfg["login"],
                password = self.cfg["password"],
                server   = self.cfg["server"],
                timeout  = self.cfg["timeout"],
            ):
                info = mt5.account_info()
                logger.info(f"✅  Connected | Account: {info.login} | "
                            f"Balance: {info.balance:.2f} {info.currency} | "
                            f"Broker: {info.company}")
                self.connected = True
                return True

            err = mt5.last_error()
            logger.warning(f"Connection failed: {err}. Retrying in {self._retry_wait}s...")
            time.sleep(self._retry_wait)

        logger.error("❌  Could not connect to MT5 after max retries.")
        return False

    def disconnect(self):
        mt5.shutdown()
        self.connected = False
        logger.info("MT5 disconnected.")

    def ensure_connected(self) -> bool:
        if not self.connected or not mt5.terminal_info():
            logger.warning("MT5 disconnected — attempting reconnect...")
            return self.connect()
        return True

    # ── Account Info ───────────────────────────────────────────────────────────
    def get_account_info(self) -> dict:
        self.ensure_connected()
        info = mt5.account_info()
        if info is None:
            return {}
        return {
            "login":    info.login,
            "balance":  info.balance,
            "equity":   info.equity,
            "margin":   info.margin,
            "free_margin": info.margin_free,
            "profit":   info.profit,
            "leverage": info.leverage,
            "currency": info.currency,
        }

    # ── Market Data ────────────────────────────────────────────────────────────
    def get_candles(self, symbol: str, timeframe: str, count: int = 500) -> list:
        """Returns list of OHLCV dicts, newest last."""
        self.ensure_connected()
        tf = TF_MAP.get(timeframe.upper())
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")

        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning(f"No candle data for {symbol} {timeframe}")
            return []

        return [
            {
                "time":   datetime.utcfromtimestamp(r["time"]),
                "open":   r["open"],
                "high":   r["high"],
                "low":    r["low"],
                "close":  r["close"],
                "volume": r["tick_volume"],
            }
            for r in rates
        ]

    def get_tick(self, symbol: str) -> Optional[dict]:
        self.ensure_connected()
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "bid":    tick.bid,
            "ask":    tick.ask,
            "spread": round(tick.ask - tick.bid, 5),
            "time":   datetime.utcfromtimestamp(tick.time),
        }

    def get_spread_pips(self, symbol: str) -> float:
        info = mt5.symbol_info(symbol)
        if info is None:
            return 999.0
        spread_points = info.spread
        pip_size = 0.0001 if "JPY" not in symbol else 0.01
        point    = info.point
        return round(spread_points * point / pip_size, 2)

    # ── Orders ─────────────────────────────────────────────────────────────────
    def place_market_order(self, symbol: str, order_type: str,
                           volume: float, sl: float, tp: float,
                           comment: str = "ICT_BOT") -> Optional[dict]:
        """
        order_type: 'BUY' or 'SELL'
        Returns order result dict or None on failure.
        """
        self.ensure_connected()
        tick = self.get_tick(symbol)
        if not tick:
            logger.error(f"Cannot get tick for {symbol}")
            return None

        price  = tick["ask"] if order_type == "BUY" else tick["bid"]
        action = mt5.ORDER_TYPE_BUY if order_type == "BUY" else mt5.ORDER_TYPE_SELL

        # Try without SL/TP first, add them after if needed
        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       volume,
            "type":         action,
            "price":        price,
            "deviation":    50,  # Large deviation for market orders
            "magic":        20250101,
            "comment":      comment,
            "type_time":    mt5.ORDER_TIME_GTC,
        }
        
        # Don't specify type_filling - let MT5 choose automatically

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else f"Code {result.retcode}: {result.comment}"
            logger.error(f"❌  Order failed on {symbol}: {err}")
            return None

        ticket = result.order
        deal_ticket = getattr(result, "deal", None)
        position_id = getattr(result, "position", None) or ticket
        logger.info(f"✅  ORDER PLACED | {order_type} {volume} {symbol} | "
                    f"Price: {result.price} | Ticket: {ticket} | Deal: {deal_ticket} | Position: {position_id}")
        
        # Now add SL/TP via modify
        if sl > 0 or tp > 0:
            time.sleep(0.5)  # Brief pause before modify
            modify_result = self.modify_sl_tp(ticket, sl, tp)
            if modify_result:
                logger.info(f"✅  SL/TP added | SL: {sl} | TP: {tp}")
            else:
                logger.warning(f"⚠️  Trade opened but SL/TP failed to set")
        
        return {
            "ticket":  ticket,
            "order_ticket": ticket,
            "deal_ticket": deal_ticket,
            "position_id": position_id,
            "symbol":  symbol,
            "type":    order_type,
            "volume":  volume,
            "price":   result.price,
            "sl":      sl,
            "tp":      tp,
            "time":    datetime.utcnow(),
            "comment": comment,
        }

    def close_position(self, ticket: int) -> bool:
        self.ensure_connected()
        position = None
        for pos in mt5.positions_get():
            if pos.ticket == ticket:
                position = pos
                break

        if position is None:
            logger.warning(f"Position {ticket} not found.")
            return False

        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = self.get_tick(position.symbol)
        price = tick["bid"] if order_type == mt5.ORDER_TYPE_SELL else tick["ask"]

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       position.symbol,
            "volume":       position.volume,
            "type":         order_type,
            "position":     ticket,
            "price":        price,
            "deviation":    50,
            "magic":        20250101,
            "comment":      "ICT_BOT_CLOSE",
            "type_time":    mt5.ORDER_TIME_GTC,
        }

        result = mt5.order_send(request)
        if result and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"✅  CLOSED position {ticket} on {position.symbol}")
            return True

        logger.error(f"❌  Failed to close {ticket}: {result.comment if result else 'None'}")
        return False

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        self.ensure_connected()
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       round(sl, 5),
            "tp":       round(tp, 5),
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return True
        else:
            if result:
                logger.debug(f"Modify SL/TP failed: {result.comment}")
            return False

    # ── Positions ──────────────────────────────────────────────────────────────
    def get_open_positions(self) -> list:
        self.ensure_connected()
        positions = mt5.positions_get()
        if positions is None:
            return []
        return [
            {
                "ticket":  p.ticket,
                "symbol":  p.symbol,
                "type":    "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                "volume":  p.volume,
                "open_price": p.price_open,
                "sl":      p.sl,
                "tp":      p.tp,
                "profit":  p.profit,
                "comment": p.comment,
                "open_time": datetime.utcfromtimestamp(p.time),
            }
            for p in positions
            if p.magic == 20250101  # Only bot-managed trades
        ]

    def get_today_trades(self) -> list:
        self.ensure_connected()
        today = datetime.utcnow().replace(hour=0, minute=0, second=0)
        history = mt5.history_deals_get(today, datetime.utcnow())
        if history is None:
            return []
        trades = []
        for d in history:
            deal_type = "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL"
            position_id = getattr(d, "position_id", None) or getattr(d, "position", None)
            deal_entry = getattr(d, "entry", None)
            trades.append(
                {
                    "deal_ticket": d.ticket,
                    "ticket": d.ticket,
                    "order_ticket": getattr(d, "order", None),
                    "position_id": position_id,
                    "symbol": d.symbol,
                    "type": deal_type,
                    "entry": deal_entry,
                    "magic": getattr(d, "magic", None),
                    "volume": d.volume,
                    "price": d.price,
                    "profit": d.profit,
                    "time": datetime.utcfromtimestamp(d.time).isoformat(),
                }
            )
        return trades
