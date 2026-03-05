"""
Trading Engine - WITH AI INTEGRATION
Enhanced version with Memory & Brain system added to your existing code.
"""

import asyncio
import logging
import time
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
from trailing_manager import StructureTrailingManager
from loss_analyzer import LossAnalyzer

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

        login_raw = str(config.get("mt5", {}).get("login", "")).strip()
        login_safe = "".join(ch for ch in login_raw if ch.isdigit()) or "default"
        db_path = Path(__file__).parent.parent / "memory" / f"trading_memory_{login_safe}.db"
        db_path.parent.mkdir(exist_ok=True)
        self.memory = TradingMemoryDB(db_path)
        self.brain = TradingBrain(self.memory, self.cfg)
        self.hybrid_gate = HybridGate(self.cfg, self.memory)
        self.sniper_filter = SniperFilter(self.cfg)
        self.trailing = StructureTrailingManager(self.cfg)
        self.loss_analyzer = LossAnalyzer(self.mt5, self.strategy, self.memory, self.cfg)
        self._sl_update_attempts: dict[int, int] = {}
        self._tm_partial_retry_after: dict[str, float] = {}
        self._tm_invalid_risk_logged: set[str] = set()

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
        self._pending_cancel_reasons: deque[dict] = deque(maxlen=20)
        self._daily_loss_flatten_last_at: Optional[datetime] = None
        self._adaptive_last_validate_at: Optional[datetime] = None

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
                await self.manage_open_positions()
                await self._fallback_closed_trade_sync()
                self._maybe_validate_adaptive_rules()
            except Exception as e:
                logger.error(f"Manage error: {e}", exc_info=True)
            await asyncio.sleep(self._manage_interval)

    async def _news_loop(self):
        while not self.shutdown.is_set():
            await asyncio.sleep(self._news_interval)
            await self.news.update()

    def _maybe_validate_adaptive_rules(self):
        al_cfg = self.cfg.get("adaptive_learning", {})
        if not isinstance(al_cfg, dict) or not bool(al_cfg.get("enabled", False)):
            return

        now = datetime.utcnow()
        interval = int(al_cfg.get("validation_interval_seconds", 3600) or 3600)
        if self._adaptive_last_validate_at is not None:
            elapsed = (now - self._adaptive_last_validate_at).total_seconds()
            if elapsed < max(60, interval):
                return

        try:
            self.loss_analyzer.validate_rules_job()
            self._adaptive_last_validate_at = now
        except Exception as e:
            logger.error("ADAPTIVE_LEARNING_ERROR action=validate_rules err=%s", e, exc_info=True)

    async def _scan_all_pairs(self):
        account = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        self._sync_live_positions_to_memory(positions)
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
        pending_orders = []
        if hasattr(self.mt5, "get_pending_orders"):
            try:
                pending_orders = [o for o in self.mt5.get_pending_orders(symbol) if str(o.get("symbol")) == symbol]
            except Exception:
                pending_orders = []
        if pending_orders:
            self._manage_pending_orders(symbol, pending_orders)
            pending_orders = [o for o in pending_orders if self._is_order_still_pending(o.get("ticket"))]
            if pending_orders:
                logger.info(f"SKIP_ENTRY_PENDING_EXISTS: symbol={symbol} pending={len(pending_orders)}")
                return False

        existing = [p for p in open_positions if p["symbol"] == symbol]
        if existing:
            logger.info(f"SKIP_ENTRY_OPEN_POSITION: symbol={symbol} open={len(existing)}")
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
                remaining = max(0, int(self._signal_cooldown - elapsed))
                logger.info(f"SKIP_ENTRY_SIGNAL_COOLDOWN: symbol={symbol} remaining={remaining}")
                return False

        blocked_news, reason = self.news.is_blocked(symbol)
        if blocked_news:
            logger.info(f"SKIP_ENTRY_NEWS: symbol={symbol} reason={reason}")
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
                if reason_text.startswith("MAX_DAILY_LOSS_EQUITY"):
                    await self._handle_prop_daily_loss_breach(reason_text, open_positions=open_positions)
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
        candles_h1 = self.mt5.get_candles(symbol, "H1", 300)
        candles_m15 = self.mt5.get_candles(symbol, self.tf["entry"], 200)
        candles_m5 = self.mt5.get_candles(symbol, self.tf["trigger"], 100)
        candles_m1 = self.mt5.get_candles(symbol, "M1", 100)
        tick = self.mt5.get_tick(symbol)
        spread = self.mt5.get_spread_pips(symbol)

        if not candles_h4 or not candles_h1 or not candles_m15 or not tick:
            logger.warning(f"Incomplete data for {symbol} - skipping")
            return False

        signal = self.strategy.analyze(symbol, candles_h4, candles_m15, candles_m5, candles_m1, spread)

        if signal and signal.valid:
            setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
            passed, reason, metrics = self._sniper_filter(signal, symbol, candles_m5, candles_m15, candles_h4, candles_h1)
            if not passed:
                log_msg = (
                    f"SKIP_SNIPER_{reason}: symbol={symbol} setup={setup_name} "
                    f"sl_pips={metrics.sl_pips:.2f} rr={metrics.rr:.2f} "
                    f"confidence={metrics.confidence:.2f} killzone={metrics.killzone} "
                    f"htf_bias={metrics.htf_bias} sweep={metrics.sweep_detected} "
                    f"disp={metrics.displacement_strength:.2f} mss={metrics.structure_shift_detected} "
                    f"entry_dist={metrics.entry_distance_pips:.2f} market={metrics.market_state}"
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
                        "htf_bias": metrics.htf_bias,
                        "sweep": bool(metrics.sweep_detected),
                        "displacement_strength": round(float(metrics.displacement_strength), 4),
                        "entry_distance_pips": round(float(metrics.entry_distance_pips), 4),
                        "mss": bool(metrics.structure_shift_detected),
                        "market_state": metrics.market_state,
                        "risk_scale": round(float(getattr(metrics, "risk_scale", 1.0)), 4),
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

            corr_scale, corr_detail = self.risk.correlation_risk_scale(
                symbol=symbol,
                direction=str(getattr(signal.direction, "value", "")),
                open_positions=open_positions,
            )
            effective_risk_scale = float(getattr(metrics, "risk_scale", 1.0)) * float(corr_scale)
            risk_pct_preview = self.risk.estimate_used_risk_pct(
                confidence=float(getattr(signal, "confidence", 0.0)),
                rr=float(getattr(signal, "rr", 0.0)),
                risk_scale=float(effective_risk_scale),
            )
            logger.info(
                "CORR_STATUS symbol=%s setup=%s direction=%s %s risk_scale=%.2f final_risk_pct=%.3f",
                symbol,
                setup_name,
                str(getattr(signal.direction, "value", "")),
                corr_detail,
                float(effective_risk_scale),
                float(risk_pct_preview),
            )

            setup_id = self._build_setup_id(signal)
            can_trade_signal, reason_signal = self.risk.can_trade(
                open_positions=open_positions,
                account_balance=balance,
                setup_id=setup_id,
                symbol=symbol,
                direction=str(getattr(signal.direction, "value", "")),
                equity=self._latest_equity,
                current_daily_pnl=float(daily_metrics.get("daily_pnl", 0.0)),
                confidence=float(getattr(signal, "confidence", 0.0)),
                rr=float(getattr(signal, "rr", 0.0)),
                risk_scale=float(effective_risk_scale),
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
                    if reason_text.startswith("MAX_DAILY_LOSS_EQUITY"):
                        await self._handle_prop_daily_loss_breach(reason_text, open_positions=open_positions)
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
                logger.info(
                    f"ICT_ENTRY_CONTEXT: symbol={symbol} setup={setup_name} htf_bias={metrics.htf_bias} "
                    f"sweep={metrics.sweep_detected} disp_strength={metrics.displacement_strength:.2f} "
                    f"entry_dist={metrics.entry_distance_pips:.2f} mss={metrics.structure_shift_detected} "
                    f"killzone={metrics.killzone} market_state={metrics.market_state} "
                    f"risk_scale={float(effective_risk_scale):.2f} mode={metrics.entry_mode}"
                )
                await self._execute_signal(
                    signal,
                    balance,
                    spread,
                    candles_h4,
                    candles_m15,
                    candles_m5,
                    metrics,
                    setup_id=setup_id,
                    risk_scale_override=float(effective_risk_scale),
                )
            else:
                logger.warning(f"Setup '{signal.setup_type.value}' DISABLED due to low performance")
            return True

        logger.info(f"NO_SIGNAL: symbol={symbol}")
        return False

    def _is_order_still_pending(self, ticket) -> bool:
        if not hasattr(self.mt5, "get_pending_orders"):
            return False
        try:
            orders = self.mt5.get_pending_orders()
            t = int(ticket)
            return any(int(o.get("ticket", -1)) == t for o in orders)
        except Exception:
            return False

    def _manage_pending_orders(self, symbol: str, orders: list[dict]):
        tick = self.mt5.get_tick(symbol)
        if not tick:
            return
        now = datetime.utcnow()
        max_age_min = int(self.cfg.get("execution", {}).get("pending_max_age_minutes", 90))
        dist_limit = float(self.cfg.get("entry_distance_limit", {}).get(symbol, 0.0) or 0.0)
        pip = self.strategy.get_pip_size(symbol)
        for o in orders:
            ticket = int(o.get("ticket", 0) or 0)
            if ticket <= 0:
                continue
            entry = float(o.get("price_open", 0.0) or 0.0)
            if entry <= 0:
                continue
            setup_time = o.get("time_setup")
            age_min = ((now - setup_time).total_seconds() / 60.0) if isinstance(setup_time, datetime) else 0.0
            market_mid = (float(tick["bid"]) + float(tick["ask"])) / 2.0
            dist_pips = abs(market_mid - entry) / pip if pip > 0 else 0.0
            stale_age = max_age_min > 0 and age_min > max_age_min
            stale_dist = dist_limit > 0 and dist_pips > (dist_limit * 2.0)
            if stale_age or stale_dist:
                reason = "AGE" if stale_age else "DISTANCE"
                if hasattr(self.mt5, "cancel_order") and self.mt5.cancel_order(ticket):
                    logger.info(
                        f"CANCEL_PENDING_{reason}: symbol={symbol} ticket={ticket} "
                        f"age_min={age_min:.1f} dist_pips={dist_pips:.2f}"
                    )
                    self._pending_cancel_reasons.append(
                        {
                            "ts": datetime.utcnow().isoformat(),
                            "symbol": symbol,
                            "ticket": ticket,
                            "reason": f"PENDING_{reason}",
                            "age_min": round(age_min, 2),
                            "dist_pips": round(dist_pips, 4),
                        }
                    )

    async def _execute_signal(
        self,
        signal: Signal,
        balance: float,
        spread: float,
        candles_h4: list,
        candles_m15: list,
        candles_m5: list,
        sniper_metrics=None,
        setup_id: str = "",
        risk_scale_override: Optional[float] = None,
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

        planned_mode = str(getattr(sniper_metrics, "entry_mode", "CONFIRMATION")).upper()
        planned_entry = float(getattr(signal, "entry", 0.0) or 0.0)
        disp_ok = bool(getattr(sniper_metrics, "displacement_confirmed", True))
        entry_dist = float(getattr(sniper_metrics, "entry_distance_pips", 0.0) or 0.0)
        risk_scale_used = float(
            risk_scale_override
            if risk_scale_override is not None
            else float(getattr(sniper_metrics, "risk_scale", 1.0))
        )
        final_risk_pct = self.risk.estimate_used_risk_pct(
            confidence=float(signal.confidence),
            rr=float(getattr(signal, "rr", 0.0)),
            risk_scale=risk_scale_used,
        )
        thesis = self.risk.get_trade_thesis(signal.symbol, signal.direction.value)
        corr_decision = "SCALE" if risk_scale_used < 1.0 else "OK"
        logger.info(
            "THESIS=%s CORR_DECISION=%s PAIR_TRIGGER=EXECUTION risk_scale=%.2f final_risk_pct=%.3f",
            thesis,
            corr_decision,
            risk_scale_used,
            final_risk_pct,
        )

        if self._is_hybrid_mode():
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
            entry=planned_entry,
            sl=signal.sl,
            tp=signal.tp,
            account_balance=balance,
            confidence=signal.confidence,
            rr=float(getattr(signal, "rr", 0.0)),
            risk_scale=risk_scale_used,
            in_kill_zone=in_kill_zone,
            open_positions=self.mt5.get_open_positions(),
            daily_pnl=float(self.memory.get_daily_summary(*self._daily_window_utc()).get("daily_pnl", 0.0)),
        )

        if lot < 0.01:
            logger.warning(f"Lot size too small ({lot}) - skip")
            return

        adaptive_block, adaptive_reason = self.loss_analyzer.should_block_entry(
            symbol=signal.symbol,
            setup_type=signal.setup_type.value,
            direction=signal.direction.value,
            candles_h4=candles_h4,
            candles_m15=candles_m15,
            candles_m5=candles_m5,
            setup_id=setup_id,
        )
        if adaptive_block:
            logger.warning(
                "SKIP_ENTRY_ADAPTIVE: symbol=%s setup=%s direction=%s reason=%s",
                signal.symbol,
                signal.setup_type.value,
                signal.direction.value,
                adaptive_reason,
            )
            self._skip_reasons.append(
                {
                    "ts": datetime.utcnow().isoformat(),
                    "symbol": signal.symbol,
                    "setup_type": signal.setup_type.value,
                    "reason": "ADAPTIVE_BLOCK",
                    "detail": adaptive_reason,
                }
            )
            return
        if str(adaptive_reason).startswith("WOULD_BLOCK"):
            logger.info(
                "ADAPTIVE_SHADOW: symbol=%s setup=%s detail=%s",
                signal.symbol,
                signal.setup_type.value,
                adaptive_reason,
            )

        result = None
        if planned_mode == "LIMIT" and hasattr(self.mt5, "place_limit_order"):
            result = self.mt5.place_limit_order(
                symbol=signal.symbol,
                order_type=signal.direction.value,
                volume=lot,
                entry=planned_entry,
                sl=signal.sl,
                tp=signal.tp,
                comment=f"ICT_{signal.setup_type.value}_LIMIT",
            )
        elif planned_mode == "LIMIT":
            logger.info(
                f"ENTRY_MODE_FALLBACK: symbol={signal.symbol} setup={signal.setup_type.value} "
                f"mode=CONFIRMATION reason=LIMIT_API_UNAVAILABLE entry_dist={entry_dist:.2f}"
            )
            if not disp_ok:
                logger.info(
                    f"INFO: NOT_ENFORCED_DISPLACEMENT: symbol={signal.symbol} setup={signal.setup_type.value}"
                )
                logger.info(
                    f"RELAXED_GATE: displacement_not_required symbol={signal.symbol} setup={signal.setup_type.value}"
                )

        if result is None:
            result = self.mt5.place_market_order(
                symbol=signal.symbol,
                order_type=signal.direction.value,
                volume=lot,
                sl=signal.sl,
                tp=signal.tp,
                comment=f"ICT_{signal.setup_type.value}",
            )

        if result:
            is_pending = bool(result.get("is_pending", False))
            if is_pending:
                logger.info(
                    "ENTRY_PENDING_ONLY: symbol=%s setup=%s direction=%s order=%s entry=%.5f sl=%.5f tp=%.5f",
                    signal.symbol,
                    signal.setup_type.value,
                    signal.direction.value,
                    int(result.get("order_ticket") or result.get("ticket") or 0),
                    float(result.get("price") or planned_entry or 0.0),
                    float(signal.sl),
                    float(signal.tp),
                )
                return

            in_kill_zone, kz_name = self.strategy.in_kill_zone()
            kz_name = kz_name or "NONE"
            self.sniper_filter.register_entry(
                signal.symbol,
                kz_name,
                str(getattr(signal.setup_type, "value", signal.setup_type)),
            )
            self.risk.record_open(
                result,
                setup_type=signal.setup_type.value,
                setup_id=setup_id,
                reason=signal.reason,
            )
            self._last_signals[signal.symbol] = datetime.utcnow()

            htf_bias = self.strategy.get_htf_bias(candles_h4).value
            setup_class = "REVERSAL" if bool(getattr(sniper_metrics, "is_reversal", False)) else "CONTINUATION"
            validity_tags = [
                f"SETUP_CLASS_{setup_class}",
                f"SWEEP_{'YES' if bool(getattr(sniper_metrics, 'sweep_detected', False)) else 'NO'}",
                f"DISPLACEMENT_{'YES' if bool(getattr(sniper_metrics, 'displacement_confirmed', False)) else 'NO'}",
                f"MSS_{'YES' if bool(getattr(sniper_metrics, 'structure_shift_detected', False)) else 'NO'}",
                f"ENTRY_MODE_{str(getattr(sniper_metrics, 'entry_mode', 'CONFIRMATION')).upper()}",
                f"MARKET_{str(getattr(sniper_metrics, 'market_state', 'TREND')).upper()}",
            ]
            analysis_conditions = list(analysis["conditions_met"][:])
            analysis_conditions.extend(validity_tags)

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
                conditions_met=analysis_conditions,
                expected_outcome=analysis["expected_outcome"],
                confidence_input=signal.confidence,
                setup_class=setup_class,
                validity_tags=validity_tags,
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
        await self.manage_open_positions()

    async def manage_open_positions(self):
        positions = self.mt5.get_open_positions()
        self._sync_live_positions_to_memory(positions)
        if not positions:
            return

        account = self.mt5.get_account_info()
        balance = float(account.get("balance", 0.0) or 0.0)
        equity = float(account.get("equity", balance) or balance)
        self._latest_equity = equity
        daily_metrics = self.memory.get_daily_summary(*self._daily_window_utc())
        ok_global, reason_global = self.risk.can_trade(
            open_positions=positions,
            account_balance=balance,
            equity=equity,
            current_daily_pnl=float(daily_metrics.get("daily_pnl", 0.0)),
        )
        if not ok_global and str(reason_global).startswith("MAX_DAILY_LOSS_EQUITY"):
            await self._handle_prop_daily_loss_breach(str(reason_global), open_positions=positions)
            return

        tick_cache: dict[str, dict] = {}
        info_cache: dict[str, dict] = {}
        candles_m5_cache: dict[str, list] = {}
        candles_m1_cache: dict[str, list] = {}
        tm_cfg = self._trade_management_config()
        now_mono = time.monotonic()

        for pos in positions:
            symbol = str(pos["symbol"])
            side = str(pos.get("type", "")).upper()
            current_sl = float(pos.get("sl", 0.0) or 0.0)
            ticket = int(pos.get("ticket", 0) or 0)
            if ticket <= 0 or side not in ("BUY", "SELL"):
                continue

            tick = tick_cache.get(symbol)
            if tick is None:
                tick = self.mt5.get_tick(symbol)
                if tick:
                    tick_cache[symbol] = tick
            if not tick:
                continue

            bid = float(tick["bid"])
            ask = float(tick["ask"])

            symbol_info = info_cache.get(symbol)
            if symbol_info is None and hasattr(self.mt5, "get_symbol_info"):
                symbol_info = self.mt5.get_symbol_info(symbol) or {}
                info_cache[symbol] = symbol_info

            open_trade = None
            if hasattr(self.memory, "find_open_trade_for_exit"):
                try:
                    open_trade = self.memory.find_open_trade_for_exit(position_id=ticket, ticket=ticket)
                except Exception:
                    open_trade = None

            tm_res = self._apply_trade_management_rules(
                position=pos,
                bid=bid,
                ask=ask,
                symbol_info=symbol_info or {},
                open_trade=open_trade,
                tm_cfg=tm_cfg,
                now_mono=now_mono,
            )
            if bool(tm_res.get("closed", False)):
                continue
            if isinstance(tm_res.get("sl_updated"), (float, int)):
                pos["sl"] = float(tm_res["sl_updated"])
                current_sl = float(pos.get("sl", 0.0) or 0.0)
            if bool(tm_res.get("skip_trailing", False)):
                continue

            candles_m5 = candles_m5_cache.get(symbol)
            if candles_m5 is None:
                candles_m5 = self.mt5.get_candles(symbol, "M5", 150)
                candles_m5_cache[symbol] = candles_m5 or []
            candles_m1 = candles_m1_cache.get(symbol)
            if candles_m1 is None:
                candles_m1 = self.mt5.get_candles(symbol, "M1", 300)
                candles_m1_cache[symbol] = candles_m1 or []
            if not candles_m5 and not candles_m1:
                continue

            eval_res = self.trailing.evaluate_position(
                position=pos,
                candles_m5=list(candles_m5 or []),
                candles_m1=list(candles_m1 or candles_m5 or []),
                bid=bid,
                ask=ask,
                symbol_info=symbol_info,
            )
            new_sl = eval_res.get("new_sl")
            reason = str(eval_res.get("reason") or "")
            if not isinstance(new_sl, (float, int)):
                continue
            new_sl = float(new_sl)

            if side == "BUY" and new_sl <= current_sl:
                continue
            if side == "SELL" and new_sl >= current_sl:
                continue

            attempt = int(self._sl_update_attempts.get(ticket, 0)) + 1
            if hasattr(self.mt5, "modify_sl_tp_detailed"):
                mod = self.mt5.modify_sl_tp_detailed(ticket, new_sl, pos["tp"])
                ok = bool(mod.get("ok", False))
                retcode = mod.get("retcode")
                comment = str(mod.get("comment", ""))
                last_error = str(mod.get("last_error", ""))
            else:
                ok = bool(self.mt5.modify_sl_tp(ticket, new_sl, pos["tp"]))
                retcode = None
                comment = ""
                last_error = ""

            if ok:
                self._sl_update_attempts[ticket] = 0
                logger.info(
                    f"SL_UPDATE: symbol={symbol} ticket={ticket} side={side} old_sl={current_sl:.5f} "
                    f"new_sl={new_sl:.5f} reason={reason} attempt={attempt}"
                )
            else:
                self._sl_update_attempts[ticket] = attempt
                point = float((symbol_info or {}).get("point", 0.0) or 0.0)
                stops_level = int((symbol_info or {}).get("stops_level", 0) or 0)
                freeze_level = int((symbol_info or {}).get("freeze_level", 0) or 0)
                stop_dist = point * max(stops_level, 0)
                freeze_dist = point * max(freeze_level, 0)
                logger.warning(
                    f"SL_UPDATE_REJECTED: symbol={symbol} ticket={ticket} side={side} old_sl={current_sl:.5f} "
                    f"new_sl={new_sl:.5f} reason={reason} attempt={attempt} retcode={retcode} "
                    f"broker_error={comment or last_error} stop_level={stops_level} stop_dist={stop_dist:.5f} "
                    f"freeze_level={freeze_level} freeze_dist={freeze_dist:.5f}"
                )

        self._cleanup_partial_retry_cache(positions, now_mono)

    def _trade_management_config(self) -> dict:
        root = self.cfg.get("trade_management", {}) if isinstance(self.cfg.get("trade_management", {}), dict) else {}
        partials_raw = root.get("partials", {}) if isinstance(root.get("partials", {}), dict) else {}
        giveback_raw = root.get("giveback_guard", {}) if isinstance(root.get("giveback_guard", {}), dict) else {}
        time_exit_raw = root.get("time_exit", {}) if isinstance(root.get("time_exit", {}), dict) else {}

        tp1_sl_mode = str(partials_raw.get("tp1_sl_mode", "BE_PLUS")).upper().strip()
        if tp1_sl_mode not in ("BE", "BE_PLUS"):
            tp1_sl_mode = "BE_PLUS"

        return {
            "partials": {
                "enabled": bool(partials_raw.get("enabled", True)),
                "tp1_r": float(partials_raw.get("tp1_r", 1.0) or 1.0),
                "tp1_close_pct": float(partials_raw.get("tp1_close_pct", 0.60) or 0.60),
                "tp1_sl_mode": tp1_sl_mode,
                "tp1_be_plus_r": float(partials_raw.get("tp1_be_plus_r", 0.05) or 0.05),
                "tp2_enabled": bool(partials_raw.get("tp2_enabled", True)),
                "tp2_r": float(partials_raw.get("tp2_r", 2.0) or 2.0),
                "tp2_close_pct": float(partials_raw.get("tp2_close_pct", 0.25) or 0.25),
                "tp2_sl_lock_r": float(partials_raw.get("tp2_sl_lock_r", 1.0) or 1.0),
                "trail_only_after_tp1": bool(partials_raw.get("trail_only_after_tp1", True)),
            },
            "giveback_guard": {
                "enabled": bool(giveback_raw.get("enabled", True)),
                "activate_at_r": float(giveback_raw.get("activate_at_r", 1.2) or 1.2),
                "max_giveback_pct": float(giveback_raw.get("max_giveback_pct", 0.60) or 0.60),
            },
            "time_exit": {
                "enabled": bool(time_exit_raw.get("enabled", False)),
                "max_minutes_open": int(time_exit_raw.get("max_minutes_open", 90) or 90),
            },
            "partial_retry_seconds": max(10, int(root.get("partial_retry_seconds", 60) or 60)),
        }

    def _trade_id_from_position(self, position: dict) -> str:
        try:
            ticket = int(position.get("ticket", 0) or 0)
        except Exception:
            ticket = 0
        return str(ticket) if ticket > 0 else ""

    def _sl_tightens(self, side: str, current_sl: float, candidate_sl: float, point: float = 0.0) -> bool:
        eps = max(float(point or 0.0) * 0.2, 1e-9)
        if candidate_sl <= 0:
            return False
        if side == "BUY":
            return current_sl <= 0 or candidate_sl > (current_sl + eps)
        if side == "SELL":
            return current_sl <= 0 or candidate_sl < (current_sl - eps)
        return False

    def _apply_position_sl(self, ticket: int, side: str, current_sl: float, candidate_sl: float, point: float = 0.0) -> dict:
        if not self._sl_tightens(side, current_sl, candidate_sl, point=point):
            return {"ok": False, "reason": "not_tightening", "new_sl": current_sl}

        if hasattr(self.mt5, "modify_position_sl"):
            mod = self.mt5.modify_position_sl(ticket, candidate_sl)
            ok = bool(mod.get("ok", False))
            retcode = mod.get("retcode")
            comment = str(mod.get("comment", ""))
            last_error = str(mod.get("last_error", ""))
        elif hasattr(self.mt5, "modify_sl_tp_detailed"):
            live_pos = next((p for p in self.mt5.get_open_positions() if int(p.get("ticket", 0) or 0) == int(ticket)), None)
            live_tp = float((live_pos or {}).get("tp", 0.0) or 0.0)
            mod = self.mt5.modify_sl_tp_detailed(ticket, candidate_sl, live_tp)
            ok = bool(mod.get("ok", False))
            retcode = mod.get("retcode")
            comment = str(mod.get("comment", ""))
            last_error = str(mod.get("last_error", ""))
        else:
            ok = bool(self.mt5.modify_sl_tp(ticket, candidate_sl, 0.0))
            retcode = None
            comment = ""
            last_error = ""

        if ok:
            return {"ok": True, "reason": "updated", "new_sl": float(candidate_sl)}
        return {
            "ok": False,
            "reason": "broker_reject",
            "new_sl": current_sl,
            "retcode": retcode,
            "comment": comment,
            "last_error": last_error,
        }

    def _persist_trade_mgmt_state(
        self,
        trade_id: str,
        tp1_done: bool,
        tp2_done: bool,
        initial_risk: float,
        original_volume: float,
        peak_r: float,
        activated_giveback: bool,
        opened_ts: str,
    ):
        upsert_fn = getattr(self.memory, "upsert_trade_mgmt_state", None)
        if not callable(upsert_fn):
            return
        try:
            upsert_fn(
                trade_id=trade_id,
                tp1_done=bool(tp1_done),
                tp2_done=bool(tp2_done),
                initial_risk=float(initial_risk),
                original_volume=float(original_volume),
                peak_r=float(peak_r),
                activated_giveback=bool(activated_giveback),
                opened_ts=str(opened_ts or ""),
            )
        except Exception as e:
            logger.error("TRADE_MGMT_STATE_UPSERT_FAILED trade_id=%s err=%s", trade_id, e, exc_info=True)

    def _is_permanent_partial_reject(self, reason: str) -> bool:
        code = str(reason or "").upper().strip()
        permanent = {
            "REQUESTED_VOLUME_NON_POSITIVE",
            "POSITION_VOLUME_NON_POSITIVE",
            "VOLUME_BELOW_MIN_LOT",
            "REMAINDER_BELOW_MIN_LOT",
            "PARTIAL_EQUALS_FULL_POSITION",
            "INVALID_CLOSE_VOLUME",
        }
        return code in permanent

    def _apply_trade_management_rules(
        self,
        position: dict,
        bid: float,
        ask: float,
        symbol_info: dict,
        open_trade: Optional[dict],
        tm_cfg: dict,
        now_mono: float,
    ) -> dict:
        result = {"closed": False, "skip_trailing": False, "sl_updated": None}

        trade_id = self._trade_id_from_position(position)
        if not trade_id:
            return result

        ticket = int(position.get("ticket", 0) or 0)
        symbol = str(position.get("symbol", "")).upper()
        side = str(position.get("type", "")).upper()
        if ticket <= 0 or side not in ("BUY", "SELL"):
            return result

        entry_price = float(position.get("open_price", 0.0) or 0.0)
        if entry_price <= 0.0:
            return result

        partials_cfg = tm_cfg.get("partials", {})
        giveback_cfg = tm_cfg.get("giveback_guard", {})
        time_exit_cfg = tm_cfg.get("time_exit", {})
        partials_enabled = bool(partials_cfg.get("enabled", False))
        giveback_enabled = bool(giveback_cfg.get("enabled", False))
        time_exit_enabled = bool(time_exit_cfg.get("enabled", False))
        trail_after_tp1 = bool(partials_cfg.get("trail_only_after_tp1", True))

        state_fn = getattr(self.memory, "get_trade_mgmt_state", None)
        state = state_fn(trade_id) if callable(state_fn) else None
        state = dict(state or {})
        tp1_done = bool(state.get("tp1_done", False))
        tp2_done = bool(state.get("tp2_done", False))
        initial_risk = float(state.get("initial_risk", 0.0) or 0.0)
        original_volume = float(state.get("original_volume", 0.0) or 0.0)
        peak_r = float(state.get("peak_r", 0.0) or 0.0)
        activated_giveback = bool(state.get("activated_giveback", False))

        opened_ts = str(state.get("opened_ts", "") or "")
        if not opened_ts:
            open_time = position.get("open_time")
            if isinstance(open_time, datetime):
                opened_ts = open_time.isoformat()
            else:
                opened_ts = datetime.utcnow().isoformat()

        # Optional time-based full close independent of R logic.
        if time_exit_enabled:
            max_minutes_open = max(1, int(time_exit_cfg.get("max_minutes_open", 90) or 90))
            open_time = position.get("open_time")
            if isinstance(open_time, datetime):
                open_minutes = (datetime.utcnow() - open_time).total_seconds() / 60.0
                if open_minutes >= float(max_minutes_open):
                    if self.mt5.close_position(ticket):
                        logger.warning(
                            "TIME_EXIT_CLOSE: ticket=%s symbol=%s side=%s open_minutes=%.1f max=%s",
                            ticket,
                            symbol,
                            side,
                            open_minutes,
                            max_minutes_open,
                        )
                        if hasattr(self.memory, "update_exit_analysis"):
                            try:
                                self.memory.update_exit_analysis(
                                    ticket=ticket,
                                    tp_hit_reason="TIME_EXIT",
                                    lessons="Position closed by configured time_exit rule.",
                                )
                            except Exception:
                                pass
                        result["closed"] = True
                        return result
                    logger.warning(
                        "TIME_EXIT_CLOSE_REJECTED: ticket=%s symbol=%s side=%s open_minutes=%.1f max=%s",
                        ticket,
                        symbol,
                        side,
                        open_minutes,
                        max_minutes_open,
                    )

        if not partials_enabled and not giveback_enabled:
            return result

        state_dirty = False
        if initial_risk <= 0.0:
            initial_sl = 0.0
            if isinstance(open_trade, dict):
                initial_sl = float(open_trade.get("sl_price", 0.0) or 0.0)
            if initial_sl <= 0.0:
                initial_sl = float(position.get("sl", 0.0) or 0.0)
            initial_risk = abs(entry_price - initial_sl)
            state_dirty = True

        if original_volume <= 0.0:
            if isinstance(open_trade, dict):
                original_volume = float(open_trade.get("lot_size", 0.0) or 0.0)
            if original_volume <= 0.0:
                original_volume = float(position.get("volume", 0.0) or 0.0)
            state_dirty = True

        if initial_risk <= 0.0:
            if trade_id not in self._tm_invalid_risk_logged:
                self._tm_invalid_risk_logged.add(trade_id)
                logger.warning(
                    "TRADE_MGMT_DISABLED_INVALID_RISK: ticket=%s symbol=%s side=%s entry=%.5f sl=%.5f",
                    ticket,
                    symbol,
                    side,
                    entry_price,
                    float(position.get("sl", 0.0) or 0.0),
                )
            if state_dirty:
                self._persist_trade_mgmt_state(
                    trade_id=trade_id,
                    tp1_done=tp1_done,
                    tp2_done=tp2_done,
                    initial_risk=initial_risk,
                    original_volume=original_volume,
                    peak_r=peak_r,
                    activated_giveback=activated_giveback,
                    opened_ts=opened_ts,
                )
            result["skip_trailing"] = bool(partials_enabled and trail_after_tp1 and not tp1_done)
            return result

        sign = 1.0 if side == "BUY" else -1.0
        exit_price = float(bid if side == "BUY" else ask)
        r_now = ((exit_price - entry_price) / initial_risk) if side == "BUY" else ((entry_price - exit_price) / initial_risk)

        current_sl = float(position.get("sl", 0.0) or 0.0)
        point = float((symbol_info or {}).get("point", 0.0) or 0.0)
        retry_seconds = max(10, int(tm_cfg.get("partial_retry_seconds", 60) or 60))

        if partials_enabled and not tp1_done and r_now >= float(partials_cfg.get("tp1_r", 1.0) or 1.0):
            tp1_key = f"{trade_id}:TP1"
            retry_after = float(self._tm_partial_retry_after.get(tp1_key, 0.0) or 0.0)
            if now_mono >= retry_after:
                req_volume = float(position.get("volume", 0.0) or 0.0) * float(partials_cfg.get("tp1_close_pct", 0.60) or 0.60)
                partial = self.mt5.partial_close_position(ticket, req_volume) if hasattr(self.mt5, "partial_close_position") else {"ok": False, "comment": "PARTIAL_CLOSE_NOT_SUPPORTED"}
                if bool(partial.get("ok", False)):
                    closed_volume = float(partial.get("closed_volume", 0.0) or 0.0)
                    mode = str(partials_cfg.get("tp1_sl_mode", "BE_PLUS")).upper()
                    if mode == "BE_PLUS":
                        tp1_sl = entry_price + (sign * float(partials_cfg.get("tp1_be_plus_r", 0.05) or 0.05) * initial_risk)
                    else:
                        tp1_sl = entry_price
                    sl_res = self._apply_position_sl(ticket, side, current_sl, tp1_sl, point=point)
                    if bool(sl_res.get("ok", False)):
                        current_sl = float(sl_res.get("new_sl", current_sl) or current_sl)
                        result["sl_updated"] = current_sl
                    tp1_done = True
                    state_dirty = True
                    self._tm_partial_retry_after.pop(tp1_key, None)
                    logger.info(
                        "TP1_EXECUTED: ticket=%s symbol=%s side=%s R=%.2f closed_volume=%.2f new_sl=%.5f",
                        ticket,
                        symbol,
                        side,
                        r_now,
                        closed_volume,
                        current_sl,
                    )
                    if not bool(sl_res.get("ok", False)):
                        logger.warning(
                            "TP1_BE_MOVE_REJECTED: ticket=%s symbol=%s side=%s target_sl=%.5f retcode=%s broker_error=%s",
                            ticket,
                            symbol,
                            side,
                            tp1_sl,
                            sl_res.get("retcode"),
                            str(sl_res.get("comment", "")) or str(sl_res.get("last_error", "")),
                        )
                else:
                    reason = str(partial.get("comment", "PARTIAL_REJECTED"))
                    self._tm_partial_retry_after[tp1_key] = now_mono + retry_seconds
                    if self._is_permanent_partial_reject(reason):
                        tp1_done = True
                        state_dirty = True
                        logger.warning(
                            "TP1_SKIPPED_VOLUME: ticket=%s symbol=%s side=%s R=%.2f reason=%s",
                            ticket,
                            symbol,
                            side,
                            r_now,
                            reason,
                        )
                    else:
                        logger.warning(
                            "TP1_REJECTED: ticket=%s symbol=%s side=%s R=%.2f retcode=%s reason=%s broker_error=%s",
                            ticket,
                            symbol,
                            side,
                            r_now,
                            partial.get("retcode"),
                            reason,
                            str(partial.get("last_error", "")),
                        )

        if (
            partials_enabled
            and bool(partials_cfg.get("tp2_enabled", True))
            and not tp2_done
            and r_now >= float(partials_cfg.get("tp2_r", 2.0) or 2.0)
        ):
            tp2_key = f"{trade_id}:TP2"
            retry_after = float(self._tm_partial_retry_after.get(tp2_key, 0.0) or 0.0)
            if now_mono >= retry_after:
                req_volume = float(original_volume) * float(partials_cfg.get("tp2_close_pct", 0.25) or 0.25)
                partial = self.mt5.partial_close_position(ticket, req_volume) if hasattr(self.mt5, "partial_close_position") else {"ok": False, "comment": "PARTIAL_CLOSE_NOT_SUPPORTED"}
                if bool(partial.get("ok", False)):
                    closed_volume = float(partial.get("closed_volume", 0.0) or 0.0)
                    tp2_sl = entry_price + (sign * float(partials_cfg.get("tp2_sl_lock_r", 1.0) or 1.0) * initial_risk)
                    sl_res = self._apply_position_sl(ticket, side, current_sl, tp2_sl, point=point)
                    if bool(sl_res.get("ok", False)):
                        current_sl = float(sl_res.get("new_sl", current_sl) or current_sl)
                        result["sl_updated"] = current_sl
                    tp2_done = True
                    state_dirty = True
                    self._tm_partial_retry_after.pop(tp2_key, None)
                    logger.info(
                        "TP2_EXECUTED: ticket=%s symbol=%s side=%s R=%.2f closed_volume=%.2f new_sl=%.5f",
                        ticket,
                        symbol,
                        side,
                        r_now,
                        closed_volume,
                        current_sl,
                    )
                    if not bool(sl_res.get("ok", False)):
                        logger.warning(
                            "TP2_LOCK_REJECTED: ticket=%s symbol=%s side=%s target_sl=%.5f retcode=%s broker_error=%s",
                            ticket,
                            symbol,
                            side,
                            tp2_sl,
                            sl_res.get("retcode"),
                            str(sl_res.get("comment", "")) or str(sl_res.get("last_error", "")),
                        )
                else:
                    reason = str(partial.get("comment", "PARTIAL_REJECTED"))
                    self._tm_partial_retry_after[tp2_key] = now_mono + retry_seconds
                    if self._is_permanent_partial_reject(reason):
                        tp2_done = True
                        state_dirty = True
                        logger.warning(
                            "TP2_SKIPPED_VOLUME: ticket=%s symbol=%s side=%s R=%.2f reason=%s",
                            ticket,
                            symbol,
                            side,
                            r_now,
                            reason,
                        )
                    else:
                        logger.warning(
                            "TP2_REJECTED: ticket=%s symbol=%s side=%s R=%.2f retcode=%s reason=%s broker_error=%s",
                            ticket,
                            symbol,
                            side,
                            r_now,
                            partial.get("retcode"),
                            reason,
                            str(partial.get("last_error", "")),
                        )

        if giveback_enabled:
            activate_at_r = float(giveback_cfg.get("activate_at_r", 1.2) or 1.2)
            max_giveback_pct = float(giveback_cfg.get("max_giveback_pct", 0.60) or 0.60)
            if r_now >= activate_at_r:
                if not activated_giveback:
                    activated_giveback = True
                    state_dirty = True
                if r_now > peak_r:
                    peak_r = r_now
                    state_dirty = True
            if activated_giveback and peak_r >= activate_at_r and peak_r > 0.0:
                giveback = max(0.0, (peak_r - r_now) / peak_r)
                if giveback >= max_giveback_pct:
                    if self.mt5.close_position(ticket):
                        logger.warning(
                            "GIVEBACK_GUARD_CLOSE: ticket=%s symbol=%s side=%s peak_R=%.2f R_now=%.2f giveback=%.2f",
                            ticket,
                            symbol,
                            side,
                            peak_r,
                            r_now,
                            giveback,
                        )
                        if hasattr(self.memory, "update_exit_analysis"):
                            try:
                                self.memory.update_exit_analysis(
                                    ticket=ticket,
                                    tp_hit_reason="GIVEBACK_GUARD",
                                    lessons="Position closed by giveback guard after peak R retrace.",
                                )
                            except Exception:
                                pass
                        result["closed"] = True
                        state_dirty = True
                        self._persist_trade_mgmt_state(
                            trade_id=trade_id,
                            tp1_done=tp1_done,
                            tp2_done=tp2_done,
                            initial_risk=initial_risk,
                            original_volume=original_volume,
                            peak_r=peak_r,
                            activated_giveback=activated_giveback,
                            opened_ts=opened_ts,
                        )
                        return result
                    logger.warning(
                        "GIVEBACK_GUARD_REJECTED: ticket=%s symbol=%s side=%s peak_R=%.2f R_now=%.2f",
                        ticket,
                        symbol,
                        side,
                        peak_r,
                        r_now,
                    )

        if partials_enabled and trail_after_tp1 and not tp1_done:
            result["skip_trailing"] = True

        if state_dirty:
            self._persist_trade_mgmt_state(
                trade_id=trade_id,
                tp1_done=tp1_done,
                tp2_done=tp2_done,
                initial_risk=initial_risk,
                original_volume=original_volume,
                peak_r=peak_r,
                activated_giveback=activated_giveback,
                opened_ts=opened_ts,
            )

        return result

    def _cleanup_partial_retry_cache(self, positions: list, now_mono: float):
        if not self._tm_partial_retry_after:
            return
        live_ids = {self._trade_id_from_position(p) for p in positions}
        live_ids.discard("")
        new_cache: dict[str, float] = {}
        for key, ts in self._tm_partial_retry_after.items():
            trade_id = str(key).split(":", 1)[0]
            if trade_id in live_ids and float(ts or 0.0) > now_mono:
                new_cache[key] = float(ts)
        self._tm_partial_retry_after = new_cache

    def _sync_live_positions_to_memory(self, positions: Optional[list] = None):
        ensure_fn = getattr(self.memory, "ensure_open_trade_from_position", None)
        if not callable(ensure_fn):
            return

        live_positions = list(positions or [])
        if not live_positions:
            return

        inserted = 0
        linked = 0
        for pos in live_positions:
            try:
                action = str(ensure_fn(pos))
            except Exception as e:
                logger.error(f"DB_OPEN_SYNC_FAILED: {e}", exc_info=True)
                continue
            if action == "inserted":
                inserted += 1
            elif action == "linked":
                linked += 1

        if inserted > 0 or linked > 0:
            logger.warning(
                "DB_OPEN_SYNC_SUMMARY live_positions=%s inserted=%s linked=%s",
                len(live_positions),
                inserted,
                linked,
            )

    async def _handle_prop_daily_loss_breach(self, reason: str, open_positions: Optional[list] = None):
        now = datetime.utcnow()
        if self._daily_loss_flatten_last_at and (now - self._daily_loss_flatten_last_at).total_seconds() < 15:
            return
        self._daily_loss_flatten_last_at = now

        canceled = 0
        pending_total = 0
        if hasattr(self.mt5, "get_pending_orders") and hasattr(self.mt5, "cancel_order"):
            try:
                pending_orders = self.mt5.get_pending_orders()
            except Exception:
                pending_orders = []
            pending_total = len(pending_orders)
            for order in pending_orders:
                ticket = int(order.get("ticket", 0) or 0)
                if ticket <= 0:
                    continue
                if self.mt5.cancel_order(ticket):
                    canceled += 1

        close_all = bool(self.risk.should_close_all_on_daily_loss_breach())
        closed = 0
        close_total = 0
        if close_all and hasattr(self.mt5, "close_position"):
            positions = list(open_positions or self.mt5.get_open_positions())
            close_total = len(positions)
            for pos in positions:
                ticket = int(pos.get("ticket", 0) or 0)
                if ticket <= 0:
                    continue
                if self.mt5.close_position(ticket):
                    closed += 1

        logger.error(
            "PROP_DAILY_LOSS_BREACH_FLATTEN reason=%s close_all=%s canceled_pending=%s/%s closed_positions=%s/%s",
            reason,
            int(close_all),
            canceled,
            pending_total,
            closed,
            close_total,
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
                self.risk.on_trade_closed(
                    symbol=rec.symbol,
                    outcome="BREAKEVEN",
                    pnl=0.0,
                    exit_time=rec.close_time,
                    ticket=tkt,
                    direction=rec.direction,
                    setup_id=rec.setup_id,
                )

    def get_status(self) -> dict:
        account = self.mt5.get_account_info()
        positions = self.mt5.get_open_positions()
        stats = self.risk.get_stats()
        memory_stats = self.memory.get_overall_summary()
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
            **memory_stats,
            "daily_pnl": daily["daily_pnl"],
            "daily_trades": daily["trades_today_count"],
            "daily_winrate": daily["win_rate"],
            "daily_win_rate": daily["win_rate"],
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

        setup_perf = self.memory.get_all_setup_performance()
        adaptive_stats = self.memory.get_adaptive_learning_stats() if hasattr(self.memory, "get_adaptive_learning_stats") else {}
        if hasattr(self, "loss_analyzer"):
            try:
                adaptive_stats = {**adaptive_stats, **self.loss_analyzer.get_learning_stats()}
            except Exception:
                pass
        exec_cfg = self.cfg.get("execution", {}) if isinstance(self.cfg.get("execution", {}), dict) else {}
        forced = exec_cfg.get("force_enable_setups", [])
        if isinstance(forced, list) and forced:
            forced_set = {str(x).upper().strip() for x in forced if str(x).strip()}
            for row in setup_perf:
                setup_name = str(row.get("setup", "")).upper()
                if setup_name in forced_set:
                    row["enabled"] = True
                    row["forced_enabled"] = True

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "server_time": datetime.utcnow().isoformat(),
            "account": account,
            "positions": self._json_safe_positions(positions),
            "stats": merged_stats,
            "connected": self.mt5.connected,
            "pair_biases": pair_biases,
            "trade_log": trade_log,
            "setup_performance": setup_perf,
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
            "adaptive_learning": adaptive_stats,
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
        candles_h1: list,
    ):
        in_kill_zone, kz_name = self.strategy.in_kill_zone()
        kz_name = kz_name or "NONE"
        return self.sniper_filter.evaluate(
            signal=signal,
            symbol=symbol,
            candles_m5=candles_m5,
            candles_m15=candles_m15,
            candles_h4=candles_h4,
            candles_h1=candles_h1,
            killzone=kz_name,
            in_killzone=in_kill_zone,
        )

    def _build_setup_id(self, signal: Signal) -> str:
        symbol = str(getattr(signal, "symbol", "")).upper()
        side = str(getattr(getattr(signal, "direction", None), "value", "")).upper()
        setup = str(getattr(getattr(signal, "setup_type", None), "value", "")).upper()
        entry = float(getattr(signal, "entry", 0.0) or 0.0)
        sl = float(getattr(signal, "sl", 0.0) or 0.0)
        sig_time = getattr(signal, "time", datetime.utcnow())
        if not isinstance(sig_time, datetime):
            sig_time = datetime.utcnow()
        return f"{symbol}:{side}:{setup}:{entry:.5f}:{sl:.5f}:{sig_time.strftime('%Y%m%d%H%M')}"

    def _json_safe_positions(self, positions: list) -> list:
        out = []
        for p in positions:
            item = dict(p)
            ot = item.get("open_time")
            if isinstance(ot, datetime):
                item["open_time"] = ot.isoformat()
            out.append(item)
        return out
