"""
Trading Engine — WITH AI INTEGRATION
─────────────────────────────────────
Enhanced version with Memory & Brain system added to your existing code.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

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

        # ★ NEW: AI Memory and Brain
        db_path = Path(__file__).parent.parent / "memory" / "trading_memory.db"
        db_path.parent.mkdir(exist_ok=True)
        self.memory = TradingMemoryDB(db_path)
        self.brain = TradingBrain(self.memory)

        self._scan_interval   = 10
        self._manage_interval = 5
        self._news_interval   = 3600
        self._last_signals: dict[str, datetime] = {}
        self._signal_cooldown = 300
        self._tz = ZoneInfo("Africa/Gaborone")
        self._last_daily_metrics = {}
        self._last_guard_rails = {}
        self.analyzer_running = False
        self.analyzer_last_tick = None

    # ── Startup ────────────────────────────────────────────────────────────────
    async def _startup(self) -> bool:
        logger.info("🔌  Connecting to MT5...")
        if not self.mt5.connect():
            logger.critical("❌  MT5 connection failed. Exiting.")
            return False

        logger.info("📰  Fetching news calendar...")
        await self.news.update()

        logger.info(f"🎯  Trading {len(self.pairs)} pairs: {self.pairs}")
        
        # ★ NEW: Show AI performance report
        logger.info("\n" + self.brain.generate_performance_report())
        
        return True

    # ── Main Run Loop ─────────────────────────────────────────────────────────
    async def run(self):
        if not await self._startup():
            self.shutdown.set()
            return

        # ★ NEW: Add trade analyzer
        from trade_analyzer import TradeAnalyzer
        analyzer = TradeAnalyzer(self)
        logger.info("Analyzer task creating...")

        scan_task     = asyncio.create_task(self._scan_loop())
        manage_task   = asyncio.create_task(self._manage_loop())
        news_task     = asyncio.create_task(self._news_loop())
        analyzer_task = asyncio.create_task(analyzer.run())  # ★ NEW
        logger.info("Analyzer task started")

        await self.shutdown.wait()

        scan_task.cancel()
        manage_task.cancel()
        news_task.cancel()
        analyzer_task.cancel()  # ★ NEW

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

    # ── Scan All Pairs ─────────────────────────────────────────────────────────
    async def _scan_all_pairs(self):
        account   = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        balance   = account.get("balance", 0)
        start_utc, end_utc = self._daily_window_utc()
        daily_metrics = self.memory.get_daily_summary(start_utc, end_utc)
        guard_rails = self._build_guard_rails(balance, daily_metrics)
        self._last_daily_metrics = daily_metrics
        self._last_guard_rails = guard_rails

        for symbol in self.pairs:
            try:
                await self._scan_symbol(symbol, positions, balance, guard_rails)
            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}", exc_info=True)

    async def _scan_symbol(self, symbol: str, open_positions: list, balance: float, guard_rails: dict):
        existing = [p for p in open_positions if p["symbol"] == symbol]
        if existing:
            return

        if guard_rails.get("triggered"):
            logger.warning(f"SKIP_ENTRY_GUARDRAILS: reason={guard_rails.get('reason', 'UNKNOWN')}")
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
            # ★ NEW: Check if setup is enabled by AI brain
            if not self.brain.should_disable_setup(signal.setup_type.value):
                await self._execute_signal(signal, balance, spread, candles_h4, candles_m15, candles_m5)
            else:
                logger.warning(f"🧠  Setup '{signal.setup_type.value}' DISABLED due to low performance")

    # ── Execute Signal ─────────────────────────────────────────────────────────
    async def _execute_signal(self, signal: Signal, balance: float, spread: float, 
                               candles_h4: list, candles_m15: list, candles_m5: list):
        
        # ★ NEW: Get AI-learned confidence
        adaptive_confidence = self.brain.get_adaptive_confidence(signal.setup_type.value)
        original_confidence = signal.confidence
        signal.confidence = adaptive_confidence / 100  # Convert to 0-1
        
        logger.info(f"🎯  Executing signal: {signal.symbol} {signal.direction.value} "
                    f"| {signal.setup_type.value} | Conf: {original_confidence:.0%}→{signal.confidence:.0%} (learned)")

        # ★ NEW: Get AI reasoning for this trade
        analysis = self.brain.analyze_entry_conditions(
            signal.symbol, signal.setup_type.value,
            candles_h4, candles_m15, candles_m5, signal
        )
        
        logger.info(f"🧠  Reasoning: {analysis['reasoning']}")
        for condition in analysis['conditions_met'][:3]:
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
            self.risk.record_open(result, signal.setup_type.value, signal.reason)
            self._last_signals[signal.symbol] = datetime.utcnow()

            # ★ NEW: Record in AI memory
            htf_bias = self.strategy.get_htf_bias(candles_h4).value
            
            trade_memory = TradeMemory(
                ticket=result["ticket"],
                order_ticket=result.get("order_ticket"),
                deal_ticket=result.get("deal_ticket"),
                position_id=result.get("position_id"),
                symbol=signal.symbol,
                direction=signal.direction.value,
                setup_type=signal.setup_type.value,
                entry_price=result["price"],
                sl_price=signal.sl,
                tp_price=signal.tp,
                lot_size=lot,
                htf_bias=htf_bias,
                kill_zone=kz_name,
                spread_pips=spread,
                reason=analysis['reasoning'],
                conditions_met=analysis['conditions_met'],
                expected_outcome=analysis['expected_outcome'],
                confidence_input=signal.confidence,
                entry_time=datetime.utcnow()
            )
            
            self.memory.record_entry(trade_memory)
            logger.info(f"📝  Recorded entry in AI memory: {signal.setup_type.value} {signal.symbol} #{result['ticket']}")

            await self.notifier.send(
                f"🤖 NEW TRADE — AI Confidence: {signal.confidence:.0%}\n"
                f"{'─'*30}\n"
                f"Pair:  {signal.symbol}\n"
                f"Type:  {signal.direction.value}\n"
                f"Setup: {signal.setup_type.value}\n"
                f"Entry: {result['price']}\n"
                f"SL:    {signal.sl}\n"
                f"TP:    {signal.tp}\n"
                f"Lot:   {lot}\n"
                f"RR:    1:{signal.rr}\n"
                f"Zone:  {kz_name}\n"
                f"\n🧠 REASONING:\n{analysis['reasoning']}\n"
                f"\n📊 CONDITIONS:\n" + "\n".join(f"  • {c}" for c in analysis['conditions_met'][:3])
            )

    # ── Manage Open Positions ──────────────────────────────────────────────────
    async def _manage_positions(self):
        positions = self.mt5.get_open_positions()
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

    # ── Status (for dashboard) ────────────────────────────────────────────────
    def get_status(self) -> dict:
        account   = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        stats     = self.risk.get_stats()
        max_loss_pct = float(self.cfg.get("risk", {}).get("max_daily_loss_pct", 0.0))
        start_utc, end_utc = self._daily_window_utc()
        daily = self.memory.get_daily_summary(start_utc, end_utc)
        balance = float(account.get("balance", 0.0))
        guard_rails = self._build_guard_rails(balance, daily)
        self._last_daily_metrics = daily
        self._last_guard_rails = guard_rails
        events_enabled = bool(self.cfg.get("news", {}).get("enabled", False))
        upcoming = self.news.get_upcoming(8) if events_enabled else []
        events_status = "ok" if events_enabled else "not_configured"

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
        
        # ★ NEW: Get trade log from AI memory (more detailed)
        trade_log = self.memory.get_recent_trades(20)
        for t in trade_log:
            if isinstance(t.get("time"), datetime):
                t["time"] = t["time"].isoformat()

        merged_stats = {
            **stats,
            "daily_pnl": daily["daily_pnl"],
            "daily_trades": daily["trades_today_count"],
            "winrate": daily["win_rate"],
            "win_rate": daily["win_rate"],
            "trades_today_count": daily["trades_today_count"],
            "wins_today_count": daily["wins_today_count"],
            "losses_today_count": daily["losses_today_count"],
            "profit_factor": daily["profit_factor"],
            "avg_pnl": daily["avg_pnl"],
        }

        upcoming_events = [
            {
                "time": e.time.isoformat(),
                "currency": e.currency,
                "impact": e.impact,
                "title": e.title,
            }
            for e in upcoming[:5]
        ]

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "server_time": datetime.utcnow().isoformat(),
            "account":   account,
            "positions": self._json_safe_positions(positions),
            "stats":     merged_stats,
            "connected": self.mt5.connected,
            "pair_biases": pair_biases,
            "trade_log": trade_log,
            "setup_performance": self.memory.get_all_setup_performance(),  # ★ NEW
            "daily_pnl": daily["daily_pnl"],
            "win_rate": daily["win_rate"],
            "trades_today_count": daily["trades_today_count"],
            "wins_today_count": daily["wins_today_count"],
            "losses_today_count": daily["losses_today_count"],
            "profit_factor": daily["profit_factor"],
            "avg_pnl": daily["avg_pnl"],
            "guard_rails": guard_rails,
            "risk": {
                "risk_per_trade": float(self.cfg.get("risk", {}).get("risk_per_trade_pct", 0.0)),
                "max_daily_loss_pct": max_loss_pct,
                "max_daily_trades": int(self.cfg.get("risk", {}).get("max_daily_trades", 0)),
            },
            "daily_metrics_timezone": "Africa/Gaborone",
            "daily_window_utc": {
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
            },
            "upcoming_events": upcoming_events,
            "events_status": events_status,
            "upcoming_news": [
                {
                    "time":     e.time.strftime("%H:%M"),
                    "time_iso": e.time.isoformat(),
                    "currency": e.currency,
                    "impact":   e.impact,
                    "title":    e.title,
                }
                for e in upcoming[:5]
            ],
            "analyzer": {
                "running": bool(self.analyzer_running),
                "last_tick": self.analyzer_last_tick.isoformat() if isinstance(self.analyzer_last_tick, datetime) else None,
            },
        }

    def _daily_window_utc(self) -> tuple[datetime, datetime]:
        now_utc = datetime.now(timezone.utc)
        now_local = now_utc.astimezone(self._tz)
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = end_local.astimezone(timezone.utc).replace(tzinfo=None)
        return start_utc, end_utc

    def _build_guard_rails(self, account_balance: float, daily: dict) -> dict:
        risk_cfg = self.cfg.get("risk", {})
        daily_max_trades = int(risk_cfg.get("max_daily_trades", 0))
        risk_per_trade = float(risk_cfg.get("risk_per_trade_pct", 0.0))
        daily_max_loss_pct = float(risk_cfg.get("max_daily_loss_pct", 0.0))
        daily_max_loss = abs(account_balance * (daily_max_loss_pct / 100.0))
        current_daily_pnl = float(daily.get("daily_pnl", 0.0))
        trades_today = int(daily.get("trades_today_count", 0))
        remaining_loss_buffer = round(daily_max_loss + current_daily_pnl, 2)

        reason = ""
        triggered = False
        if current_daily_pnl <= -abs(daily_max_loss):
            triggered = True
            reason = "DAILY_MAX_LOSS"
        elif daily_max_trades > 0 and trades_today >= daily_max_trades:
            triggered = True
            reason = "DAILY_MAX_TRADES"

        return {
            "daily_max_loss": round(daily_max_loss, 2),
            "daily_max_trades": daily_max_trades,
            "risk_per_trade": risk_per_trade,
            "current_daily_pnl": round(current_daily_pnl, 2),
            "remaining_loss_buffer": remaining_loss_buffer,
            "trades_today": trades_today,
            "triggered": triggered,
            "reason": reason,
        }

    def _json_safe_positions(self, positions: list) -> list:
        out = []
        for p in positions:
            item = dict(p)
            ot = item.get("open_time")
            if isinstance(ot, datetime):
                item["open_time"] = ot.isoformat()
            out.append(item)
        return out
