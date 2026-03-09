"""
Risk Manager — Position Sizing & Account Protection
════════════════════════════════════════════════════
Controls how much capital is risked on each trade and enforces account-level
safety limits. This is the LAST gate before an order is placed.

Key responsibilities:
    LOT SIZING:         Calculate position size based on risk % of account balance
    DAILY LIMITS:       Max daily trades, max daily loss (PnL + equity floor)
    COOLDOWNS:          Per-symbol, after-loss, and global throttle timers
    LOSS STREAK:        Pause trading after N consecutive losses
    PROP GUARDRAILS:    Prop firm-specific rules (daily loss cap, profit lock,
                        max total open risk, stop-for-day triggers)
    CORRELATION:        USD-thesis grouping — blocks correlated pairs (e.g.
                        EURUSD + GBPUSD both long = same USD short thesis)
    DRAWDOWN:           Max cumulative drawdown from equity peak

All limits reset at midnight UTC each day via _check_reset().
The can_trade() method is called before every entry to check all conditions.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger("RISK")


@dataclass
class TradeRecord:
    ticket: int
    symbol: str
    direction: str
    volume: float
    entry: float
    sl: float
    tp: float
    open_time: datetime
    close_time: Optional[datetime] = None
    close_price: Optional[float] = None
    pnl: float = 0.0
    setup_type: str = ""
    setup_id: str = ""
    reason: str = ""


class RiskManager:
    def __init__(self, config: dict):
        self.config = config
        self.cfg = config.get("risk", {})
        self.mode_cfg = config.get("mode", {})
        self.mode_cd_cfg = self.mode_cfg.get("cooldown", {})
        self.exec_cfg = config.get("execution", {})
        self.prop_cfg = self.exec_cfg.get("prop", {}) if isinstance(self.exec_cfg.get("prop", {}), dict) else {}
        self.guard_cfg = config.get("prop_guardrails", {}) if isinstance(config.get("prop_guardrails", {}), dict) else {}
        self.corr_cfg = config.get("correlation", {}) if isinstance(config.get("correlation", {}), dict) else {}
        self.journal: list[TradeRecord] = []

        self._today: date = datetime.utcnow().date()
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._processed_closes_today: set[int] = set()

        self._pause_until: Optional[datetime] = None
        self._lock_reason: str = ""
        self._require_new_setup: bool = False
        self._blocked_setup_id: str = ""
        self._last_seen_setup_id: str = ""

        self._cooldown_until_by_symbol: dict[str, datetime] = {}
        self._loss_cooldown_until_by_symbol: dict[str, datetime] = {}
        self._close_events_by_symbol: dict[str, list[datetime]] = {}
        self._loss_events_by_symbol: dict[str, list[datetime]] = {}
        self._recent_losses_global: list[datetime] = []
        self._global_throttle_until: Optional[datetime] = None
        self._consecutive_losses: int = 0

        self._start_day_balance: Optional[float] = None
        self._start_day_equity: Optional[float] = None
        self._start_day_ref: Optional[float] = None
        self._start_day_floor: Optional[float] = None

        self._thesis_loss_events: dict[str, list[datetime]] = {}
        self._thesis_cooldown_until: dict[str, datetime] = {}
        self._thesis_risk_scale_until: dict[str, datetime] = {}
        self._symbol_dir_block_until: dict[tuple[str, str], datetime] = {}
        self._symbol_dir_block_setup_id: dict[tuple[str, str], str] = {}

        self._equity_peak: Optional[float] = None
        self._last_drawdown_pct: float = 0.0
        self._prop_daily_lock_until: Optional[datetime] = None
        self._prop_loss_pause_until: Optional[datetime] = None
        self._prop_stop_for_day_until: Optional[datetime] = None
        self._total_open_risk_estimate_pct: float = 0.0

        self.target_stop_pips = {
            "XAUUSD": 5,
            "EURUSD": 5,
            "GBPUSD": 5,
            "AUDUSD": 5,
            "USDJPY": 5,
            "US30": 8,
            "NAS100": 8,
            "SPX500": 8,
        }

    def _check_reset(self):
        today = datetime.utcnow().date()
        if today != self._today:
            logger.info(
                f"New day {today} resetting daily counters. Yesterday P&L: {self._daily_pnl:+.2f}"
            )
            self._today = today
            self._daily_pnl = 0.0
            self._daily_trades = 0
            self._processed_closes_today.clear()
            self._pause_until = None
            self._lock_reason = ""
            self._require_new_setup = False
            self._blocked_setup_id = ""
            self._last_seen_setup_id = ""
            self._cooldown_until_by_symbol.clear()
            self._loss_cooldown_until_by_symbol.clear()
            self._close_events_by_symbol.clear()
            self._loss_events_by_symbol.clear()
            self._recent_losses_global.clear()
            self._global_throttle_until = None
            self._consecutive_losses = 0
            self._start_day_balance = None
            self._start_day_equity = None
            self._start_day_ref = None
            self._start_day_floor = None
            self._thesis_loss_events.clear()
            self._thesis_cooldown_until.clear()
            self._thesis_risk_scale_until.clear()
            self._symbol_dir_block_until.clear()
            self._symbol_dir_block_setup_id.clear()
            self._equity_peak = None
            self._last_drawdown_pct = 0.0
            self._prop_daily_lock_until = None
            self._prop_loss_pause_until = None
            self._prop_stop_for_day_until = None
            self._total_open_risk_estimate_pct = 0.0

    def _now(self) -> datetime:
        return datetime.utcnow()

    def _prune(self, events: list[datetime], max_age_seconds: int, now: Optional[datetime] = None):
        ref = now or self._now()
        cutoff = ref - timedelta(seconds=max_age_seconds)
        while events and events[0] < cutoff:
            events.pop(0)

    def _seconds_remaining(self, until: Optional[datetime], now: Optional[datetime] = None) -> int:
        if not until:
            return 0
        ref = now or self._now()
        if ref >= until:
            return 0
        return int((until - ref).total_seconds())

    def _is_hybrid_mode(self) -> bool:
        return str(self.mode_cfg.get("type", "normal")).lower() == "hybrid"

    def _is_prop_mode(self) -> bool:
        profile = str(self.exec_cfg.get("profile", "normal")).strip().upper()
        return profile == "PROP_CHALLENGE" and bool(self.prop_cfg.get("enabled", False))

    def _guardrails_enabled(self) -> bool:
        return bool(self.guard_cfg.get("enabled", False)) or self._is_prop_mode()

    def _daily_profit_lock_pct(self) -> float:
        if "daily_profit_lock_pct" in self.guard_cfg:
            return float(self.guard_cfg.get("daily_profit_lock_pct", 0.0) or 0.0)
        return float(self.prop_cfg.get("daily_profit_lock_pct", 0.0) or 0.0)

    def _daily_loss_cap_pct(self) -> float:
        if "daily_loss_cap_pct" in self.guard_cfg:
            return float(self.guard_cfg.get("daily_loss_cap_pct", 0.0) or 0.0)
        if self._is_prop_mode():
            return float(self.prop_cfg.get("max_daily_loss_pct", self.cfg.get("max_daily_loss_pct", 0.0)) or 0.0)
        return float(self.cfg.get("max_daily_loss_pct", 0.0) or 0.0)

    def _loss_streak_limit(self) -> int:
        if "loss_streak_limit" in self.guard_cfg:
            return int(self.guard_cfg.get("loss_streak_limit", 0) or 0)
        if self._is_prop_mode():
            return int(self.prop_cfg.get("max_consecutive_losses_stop", 0) or 0)
        return int(self.cfg.get("max_consecutive_losses", 0) or 0)

    def _loss_streak_pause_minutes(self) -> int:
        if "pause_minutes_on_streak" in self.guard_cfg:
            return int(self.guard_cfg.get("pause_minutes_on_streak", 0) or 0)
        if self._is_prop_mode():
            return int(self.prop_cfg.get("loss_pause_minutes", 0) or 0)
        return int(self.cfg.get("loss_streak_cooldown_minutes", 0) or 0)

    def _stop_for_day_on_streak(self) -> bool:
        if "stop_for_day_on_streak" in self.guard_cfg:
            return bool(self.guard_cfg.get("stop_for_day_on_streak", False))
        return bool(self.prop_cfg.get("stop_for_day_on_loss_streak", True))

    def _end_of_day_utc(self, now: Optional[datetime] = None) -> datetime:
        ref = now or self._now()
        return ref.replace(hour=23, minute=59, second=59, microsecond=0)

    def _close_all_on_daily_loss_breach(self) -> bool:
        return bool(self.guard_cfg.get("close_all_on_daily_loss_breach", True))

    def _ensure_day_reference(self, balance: float, equity: Optional[float], now: datetime) -> None:
        if self._start_day_ref is not None and self._start_day_floor is not None:
            return
        bal = float(balance)
        eq = float(equity if equity is not None else bal)
        self._start_day_balance = bal
        self._start_day_equity = eq
        self._start_day_ref = max(self._start_day_balance, self._start_day_equity)
        loss_pct = self._daily_loss_cap_pct()
        self._start_day_floor = self._start_day_ref * (1.0 - (loss_pct / 100.0))
        logger.warning(
            "DAY_REF_SET ts=%s start_day_balance=%.2f start_day_equity=%.2f start_day_ref=%.2f "
            "daily_loss_cap_pct=%.2f start_day_floor=%.2f",
            now.isoformat(),
            self._start_day_balance,
            self._start_day_equity,
            self._start_day_ref,
            loss_pct,
            self._start_day_floor,
        )

    def _normalize_direction(self, direction: str) -> str:
        d = str(direction or "").strip().upper()
        if d in ("BUY", "LONG"):
            return "BUY"
        if d in ("SELL", "SHORT"):
            return "SELL"
        return ""

    def _trade_thesis(self, symbol: str, direction: str) -> str:
        sym = str(symbol or "").upper().strip()
        side = self._normalize_direction(direction)
        if not sym or side not in ("BUY", "SELL"):
            return "OTHER"

        thesis_groups = self.corr_cfg.get("thesis_groups", {})
        if isinstance(thesis_groups, dict):
            sym_cfg = thesis_groups.get(sym, {})
            if isinstance(sym_cfg, dict):
                mapped = sym_cfg.get(side)
                if mapped:
                    val = str(mapped).strip().upper()
                    if val in ("USD_LONG", "USD_SHORT", "OTHER"):
                        return val

        if sym.startswith("USD") and len(sym) == 6:
            return "USD_LONG" if side == "BUY" else "USD_SHORT"
        if sym.endswith("USD") and len(sym) == 6:
            return "USD_SHORT" if side == "BUY" else "USD_LONG"
        return "OTHER"

    def _pair_key(self, a: str, b: str) -> tuple[str, str]:
        s1 = str(a or "").upper().strip()
        s2 = str(b or "").upper().strip()
        return tuple(sorted((s1, s2)))

    def _dangerous_corr_pairs(self) -> set[tuple[str, str]]:
        default_pairs = [
            ("EURUSD", "GBPUSD"),
            ("EURUSD", "USDCHF"),
            ("GBPUSD", "USDCHF"),
        ]
        if "dangerous_pairs" in self.corr_cfg:
            raw = self.corr_cfg.get("dangerous_pairs", [])
            out: set[tuple[str, str]] = set()
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        out.add(self._pair_key(item[0], item[1]))
            return out

        raw = default_pairs
        out: set[tuple[str, str]] = set()
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    out.add(self._pair_key(item[0], item[1]))
        return out

    def _medium_corr_scales(self) -> dict[tuple[str, str], float]:
        default_map = {
            ("EURUSD", "AUDUSD"): 0.65,
            ("GBPUSD", "AUDUSD"): 0.80,
        }
        raw = self.corr_cfg.get("medium_pair_scale", default_map)
        out: dict[tuple[str, str], float] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                scale = max(0.0, min(1.0, float(v or 1.0)))
                if isinstance(k, str):
                    parts = [x.strip().upper() for x in k.replace("/", "|").split("|") if x.strip()]
                    if len(parts) >= 2:
                        out[self._pair_key(parts[0], parts[1])] = scale
                elif isinstance(k, (list, tuple)) and len(k) >= 2:
                    out[self._pair_key(k[0], k[1])] = scale
        if not out:
            for (a, b), s in default_map.items():
                out[self._pair_key(a, b)] = max(0.0, min(1.0, float(s)))
        return out

    def _same_usd_thesis_direction(self, sym_a: str, dir_a: str, sym_b: str, dir_b: str) -> bool:
        ta = self._trade_thesis(sym_a, dir_a)
        tb = self._trade_thesis(sym_b, dir_b)
        return ta in ("USD_LONG", "USD_SHORT") and ta == tb

    def _single_loss_risk_scale(self) -> float:
        return max(0.0, min(1.0, float(self.corr_cfg.get("single_loss_risk_scale", 0.5) or 0.5)))

    def _single_loss_risk_scale_seconds(self) -> int:
        return int(self.corr_cfg.get("single_loss_risk_scale_seconds", 3600) or 3600)

    def get_trade_thesis(self, symbol: str, direction: str) -> str:
        return self._trade_thesis(symbol, direction)

    def estimate_used_risk_pct(self, confidence: float = 0.0, rr: float = 0.0, risk_scale: float = 1.0) -> float:
        return float(self._used_risk_pct(confidence=confidence, rr=rr, risk_scale=risk_scale))

    def correlation_risk_scale(self, symbol: str, direction: str, open_positions: list) -> tuple[float, str]:
        if not bool(self.corr_cfg.get("enabled", False)):
            return 1.0, "THESIS=OTHER CORR_DECISION=OK PAIR_TRIGGER=OFF"
        sym = str(symbol or "").upper()
        side = self._normalize_direction(direction)
        if not sym or side not in ("BUY", "SELL"):
            return 1.0, "THESIS=OTHER CORR_DECISION=OK PAIR_TRIGGER=INVALID"

        thesis = self._trade_thesis(sym, side)
        if thesis not in ("USD_LONG", "USD_SHORT"):
            return 1.0, "THESIS=OTHER CORR_DECISION=OK PAIR_TRIGGER=NON_USD"

        scale = 1.0
        triggers: list[str] = []
        med = self._medium_corr_scales()
        for p in open_positions or []:
            p_sym = str((p or {}).get("symbol", "")).upper()
            p_side = self._normalize_direction((p or {}).get("type", ""))
            if not p_sym or p_side not in ("BUY", "SELL"):
                continue
            if not self._same_usd_thesis_direction(sym, side, p_sym, p_side):
                continue
            key = self._pair_key(sym, p_sym)
            pair_scale = med.get(key)
            if pair_scale is not None and pair_scale < scale:
                scale = pair_scale
                triggers.append(f"{key[0]}<->{key[1]}")

        scale_until = self._thesis_risk_scale_until.get(thesis)
        if scale_until and self._now() < scale_until:
            thesis_scale = self._single_loss_risk_scale()
            if thesis_scale < scale:
                scale = thesis_scale
                if "THESIS_LOSS_SCALE" not in triggers:
                    triggers.append("THESIS_LOSS_SCALE")

        scale = max(0.0, min(1.0, scale))
        decision = "SCALE" if scale < 1.0 else "OK"
        pair_trigger = ",".join(triggers) if triggers else "NONE"
        detail = f"THESIS={thesis} CORR_DECISION={decision} PAIR_TRIGGER={pair_trigger}"
        return scale, detail

    def should_close_all_on_daily_loss_breach(self) -> bool:
        return self._close_all_on_daily_loss_breach()

    def _used_risk_pct(self, confidence: float = 0.0, rr: float = 0.0, risk_scale: float = 1.0) -> float:
        scale = max(0.0, min(1.0, float(risk_scale)))
        if self._is_prop_mode():
            base = float(self.prop_cfg.get("base_risk_per_trade_pct", self.cfg.get("risk_per_trade_pct", 1.0)))
            cap = float(self.prop_cfg.get("max_risk_per_trade_pct", base))
            used = min(base, cap)
            if confidence >= 0.80 and rr >= 2.5:
                used = min(max(base, cap), cap)
            return max(0.0, used * scale)
        base = float(self.cfg.get("risk_per_trade_pct", 1.0))
        return max(0.0, base * scale)

    def set_cooldown(
        self,
        minutes: int,
        reason: str,
        require_new_setup: bool = True,
        blocked_setup_id: str = "",
    ):
        now = self._now()
        self._pause_until = now + timedelta(minutes=max(0, int(minutes)))
        self._lock_reason = reason
        self._require_new_setup = bool(require_new_setup)
        self._blocked_setup_id = blocked_setup_id or self._blocked_setup_id

        logger.warning(
            f"Trading paused until {self._pause_until.isoformat()} | "
            f"Reason: {reason} | Require new setup: {self._require_new_setup} | "
            f"Blocked setup_id: {self._blocked_setup_id or '-'}"
        )

    def clear_lock(self):
        self._pause_until = None
        self._lock_reason = ""
        self._require_new_setup = False
        self._blocked_setup_id = ""

    def _derive_outcome(self, outcome: Optional[str], pnl: float) -> str:
        if outcome:
            out = str(outcome).upper()
            if out in ("WIN", "LOSS", "BREAKEVEN", "BE"):
                return "BREAKEVEN" if out == "BE" else out
        if pnl > 0:
            return "WIN"
        if pnl < 0:
            return "LOSS"
        return "BREAKEVEN"

    def on_trade_closed(
        self,
        symbol: str,
        outcome: Optional[str],
        pnl: float,
        exit_time: Optional[datetime] = None,
        ticket: Optional[int] = None,
        direction: str = "",
        setup_id: str = "",
    ):
        self._check_reset()
        now = exit_time or self._now()
        if ticket is not None:
            try:
                t = int(ticket)
            except Exception:
                t = None
            if t is None:
                logger.warning(f"DUP_CLOSE_IGNORED ticket_invalid={ticket} symbol={symbol} pnl={float(pnl):+.2f}")
            else:
                if t in self._processed_closes_today:
                    logger.warning(f"DUP_CLOSE_IGNORED ticket={t} symbol={symbol} pnl={float(pnl):+.2f}")
                    return
                self._processed_closes_today.add(t)
        out = self._derive_outcome(outcome, pnl)
        sym = str(symbol or "").upper()
        if not sym:
            return

        self._daily_pnl += float(pnl)

        self._close_events_by_symbol.setdefault(sym, []).append(now)
        self._prune(self._close_events_by_symbol[sym], 3600, now)

        cd_sym = int(self.mode_cd_cfg.get("per_symbol_seconds", 0))
        cd_loss = int(self.mode_cd_cfg.get("after_loss_seconds", 0))
        cd_win = int(self.mode_cd_cfg.get("after_win_seconds", 0))
        cooldown_seconds = cd_sym
        if out == "LOSS":
            cooldown_seconds = max(cooldown_seconds, cd_loss)
        elif out == "WIN":
            cooldown_seconds = max(cooldown_seconds, cd_win)
        if cooldown_seconds > 0:
            self._cooldown_until_by_symbol[sym] = now + timedelta(seconds=cooldown_seconds)

        if out == "LOSS":
            self._loss_events_by_symbol.setdefault(sym, []).append(now)
            self._prune(self._loss_events_by_symbol[sym], 3600, now)
            self._recent_losses_global.append(now)
            self._prune(self._recent_losses_global, 20 * 60, now)

            self._consecutive_losses += 1
            if cd_loss > 0:
                self._loss_cooldown_until_by_symbol[sym] = now + timedelta(seconds=cd_loss)

            glb_loss_seconds = int(self.mode_cd_cfg.get("global_after_loss_seconds", 0))
            if glb_loss_seconds > 0 and len(self._recent_losses_global) >= 2:
                self._global_throttle_until = now + timedelta(seconds=glb_loss_seconds)

            side = self._normalize_direction(direction)
            if side in ("BUY", "SELL"):
                key = (sym, side)
                block_seconds = int(self.mode_cd_cfg.get("after_loss_seconds", 0) or 0)
                if block_seconds > 0:
                    self._symbol_dir_block_until[key] = now + timedelta(seconds=block_seconds)
                    self._symbol_dir_block_setup_id[key] = str(setup_id or "").strip()

            if bool(self.corr_cfg.get("enabled", False)):
                thesis = self._trade_thesis(sym, direction)
                if thesis in ("USD_LONG", "USD_SHORT"):
                    self._thesis_loss_events.setdefault(thesis, []).append(now)
                    window_seconds = int(self.corr_cfg.get("loss_window_seconds", 3600) or 3600)
                    self._prune(self._thesis_loss_events[thesis], window_seconds, now)
                    loss_count = len(self._thesis_loss_events[thesis])
                    single_scale = self._single_loss_risk_scale()
                    single_scale_seconds = self._single_loss_risk_scale_seconds()
                    if loss_count >= 1 and single_scale < 1.0 and single_scale_seconds > 0:
                        scale_until = now + timedelta(seconds=single_scale_seconds)
                        self._thesis_risk_scale_until[thesis] = scale_until
                        logger.warning(
                            "THESIS_RISK_SCALE_SET thesis=%s losses=%s window_seconds=%s risk_scale=%.2f until=%s",
                            thesis,
                            loss_count,
                            window_seconds,
                            single_scale,
                            scale_until.isoformat(),
                        )
                    if loss_count >= 2:
                        cool_seconds = int(self.corr_cfg.get("cooldown_seconds_after_thesis_loss", 1800) or 1800)
                        until = now + timedelta(seconds=max(0, cool_seconds))
                        self._thesis_cooldown_until[thesis] = until
                        self._thesis_risk_scale_until.pop(thesis, None)
                        logger.warning(
                            "THESIS_COOLDOWN_SET thesis=%s losses=%s window_seconds=%s until=%s",
                            thesis,
                            loss_count,
                            window_seconds,
                            until.isoformat(),
                        )
        elif out == "WIN":
            self._consecutive_losses = 0
            self._loss_cooldown_until_by_symbol.pop(sym, None)
            if bool(self.corr_cfg.get("enabled", False)):
                thesis = self._trade_thesis(sym, direction)
                if thesis in ("USD_LONG", "USD_SHORT"):
                    self._thesis_risk_scale_until.pop(thesis, None)
        else:
            self._consecutive_losses = 0

        max_consecutive_losses = int(self.cfg.get("max_consecutive_losses", 0))
        streak_cd_minutes = int(self.cfg.get("loss_streak_cooldown_minutes", 0))
        if max_consecutive_losses > 0 and self._consecutive_losses >= max_consecutive_losses:
            if streak_cd_minutes > 0:
                self._pause_until = now + timedelta(minutes=streak_cd_minutes)
                self._lock_reason = f"LOSS_STREAK_{self._consecutive_losses}"

        if self._guardrails_enabled():
            pause_n = int(self.prop_cfg.get("max_consecutive_losses_pause", 0))
            pause_minutes = self._loss_streak_pause_minutes()
            stop_n = self._loss_streak_limit()
            stop_for_day = self._stop_for_day_on_streak()
            if stop_n > 0 and self._consecutive_losses >= stop_n and stop_for_day:
                self._prop_stop_for_day_until = self._end_of_day_utc(now)
                self._prop_daily_lock_until = self._prop_stop_for_day_until
            elif pause_n > 0 and self._consecutive_losses >= pause_n and pause_minutes > 0:
                self._prop_loss_pause_until = now + timedelta(minutes=pause_minutes)
            elif stop_n > 0 and self._consecutive_losses >= stop_n and pause_minutes > 0:
                self._prop_loss_pause_until = now + timedelta(minutes=pause_minutes)

    def should_cooldown(self, symbol: str) -> tuple[bool, str, int]:
        self._check_reset()
        now = self._now()
        sym = str(symbol or "").upper()
        if not sym:
            return False, "OK", 0

        if self._pause_until and now < self._pause_until:
            rem = self._seconds_remaining(self._pause_until, now)
            return True, f"LOSS_STREAK_COOLDOWN until={self._pause_until.isoformat()}", rem

        if self._global_throttle_until and now < self._global_throttle_until:
            rem = self._seconds_remaining(self._global_throttle_until, now)
            return True, f"GLOBAL_LOSS_THROTTLE until={self._global_throttle_until.isoformat()}", rem

        sym_until = self._cooldown_until_by_symbol.get(sym)
        if sym_until and now < sym_until:
            rem = self._seconds_remaining(sym_until, now)
            return True, f"SYMBOL_COOLDOWN until={sym_until.isoformat()}", rem

        loss_until = self._loss_cooldown_until_by_symbol.get(sym)
        if loss_until and now < loss_until:
            rem = self._seconds_remaining(loss_until, now)
            return True, f"SYMBOL_AFTER_LOSS_COOLDOWN until={loss_until.isoformat()}", rem

        max_trades_hour = int(self.cfg.get("max_trades_per_symbol_per_hour", 0))
        sym_events = self._close_events_by_symbol.get(sym, [])
        if sym_events:
            self._prune(sym_events, 3600, now)
        if max_trades_hour > 0 and len(sym_events) >= max_trades_hour:
            oldest = min(sym_events)
            rem = max(0, int(3600 - (now - oldest).total_seconds()))
            return True, f"MAX_TRADES_PER_SYMBOL_PER_HOUR {len(sym_events)}/{max_trades_hour}", rem

        max_loss_hour = int(self.cfg.get("max_loss_trades_per_symbol_per_hour", 0))
        sym_loss_events = self._loss_events_by_symbol.get(sym, [])
        if sym_loss_events:
            self._prune(sym_loss_events, 3600, now)
        if max_loss_hour > 0 and len(sym_loss_events) >= max_loss_hour:
            oldest = min(sym_loss_events)
            rem = max(0, int(3600 - (now - oldest).total_seconds()))
            return True, f"MAX_LOSS_TRADES_PER_SYMBOL_PER_HOUR {len(sym_loss_events)}/{max_loss_hour}", rem

        return False, "OK", 0

    def can_trade(
        self,
        open_positions: list,
        account_balance: float,
        setup_id: str = "",
        symbol: str = "",
        direction: str = "",
        equity: Optional[float] = None,
        current_daily_pnl: Optional[float] = None,
        confidence: float = 0.0,
        rr: float = 0.0,
        risk_scale: float = 1.0,
    ) -> tuple[bool, str]:
        self._check_reset()

        if setup_id:
            self._last_seen_setup_id = setup_id

        now = self._now()
        self._ensure_day_reference(account_balance, equity, now)

        if self._guardrails_enabled():
            max_loss_pct = self._daily_loss_cap_pct()
            if (
                max_loss_pct > 0
                and equity is not None
                and self._start_day_floor is not None
                and self._start_day_ref is not None
            ):
                eq = float(equity)
                if eq <= self._start_day_floor:
                    self._prop_stop_for_day_until = self._end_of_day_utc(now)
                    self._prop_daily_lock_until = self._prop_stop_for_day_until
                    reason = (
                        "MAX_DAILY_LOSS_EQUITY "
                        f"eq={eq:.2f} floor={self._start_day_floor:.2f} ref={self._start_day_ref:.2f} "
                        f"pct={max_loss_pct:.2f} close_all={int(self._close_all_on_daily_loss_breach())}"
                    )
                    logger.error(reason)
                    return False, reason

        daily_profit_target = float(self.cfg.get("daily_profit_target_usd", 1000.0))
        if daily_profit_target > 0 and self._daily_pnl >= daily_profit_target:
            return False, f"Daily profit target hit: {self._daily_pnl:+.2f} / {daily_profit_target:+.2f}"

        if self._pause_until is not None and now < self._pause_until:
            return False, f"LOSS_STREAK_COOLDOWN until={self._pause_until.isoformat()}"

        if self._global_throttle_until is not None and now < self._global_throttle_until:
            return False, f"GLOBAL_LOSS_THROTTLE until={self._global_throttle_until.isoformat()}"

        if self._guardrails_enabled():
            if self._prop_stop_for_day_until is not None and now < self._prop_stop_for_day_until:
                return False, f"LOSS_STREAK_STOP_DAY until={self._prop_stop_for_day_until.isoformat()}"
            if self._prop_loss_pause_until is not None and now < self._prop_loss_pause_until:
                return False, f"LOSS_STREAK_PAUSE until={self._prop_loss_pause_until.isoformat()}"
            if self._prop_daily_lock_until is not None and now < self._prop_daily_lock_until:
                return False, f"DAILY_PROFIT_LOCK until={self._prop_daily_lock_until.isoformat()}"

        if self._require_new_setup and self._blocked_setup_id:
            if setup_id and setup_id == self._blocked_setup_id:
                return False, "Waiting for a NEW setup (same setup_id blocked after protection close)"
            if setup_id and setup_id != self._blocked_setup_id:
                logger.info(f"New setup detected ({setup_id}) lifting new-setup gate.")
                self._require_new_setup = False
                self._blocked_setup_id = ""

        sym = str(symbol or "").upper()
        side = self._normalize_direction(direction)
        if sym and side in ("BUY", "SELL"):
            key = (sym, side)
            block_until = self._symbol_dir_block_until.get(key)
            blocked_setup = self._symbol_dir_block_setup_id.get(key, "")
            if block_until and now < block_until:
                rem = self._seconds_remaining(block_until, now)
                return (
                    False,
                    "REENTRY_DIR_COOLDOWN "
                    f"symbol={sym} direction={side} until={block_until.isoformat()} "
                    f"remaining={rem}s setup_id={setup_id or '-'} blocked_setup_id={blocked_setup or '-'}",
                )
            if blocked_setup:
                if not setup_id or setup_id == blocked_setup:
                    return (
                        False,
                        "REENTRY_SETUP_BLOCK "
                        f"symbol={sym} direction={side} setup_id={setup_id or '-'} "
                        f"blocked_setup_id={blocked_setup}",
                    )
                logger.info(
                    "REENTRY_SETUP_NEW symbol=%s direction=%s old_setup_id=%s new_setup_id=%s",
                    sym,
                    side,
                    blocked_setup,
                    setup_id,
                )
                self._symbol_dir_block_setup_id.pop(key, None)

        candidate_thesis = self._trade_thesis(sym, side)
        if bool(self.corr_cfg.get("enabled", False)) and candidate_thesis in ("USD_LONG", "USD_SHORT"):
            th_cd_until = self._thesis_cooldown_until.get(candidate_thesis)
            if th_cd_until and now < th_cd_until:
                rem = self._seconds_remaining(th_cd_until, now)
                return (
                    False,
                    "THESIS_COOLDOWN "
                    f"THESIS={candidate_thesis} CORR_DECISION=BLOCK PAIR_TRIGGER=THESIS_COOLDOWN "
                    f"until={th_cd_until.isoformat()} remaining={rem}",
                )

            same = 0
            opp = 0
            opposite = "USD_SHORT" if candidate_thesis == "USD_LONG" else "USD_LONG"
            for p in open_positions or []:
                p_sym = str((p or {}).get("symbol", "")).upper()
                p_side = self._normalize_direction((p or {}).get("type", ""))
                if self._same_usd_thesis_direction(sym, side, p_sym, p_side):
                    pair_key = self._pair_key(sym, p_sym)
                    if pair_key in self._dangerous_corr_pairs():
                        return (
                            False,
                            "CORR_BLOCK_DANGEROUS "
                            f"THESIS={candidate_thesis} CORR_DECISION=BLOCK "
                            f"PAIR_TRIGGER={pair_key[0]}<->{pair_key[1]}",
                        )
                p_thesis = self._trade_thesis(p_sym, p_side)
                if p_thesis == candidate_thesis:
                    same += 1
                elif p_thesis == opposite:
                    opp += 1

            max_usd_short_open = int(self.corr_cfg.get("max_usd_short_open", 0) or 0)
            if candidate_thesis == "USD_SHORT" and max_usd_short_open > 0 and same >= max_usd_short_open:
                return (
                    False,
                    "CORR_BLOCK_USD_WEAKNESS "
                    f"THESIS={candidate_thesis} CORR_DECISION=BLOCK PAIR_TRIGGER=BASKET_CAP "
                    f"open_same={same} max={max_usd_short_open}",
                )

            max_same = int(self.corr_cfg.get("max_same_thesis_open", 1) or 1)
            if same >= max_same:
                return (
                    False,
                    "CORR_BLOCK "
                    f"THESIS={candidate_thesis} CORR_DECISION=BLOCK PAIR_TRIGGER=BASKET_CAP "
                    f"open_same={same} max={max_same}",
                )

            if bool(self.corr_cfg.get("block_opposite_thesis", False)) and opp > 0:
                return (
                    False,
                    "CORR_BLOCK_OPPOSITE "
                    f"THESIS={candidate_thesis} CORR_DECISION=BLOCK PAIR_TRIGGER=OPPOSITE_THESIS "
                    f"opposite_open={opp}",
                )

        if symbol:
            blocked, reason, _ = self.should_cooldown(symbol)
            if blocked:
                return False, reason

        max_open = int(self.cfg.get("max_open_trades", 0))
        if self._is_prop_mode():
            max_open = int(self.prop_cfg.get("max_open_trades", max_open))
        if max_open > 0 and len(open_positions) >= max_open:
            return False, f"MAX_OPEN_TRADES ({len(open_positions)}/{max_open})"

        if self._daily_trades >= int(self.cfg.get("max_daily_trades", 0)):
            return False, f"Max daily trades reached ({self.cfg.get('max_daily_trades', 0)})"

        effective_daily_pnl = float(self._daily_pnl if current_daily_pnl is None else current_daily_pnl)
        max_loss_pct = self._daily_loss_cap_pct()
        max_loss = account_balance * (max_loss_pct / 100.0)
        if max_loss_pct > 0 and effective_daily_pnl <= -abs(max_loss):
            return False, (
                f"MAX_DAILY_LOSS daily_pnl={effective_daily_pnl:.2f} "
                f"limit=-{abs(max_loss):.2f} pct={max_loss_pct:.2f}"
            )

        if self._guardrails_enabled():
            profit_lock_pct = self._daily_profit_lock_pct()
            if profit_lock_pct > 0:
                profit_lock_usd = account_balance * (profit_lock_pct / 100.0)
                if effective_daily_pnl >= profit_lock_usd:
                    self._prop_daily_lock_until = self._end_of_day_utc(now)
                    return False, f"DAILY_PROFIT_LOCK until={self._prop_daily_lock_until.isoformat()}"

            next_trade_risk_pct = self._used_risk_pct(confidence=confidence, rr=rr, risk_scale=risk_scale)
            current_open_risk_pct = len(open_positions) * next_trade_risk_pct
            self._total_open_risk_estimate_pct = current_open_risk_pct
            max_total_open_risk_pct = float(
                self.guard_cfg.get("max_total_open_risk_pct", self.prop_cfg.get("max_total_open_risk_pct", 0.0)) or 0.0
            )
            if (
                max_total_open_risk_pct > 0
                and (current_open_risk_pct + next_trade_risk_pct) > max_total_open_risk_pct
            ):
                return (
                    False,
                    "MAX_TOTAL_OPEN_RISK "
                    f"current={current_open_risk_pct:.2f} next={next_trade_risk_pct:.2f} limit={max_total_open_risk_pct:.2f}",
                )

        if equity is not None:
            eq = float(equity)
            if self._equity_peak is None or eq > self._equity_peak:
                self._equity_peak = eq
            max_dd_pct = float(self.cfg.get("max_cumulative_loss_pct", 0.0))
            if self._equity_peak and self._equity_peak > 0:
                dd_pct = ((self._equity_peak - eq) / self._equity_peak) * 100.0
                self._last_drawdown_pct = max(0.0, dd_pct)
                if max_dd_pct > 0 and dd_pct >= max_dd_pct:
                    return False, f"DRAWDOWN dd={dd_pct:.2f}% limit={max_dd_pct:.2f}%"

        return True, "OK"

    def calculate_lot_size(
        self,
        symbol: str,
        entry: float,
        sl: float,
        tp: float,
        account_balance: float,
        confidence: float = 0.75,
        in_kill_zone: bool = False,
        open_positions: Optional[list] = None,
        daily_pnl: Optional[float] = None,
        pip_value_per_lot: Optional[float] = None,
        volume_min: Optional[float] = None,
        volume_max: Optional[float] = None,
        volume_step: Optional[float] = None,
        rr: float = 0.0,
        risk_scale: float = 1.0,
    ) -> float:
        pip_size = self._get_pip_size(symbol)
        stop_pips = abs(entry - sl) / pip_size

        if stop_pips <= 0:
            logger.warning(f"{symbol}: Invalid stop distance (stop_pips={stop_pips})")
            return 0.0

        sym = str(symbol).upper()
        soft_cfg = self._soft_sl_cfg_for_symbol(sym)
        soft_enabled = bool(soft_cfg.get("enabled", False))
        if self._is_prop_mode():
            cap_cfg = self.exec_cfg.get("max_sl_pips", {})
            sl_cap = 0.0
            if isinstance(cap_cfg, dict):
                sl_cap = float(cap_cfg.get(sym, 0.0) or 0.0)
            else:
                sl_cap = float(cap_cfg or 0.0)
            allow_pct = max(0.0, float(self.exec_cfg.get("soft_sl_cap_allow_pct", 0.0) or 0.0))
            hard_limit = sl_cap * (1.0 + allow_pct) if bool(self.exec_cfg.get("soft_sl_cap", False)) else sl_cap
            if sl_cap > 0 and stop_pips > hard_limit:
                logger.warning(
                    f"SKIP_SNIPER_SL_CAP: symbol={sym} reason=SL_TOO_WIDE_PIPS sl_pips={stop_pips:.2f} cap={sl_cap:.2f}"
                )
                return 0.0
        elif soft_enabled:
            sl_cap = float(soft_cfg.get("max_sl_pips", 0.0) or 0.0)
            hard_mult = float(soft_cfg.get("hard_reject_multiplier", 2.0) or 2.0)
            if sl_cap > 0 and stop_pips > (sl_cap * max(1.0, hard_mult)):
                logger.warning(
                    f"SKIP_SNIPER_SL_CAP: symbol={sym} reason=SL_TOO_WIDE_PIPS sl_pips={stop_pips:.2f} cap={sl_cap:.2f} hard_mult={hard_mult:.2f}"
                )
                return 0.0
        else:
            sl_caps = self.exec_cfg.get("max_sl_pips", {})
            sl_cap = 0.0
            if isinstance(sl_caps, dict):
                try:
                    sl_cap = float(sl_caps.get(sym, 0.0))
                except Exception:
                    sl_cap = 0.0
            if sl_cap > 0 and stop_pips > sl_cap:
                logger.warning(
                    f"SKIP_SNIPER_SL_CAP: symbol={sym} reason=SL_TOO_WIDE_PIPS sl_pips={stop_pips:.2f} cap={sl_cap:.2f}"
                )
                return 0.0

        base_risk_pct = self._used_risk_pct(confidence=confidence, rr=rr, risk_scale=risk_scale)
        risk_multiplier = self.compute_risk_multiplier(sym, stop_pips)
        effective_base_risk_pct = base_risk_pct * risk_multiplier
        confidence_scaled = effective_base_risk_pct * (0.8 + confidence * 0.4)
        risk_amount = account_balance * (confidence_scaled / 100.0)

        if soft_enabled:
            sl_cap = float(soft_cfg.get("max_sl_pips", 0.0) or 0.0)
            soft_buffer = float(soft_cfg.get("soft_buffer_pips", 0.0) or 0.0)
            if sl_cap > 0 and stop_pips > sl_cap and stop_pips <= (sl_cap + max(0.0, soft_buffer)):
                logger.info(
                    f"RISK_SOFT_SL_CAP: symbol={sym} sl_pips={stop_pips:.2f} cap={sl_cap:.2f} mult={risk_multiplier:.3f} "
                    f"base_risk={base_risk_pct:.3f}% effective_risk={effective_base_risk_pct:.3f}%"
                )

        if in_kill_zone:
            kz_mult = float(self.cfg.get("kill_zone_risk_mult", 1.3))
            risk_amount *= kz_mult
            logger.info(f"Kill zone active risk increased to ${risk_amount:.2f} (x{kz_mult:.2f})")

        max_sl_usd = 0.0
        try:
            max_sl_usd = float(self.exec_cfg.get("max_sl_usd", 0.0))
        except Exception:
            max_sl_usd = 0.0
        max_risk_per_trade_usd = 0.0
        try:
            max_risk_per_trade_usd = float(self.cfg.get("max_risk_per_trade_usd", 0.0))
        except Exception:
            max_risk_per_trade_usd = 0.0
        cap_candidates = [v for v in (max_sl_usd, max_risk_per_trade_usd) if v > 0]
        hard_cap_usd = min(cap_candidates) if cap_candidates else 0.0
        slippage_buffer_pct = 0.0
        try:
            slippage_buffer_pct = float(self.cfg.get("slippage_buffer_pct", 0.0) or 0.0)
        except Exception:
            slippage_buffer_pct = 0.0
        slippage_buffer_pct = min(0.50, max(0.0, slippage_buffer_pct))
        capped_budget_usd = hard_cap_usd * (1.0 - slippage_buffer_pct) if hard_cap_usd > 0 else 0.0
        if capped_budget_usd > 0 and risk_amount > capped_budget_usd:
            logger.info(
                f"RISK_HARD_CAP: symbol={sym} raw_target={risk_amount:.2f} cap={hard_cap_usd:.2f} "
                f"buffer={slippage_buffer_pct:.2%} used_target={capped_budget_usd:.2f}"
            )
            risk_amount = capped_budget_usd

        pip_value = float(pip_value_per_lot or 0.0)
        if pip_value <= 0:
            pip_value = 10.0
            logger.warning(
                f"{symbol}: Missing pip_value_per_lot, falling back to {pip_value:.2f}. "
                "This can reduce risk precision on non-USD quote pairs."
            )

        raw_lot = risk_amount / (stop_pips * pip_value)
        if raw_lot <= 0:
            return 0.0

        lot = raw_lot

        if volume_max is not None:
            lot = min(lot, float(volume_max))

        lot = self._round_lot_to_step(lot, volume_step)

        if volume_min is not None and lot < float(volume_min):
            min_lot = self._round_lot_to_step(float(volume_min), volume_step)
            min_risk = min_lot * stop_pips * pip_value
            if min_risk > risk_amount * 1.01:
                logger.warning(
                    f"SKIP_SNIPER_SL_CAP: symbol={sym} reason=SL_TOO_WIDE_USD "
                    f"min_lot={min_lot} min_risk={min_risk:.2f} allowed={risk_amount:.2f}"
                )
                return 0.0
            lot = min_lot

        lot = max(0.0, lot)

        final_risk = lot * stop_pips * pip_value
        if capped_budget_usd > 0 and final_risk > capped_budget_usd:
            lot_cap = self._round_lot_to_step(capped_budget_usd / (stop_pips * pip_value), volume_step)
            if volume_min is not None and lot_cap < float(volume_min):
                logger.warning(
                    f"SKIP_SNIPER_SL_CAP: symbol={sym} reason=LOT_CAP_BELOW_MIN "
                    f"lot_cap={lot_cap:.2f} min_lot={float(volume_min):.2f} cap={capped_budget_usd:.2f}"
                )
                return 0.0
            lot = max(0.0, min(lot, lot_cap))
            final_risk = lot * stop_pips * pip_value
            if capped_budget_usd > 0 and final_risk > (capped_budget_usd * 1.001):
                logger.warning(
                    f"SKIP_SNIPER_SL_CAP: symbol={sym} reason=FINAL_RISK_ABOVE_CAP "
                    f"final_risk={final_risk:.2f} cap={capped_budget_usd:.2f}"
                )
                return 0.0
        target_stop = self.target_stop_pips.get(symbol, None)
        extra = f" (target stop {target_stop}p)" if target_stop is not None else ""

        logger.info(
            f"{symbol} | Stop: {stop_pips:.1f}p{extra} | Lot: {lot:.2f} | "
            f"Risk: ${final_risk:.2f} (target ${risk_amount:.2f}) | Conf: {confidence:.0%}"
        )

        if target_stop and stop_pips > target_stop * 1.5:
            logger.warning(f"{symbol}: Stop wider than target ({stop_pips:.1f}p vs {target_stop}p)")

        return lot

    def _get_pip_size(self, symbol: str) -> float:
        s = symbol.upper()
        if "JPY" in s:
            return 0.01
        if s in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in s:
            return 0.1
        return 0.0001

    def _soft_sl_cfg_for_symbol(self, symbol: str) -> dict:
        sym = str(symbol or "").upper()
        block = self.exec_cfg.get("soft_sl_cap", {})
        if not isinstance(block, dict):
            return {"enabled": False}
        default_cfg = block.get("default", {}) if isinstance(block.get("default", {}), dict) else {}
        per_all = block.get("per_symbol", {}) if isinstance(block.get("per_symbol", {}), dict) else {}
        per_cfg = per_all.get(sym, {}) if isinstance(per_all.get(sym, {}), dict) else {}
        out = {**default_cfg, **per_cfg}
        out["enabled"] = bool(block.get("enabled", False))
        return out

    def compute_risk_multiplier(self, symbol: str, sl_pips: float) -> float:
        if self._is_prop_mode():
            cap_cfg = self.exec_cfg.get("max_sl_pips", {})
            cap = 0.0
            if isinstance(cap_cfg, dict):
                cap = float(cap_cfg.get(str(symbol or "").upper(), 0.0) or 0.0)
            else:
                cap = float(cap_cfg or 0.0)
            soft_enabled = bool(self.exec_cfg.get("soft_sl_cap", False))
            if not soft_enabled or cap <= 0 or sl_pips <= 0:
                return 1.0
            scale = float(self.exec_cfg.get("soft_sl_cap_risk_scale", 1.0) or 1.0)
            allow_pct = max(0.0, float(self.exec_cfg.get("soft_sl_cap_allow_pct", 0.0) or 0.0))
            hard_limit = cap * (1.0 + allow_pct)
            if sl_pips > hard_limit:
                return 0.0
            if sl_pips > cap:
                return max(0.0, min(1.0, scale))
            return 1.0

        soft_cfg = self._soft_sl_cfg_for_symbol(symbol)
        if not bool(soft_cfg.get("enabled", False)):
            return 1.0
        cap = float(soft_cfg.get("max_sl_pips", 0.0) or 0.0)
        power = float(soft_cfg.get("risk_scale_power", 1.5) or 1.5)
        min_mult = float(soft_cfg.get("min_risk_pct_multiplier", 0.35) or 0.35)
        if cap <= 0 or sl_pips <= 0:
            return 1.0
        mult = (cap / float(sl_pips)) ** power
        mult = max(mult, min_mult)
        mult = min(mult, 1.0)
        return mult

    def _round_lot_to_step(self, lot: float, step: Optional[float]) -> float:
        if lot <= 0:
            return 0.0

        if step is None or step <= 0:
            return float(int(lot * 100)) / 100.0

        step = float(step)
        steps = int(lot / step)
        return steps * step

    def record_open(self, trade: dict, setup_type: str = "", setup_id: str = "", reason: str = ""):
        self._daily_trades += 1
        record = TradeRecord(
            ticket=trade["ticket"],
            symbol=trade["symbol"],
            direction=trade["type"],
            volume=trade["volume"],
            entry=trade["price"],
            sl=trade["sl"],
            tp=trade["tp"],
            open_time=trade["time"],
            setup_type=setup_type,
            setup_id=setup_id,
            reason=reason,
        )
        self.journal.append(record)
        logger.info(
            f"Trade #{trade['ticket']} recorded | {trade['type']} {trade['volume']} {trade['symbol']} "
            f"| setup_id={setup_id or '-'}"
        )

    def record_close(self, ticket: int, close_price: float, pnl: float):
        for r in self.journal:
            if r.ticket == ticket:
                r.close_time = self._now()
                r.close_price = close_price
                r.pnl = pnl
                outcome = "WIN" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
                self.on_trade_closed(
                    symbol=r.symbol,
                    outcome=outcome,
                    pnl=pnl,
                    exit_time=r.close_time,
                    ticket=ticket,
                    direction=r.direction,
                    setup_id=r.setup_id,
                )
                logger.info(
                    f"Trade #{ticket} closed | P&L: {pnl:+.2f} | Daily P&L: {self._daily_pnl:+.2f}"
                )
                return

    def get_guardrail_status(self) -> dict:
        now = self._now()
        per_symbol = {}
        symbols = set(self._cooldown_until_by_symbol.keys()) | set(self._loss_cooldown_until_by_symbol.keys())
        for sym in symbols:
            rem = 0
            rem = max(rem, self._seconds_remaining(self._cooldown_until_by_symbol.get(sym), now))
            rem = max(rem, self._seconds_remaining(self._loss_cooldown_until_by_symbol.get(sym), now))
            if rem > 0:
                per_symbol[sym] = rem

        thesis_cooldowns = {}
        for thesis, until in self._thesis_cooldown_until.items():
            rem = self._seconds_remaining(until, now)
            if rem > 0:
                thesis_cooldowns[thesis] = rem
        thesis_risk_scales = {}
        for thesis, until in self._thesis_risk_scale_until.items():
            rem = self._seconds_remaining(until, now)
            if rem > 0:
                thesis_risk_scales[thesis] = {
                    "seconds_remaining": rem,
                    "risk_scale": self._single_loss_risk_scale(),
                }

        prop_mode = self._guardrails_enabled()
        return {
            "mode": str(self.mode_cfg.get("type", "normal")).lower(),
            "cooldowns_per_symbol_seconds": per_symbol,
            "cooldowns_per_thesis_seconds": thesis_cooldowns,
            "thesis_risk_scales": thesis_risk_scales,
            "consecutive_losses": int(self._consecutive_losses),
            "global_throttle_seconds_remaining": self._seconds_remaining(self._global_throttle_until, now),
            "loss_streak_cooldown_seconds_remaining": self._seconds_remaining(self._pause_until, now),
            "drawdown_pct": round(float(self._last_drawdown_pct), 4),
            "drawdown_limit_pct": float(self.cfg.get("max_cumulative_loss_pct", 0.0)),
            "prop_mode_enabled": prop_mode,
            "daily_profit_lock_active": bool(self._prop_daily_lock_until and now < self._prop_daily_lock_until),
            "daily_profit_lock_remaining_seconds": self._seconds_remaining(self._prop_daily_lock_until, now),
            "loss_pause_active": bool(self._prop_loss_pause_until and now < self._prop_loss_pause_until),
            "loss_pause_remaining_seconds": self._seconds_remaining(self._prop_loss_pause_until, now),
            "loss_streak_stop_day_active": bool(self._prop_stop_for_day_until and now < self._prop_stop_for_day_until),
            "loss_streak_stop_day_remaining_seconds": self._seconds_remaining(self._prop_stop_for_day_until, now),
            "total_open_risk_estimate_pct": round(float(self._total_open_risk_estimate_pct), 4),
            "start_day_ref": self._start_day_ref,
            "start_day_floor": self._start_day_floor,
            "close_all_on_daily_loss_breach": self._close_all_on_daily_loss_breach(),
        }

    def get_stats(self) -> dict:
        closed = [r for r in self.journal if r.close_time is not None]
        if not closed:
            return {
                "trades": 0,
                "winrate": 0,
                "total_pnl": 0,
                "daily_pnl": self._daily_pnl,
                "daily_trades": self._daily_trades,
            }

        wins = [r for r in closed if r.pnl > 0]
        losses = [r for r in closed if r.pnl < 0]
        total = sum(r.pnl for r in closed)

        avg_win = sum(r.pnl for r in wins) / len(wins) if wins else 0
        avg_loss = sum(r.pnl for r in losses) / len(losses) if losses else 0
        expectancy = (
            (len(wins) / len(closed) * avg_win) +
            (len(losses) / len(closed) * avg_loss)
        ) if closed else 0

        return {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "winrate": round(len(wins) / len(closed) * 100, 1),
            "total_pnl": round(total, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "expectancy": round(expectancy, 2),
            "daily_trades": self._daily_trades,
            "consecutive_losses": self._consecutive_losses,
        }
