"""
Trading Engine — With AI Memory & Brain
────────────────────────────────────────
Now includes:
  • Trading Memory (SQLite database)
  • Trading Brain (AI analysis)
  • Adaptive confidence scoring
  • Self-learning from wins/losses
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from mt5_connector import MT5Connector
from ict_strategy import ICTStrategy, Signal, Direction
from risk_manager import RiskManager
from news_filter import NewsFilter
from notifier import Notifier
from trading_memory import TradingMemoryDB, TradeMemory
from trading_brain import TradingBrain

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
        
        # NEW: Memory and Brain
        db_path = Path(__file__).parent.parent / "memory" / "trading_memory.db"
        db_path.parent.mkdir(exist_ok=True)
        self.memory = TradingMemoryDB(db_path)
        self.brain = TradingBrain(self.memory)

        self._scan_interval   = 10
        self._manage_interval = 5
        self._news_interval   = 3600
        self._last_signals: dict[str, datetime] = {}
        self._signal_cooldown = 300

    async def _startup(self) -> bool:
        logger.info("🔌  Connecting to MT5...")
        if not self.mt5.connect():
            logger.critical("❌  MT5 connection failed. Exiting.")
            return False

        logger.info("📰  Fetching news calendar...")
        await self.news.update()

        logger.info(f"🎯  Trading {len(self.pairs)} pairs: {self.pairs}")
        
        # Print performance report
        logger.info("\n" + self.brain.generate_performance_report())
        
        return True

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
        while not self.shutdown.is_set():
            try:
                await self._scan_all_pairs()
            except Exception as e:
                logger.error(f"Scan error: {e}", exc_info=True)
            await asyncio.sleep(self._scan_interval)

    async def _manage_loop(self):
        while not self.shutdown.is_set():
            try:
                await self._manage_positions()
            except Exception as e:
                logger.error(f"Manage error: {e}", exc_info=True)
            await asyncio.sleep(self._manage_interval)

    async def _news_loop(self):
        while not self.shutdown.is_set():
            await asyncio.sleep(self._news_interval)
            await self.news.update()

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
        existing = [p for p in open_positions if p["symbol"] == symbol]
        if existing:
            return

        last = self._last_signals.get(symbol)
        if last:
            elapsed = (datetime.utcnow() - last).total_seconds()
            if elapsed < self._signal_cooldown:
                return

        blocked, reason = self.news.is_blocked(symbol)
        if blocked:
            logger.debug(f"{symbol}: {reason}")
            return

        can_trade, reason = self.risk.can_trade(open_positions, balance)
        if not can_trade:
            logger.warning(f"⛔  Risk block: {reason}")
            return

        candles_h4  = self.mt5.get_candles(symbol, self.tf["bias"],   300)
        candles_m15 = self.mt5.get_candles(symbol, self.tf["entry"],  200)
        candles_m5  = self.mt5.get_candles(symbol, self.tf["trigger"], 100)
        candles_m1  = self.mt5.get_candles(symbol, "M1",              100)
        tick        = self.mt5.get_tick(symbol)
        spread      = self.mt5.get_spread_pips(symbol)

        if not candles_h4 or not candles_m15 or not tick:
            logger.warning(f"Incomplete data for {symbol} — skipping")
            return

        signal = self.strategy.analyze(
            symbol, candles_h4, candles_m15, candles_m5, candles_m1, spread
        )

        if signal and signal.valid:
            # Check if setup is enabled (brain decision)
            if not self.brain.should_disable_setup(signal.setup_type.value):
                await self._execute_signal(signal, balance, spread, candles_h4, candles_m15, candles_m5)
            else:
                logger.warning(f"🧠  Setup '{signal.setup_type.value}' is DISABLED due to low performance")

    async def _execute_signal(self, signal: Signal, balance: float, spread_pips: float,
                               candles_h4: list, candles_m15: list, candles_m5: list):
        
        # Get AI-powered confidence (based on actual performance)
        adaptive_confidence = self.brain.get_adaptive_confidence(signal.setup_type.value)
        
        # Override signal confidence with learned confidence
        original_confidence = signal.confidence
        signal.confidence = adaptive_confidence / 100  # Convert to 0-1
        
        logger.info(f"🎯  Executing: {signal.symbol} {signal.direction.value} | "
                    f"{signal.setup_type.value} | "
                    f"Conf: {original_confidence:.0%}→{signal.confidence:.0%} (learned)")
        
        # Get AI analysis of WHY we're taking this trade
        analysis = self.brain.analyze_entry_conditions(
            signal.symbol, signal.setup_type.value,
            candles_h4, candles_m15, candles_m5, signal
        )
        
        logger.info(f"🧠  Reasoning: {analysis['reasoning']}")
        for condition in analysis['conditions_met']:
            logger.info(f"   ✓ {condition}")
        
        in_kill_zone, kz_name = self.strategy.in_kill_zone()
        
        lot = self.risk.calculate_lot_size(
            symbol=signal.symbol,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            account_balance=balance,
            confidence=signal.confidence,
            in_kill_zone=in_kill_zone
        )

        if lot < 0.01:
            logger.warning(f"Lot size too small ({lot}) — skip")
            return

        result = self.mt5.place_market_order(
            symbol     = signal.symbol,
            order_type = signal.direction.value,
            volume     = lot,
            sl         = signal.sl,
            tp         = signal.tp,
            comment    = f"ICT_{signal.setup_type.value}"
        )

        if result:
            # Record in risk manager
            self.risk.record_open(result, signal.setup_type.value, signal.reason)
            self._last_signals[signal.symbol] = datetime.utcnow()
            
            # NEW: Record in AI memory with full context
            htf_bias = self.strategy.get_htf_bias(candles_h4).value
            
            trade_memory = TradeMemory(
                ticket=result["ticket"],
                symbol=signal.symbol,
                direction=signal.direction.value,
                setup_type=signal.setup_type.value,
                entry_price=result["price"],
                sl_price=signal.sl,
                tp_price=signal.tp,
                lot_size=lot,
                htf_bias=htf_bias,
                kill_zone=kz_name,
                spread_pips=spread_pips,
                reason=analysis['reasoning'],
                conditions_met=analysis['conditions_met'],
                expected_outcome=analysis['expected_outcome'],
                confidence_input=signal.confidence,
                entry_time=datetime.utcnow()
            )
            
            self.memory.record_entry(trade_memory)

            await self.notifier.send(
                f"🤖 NEW TRADE - AI CONFIDENCE: {signal.confidence:.0%}\n"
                f"{'─'*40}\n"
                f"Pair:  {signal.symbol}\n"
                f"Type:  {signal.direction.value}\n"
                f"Setup: {signal.setup_type.value}\n"
                f"Entry: {result['price']}\n"
                f"SL:    {signal.sl}\n"
                f"TP:    {signal.tp}\n"
                f"Lot:   {lot}\n"
                f"RR:    1:{signal.rr}\n"
                f"\n🧠 REASONING:\n{analysis['reasoning']}\n"
                f"\n✓ CONDITIONS MET:\n" + "\n".join(f"  • {c}" for c in analysis['conditions_met'][:3]) +
                f"\n\n📊 EXPECTED:\n{analysis['expected_outcome']}"
            )

    async def _manage_positions(self):
        """Position management + AI learning from closed trades"""
        positions = self.mt5.get_open_positions()
        
        # Check for recently closed trades and learn from them
        today_trades = self.mt5.get_today_trades()
        for trade in today_trades:
            # Check if we've already analyzed this trade
            # (You'd track this with a set of analyzed tickets)
            ticket = trade["ticket"]
            
            # Get the trade record from memory
            # If it exists and hasn't been analyzed, analyze it
            # This is simplified - you'd add logic to track analyzed trades
        
        if not positions:
            return

        if not hasattr(self, '_trailing_2022'):
            from ict_2022_trailing import ICT2022TrailingStop
            self._trailing_2022 = ICT2022TrailingStop(self.cfg, self.mt5)

        for pos in positions:
            tick = self.mt5.get_tick(pos["symbol"])
            if not tick:
                continue

            bid = tick["bid"]
            ask = tick["ask"]
            mid_price = (bid + ask) / 2
            
            spread = tick.get("spread", 0)
            pip_size = self.strategy.get_pip_size(pos["symbol"])
            spread_pips = spread / pip_size if pip_size > 0 else 0
            
            candles_m5 = self.mt5.get_candles(pos["symbol"], "M5", 20)
            if not candles_m5:
                continue
            
            new_sl = self._trailing_2022.get_trailing_sl(
                position=pos,
                current_price=mid_price,
                candles_m5=candles_m5,
                spread_pips=spread_pips
            )
            
            if new_sl and new_sl != pos["sl"]:
                if pos["type"] == "BUY" and new_sl > pos["sl"]:
                    if self.mt5.modify_sl_tp(pos["ticket"], new_sl, pos["tp"]):
                        logger.info(f"📈  Trailing SL | {pos['symbol']} #{pos['ticket']} | "
                                    f"{pos['sl']:.5f} → {new_sl:.5f}")
                
                elif pos["type"] == "SELL" and new_sl < pos["sl"]:
                    if self.mt5.modify_sl_tp(pos["ticket"], new_sl, pos["tp"]):
                        logger.info(f"📉  Trailing SL | {pos['symbol']} #{pos['ticket']} | "
                                    f"{pos['sl']:.5f} → {new_sl:.5f}")

    def get_status(self) -> dict:
        """Status for Command Center with AI insights"""
        account   = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        stats     = self.risk.get_stats()
        upcoming  = self.news.get_upcoming(2)

        pair_biases = {}
        for symbol in self.pairs:
            try:
                candles_h4 = self.mt5.get_candles(symbol, "H4", 50)
                if candles_h4 and len(candles_h4) >= 30:
                    bias = self.strategy.get_htf_bias(candles_h4)
                    pair_biases[symbol] = bias.value
            except Exception as e:
                logger.debug(f"Could not get bias for {symbol}: {e}")
                pair_biases[symbol] = "NEUTRAL"
        
        # Get recent trades from AI memory (more detailed than risk manager)
        trade_log = self.memory.get_recent_trades(20)

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "account":   account,
            "positions": positions,
            "stats":     stats,
            "connected": self.mt5.connected,
            "pair_biases": pair_biases,
            "trade_log": trade_log,
            "setup_performance": self.memory.get_all_setup_performance(),
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
