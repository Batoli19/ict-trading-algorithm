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

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        self.ensure_connected()
        info = mt5.symbol_info(symbol)
        if info is None:
            return None
        return {
            "digits": int(getattr(info, "digits", 0) or 0),
            "point": float(getattr(info, "point", 0.0) or 0.0),
            "stops_level": int(getattr(info, "trade_stops_level", 0) or 0),
            "freeze_level": int(getattr(info, "trade_freeze_level", 0) or 0),
        }

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

    def place_limit_order(
        self,
        symbol: str,
        order_type: str,
        volume: float,
        entry: float,
        sl: float,
        tp: float,
        comment: str = "ICT_BOT_LIMIT",
    ) -> Optional[dict]:
        """
        order_type: 'BUY' or 'SELL'
        Places a pending BUY_LIMIT / SELL_LIMIT order.
        """
        self.ensure_connected()
        tick = self.get_tick(symbol)
        if not tick:
            logger.error(f"Cannot get tick for {symbol}")
            return None

        if order_type == "BUY":
            pending_type = mt5.ORDER_TYPE_BUY_LIMIT
            if entry >= tick["ask"]:
                logger.warning(
                    f"Invalid BUY_LIMIT for {symbol}: entry={entry:.5f} must be below ask={tick['ask']:.5f}"
                )
                return None
        else:
            pending_type = mt5.ORDER_TYPE_SELL_LIMIT
            if entry <= tick["bid"]:
                logger.warning(
                    f"Invalid SELL_LIMIT for {symbol}: entry={entry:.5f} must be above bid={tick['bid']:.5f}"
                )
                return None

        request = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": symbol,
            "volume": volume,
            "type": pending_type,
            "price": float(entry),
            "sl": float(sl) if sl else 0.0,
            "tp": float(tp) if tp else 0.0,
            "deviation": 20,
            "magic": 20250101,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = mt5.last_error() if result is None else f"Code {result.retcode}: {result.comment}"
            logger.error(f"Limit order failed on {symbol}: {err}")
            return None

        order_ticket = getattr(result, "order", None)
        logger.info(
            f"PENDING LIMIT PLACED | {order_type}_LIMIT {volume} {symbol} | "
            f"entry={entry:.5f} sl={sl:.5f} tp={tp:.5f} order={order_ticket}"
        )
        return {
            "ticket": order_ticket,
            "order_ticket": order_ticket,
            "deal_ticket": None,
            "position_id": None,
            "symbol": symbol,
            "type": order_type,
            "volume": volume,
            "price": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "time": datetime.utcnow(),
            "comment": comment,
            "is_pending": True,
        }

    def get_pending_orders(self, symbol: Optional[str] = None) -> list:
        self.ensure_connected()
        orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
        if orders is None:
            return []
        out = []
        for o in orders:
            if getattr(o, "magic", None) != 20250101:
                continue
            out.append(
                {
                    "ticket": int(o.ticket),
                    "symbol": o.symbol,
                    "type": int(o.type),
                    "volume": float(o.volume_current),
                    "price_open": float(o.price_open),
                    "sl": float(getattr(o, "sl", 0.0) or 0.0),
                    "tp": float(getattr(o, "tp", 0.0) or 0.0),
                    "time_setup": datetime.utcfromtimestamp(int(o.time_setup)),
                    "comment": str(getattr(o, "comment", "") or ""),
                }
            )
        return out

    def cancel_order(self, ticket: int) -> bool:
        self.ensure_connected()
        request = {
            "action": mt5.TRADE_ACTION_REMOVE,
            "order": int(ticket),
            "magic": 20250101,
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            logger.info(f"PENDING ORDER CANCELED | ticket={ticket}")
            return True
        logger.warning(f"Failed cancel pending order ticket={ticket} err={result.comment if result else 'None'}")
        return False

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

    def modify_sl_tp_detailed(self, ticket: int, sl: float, tp: float) -> dict:
        self.ensure_connected()
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       round(sl, 5),
            "tp":       round(tp, 5),
        }
        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {"ok": True, "retcode": int(result.retcode), "comment": str(getattr(result, "comment", "") or "")}
        err = mt5.last_error()
        if result:
            logger.debug(f"Modify SL/TP failed: {result.comment}")
            return {
                "ok": False,
                "retcode": int(getattr(result, "retcode", 0) or 0),
                "comment": str(getattr(result, "comment", "") or ""),
                "last_error": str(err),
            }
        return {"ok": False, "retcode": None, "comment": "no_result", "last_error": str(err)}

    def modify_sl_tp(self, ticket: int, sl: float, tp: float) -> bool:
        return bool(self.modify_sl_tp_detailed(ticket, sl, tp).get("ok", False))

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
        now = datetime.utcnow()
        history = mt5.history_deals_get(today, now)
        if history is None:
            return []
        comment_map = self._history_order_comment_map(today, now)
        return self._normalize_deals(history, order_comment_map=comment_map)

    def get_deals_between(self, from_dt: datetime, to_dt: datetime) -> list:
        self.ensure_connected()
        history = mt5.history_deals_get(from_dt, to_dt)
        if history is None:
            return []
        comment_map = self._history_order_comment_map(from_dt, to_dt)
        return self._normalize_deals(history, order_comment_map=comment_map)

    def _history_order_comment_map(self, from_dt: datetime, to_dt: datetime) -> dict:
        out = {}
        try:
            orders = mt5.history_orders_get(from_dt, to_dt)
        except Exception:
            return out
        if orders is None:
            return out
        for o in orders:
            try:
                ticket = int(getattr(o, "ticket", 0) or 0)
            except Exception:
                continue
            if ticket <= 0:
                continue
            comment = str(getattr(o, "comment", "") or "").strip()
            if comment and ticket not in out:
                out[ticket] = comment
        return out

    def _normalize_deals(self, deals, order_comment_map: Optional[dict] = None) -> list:
        order_comment_map = order_comment_map or {}
        out = []
        for d in deals:
            deal_type = "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL"
            position_id = getattr(d, "position_id", None) or getattr(d, "position", None)
            deal_entry = getattr(d, "entry", None)
            order_ticket = getattr(d, "order", None)
            comment = str(getattr(d, "comment", "") or "")
            if not comment and order_ticket is not None:
                try:
                    comment = str(order_comment_map.get(int(order_ticket), "") or "")
                except Exception:
                    comment = ""
            out.append({
                "deal_ticket": d.ticket,
                "ticket": d.ticket,
                "order_ticket": order_ticket,
                "position_id": position_id,
                "symbol": d.symbol,
                "entry": deal_entry,
                "type": deal_type,
                "profit": d.profit,
                "price": d.price,
                "volume": d.volume,
                "time": datetime.utcfromtimestamp(d.time).isoformat(),
                "magic": getattr(d, "magic", None),
                "comment": comment,
            })
        return out
