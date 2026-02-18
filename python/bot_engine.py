"""
Trading Engine
───────────────
The central orchestrator:
  • Connects to MT5
  • Loops through all symbols every N seconds
  • Runs ICT strategy analysis
  • Applies risk & news filters
  • Places / manages trades
  • Manages open positions (trailing SL, forced close)
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from mt5_connector import MT5Connector
from ict_strategy import ICTStrategy, Signal, Direction
from risk_manager import RiskManager
from news_filter import NewsFilter
from notifier import Notifier

logger = logging.getLogger("ENGINE")


class TradingEngine:
    def __init__(self, config: dict, news_filter: NewsFilter,
                 shutdown_event: asyncio.Event):
        self.cfg           = config
        self.pairs         = config["pairs"]
        self.tf            = config["timeframes"]
        self.shutdown      = shutdown_event

        self.mt5           = MT5Connector(config)
        self.strategy      = ICTStrategy(config)
        self.risk          = RiskManager(config)
        self.news          = news_filter
        self.notifier      = Notifier(config.get("notifications", {}))

        self._scan_interval   = 10   # seconds between full scans
        self._manage_interval = 5    # seconds between position management
        self._news_interval   = 3600 # refresh news hourly
        self._last_signals: dict[str, datetime] = {}
        self._signal_cooldown = 300  # 5 min between signals on same pair

    # ── Startup ────────────────────────────────────────────────────────────────
    async def _startup(self) -> bool:
        logger.info("🔌  Connecting to MT5...")
        if not self.mt5.connect():
            logger.critical("❌  MT5 connection failed. Exiting.")
            return False

        logger.info("📰  Fetching news calendar...")
        await self.news.update()

        logger.info(f"🎯  Trading {len(self.pairs)} pairs: {self.pairs}")
        return True

    # ── Main Run Loop ─────────────────────────────────────────────────────────
    async def run(self):
        if not await self._startup():
            self.shutdown.set()
            return

        scan_task   = asyncio.create_task(self._scan_loop())
        manage_task = asyncio.create_task(self._manage_loop())
        news_task   = asyncio.create_task(self._news_loop())

        await self.shutdown.wait()

        scan_task.cancel()
        manage_task.cancel()
        news_task.cancel()

    async def _scan_loop(self):
        """Continuously scan all pairs for entry signals."""
        while not self.shutdown.is_set():
            try:
                await self._scan_all_pairs()
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
            await asyncio.sleep(self._scan_interval)

    async def _manage_loop(self):
        """Continuously manage open positions."""
        while not self.shutdown.is_set():
            try:
                await self._manage_positions()
            except Exception as e:
                logger.error(f"Manage error: {e}", exc_info=True)
            await asyncio.sleep(self._manage_interval)

    async def _news_loop(self):
        """Periodically refresh news calendar."""
        while not self.shutdown.is_set():
            await asyncio.sleep(self._news_interval)
            await self.news.update()

    # ── Scan All Pairs ─────────────────────────────────────────────────────────
    async def _scan_all_pairs(self):
        account   = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        balance   = account.get("balance", 0)

        for symbol in self.pairs:
            try:
                await self._scan_symbol(symbol, positions, balance)
            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}", exc_info=True)

    async def _scan_symbol(self, symbol: str, open_positions: list, balance: float):
        # ── Gate 1: Already have a position on this pair ──────────────────────
        existing = [p for p in open_positions if p["symbol"] == symbol]
        if existing:
            return  # Don't stack positions on same symbol

        # ── Gate 2: Signal cooldown ───────────────────────────────────────────
        last = self._last_signals.get(symbol)
        if last:
            elapsed = (datetime.utcnow() - last).total_seconds()
            if elapsed < self._signal_cooldown:
                return

        # ── Gate 3: News block ────────────────────────────────────────────────
        blocked, reason = self.news.is_blocked(symbol)
        if blocked:
            logger.debug(f"{symbol}: {reason}")
            return

        # ── Gate 4: Risk guard ────────────────────────────────────────────────
        can_trade, reason = self.risk.can_trade(open_positions, balance)
        if not can_trade:
            logger.warning(f"⛔  Risk block: {reason}")
            return

        # ── Fetch candle data ─────────────────────────────────────────────────
        candles_h4  = self.mt5.get_candles(symbol, self.tf["bias"],   300)
        candles_m15 = self.mt5.get_candles(symbol, self.tf["entry"],  200)
        candles_m5  = self.mt5.get_candles(symbol, self.tf["trigger"], 100)
        candles_m1  = self.mt5.get_candles(symbol, "M1",              100)
        tick        = self.mt5.get_tick(symbol)
        spread      = self.mt5.get_spread_pips(symbol)

        if not candles_h4 or not candles_m15 or not tick:
            logger.warning(f"Incomplete data for {symbol} — skipping")
            return

        # ── Run ICT Strategy ──────────────────────────────────────────────────
        signal = self.strategy.analyze(
            symbol, candles_h4, candles_m15, candles_m5, candles_m1, spread
        )

        if signal and signal.valid:
            await self._execute_signal(signal, balance)

    # ── Execute Signal ─────────────────────────────────────────────────────────
    async def _execute_signal(self, signal: Signal, balance: float):
        logger.info(f"🎯  Executing signal: {signal.symbol} {signal.direction.value} "
                    f"| {signal.setup_type.value} | Conf: {signal.confidence:.0%}")

        # Calculate position size
        lot = self.risk.calculate_lot_size(
            symbol=signal.symbol,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            account_balance=balance
        )

        if lot < 0.01:
            logger.warning(f"Lot size too small ({lot}) — skip")
            return

        # Place order
        result = self.mt5.place_market_order(
            symbol     = signal.symbol,
            order_type = signal.direction.value,
            volume     = lot,
            sl         = signal.sl,
            tp         = signal.tp,
            comment    = f"ICT_{signal.setup_type.value}"
        )

        if result:
            # Record in journal
            self.risk.record_open(result, signal.setup_type.value, signal.reason)
            self._last_signals[signal.symbol] = datetime.utcnow()

            # Check kill zone for notification
            in_kill_zone, kz_name = self.strategy.in_kill_zone()

            # Notify
            await self.notifier.send(
                f"🤖 NEW TRADE\n"
                f"{'─'*30}\n"
                f"Pair:  {signal.symbol}\n"
                f"Type:  {signal.direction.value}\n"
                f"Setup: {signal.setup_type.value}\n"
                f"Entry: {result['price']}\n"
                f"SL:    {signal.sl}\n"
                f"TP:    {signal.tp}\n"
                f"Lot:   {lot}\n"
                f"RR:    1:{signal.rr}\n"
                f"Conf:  {signal.confidence:.0%}\n"
                f"Zone:  {kz_name}\n"
                f"Reason: {signal.reason}"
            )

    # ── Manage Open Positions ──────────────────────────────────────────────────
    async def _manage_positions(self):
        positions = self.mt5.get_open_positions()
        if not positions:
            return

        for pos in positions:
            tick = self.mt5.get_tick(pos["symbol"])
            if not tick:
                continue

            current_price = tick["bid"] if pos["type"] == "SELL" else tick["ask"]

            # Trailing Stop
            new_sl = self.risk.get_trailing_sl(pos, current_price)
            if new_sl and new_sl != pos["sl"]:
                if self.mt5.modify_sl_tp(pos["ticket"], new_sl, pos["tp"]):
                    logger.info(f"📈  Trailing SL moved | {pos['symbol']} | "
                                f"{pos['sl']:.5f} → {new_sl:.5f}")

    # ── Shutdown ───────────────────────────────────────────────────────────────
    async def shutdown(self):
        logger.info("Closing MT5 connection...")
        self.mt5.disconnect()
        stats = self.risk.get_stats()
        logger.info(f"📊  Session stats: {stats}")

    # ── Status (for dashboard) ────────────────────────────────────────────────
    def get_status(self) -> dict:
        account   = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        stats     = self.risk.get_stats()
        upcoming  = self.news.get_upcoming(2)

        # Get H4 bias for all pairs (for Command Center heatmap)
        pair_biases = {}
        for symbol in self.pairs:
            try:
                candles_h4 = self.mt5.get_candles(symbol, "H4", 50)
                if candles_h4 and len(candles_h4) >= 30:
                    bias = self.strategy.get_htf_bias(candles_h4)
                    pair_biases[symbol] = bias.value  # 'BULLISH', 'BEARISH', 'NEUTRAL'
            except Exception as e:
                logger.debug(f"Could not get bias for {symbol}: {e}")
                pair_biases[symbol] = "NEUTRAL"
        
        # Build trade log from journal (for Command Center trade log)
        trade_log = []
        for record in self.risk.journal[-20:]:  # Last 20 trades
            trade_log.append({
                'time': record.open_time.strftime('%H:%M:%S'),
                'symbol': record.symbol,
                'type': record.direction,
                'setup': record.setup_type,
                'pnl': record.pnl,
                'lot': record.volume,
            })

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "account":   account,
            "positions": positions,
            "stats":     stats,
            "connected": self.mt5.connected,
            "pair_biases": pair_biases,
            "trade_log": trade_log,
            "upcoming_news": [
                {
                    "time":     e.time.strftime("%H:%M"),
                    "currency": e.currency,
                    "impact":   e.impact,
                    "title":    e.title,
                }
                for e in upcoming
            ],
        }