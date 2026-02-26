"""
Trading Engine - WITH AI INTEGRATION
Enhanced version with Memory & Brain system added to your existing code.
"""

import asyncio
import logging
from collections import deque
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
from cooldown_manager import CooldownManager
from hybrid_gate import HybridGate
from sniper_filter import SniperFilter

logger = logging.getLogger("ENGINE")


class TradingEngine:
    def __init__(self, config: dict, news_filter: NewsFilter, shutdown_event: asyncio.Event):
        self.cfg = config
        self.pairs = config["pairs"]
        self.tf = config["timeframes"]
        self.shutdown = shutdown_event

        self.mt5 = MT5Connector(config)
        self.strategy = ICTStrategy(config)
        self.risk = RiskManager(config)
        self.news = news_filter
        self.notifier = Notifier(config.get("notifications", {}))
        self.cooldowns = CooldownManager(self.cfg)

        db_path = Path(__file__).parent.parent / "memory" / "trading_memory.db"
        db_path.parent.mkdir(exist_ok=True)
        self.memory = TradingMemoryDB(db_path)
        self.brain = TradingBrain(self.memory)
        self.hybrid_gate = HybridGate(self.cfg, self.memory)
        self.sniper_filter = SniperFilter(self.cfg)

        self._scan_interval = 10
        self._manage_interval = 5
        self._news_interval = 3600
        self._last_signals: dict[str, datetime] = {}
        self._signal_cooldown = 300
        self._tz = ZoneInfo("Africa/Gaborone")
        self._last_daily_metrics = {}
        self._last_guard_rails = {}
        self._latest_equity = 0.0
        self._skip_reasons: deque[dict] = deque(maxlen=20)
        self._last_decisions: deque[dict] = deque(maxlen=20)
        self.analyzer_running = False
        self.analyzer_last_tick = None
        self.started_at_utc = datetime.utcnow()

    async def _startup(self) -> bool:
        logger.info("Connecting to MT5...")
        if not self.mt5.connect():
            logger.critical("MT5 connection failed. Exiting.")
            return False

        logger.info("Fetching news calendar...")
        await self.news.update()

        logger.info(f"Trading {len(self.pairs)} pairs: {self.pairs}")
        logger.info("\n" + self.brain.generate_performance_report())
        return True

    async def run(self):
        self.started_at_utc = datetime.utcnow()
        if not await self._startup():
            self.shutdown.set()
            return

        from trade_analyzer import TradeAnalyzer

        analyzer = TradeAnalyzer(self)
        logger.info("Analyzer task creating...")

        scan_task = asyncio.create_task(self._scan_loop())
        manage_task = asyncio.create_task(self._manage_loop())
        news_task = asyncio.create_task(self._news_loop())
        analyzer_task = asyncio.create_task(analyzer.run())
        logger.info("Analyzer task started")

        await self.shutdown.wait()

        scan_task.cancel()
        manage_task.cancel()
        news_task.cancel()
        analyzer_task.cancel()

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
                await self._fallback_closed_trade_sync()
            except Exception as e:
                logger.error(f"Manage error: {e}", exc_info=True)
            await asyncio.sleep(self._manage_interval)

    async def _news_loop(self):
        while not self.shutdown.is_set():
            await asyncio.sleep(self._news_interval)
            await self.news.update()

    async def _scan_all_pairs(self):
        account = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        balance = account.get("balance", 0)
        equity = float(account.get("equity", account.get("balance", 0.0)))
        self._latest_equity = equity

        if not self._is_hybrid_mode():
            ok, reason = self.cooldowns.update_equity_peak_and_check_dd(equity)
            if not ok:
                logger.warning(f"GUARD_EQUITY: {reason}")

        start_utc, end_utc = self._daily_window_utc()
        daily_metrics = self.memory.get_daily_summary(start_utc, end_utc)
        guard_rails = self._build_guard_rails(balance, daily_metrics)
        self._last_daily_metrics = daily_metrics
        self._last_guard_rails = guard_rails

        scan_pairs = self._get_scan_pairs()
        session_name = self._current_session_name()

        if self._is_hybrid_mode() and bool(self.cfg.get("mode", {}).get("session_pair_rules", False)):
            preferred = "EURUSD" if session_name == "LONDON" else "XAUUSD" if session_name == "NY" else None
            if preferred and preferred in scan_pairs:
                preferred_signal = False
                try:
                    preferred_signal = await self._scan_symbol(
                        preferred, positions, balance, guard_rails, daily_metrics, session_name
                    )
                except Exception as e:
                    logger.error(f"Error scanning {preferred}: {e}", exc_info=True)

                for symbol in scan_pairs:
                    if symbol == preferred:
                        continue
                    if preferred_signal:
                        logger.info(f"SKIP_ENTRY_PAIRGATE: symbol={symbol} session={session_name}")
                        continue
                    try:
                        await self._scan_symbol(symbol, positions, balance, guard_rails, daily_metrics, session_name)
                    except Exception as e:
                        logger.error(f"Error scanning {symbol}: {e}", exc_info=True)
                return

        for symbol in scan_pairs:
            try:
                await self._scan_symbol(symbol, positions, balance, guard_rails, daily_metrics, session_name)
            except Exception as e:
                logger.error(f"Error scanning {symbol}: {e}", exc_info=True)

    async def _scan_symbol(
        self,
        symbol: str,
        open_positions: list,
        balance: float,
        guard_rails: dict,
        daily_metrics: dict,
        session_name: str,
    ) -> bool:
        existing = [p for p in open_positions if p["symbol"] == symbol]
        if existing:
            return False
        paper_trade_only = False

        if not self._is_hybrid_mode():
            can_enter, cd_reason = self.cooldowns.can_enter(symbol)
            if not can_enter:
                logger.info(f"SKIP_ENTRY_COOLDOWN: symbol={symbol} reason={cd_reason} remaining=0")
                return False

        blocked, cd_reason, remaining = self.risk.should_cooldown(symbol)
        if blocked:
            logger.info(f"SKIP_ENTRY_COOLDOWN: symbol={symbol} reason={cd_reason} remaining={remaining}")
            return False

        max_open = int(self.cfg.get("risk", {}).get("max_open_trades", 0))
        if max_open > 0 and len(open_positions) >= max_open:
            logger.warning(f"SKIP_ENTRY_MAXOPEN: open={len(open_positions)} max={max_open}")
            return False

        if symbol == "XAUUSD":
            xcap = int(self.cfg.get("risk", {}).get("xauusd_max_trades_per_day", 0))
            if xcap > 0:
                start_utc, end_utc = self._daily_window_utc()
                x_count = self.memory.count_trades_for_symbol_between("XAUUSD", start_utc, end_utc)
                if x_count >= xcap:
                    logger.warning(f"SKIP_ENTRY_XAUUSD_CAP: {x_count}/{xcap} trades today")
                    return False

        if guard_rails.get("triggered"):
            logger.warning(f"SKIP_ENTRY_GUARDRAILS: reason={guard_rails.get('reason', 'UNKNOWN')}")
            return False

        last = self._last_signals.get(symbol)
        if last:
            elapsed = (datetime.utcnow() - last).total_seconds()
            if elapsed < self._signal_cooldown:
                return False

        blocked_news, reason = self.news.is_blocked(symbol)
        if blocked_news:
            logger.debug(f"{symbol}: {reason}")
            return False

        can_trade, reason = self.risk.can_trade(
            open_positions=open_positions,
            account_balance=balance,
            symbol=symbol,
            equity=self._latest_equity,
            current_daily_pnl=float(daily_metrics.get("daily_pnl", 0.0)),
        )
        if not can_trade:
            reason_text = str(reason)
            if reason_text.startswith("MAX_OPEN_TRADES"):
                logger.warning(f"SKIP_ENTRY_MAXOPEN: open={len(open_positions)} max={max_open}")
            elif reason_text.startswith("DRAWDOWN"):
                limit = float(self.cfg.get("risk", {}).get("max_cumulative_loss_pct", 0.0))
                logger.warning(f"SKIP_ENTRY_DRAWDOWN: dd={reason_text} limit={limit}")
            elif reason_text.startswith("DAILY_PROFIT_LOCK"):
                prop_cfg = self.cfg.get("execution", {}).get("prop", {})
                mode = str(prop_cfg.get("after_profit_lock_mode", "")).upper()
                logger.warning(f"SKIP_PROP_DAILY_PROFIT_LOCK: symbol={symbol} reason={reason_text}")
                if mode == "PAPER_TRADE_LOG_ONLY":
                    paper_trade_only = True
                else:
                    self._skip_reasons.append(
                        {"ts": datetime.utcnow().isoformat(), "symbol": symbol, "reason": "DAILY_PROFIT_LOCK"}
                    )
                    return False
            elif reason_text.startswith("LOSS_STREAK_PAUSE"):
                rem = self.risk.get_guardrail_status().get("loss_pause_remaining_seconds", 0)
                logger.warning(f"SKIP_PROP_LOSS_STREAK_PAUSE: symbol={symbol} remaining={rem}")
                self._skip_reasons.append(
                    {"ts": datetime.utcnow().isoformat(), "symbol": symbol, "reason": "LOSS_STREAK_PAUSE", "remaining": rem}
                )
                return False
            elif reason_text.startswith("LOSS_STREAK_STOP_DAY"):
                logger.warning(f"SKIP_PROP_LOSS_STREAK_STOP_DAY: symbol={symbol} reason={reason_text}")
                self._skip_reasons.append(
                    {"ts": datetime.utcnow().isoformat(), "symbol": symbol, "reason": "LOSS_STREAK_STOP_DAY"}
                )
                return False
            elif reason_text.startswith("MAX_TOTAL_OPEN_RISK"):
                logger.warning(f"SKIP_PROP_MAX_TOTAL_OPEN_RISK: {reason_text}")
                self._skip_reasons.append(
                    {"ts": datetime.utcnow().isoformat(), "symbol": symbol, "reason": "MAX_TOTAL_OPEN_RISK", "detail": reason_text}
                )
                return False
            elif reason_text.startswith("DAILY_LOSS_LIMIT") or reason_text.startswith("MAX_DAILY_LOSS"):
                logger.warning(f"SKIP_PROP_MAX_DAILY_LOSS: {reason_text}")
                self._skip_reasons.append(
                    {"ts": datetime.utcnow().isoformat(), "symbol": symbol, "reason": "MAX_DAILY_LOSS", "detail": reason_text}
                )
                return False
            elif "COOLDOWN" in reason_text.upper():
                logger.info(f"SKIP_ENTRY_COOLDOWN: symbol={symbol} reason={reason_text} remaining=0")
            else:
                logger.warning(f"Risk block: {reason_text}")
            if not paper_trade_only:
                return False

        candles_h4 = self.mt5.get_candles(symbol, self.tf["bias"], 300)
        candles_m15 = self.mt5.get_candles(symbol, self.tf["entry"], 200)
        candles_m5 = self.mt5.get_candles(symbol, self.tf["trigger"], 100)
        candles_m1 = self.mt5.get_candles(symbol, "M1", 100)
        tick = self.mt5.get_tick(symbol)
        spread = self.mt5.get_spread_pips(symbol)

        if not candles_h4 or not candles_m15 or not tick:
            logger.warning(f"Incomplete data for {symbol} - skipping")
            return False

        signal = self.strategy.analyze(symbol, candles_h4, candles_m15, candles_m5, candles_m1, spread)

        if signal and signal.valid:
            passed, reason, metrics = self._sniper_filter(signal, symbol, candles_m5, candles_m15, candles_h4)
            if not passed:
                setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
                log_msg = (
                    f"SKIP_SNIPER_{reason}: symbol={symbol} setup={setup_name} "
                    f"sl_pips={metrics.sl_pips:.2f} rr={metrics.rr:.2f} "
                    f"confidence={metrics.confidence:.2f} killzone={metrics.killzone}"
                )
                logger.info(log_msg)
                self._skip_reasons.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "symbol": symbol,
                        "setup_type": setup_name,
                        "reason": reason,
                        "sl_pips": round(float(metrics.sl_pips), 4),
                        "rr": round(float(metrics.rr), 4),
                        "confidence": round(float(metrics.confidence), 4),
                        "killzone": metrics.killzone,
                    }
                )
                return False

            if float(getattr(metrics, "risk_scale", 1.0)) < 1.0:
                setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
                logger.info(
                    f"SOFT_SL_CAP_SCALE: symbol={symbol} setup={setup_name} sl_pips={metrics.sl_pips:.2f} "
                    f"cap={float(getattr(metrics, 'sl_cap', 0.0)):.2f} scale={float(metrics.risk_scale):.3f}"
                )
            if getattr(metrics, "override", "") == "OB_PREMIUM_OVERRIDE":
                setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
                logger.info(
                    f"SNIPER_OVERRIDE_OB_PREMIUM: symbol={symbol} setup={setup_name} "
                    f"rr={metrics.rr:.2f} sl_pips={metrics.sl_pips:.2f} "
                    f"confidence={metrics.confidence:.2f} killzone={metrics.killzone}"
                )
                self._last_decisions.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "symbol": symbol,
                        "setup_type": setup_name,
                        "decision": "ALLOW",
                        "reason": "SNIPER_OVERRIDE_OB_PREMIUM",
                        "override": metrics.override,
                        "sl_pips": round(float(metrics.sl_pips), 4),
                        "rr": round(float(metrics.rr), 4),
                        "confidence": round(float(metrics.confidence), 4),
                        "killzone": metrics.killzone,
                    }
                )
            if bool(getattr(metrics, "sl_soft_cap", False)):
                setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
                risk_mult = float(self.risk.compute_risk_multiplier(symbol, float(metrics.sl_pips)))
                logger.info(
                    f"SNIPER_SOFT_SL_CAP_ALLOW: symbol={symbol} setup={setup_name} sl_pips={metrics.sl_pips:.2f} "
                    f"rr={metrics.rr:.2f} conf={metrics.confidence:.2f} risk_mult={risk_mult:.3f}"
                )
                self._last_decisions.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "symbol": symbol,
                        "setup_type": setup_name,
                        "decision": "ALLOW",
                        "reason": "SNIPER_SOFT_SL_CAP_ALLOW",
                        "sl_soft_cap": True,
                        "sl_cap": float(getattr(metrics, "sl_cap", 0.0)),
                        "sl_soft_buffer": float(getattr(metrics, "sl_soft_buffer", 0.0)),
                        "sl_pips": round(float(metrics.sl_pips), 4),
                        "rr": round(float(metrics.rr), 4),
                        "confidence": round(float(metrics.confidence), 4),
                        "risk_mult": round(risk_mult, 6),
                        "killzone": metrics.killzone,
                    }
                )

            can_trade_signal, reason_signal = self.risk.can_trade(
                open_positions=open_positions,
                account_balance=balance,
                symbol=symbol,
                equity=self._latest_equity,
                current_daily_pnl=float(daily_metrics.get("daily_pnl", 0.0)),
                confidence=float(getattr(signal, "confidence", 0.0)),
                rr=float(getattr(signal, "rr", 0.0)),
                risk_scale=float(getattr(metrics, "risk_scale", 1.0)),
            )
            if not can_trade_signal:
                reason_text = str(reason_signal)
                if reason_text.startswith("MAX_TOTAL_OPEN_RISK"):
                    logger.warning(f"SKIP_PROP_MAX_TOTAL_OPEN_RISK: {reason_text}")
                elif reason_text.startswith("DAILY_PROFIT_LOCK"):
                    logger.warning(f"SKIP_PROP_DAILY_PROFIT_LOCK: symbol={symbol} reason={reason_text}")
                    prop_cfg = self.cfg.get("execution", {}).get("prop", {})
                    if str(prop_cfg.get("after_profit_lock_mode", "")).upper() == "PAPER_TRADE_LOG_ONLY":
                        paper_trade_only = True
                    else:
                        return False
                elif reason_text.startswith("LOSS_STREAK_PAUSE"):
                    rem = self.risk.get_guardrail_status().get("loss_pause_remaining_seconds", 0)
                    logger.warning(f"SKIP_PROP_LOSS_STREAK_PAUSE: symbol={symbol} remaining={rem}")
                    return False
                elif reason_text.startswith("LOSS_STREAK_STOP_DAY"):
                    logger.warning(f"SKIP_PROP_LOSS_STREAK_STOP_DAY: symbol={symbol} reason={reason_text}")
                    return False
                elif reason_text.startswith("DAILY_LOSS_LIMIT") or reason_text.startswith("MAX_DAILY_LOSS"):
                    logger.warning(f"SKIP_PROP_MAX_DAILY_LOSS: {reason_text}")
                    return False
                else:
                    logger.warning(f"Risk block: {reason_text}")
                    return False

            if self._is_hybrid_mode():
                in_kill_zone, _ = self.strategy.in_kill_zone()
                if not in_kill_zone:
                    setup_name = str(getattr(signal.setup_type, "value", signal.setup_type)).upper()
                    if "SCALP" in setup_name:
                        logger.info(f"SKIP_ENTRY_PAIRGATE: symbol={symbol} session={session_name}")
                        return True
                    base_floor = float(self.cfg.get("hybrid", {}).get("min_confidence", 0.60))
                    boosted_floor = min(1.0, base_floor + 0.10)
                    if float(signal.confidence) < boosted_floor:
                        logger.info(f"SKIP_ENTRY_PAIRGATE: symbol={symbol} session={session_name}")
                        return True

            if paper_trade_only:
                setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
                logger.info(
                    f"PAPER_TRADE_LOCKED: symbol={symbol} setup={setup_name} conf={float(signal.confidence):.2f} "
                    f"rr={float(getattr(signal, 'rr', 0.0)):.2f} sl_pips={float(metrics.sl_pips):.2f}"
                )
                self._last_decisions.append(
                    {
                        "ts": datetime.utcnow().isoformat(),
                        "symbol": symbol,
                        "setup_type": setup_name,
                        "decision": "PAPER_ONLY",
                        "reason": "DAILY_PROFIT_LOCK",
                        "confidence": round(float(signal.confidence), 4),
                        "rr": round(float(getattr(signal, "rr", 0.0)), 4),
                        "sl_pips": round(float(metrics.sl_pips), 4),
                    }
                )
                return True

            if not self.brain.should_disable_setup(signal.setup_type.value):
                await self._execute_signal(signal, balance, spread, candles_h4, candles_m15, candles_m5, metrics)
            else:
                logger.warning(f"Setup '{signal.setup_type.value}' DISABLED due to low performance")
            return True

        return False

    async def _execute_signal(
        self,
        signal: Signal,
        balance: float,
        spread: float,
        candles_h4: list,
        candles_m15: list,
        candles_m5: list,
        sniper_metrics=None,
    ):
        adaptive_confidence = self.brain.get_adaptive_confidence(signal.setup_type.value)
        original_confidence = signal.confidence
        signal.confidence = adaptive_confidence / 100

        logger.info(
            f"Executing signal: {signal.symbol} {signal.direction.value} | {signal.setup_type.value} "
            f"| Conf: {original_confidence:.0%}->{signal.confidence:.0%} (learned)"
        )

        analysis = self.brain.analyze_entry_conditions(
            signal.symbol, signal.setup_type.value, candles_h4, candles_m15, candles_m5, signal
        )

        logger.info(f"Reasoning: {analysis['reasoning']}")
        for condition in analysis["conditions_met"][:3]:
            logger.info(f"  - {condition}")

        in_kill_zone, kz_name = self.strategy.in_kill_zone()
        kz_name = kz_name or "NONE"

        if not self._is_hybrid_mode():
            hcfg = self.cfg.get("hybrid", {})
            if hcfg.get("trade_only_in_kill_zones", False):
                if not in_kill_zone:
                    logger.info(f"HYBRID_SKIP: not in kill zone for {signal.symbol}")
                    return
                allowed_kz = [str(x) for x in hcfg.get("allowed_kill_zones", [])]
                if allowed_kz and kz_name not in allowed_kz:
                    logger.info(f"HYBRID_SKIP: kill zone not allowed for {signal.symbol} kz={kz_name}")
                    return

            decision = self.hybrid_gate.allow_entry(
                symbol=signal.symbol,
                kz_name=kz_name,
                direction=signal.direction.value,
                setup_type=signal.setup_type.value,
                rr=float(signal.rr),
                confidence=float(signal.confidence),
            )
            if not decision.allowed:
                logger.info(f"HYBRID_SKIP: {signal.symbol} {signal.setup_type.value} reason={decision.reason}")
                return

        lot = self.risk.calculate_lot_size(
            symbol=signal.symbol,
            entry=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            account_balance=balance,
            confidence=signal.confidence,
            rr=float(getattr(signal, "rr", 0.0)),
            risk_scale=float(getattr(sniper_metrics, "risk_scale", 1.0)),
            in_kill_zone=in_kill_zone,
            open_positions=self.mt5.get_open_positions(),
            daily_pnl=float(self.memory.get_daily_summary(*self._daily_window_utc()).get("daily_pnl", 0.0)),
        )

        if lot < 0.01:
            logger.warning(f"Lot size too small ({lot}) - skip")
            return

        result = self.mt5.place_market_order(
            symbol=signal.symbol,
            order_type=signal.direction.value,
            volume=lot,
            sl=signal.sl,
            tp=signal.tp,
            comment=f"ICT_{signal.setup_type.value}",
        )

        if result:
            in_kill_zone, kz_name = self.strategy.in_kill_zone()
            kz_name = kz_name or "NONE"
            self.sniper_filter.register_entry(
                signal.symbol,
                kz_name,
                str(getattr(signal.setup_type, "value", signal.setup_type)),
            )
            self.risk.record_open(result, signal.setup_type.value, signal.reason)
            self._last_signals[signal.symbol] = datetime.utcnow()

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
                reason=analysis["reasoning"],
                conditions_met=analysis["conditions_met"],
                expected_outcome=analysis["expected_outcome"],
                confidence_input=signal.confidence,
                entry_time=datetime.utcnow(),
            )

            self.memory.record_entry(trade_memory)
            logger.info(f"Recorded entry in AI memory: {signal.setup_type.value} {signal.symbol} #{result['ticket']}")

            await self.notifier.send(
                f"NEW TRADE - AI Confidence: {signal.confidence:.0%}\n"
                f"{'-'*30}\n"
                f"Pair:  {signal.symbol}\n"
                f"Type:  {signal.direction.value}\n"
                f"Setup: {signal.setup_type.value}\n"
                f"Entry: {result['price']}\n"
                f"SL:    {signal.sl}\n"
                f"TP:    {signal.tp}\n"
                f"Lot:   {lot}\n"
                f"RR:    1:{signal.rr}\n"
                f"Zone:  {kz_name}\n"
                f"\nREASONING:\n{analysis['reasoning']}\n"
                f"\nCONDITIONS:\n" + "\n".join(f"  - {c}" for c in analysis["conditions_met"][:3])
            )

    async def _manage_positions(self):
        positions = self.mt5.get_open_positions()
        if not positions:
            return

        if not hasattr(self, "_trailing_2022"):
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
                spread_pips=spread_pips,
                bid=bid,
                ask=ask,
            )

            if new_sl and new_sl != pos["sl"]:
                if pos["type"] == "BUY" and new_sl > pos["sl"]:
                    if self.mt5.modify_sl_tp(pos["ticket"], new_sl, pos["tp"]):
                        logger.info(
                            f"Trailing SL | {pos['symbol']} #{pos['ticket']} | {pos['sl']:.5f} -> {new_sl:.5f}"
                        )
                elif pos["type"] == "SELL" and new_sl < pos["sl"]:
                    if self.mt5.modify_sl_tp(pos["ticket"], new_sl, pos["tp"]):
                        logger.info(
                            f"Trailing SL | {pos['symbol']} #{pos['ticket']} | {pos['sl']:.5f} -> {new_sl:.5f}"
                        )

    async def _fallback_closed_trade_sync(self):
        if self.analyzer_running:
            return
        live_positions = self.mt5.get_open_positions()
        live_tickets = {int(p.get("ticket")) for p in live_positions if p.get("ticket") is not None}
        for rec in self.risk.journal:
            if rec.close_time is not None:
                continue
            try:
                tkt = int(rec.ticket)
            except Exception:
                continue
            if tkt not in live_tickets:
                rec.close_time = datetime.utcnow()
                self.risk.on_trade_closed(rec.symbol, "BREAKEVEN", 0.0, rec.close_time)

    def get_status(self) -> dict:
        account = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        stats = self.risk.get_stats()
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
        hybrid_status = self.risk.get_guardrail_status()
        exec_cfg = self.cfg.get("execution", {})

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
            "account": account,
            "positions": self._json_safe_positions(positions),
            "stats": merged_stats,
            "connected": self.mt5.connected,
            "pair_biases": pair_biases,
            "trade_log": trade_log,
            "setup_performance": self.memory.get_all_setup_performance(),
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
            "hybrid_mode": {
                "enabled": self._is_hybrid_mode(),
                "config": self.cfg.get("mode", {}),
                "cooldowns_per_symbol_seconds": hybrid_status.get("cooldowns_per_symbol_seconds", {}),
                "loss_streak": hybrid_status.get("consecutive_losses", 0),
                "global_throttle_remaining": hybrid_status.get("global_throttle_seconds_remaining", 0),
                "loss_streak_cooldown_remaining": hybrid_status.get("loss_streak_cooldown_seconds_remaining", 0),
            },
            "execution_profile": str(exec_cfg.get("profile", "normal")),
            "prop_mode_enabled": bool(hybrid_status.get("prop_mode_enabled", False)),
            "prop_guardrails": hybrid_status,
            "sniper_settings": {
                "min_rr": float(exec_cfg.get("min_rr", 0.0)),
                "min_confidence": float(exec_cfg.get("min_confidence", 0.0)),
                "max_sl_pips": exec_cfg.get("max_sl_pips", {}),
                "max_sl_usd": float(exec_cfg.get("max_sl_usd", 0.0)),
            },
            "last_skip_reasons": list(self._skip_reasons),
            "last_decisions": list(self._last_decisions),
            "daily_metrics_timezone": "Africa/Gaborone",
            "daily_window_utc": {
                "start": start_utc.isoformat(),
                "end": end_utc.isoformat(),
            },
            "upcoming_events": upcoming_events,
            "events_status": events_status,
            "upcoming_news": [
                {
                    "time": e.time.strftime("%H:%M"),
                    "time_iso": e.time.isoformat(),
                    "currency": e.currency,
                    "impact": e.impact,
                    "title": e.title,
                }
                for e in upcoming[:5]
            ],
            "analyzer": {
                "running": bool(self.analyzer_running),
                "last_tick": self.analyzer_last_tick.isoformat()
                if isinstance(self.analyzer_last_tick, datetime)
                else None,
            },
        }

    def _is_hybrid_mode(self) -> bool:
        return str(self.cfg.get("mode", {}).get("type", "normal")).lower() == "hybrid"

    def _get_scan_pairs(self) -> list[str]:
        if not self._is_hybrid_mode():
            return list(self.pairs)
        focus = self.cfg.get("mode", {}).get("pairs_focus", ["XAUUSD", "EURUSD"])
        out = []
        for pair in focus:
            p = str(pair).upper()
            if p and p not in out:
                out.append(p)
        return out or list(self.pairs)

    def _current_session_name(self) -> str:
        in_kz, kz_name = self.strategy.in_kill_zone()
        if not in_kz:
            return "OFF"
        name = str(kz_name or "").upper()
        if "LONDON" in name:
            return "LONDON"
        if "NY" in name:
            return "NY"
        return name or "OFF"

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

    def _sniper_filter(
        self,
        signal: Signal,
        symbol: str,
        candles_m5: list,
        candles_m15: list,
        candles_h4: list,
    ):
        in_kill_zone, kz_name = self.strategy.in_kill_zone()
        kz_name = kz_name or "NONE"
        return self.sniper_filter.evaluate(
            signal=signal,
            symbol=symbol,
            candles_m5=candles_m5,
            candles_m15=candles_m15,
            candles_h4=candles_h4,
            killzone=kz_name,
            in_killzone=in_kill_zone,
        )

    def _json_safe_positions(self, positions: list) -> list:
        out = []
        for p in positions:
            item = dict(p)
            ot = item.get("open_time")
            if isinstance(ot, datetime):
                item["open_time"] = ot.isoformat()
            out.append(item)
        return out
