"""
MT5 Connector — MetaTrader 5 Bridge
═══════════════════════════════════
The interface between the Python bot and MetaTrader 5 terminal.
All communication with MT5 goes through this class.

Responsibilities:
    CONNECTION:     Initialize MT5, login, auto-reconnect on failure (up to 5 retries)
    DATA RETRIEVAL: Get candles (OHLCV), ticks, spread, account info, symbol specs
    ORDER MGMT:     Place market/limit orders, modify SL/TP, close/partial-close positions
    POSITION MGMT:  List open positions, get today's trades, query deal history

Magic number 20250101 tags all bot-managed trades for easy identification.

Note: Requires the MetaTrader 5 desktop terminal to be running on Windows.
"""

import time
import logging
import math
from datetime import datetime, timezone
from typing import Optional
import MetaTrader5 as mt5

logger = logging.getLogger("MT5")

#  Timeframe map 
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

    #  Connection 
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
                logger.info(f"  Connected | Account: {info.login} | "
                            f"Balance: {info.balance:.2f} {info.currency} | "
                            f"Broker: {info.company}")
                self.connected = True
                return True

            err = mt5.last_error()
            logger.warning(f"Connection failed: {err}. Retrying in {self._retry_wait}s...")
            time.sleep(self._retry_wait)

        logger.error("  Could not connect to MT5 after max retries.")
        return False

    def disconnect(self):
        mt5.shutdown()
        self.connected = False
        logger.info("MT5 disconnected.")

    def ensure_connected(self) -> bool:
        if not self.connected or not mt5.terminal_info():
            logger.warning("MT5 disconnected  attempting reconnect...")
            return self.connect()
        return True

    #  Account Info 
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

    #  Market Data 
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
        trade_tick_value = float(
            getattr(info, "trade_tick_value", 0.0)
            or getattr(info, "trade_tick_value_profit", 0.0)
            or getattr(info, "trade_tick_value_loss", 0.0)
            or 0.0
        )
        trade_tick_size = float(
            getattr(info, "trade_tick_size", 0.0)
            or getattr(info, "point", 0.0)
            or 0.0
        )
        point = float(getattr(info, "point", 0.0) or 0.0)
        pip_size = self._pip_size_for_symbol(symbol, point=point)
        pip_value_per_lot = 0.0
        if trade_tick_value > 0.0 and trade_tick_size > 0.0 and pip_size > 0.0:
            pip_value_per_lot = trade_tick_value * (pip_size / trade_tick_size)
        return {
            "digits": int(getattr(info, "digits", 0) or 0),
            "point": point,
            "stops_level": int(getattr(info, "trade_stops_level", 0) or 0),
            "freeze_level": int(getattr(info, "trade_freeze_level", 0) or 0),
            "volume_min": float(getattr(info, "volume_min", 0.0) or 0.0),
            "volume_step": float(getattr(info, "volume_step", 0.0) or 0.0),
            "volume_max": float(getattr(info, "volume_max", 0.0) or 0.0),
            "trade_tick_value": trade_tick_value,
            "trade_tick_size": trade_tick_size,
            "pip_size": pip_size,
            "pip_value_per_lot": pip_value_per_lot,
        }

    def _pip_size_for_symbol(self, symbol: str, point: float = 0.0) -> float:
        sym = str(symbol or "").upper()
        if "JPY" in sym:
            return 0.01
        if "XAU" in sym:
            return 0.1
        if sym in ("US30", "NAS100", "SPX500"):
            return 1.0
        if point > 0.0:
            if point >= 0.01:
                return 0.01
            if point >= 0.0001:
                return 0.0001
        return 0.0001

    def get_pip_value_per_lot(self, symbol: str) -> float:
        self.ensure_connected()
        tick = mt5.symbol_info_tick(symbol)
        info = self.get_symbol_info(symbol) or {}
        pip_size = float(info.get("pip_size", 0.0) or self._pip_size_for_symbol(symbol))

        # Prefer MT5's empirical order_calc_profit due to broker tick misreporting on XAU
        if tick and pip_size > 0.0:
            try:
                ref_price = float(getattr(tick, "ask", 0.0) or getattr(tick, "bid", 0.0) or 0.0)
                if ref_price > 0.0:
                    profit = mt5.order_calc_profit(
                        mt5.ORDER_TYPE_BUY,
                        symbol,
                        1.0,
                        ref_price,
                        ref_price + pip_size,
                    )
                    if profit is not None and float(profit) > 0.0:
                        return abs(float(profit))
            except Exception as e:
                logger.warning(f"order_calc_profit failed for {symbol}: {e}")

        # Fallback to symbol_info calculation if order_calc_profit fails
        pip_value = float(info.get("pip_value_per_lot", 0.0) or 0.0)
        if pip_value > 0.0:
            return pip_value

        return 0.0

    def estimate_profit_usd(
        self,
        symbol: str,
        side: str,
        volume: float,
        open_price: float,
        close_price: float,
        symbol_info: Optional[dict] = None,
    ) -> float:
        self.ensure_connected()
        sym = str(symbol or "").upper().strip()
        direction = str(side or "").upper().strip()
        qty = float(volume or 0.0)
        entry = float(open_price or 0.0)
        exit_price = float(close_price or 0.0)
        if not sym or direction not in ("BUY", "SELL") or qty <= 0.0 or entry <= 0.0 or exit_price <= 0.0:
            return 0.0

        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        try:
            profit = mt5.order_calc_profit(order_type, sym, qty, entry, exit_price)
            if profit is not None:
                return float(profit or 0.0)
        except Exception:
            pass

        info = symbol_info or self.get_symbol_info(sym) or {}
        tick_value = float(info.get("trade_tick_value", 0.0) or 0.0)
        tick_size = float(info.get("trade_tick_size", 0.0) or info.get("point", 0.0) or 0.0)
        if tick_value <= 0.0 or tick_size <= 0.0:
            return 0.0

        price_delta = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
        return float((price_delta / tick_size) * tick_value * qty)

    #  Orders 
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
            logger.error(f"  Order failed on {symbol}: {err}")
            return None

        ticket = result.order
        deal_ticket = getattr(result, "deal", None)
        position_id = getattr(result, "position", None) or ticket
        logger.info(f"  ORDER PLACED | {order_type} {volume} {symbol} | "
                    f"Price: {result.price} | Ticket: {ticket} | Deal: {deal_ticket} | Position: {position_id}")
        
        # Now add SL/TP via modify
        if sl > 0 or tp > 0:
            time.sleep(0.5)  # Brief pause before modify
            modify_result = self.modify_sl_tp(ticket, sl, tp)
            if modify_result:
                logger.info(f"  SL/TP added | SL: {sl} | TP: {tp}")
            else:
                logger.warning(f"  Trade opened but SL/TP failed to set")
        
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
            "time":    datetime.now(timezone.utc),
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
            "time": datetime.now(timezone.utc),
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

    def _position_by_ticket(self, ticket: int):
        positions = mt5.positions_get()
        if not positions:
            return None
        for pos in positions:
            if int(getattr(pos, "ticket", 0) or 0) == int(ticket):
                return pos
        return None

    def _volume_precision(self, step: float) -> int:
        step_text = f"{float(step):.8f}".rstrip("0")
        if "." not in step_text:
            return 0
        return len(step_text.split(".", 1)[1])

    def _round_volume_down(self, volume: float, step: float) -> float:
        if step <= 0:
            return max(0.0, float(volume))
        units = math.floor((float(volume) / step) + 1e-9)
        return max(0.0, units * step)

    def _resolve_partial_volume(self, symbol: str, requested_volume: float, position_volume: float) -> dict:
        info = self.get_symbol_info(symbol) or {}
        min_lot = float(info.get("volume_min", 0.01) or 0.01)
        step = float(info.get("volume_step", min_lot) or min_lot)
        max_lot = float(info.get("volume_max", 0.0) or 0.0)

        req = max(0.0, float(requested_volume or 0.0))
        pos_vol = max(0.0, float(position_volume or 0.0))
        if req <= 0.0:
            return {"ok": False, "reason": "REQUESTED_VOLUME_NON_POSITIVE", "volume": 0.0}
        if pos_vol <= 0.0:
            return {"ok": False, "reason": "POSITION_VOLUME_NON_POSITIVE", "volume": 0.0}

        target = min(req, pos_vol)
        if max_lot > 0:
            target = min(target, max_lot)

        close_volume = self._round_volume_down(target, step)
        precision = max(2, self._volume_precision(step))
        close_volume = round(close_volume, precision)

        if close_volume < min_lot:
            return {
                "ok": False,
                "reason": "VOLUME_BELOW_MIN_LOT",
                "volume": close_volume,
                "min_lot": min_lot,
                "step": step,
            }

        remaining = max(0.0, pos_vol - close_volume)
        if remaining > 0.0 and remaining < min_lot:
            adjusted = self._round_volume_down(max(0.0, pos_vol - min_lot), step)
            adjusted = round(adjusted, precision)
            if adjusted >= min_lot and adjusted < pos_vol:
                close_volume = adjusted
                remaining = max(0.0, pos_vol - close_volume)
            else:
                return {
                    "ok": False,
                    "reason": "REMAINDER_BELOW_MIN_LOT",
                    "volume": close_volume,
                    "remaining": remaining,
                    "min_lot": min_lot,
                    "step": step,
                }

        if close_volume >= (pos_vol - (step * 0.5)):
            return {
                "ok": False,
                "reason": "PARTIAL_EQUALS_FULL_POSITION",
                "volume": close_volume,
                "position_volume": pos_vol,
                "step": step,
            }

        return {
            "ok": True,
            "volume": close_volume,
            "remaining": remaining,
            "min_lot": min_lot,
            "step": step,
        }

    def close_position(self, ticket: int) -> bool:
        self.ensure_connected()
        position = self._position_by_ticket(ticket)

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
            logger.info(f"  CLOSED position {ticket} on {position.symbol}")
            return True

        logger.error(f"  Failed to close {ticket}: {result.comment if result else 'None'}")
        return False

    def partial_close_position(self, ticket: int, volume: float) -> dict:
        self.ensure_connected()
        position = self._position_by_ticket(ticket)
        if position is None:
            return {"ok": False, "retcode": None, "comment": "POSITION_NOT_FOUND", "closed_volume": 0.0}

        requested_volume = float(volume or 0.0)
        volume_check = self._resolve_partial_volume(
            symbol=str(position.symbol),
            requested_volume=requested_volume,
            position_volume=float(position.volume),
        )
        if not bool(volume_check.get("ok", False)):
            return {
                "ok": False,
                "retcode": None,
                "comment": str(volume_check.get("reason", "VOLUME_REJECTED")),
                "closed_volume": 0.0,
                "details": volume_check,
            }

        close_volume = float(volume_check.get("volume", 0.0) or 0.0)
        if close_volume <= 0.0:
            return {"ok": False, "retcode": None, "comment": "INVALID_CLOSE_VOLUME", "closed_volume": 0.0}

        order_type = mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        tick = self.get_tick(position.symbol)
        if not tick:
            return {"ok": False, "retcode": None, "comment": "NO_TICK", "closed_volume": 0.0}
        price = tick["bid"] if order_type == mt5.ORDER_TYPE_SELL else tick["ask"]

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": close_volume,
            "type": order_type,
            "position": int(ticket),
            "price": float(price),
            "deviation": 50,
            "magic": 20250101,
            "comment": "ICT_BOT_PARTIAL",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        result = mt5.order_send(request)
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            return {
                "ok": True,
                "retcode": int(result.retcode),
                "comment": str(getattr(result, "comment", "") or ""),
                "closed_volume": close_volume,
                "order": getattr(result, "order", None),
                "deal": getattr(result, "deal", None),
            }

        err = mt5.last_error()
        return {
            "ok": False,
            "retcode": int(getattr(result, "retcode", 0) or 0) if result is not None else None,
            "comment": str(getattr(result, "comment", "") or ""),
            "last_error": str(err),
            "closed_volume": 0.0,
            "requested_volume": requested_volume,
            "accepted_volume": close_volume,
        }

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

    def modify_position_sl(self, ticket: int, new_sl: float) -> dict:
        self.ensure_connected()
        position = self._position_by_ticket(ticket)
        if position is None:
            return {"ok": False, "retcode": None, "comment": "POSITION_NOT_FOUND", "last_error": ""}
        tp = float(getattr(position, "tp", 0.0) or 0.0)
        return self.modify_sl_tp_detailed(int(ticket), float(new_sl), tp)

    #  Positions 
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
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
        now = datetime.now(timezone.utc)
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


