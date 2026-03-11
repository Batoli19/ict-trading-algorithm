"""
Backtester — Historical Strategy Simulation Engine
════════════════════════════════════════════════════
Replays historical candle data through the ICT strategy pipeline to test
strategy performance WITHOUT needing a live MT5 connection.

Architecture:
    CandleReplay     — Loads CSV files and provides sliding candle windows
    SimulatedTrade   — Tracks a single simulated trade from entry to exit
    BacktestEngine   — Orchestrates the full simulation loop

Signal Flow (same as live, but with simulated data):
    1. CandleReplay provides candle windows at each timestamp
    2. ICTStrategy.analyze() detects signals from those candles
    3. SniperFilter.evaluate() validates signal quality
    4. If signal passes → create SimulatedTrade
    5. Each subsequent bar checks if SL or TP is hit
    6. Record results for reporting

Usage:
    from backtester import BacktestEngine
    engine = BacktestEngine(config, data_dir="backtest_data")
    results = engine.run()
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Import the existing strategy and filter modules — these are reused
# exactly as they run in live trading, with no modifications.
from ict_strategy import ICTStrategy, Signal, Direction, SetupType

logger = logging.getLogger("BACKTEST")


# ═══════════════════════════════════════════════════════════════════════════
# SimulatedTrade — represents a single trade during backtesting
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SimulatedTrade:
    """
    A simulated trade that tracks entry to exit during backtesting.

    Fields:
        symbol:       The trading instrument (e.g. "EURUSD")
        direction:    "BUY" or "SELL"
        setup_type:   Which ICT setup triggered the entry (e.g. "FVG", "STOP_HUNT")
        entry_price:  The price at which the trade was entered
        sl_price:     Stop-loss price
        tp_price:     Take-profit price
        confidence:   Signal confidence score (0.0 to 1.0)
        entry_time:   When the trade was opened
        exit_time:    When the trade was closed (None if still open)
        exit_price:   Price at which the trade exited (None if still open)
        exit_reason:  Why the trade closed ("TP_HIT", "SL_HIT", "END_OF_DATA")
        pnl_pips:     Profit/loss in pips
        rr_achieved:  Actual risk-reward ratio achieved
        killzone:     Which kill zone the entry was in (e.g. "LONDON_OPEN")
        htf_bias:     Higher timeframe bias at entry time
        sniper_passed: Whether the sniper filter was applied and passed
    """
    symbol: str
    direction: str
    setup_type: str
    entry_price: float
    sl_price: float
    tp_price: float
    confidence: float
    entry_time: datetime
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_pips: float = 0.0
    rr_achieved: float = 0.0
    killzone: str = "NONE"
    htf_bias: str = "NEUTRAL"
    sniper_passed: bool = False
    sniper_skip_reason: str = ""
    original_sl: float = 0.0       # Original SL at entry (before trailing)
    trail_count: int = 0           # Number of times SL was trailed
    be_applied: bool = False       # Whether breakeven was applied
    partial_taken: bool = False
    half_rr_price: float = 0.0
    peak_r: float = 0.0            # Highest R-multiple reached during the trade
    activated_giveback: bool = False # Whether the giveback guard has been activated

    @property
    def is_open(self) -> bool:
        """Check if this trade is still open (no exit yet)."""
        return self.exit_time is None

    @property
    def is_winner(self) -> bool:
        """Check if this trade was profitable."""
        return self.pnl_pips > 0

    @property
    def risk_pips(self) -> float:
        """Calculate the distance from entry to stop-loss in pips."""
        return abs(self.entry_price - self.sl_price)


# ═══════════════════════════════════════════════════════════════════════════
# CandleReplay — loads CSVs and provides sliding windows of candle data
# ═══════════════════════════════════════════════════════════════════════════

class CandleReplay:
    """
    Loads historical candle data from CSV files and provides time-aligned
    sliding windows for multi-timeframe analysis.

    This mimics what MT5Connector.get_candles() returns during live trading:
    for any given point in time, it returns the last N candles that would
    have been available.

    Example:
        replay = CandleReplay("backtest_data")
        replay.load("EURUSD")

        # Get the last 300 H4 candles available at a specific time
        candles_h4 = replay.get_candles("EURUSD", "H4", timestamp, count=300)
    """

    def __init__(self, data_dir: str | Path):
        """
        Args:
            data_dir: Path to the directory containing CSV files
                      (e.g. "backtest_data/")
        """
        self.data_dir = Path(data_dir)
        # Storage: { "EURUSD_M5": [list of candle dicts], ... }
        self._data: Dict[str, List[dict]] = {}
        # Pre-built time index for fast lookups
        # { "EURUSD_M5": [datetime, datetime, ...], ... }
        self._time_index: Dict[str, List[datetime]] = {}

    def load(self, symbol: str, timeframes: List[str] = None):
        """
        Load CSV data for a symbol across all required timeframes.

        Args:
            symbol:     e.g. "EURUSD"
            timeframes: List of timeframes to load (default: all required)
        """
        if timeframes is None:
            timeframes = ["H4", "H1", "M15", "M5", "M1"]

        for tf in timeframes:
            key = f"{symbol}_{tf}"
            csv_path = self.data_dir / f"{key}.csv"

            if not csv_path.exists():
                logger.warning(f"CSV not found: {csv_path}")
                continue

            candles = []
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Parse each CSV row into the same dict format that
                    # MT5Connector.get_candles() returns. The strategy code
                    # expects dicts with these exact keys.
                    candle = {
                        "time": datetime.strptime(
                            row["time"], "%Y-%m-%d %H:%M:%S"
                        ).replace(tzinfo=timezone.utc),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "tick_volume": int(float(row.get("tick_volume", 0))),
                    }
                    candles.append(candle)

            # Store the candles sorted by time (oldest first, newest last)
            candles.sort(key=lambda c: c["time"])
            self._data[key] = candles

            # Build a time index for fast binary search lookups
            self._time_index[key] = [c["time"] for c in candles]

            logger.info(f"Loaded {len(candles):,} candles: {key}")

        # ─── Fallback: synthesize M5/M1 from M15 if missing ───────────
        # MT5 demo often lacks M5/M1 for some symbols. Use M15 data as
        # a conservative stand-in so the backtester can still run.
        m15_key = f"{symbol}_M15"
        for fallback_tf in ("M5", "M1"):
            fb_key = f"{symbol}_{fallback_tf}"
            if fb_key not in self._data and m15_key in self._data:
                self._data[fb_key] = list(self._data[m15_key])
                self._time_index[fb_key] = list(self._time_index[m15_key])
                logger.info(
                    f"Synthesized {fb_key} from M15 data "
                    f"({len(self._data[fb_key]):,} candles)"
                )

    def get_candles(
        self,
        symbol: str,
        timeframe: str,
        current_time: datetime,
        count: int = 300,
    ) -> List[dict]:
        """
        Get the last `count` candles available at `current_time`.

        This simulates what MT5Connector.get_candles() would return if you
        called it at `current_time` — you only see candles that have already
        closed (no future data leak).

        Args:
            symbol:       e.g. "EURUSD"
            timeframe:    e.g. "M5"
            current_time: The simulated "now" (UTC datetime)
            count:        How many candles to return (default 300)

        Returns:
            List of candle dicts, oldest first, newest last.
            Returns empty list if no data available.
        """
        key = f"{symbol}_{timeframe}"
        times = self._time_index.get(key)
        candles = self._data.get(key)

        if not times or not candles:
            return []

        # Ensure current_time is timezone-aware for comparison
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)

        # Binary search to find the last candle that started AT or BEFORE
        # current_time. We use this instead of the candle's close time
        # because the strategy code looks at the last completed candle's
        # OHLC values. The candle at index i has its open time at times[i],
        # and its close time is times[i] + timeframe_duration.
        #
        # To avoid future data leak, we only include candles whose OPEN time
        # is strictly before current_time (the candle at current_time is
        # still forming and shouldn't be included).
        right = self._bisect_right(times, current_time)

        if right == 0:
            return []

        # Return the last `count` candles up to (but not including) current bar
        start = max(0, right - count)
        return candles[start:right]

    def get_m5_timeline(self, symbol: str) -> List[datetime]:
        """
        Get the full list of M5 bar timestamps for a symbol.

        The backtester iterates through these timestamps one by one,
        checking for signals at each M5 bar (the trigger timeframe).

        Returns:
            Sorted list of M5 candle open times.
        """
        key = f"{symbol}_M5"
        return list(self._time_index.get(key, []))

    def _bisect_right(self, sorted_times: List[datetime], target: datetime) -> int:
        """
        Binary search: find the index where `target` would be inserted
        to keep the list sorted (rightmost position).

        All items at index < result have time <= target.

        This is a manual implementation to avoid importing bisect
        (which doesn't play well with datetime comparison in all cases).
        """
        lo, hi = 0, len(sorted_times)
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_times[mid] <= target:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def get_loaded_symbols(self) -> List[str]:
        """Get list of symbols that have been loaded."""
        symbols = set()
        for key in self._data:
            # Key format is "EURUSD_M5" — extract the symbol part
            symbol = key.rsplit("_", 1)[0]
            symbols.add(symbol)
        return sorted(symbols)


# ═══════════════════════════════════════════════════════════════════════════
# BacktestEngine — orchestrates the full simulation
# ═══════════════════════════════════════════════════════════════════════════

class BacktestEngine:
    """
    Main backtesting engine that replays historical data through the
    ICT strategy pipeline.

    How it works:
        1. Loads candle data from CSV files (via CandleReplay)
        2. Iterates through each M5 bar chronologically
        3. At each bar, calls ICTStrategy.analyze() with the available candles
        4. If a signal is found, optionally runs SniperFilter.evaluate()
        5. If the signal passes, opens a SimulatedTrade
        6. Checks all open trades against each bar's high/low for SL/TP hits
        7. Collects all trade results for reporting

    Usage:
        engine = BacktestEngine(config)
        results = engine.run()
        # results is a list of SimulatedTrade objects
    """

    def __init__(
        self,
        config: dict,
        data_dir: str | Path = None,
        use_sniper_filter: bool = True,
        max_open_trades: int = 3,
        one_trade_per_symbol: bool = True,
        signal_cooldown_bars: int = 6,
        disabled_setups: List[str] = None,
        killzone_only: bool = None,
        learner=None,
        use_trailing: bool = True,
    ):
        """
        Args:
            config:              The bot's settings dict (from settings.json)
            data_dir:            Path to CSV data directory (default: backtest_data/)
            use_sniper_filter:   Whether to run SniperFilter on signals (default: True)
            max_open_trades:     Maximum simultaneous open trades (default: 3)
            one_trade_per_symbol: Only allow one trade per symbol at a time (default: True)
            signal_cooldown_bars: Minimum M5 bars between signals on same symbol (default: 6 = 30min)
            disabled_setups:     List of setup types to skip (e.g. ["PIN_BAR"])
            killzone_only:       Only allow trades inside kill zones (default: from config)
            learner:             Optional BacktestLearner for adaptive rule filtering
        """
        self.config = config
        self.use_sniper_filter = use_sniper_filter
        self.max_open_trades = max_open_trades
        self.one_trade_per_symbol = one_trade_per_symbol
        self.signal_cooldown_bars = signal_cooldown_bars

        # Disabled setups — read from config if not explicitly provided
        if disabled_setups is not None:
            self.disabled_setups = set(s.upper() for s in disabled_setups)
        else:
            cfg_disabled = config.get("disabled_setups", [])
            self.disabled_setups = set(s.upper() for s in cfg_disabled)

        # Per-symbol disabled setups (execution.per_symbol.<SYM>.disabled_setups)
        self.disabled_setups_by_symbol: Dict[str, set] = {}
        exec_cfg = config.get("execution", {})
        per_symbol_cfg = exec_cfg.get("per_symbol", {}) if isinstance(exec_cfg.get("per_symbol", {}), dict) else {}
        for sym, cfg in per_symbol_cfg.items():
            if not isinstance(cfg, dict):
                continue
            ds = cfg.get("disabled_setups")
            if ds:
                self.disabled_setups_by_symbol[str(sym).upper()] = set(s.upper() for s in ds)

        # Kill zone enforcement — read from config if not explicitly provided
        if killzone_only is not None:
            self.killzone_only = killzone_only
        else:
            self.killzone_only = config.get("execution", {}).get("enforce_killzones", False)

        # Optional adaptive learner (for 2-pass learning)
        self.learner = learner

        # Trailing stop simulation — reuse the real StructureTrailingManager
        self.use_trailing = use_trailing
        self.trailing_manager = None
        if use_trailing:
            try:
                from trailing_manager import StructureTrailingManager
                self.trailing_manager = StructureTrailingManager(config)
                logger.info("Trailing manager: ON (structure-based trailing)")
            except ImportError:
                logger.warning("TrailingManager not available — running without trailing")
                self.use_trailing = False

        # Default data directory is backtest_data/ in the project root
        if data_dir is None:
            data_dir = Path(__file__).resolve().parent.parent / "backtest_data"
        self.data_dir = Path(data_dir)

        # Initialize the ICT strategy using the same config as live trading
        self.strategy = ICTStrategy(config)

        # Initialize the sniper filter if requested
        self.sniper_filter = None
        if use_sniper_filter:
            try:
                from sniper_filter import SniperFilter
                self.sniper_filter = SniperFilter(config)
            except ImportError:
                logger.warning("SniperFilter not available — running without it")

        # Candle data loader
        self.replay = CandleReplay(self.data_dir)

        # Results storage
        self.trades: List[SimulatedTrade] = []
        self.signals_generated: int = 0
        self.signals_filtered: int = 0

        # Per-symbol cooldown tracking (prevents re-entry spam)
        # Maps symbol -> last signal bar index
        self._last_signal_bar: Dict[str, int] = {}

        # Stats for the "what if" comparison (signals that were filtered out)
        self.filtered_signals: List[dict] = []

    def run(
        self,
        symbols: List[str] = None,
        start_date: datetime = None,
        end_date: datetime = None,
        progress_every: int = 5000,
    ) -> List[SimulatedTrade]:
        """
        Run the full backtest simulation.

        Args:
            symbols:        List of symbols to test (default: from config)
            start_date:     Start of backtest period (default: start of data)
            end_date:       End of backtest period (default: end of data)
            progress_every: Log progress every N bars (default: 5000)

        Returns:
            List of all SimulatedTrade results (open + closed).
        """
        # Determine which symbols to test
        if symbols is None:
            symbols = self.config.get("pairs", [])

        logger.info(f"Starting backtest for {len(symbols)} symbols: {symbols}")
        logger.info(f"Sniper filter: {'ON' if self.use_sniper_filter else 'OFF'}")
        logger.info(f"Kill zone only: {'ON' if self.killzone_only else 'OFF'}")
        if self.disabled_setups:
            logger.info(f"Disabled setups: {sorted(self.disabled_setups)}")
        if self.learner:
            logger.info(f"Adaptive learner: ON ({len(getattr(self.learner, 'rules', []))} rules)")
        logger.info(f"Trailing stops: {'ON' if self.use_trailing else 'OFF'}")
        logger.info(f"Max open trades: {self.max_open_trades}")

        # True ICT Timing Debug Print
        kz_cfg = self.config.get("ict", {}).get("kill_zones", {})
        lo = kz_cfg.get("london_open", {})
        ny = kz_cfg.get("ny_open", {})
        lc = kz_cfg.get("london_close", {})
        logger.info(f"KZ_TIMES: london_open={lo.get('start', '07:00')}-{lo.get('end', '10:00')} "
                    f"ny_open={ny.get('start', '12:00')}-{ny.get('end', '15:00')} "
                    f"london_close={lc.get('start', '15:00')}-{lc.get('end', '17:00')}")

        # ─── Load all candle data from CSV files ───────────────────────
        for symbol in symbols:
            self.replay.load(symbol)

        # ─── Process each symbol ───────────────────────────────────────
        for symbol in symbols:
            self._run_symbol(symbol, start_date, end_date, progress_every)

        # ─── Close any remaining open trades at end of data ────────────
        for trade in self.trades:
            if trade.is_open:
                trade.exit_reason = "END_OF_DATA"
                trade.exit_time = datetime.now(tz=timezone.utc)
                trade.exit_price = trade.entry_price  # Flat close
                trade.pnl_pips = 0.0
                trade.rr_achieved = 0.0

        # ─── Summary ──────────────────────────────────────────────────
        total = len(self.trades)
        winners = sum(1 for t in self.trades if t.is_winner)
        losers = sum(1 for t in self.trades if not t.is_winner and t.exit_reason != "END_OF_DATA")

        logger.info(f"")
        logger.info(f"═══ Backtest Complete ═══")
        logger.info(f"  Total trades:      {total}")
        logger.info(f"  Winners:           {winners}")
        logger.info(f"  Losers:            {losers}")
        logger.info(f"  Signals generated: {self.signals_generated}")
        logger.info(f"  Signals filtered:  {self.signals_filtered}")
        if total > 0:
            logger.info(f"  Win rate:          {winners/total*100:.1f}%")
            total_pips = sum(t.pnl_pips for t in self.trades)
            logger.info(f"  Total PnL (pips):  {total_pips:+.1f}")

        return self.trades

    def _run_symbol(
        self,
        symbol: str,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        progress_every: int,
    ):
        """
        Run the backtest for a single symbol by iterating through its
        M5 timeline bar by bar.
        """
        # Get the M5 timeline (list of all M5 bar timestamps)
        timeline = self.replay.get_m5_timeline(symbol)
        if not timeline:
            logger.warning(f"No M5 data for {symbol} — skipping")
            return

        # Apply date filters if provided
        if start_date:
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            timeline = [t for t in timeline if t >= start_date]
        if end_date:
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            timeline = [t for t in timeline if t <= end_date]

        if not timeline:
            logger.warning(f"No M5 bars in date range for {symbol} — skipping")
            return

        logger.info(
            f"Processing {symbol}: {len(timeline):,} M5 bars "
            f"({timeline[0].date()} to {timeline[-1].date()})"
        )

        # Track open trades for this symbol
        open_trades_this_symbol: List[SimulatedTrade] = []

        # ─── Main simulation loop: iterate through each M5 bar ────────
        for bar_idx, current_time in enumerate(timeline):

            # Log progress periodically so the user knows it's running
            if bar_idx > 0 and bar_idx % progress_every == 0:
                logger.info(
                    f"  {symbol}: bar {bar_idx:,}/{len(timeline):,} "
                    f"({current_time.date()}) | "
                    f"trades={len(self.trades)} open={len(open_trades_this_symbol)}"
                )

            # ─── Step 1: Check open trades for SL/TP hits ─────────────
            # Get the CURRENT M5 bar's high/low to check if any open
            # trade's SL or TP was hit during this bar.
            current_m5 = self.replay.get_candles(symbol, "M5", current_time, count=1)
            if current_m5:
                bar = current_m5[-1]  # The most recent (current) bar
                self._check_exits(open_trades_this_symbol, bar, current_time, symbol)

            # Remove closed trades from the open list
            open_trades_this_symbol = [t for t in open_trades_this_symbol if t.is_open]

            # ─── Step 2: Check for new signals ────────────────────────
            # Skip if we already have a trade open on this symbol
            if self.one_trade_per_symbol and open_trades_this_symbol:
                continue

            # Skip if we've hit the max open trades limit
            total_open = sum(1 for t in self.trades if t.is_open)
            if total_open >= self.max_open_trades:
                continue

            # Skip if we're in the signal cooldown period for this symbol
            last_bar = self._last_signal_bar.get(symbol, -999)
            if bar_idx - last_bar < self.signal_cooldown_bars:
                continue

            # ─── Step 3: Get candle data windows ──────────────────────
            # Retrieve the same candle windows that the live bot would get
            candles_h4 = self.replay.get_candles(symbol, "H4", current_time, count=300)
            candles_h1 = self.replay.get_candles(symbol, "H1", current_time, count=300)
            candles_m15 = self.replay.get_candles(symbol, "M15", current_time, count=200)
            candles_m5 = self.replay.get_candles(symbol, "M5", current_time, count=100)
            candles_m1 = self.replay.get_candles(symbol, "M1", current_time, count=100)

            # Need minimum data to analyze
            if not candles_h4 or len(candles_h4) < 30:
                continue
            if not candles_m15 or len(candles_m15) < 20:
                continue
            if not candles_m5 or len(candles_m5) < 10:
                continue

            # ─── Step 4: Run ICT strategy analysis ────────────────────
            # This calls the EXACT same analyze() method used in live trading
            spread_pips = 1.5  # Simulated spread (reasonable for majors)
            signal = self.strategy.analyze(
                symbol, candles_h4, candles_m15, candles_m5,
                candles_m1 if candles_m1 else [], spread_pips
            )

            if not signal or not signal.valid:
                continue

            self.signals_generated += 1
            setup_name = str(getattr(signal.setup_type, "value", signal.setup_type))
            direction = str(getattr(signal.direction, "value", signal.direction))

            # ─── Step 5a: Check disabled setups ────────────────────────
            # Uses prefix matching: disabling "FVG" blocks "FVG", "FVG_CONTINUATION",
            # "FVG_ENTRY", etc. Exact matches still work as before.
            def _is_setup_disabled(name: str, disabled: set) -> bool:
                name_up = name.upper()
                return any(
                    name_up == d or name_up.startswith(d + "_")
                    for d in disabled
                )

            disabled = set(self.disabled_setups)
            sym_key = str(symbol).upper()
            if sym_key in self.disabled_setups_by_symbol:
                disabled |= self.disabled_setups_by_symbol[sym_key]

            if _is_setup_disabled(setup_name, disabled):
                self.signals_filtered += 1
                self.filtered_signals.append({
                    "symbol": symbol, "time": current_time.isoformat(),
                    "setup_type": setup_name, "direction": direction,
                    "confidence": signal.confidence,
                    "skip_reason": f"DISABLED_SETUP:{setup_name}",
                    "entry": signal.entry, "sl": signal.sl, "tp": signal.tp,
                })
                continue
                
            direction_filters = self.config.get("direction_filters", {})
            if sym_key in direction_filters:
                allowed_dirs = direction_filters[sym_key]
                if direction not in allowed_dirs:
                    self.signals_filtered += 1
                    self.filtered_signals.append({
                        "symbol": symbol, "time": current_time.isoformat(),
                        "setup_type": setup_name, "direction": direction,
                        "confidence": signal.confidence,
                        "skip_reason": f"FILTERED_DIRECTION:{direction}",
                        "entry": signal.entry, "sl": signal.sl, "tp": signal.tp,
                    })
                    continue

            # ─── Step 5b: Kill zone enforcement ────────────────────────
            in_kz, kz_name = self.strategy.in_kill_zone(current_time)
            kz_name = kz_name or "NONE"
            
            kz_filters = self.config.get("killzone_pair_filters", {})
            if sym_key in kz_filters:
                allowed_kzs = kz_filters[sym_key]
                if kz_name not in allowed_kzs:
                    self.signals_filtered += 1
                    self.filtered_signals.append({
                        "symbol": symbol, "time": current_time.isoformat(),
                        "setup_type": setup_name, "direction": direction,
                        "confidence": signal.confidence,
                        "skip_reason": f"FILTERED_KILLZONE:{kz_name}",
                        "entry": signal.entry, "sl": signal.sl, "tp": signal.tp,
                    })
                    continue

            if self.killzone_only and not in_kz:
                self.signals_filtered += 1
                self.filtered_signals.append({
                    "symbol": symbol, "time": current_time.isoformat(),
                    "setup_type": setup_name, "direction": direction,
                    "confidence": signal.confidence,
                    "skip_reason": "OUTSIDE_KILLZONE",
                    "entry": signal.entry, "sl": signal.sl, "tp": signal.tp,
                })
                continue

            # ─── Step 5c: Adaptive learner check ───────────────────────
            if self.learner:
                conditions = {
                    "symbol": symbol, "setup_type": setup_name,
                    "direction": direction, "killzone": kz_name,
                    "htf_bias": str(getattr(self.strategy.get_htf_bias(candles_h4), "value", "NEUTRAL")),
                    "confidence": signal.confidence,
                    "entry": signal.entry, "sl": signal.sl, "tp": signal.tp,
                }
                should_skip, skip_reason = self.learner.should_skip(signal, conditions)
                if should_skip:
                    self.signals_filtered += 1
                    self.filtered_signals.append({
                        **conditions, "time": current_time.isoformat(),
                        "skip_reason": f"LEARNER:{skip_reason}",
                    })
                    continue

            # ─── Step 5d: Run sniper filter (optional) ─────────────────
            sniper_passed = True
            sniper_skip = ""
            sniper_metrics = None

            if self.sniper_filter and self.sniper_filter.enabled():
                try:
                    passed, reason, metrics = self.sniper_filter.evaluate(
                        signal=signal,
                        symbol=symbol,
                        candles_m5=candles_m5,
                        candles_m15=candles_m15,
                        candles_h4=candles_h4,
                        candles_h1=candles_h1 if candles_h1 else None,
                        killzone=kz_name,
                        in_killzone=in_kz,
                    )
                    sniper_passed = passed
                    sniper_skip = reason if not passed else ""
                    sniper_metrics = metrics
                except Exception as e:
                    logger.debug(f"Sniper filter error: {e}")
                    sniper_passed = True  # Allow trade on filter error

            if not sniper_passed:
                self.signals_filtered += 1
                self.filtered_signals.append({
                    "symbol": symbol, "time": current_time.isoformat(),
                    "setup_type": setup_name, "direction": direction,
                    "confidence": signal.confidence,
                    "skip_reason": sniper_skip,
                    "entry": signal.entry, "sl": signal.sl, "tp": signal.tp,
                })
                continue

            # ─── Step 6: Create simulated trade ───────────────────────
            # setup_name, direction, kz_name already set above

            # Get HTF bias for the trade record
            htf_bias = str(getattr(
                self.strategy.get_htf_bias(candles_h4), "value", "NEUTRAL"
            ))

            half_rr_price = signal.entry + (signal.tp - signal.entry) / 2.0

            trade = SimulatedTrade(
                symbol=symbol,
                direction=direction,
                setup_type=setup_name,
                entry_price=signal.entry,
                sl_price=signal.sl,
                tp_price=signal.tp,
                confidence=signal.confidence,
                entry_time=current_time,
                killzone=kz_name or "NONE",
                htf_bias=htf_bias,
                sniper_passed=sniper_passed,
                sniper_skip_reason=sniper_skip,
                half_rr_price=half_rr_price,
                peak_r=0.0,
                activated_giveback=False
            )

            self.trades.append(trade)
            open_trades_this_symbol.append(trade)
            self._last_signal_bar[symbol] = bar_idx

            logger.debug(
                f"ENTRY: {symbol} {direction} {setup_name} "
                f"@ {signal.entry:.5f} SL={signal.sl:.5f} TP={signal.tp:.5f} "
                f"conf={signal.confidence:.2f} kz={kz_name}"
            )

    def _check_exits(
        self,
        open_trades: List[SimulatedTrade],
        bar: dict,
        current_time: datetime,
        symbol: str = "",
    ):
        """
        Check if any open trades should be closed based on the current bar's
        high and low prices. If trailing stops are enabled, update SL first.

        For each open trade:
            1. Apply trailing stop (moves SL toward price if conditions met)
            2. If price hits SL → close as a loss (or breakeven/profit if trailed)
            3. If price hits TP → close as a win
            4. If both SL and TP hit in same bar → assume SL hit first (conservative)
        """
        bar_high = bar["high"]
        bar_low = bar["low"]
        bar_close = bar.get("close", (bar_high + bar_low) / 2.0)

        for trade in open_trades:
            if not trade.is_open:
                continue

            # ─── Apply trailing stop before checking exits ─────────────
            if self.trailing_manager and self.use_trailing:
                # Check trail_only_after_tp1 gate
                mgmt_cfg = self.config.get("trade_management", {})
                part_cfg = mgmt_cfg.get("partials", {})
                trail_only_after_tp1 = bool(part_cfg.get("trail_only_after_tp1", False))

                # Only trail if: (a) gate is disabled, OR (b) partial already taken
                if not trail_only_after_tp1 or trade.partial_taken:
                    try:
                        # Build position dict in the format trailing_manager expects
                        position = {
                            "ticket": id(trade),  # Unique ID for state tracking
                            "symbol": trade.symbol,
                            "type": trade.direction,
                            "open_price": trade.entry_price,
                            "sl": trade.sl_price,
                            "tp": trade.tp_price,
                            "open_time": trade.entry_time,
                        }

                        # Get candle windows for trailing analysis
                        candles_m5 = self.replay.get_candles(
                            trade.symbol, "M5", current_time, count=50
                        )
                        candles_m1 = self.replay.get_candles(
                            trade.symbol, "M1", current_time, count=50
                        )

                        # Simulate bid/ask from bar close (spread = 0 in backtest)
                        bid = bar_close
                        ask = bar_close

                        result = self.trailing_manager.evaluate_position(
                            position=position,
                            candles_m5=candles_m5 or [],
                            candles_m1=candles_m1 or [],
                            bid=bid,
                            ask=ask,
                        )

                        new_sl = result.get("new_sl")
                        trail_reason = result.get("reason", "")

                        if new_sl is not None:
                            # Store original SL the first time we trail
                            if trade.original_sl == 0.0:
                                trade.original_sl = trade.sl_price

                            # Only move SL in the protective direction
                            if trade.direction == "BUY" and new_sl > trade.sl_price:
                                trade.sl_price = new_sl
                                trade.trail_count += 1
                                if trail_reason == "BE_PLUS":
                                    trade.be_applied = True
                                logger.debug(
                                    f"TRAIL: {trade.symbol} {trade.direction} "
                                    f"SL moved to {new_sl:.5f} ({trail_reason})"
                                )
                            elif trade.direction == "SELL" and new_sl < trade.sl_price:
                                trade.sl_price = new_sl
                                trade.trail_count += 1
                                if trail_reason == "BE_PLUS":
                                    trade.be_applied = True
                                logger.debug(
                                    f"TRAIL: {trade.symbol} {trade.direction} "
                                    f"SL moved to {new_sl:.5f} ({trail_reason})"
                                )
                    except Exception as e:
                        logger.debug(f"Trailing error for {trade.symbol}: {e}")

            # ─── Step 1b: Update Peak R for Giveback Guard ─────────────────
            # Determine current R-gain
            risk_dist = abs(trade.entry_price - (trade.original_sl or trade.sl_price))
            if risk_dist > 0:
                if trade.direction == "BUY":
                    r_now = (bar_close - trade.entry_price) / risk_dist
                else:
                    r_now = (trade.entry_price - bar_close) / risk_dist
                
                # Get giveback config
                mgmt_cfg = self.config.get("trade_management", {})
                gb_cfg = mgmt_cfg.get("giveback_guard", {})
                if gb_cfg.get("enabled", False):
                    activate_at_r = float(gb_cfg.get("activate_at_r", 1.2))
                    max_gb_pct = float(gb_cfg.get("max_giveback_pct", 0.60))

                    if r_now >= activate_at_r:
                        trade.activated_giveback = True
                        if r_now > trade.peak_r:
                            trade.peak_r = r_now

                    if trade.activated_giveback and trade.peak_r >= activate_at_r and trade.peak_r > 0:
                        giveback = max(0.0, (trade.peak_r - r_now) / trade.peak_r)
                        if giveback >= max_gb_pct:
                            trade.exit_time = current_time
                            trade.exit_price = bar_close
                            trade.exit_reason = "GIVEBACK_GUARD"
                            
                            # Calculate final Pips and R-multiple
                            pip_size = self._get_pip_size(trade.symbol)
                            if trade.direction == "BUY":
                                final_pips = (bar_close - trade.entry_price) / pip_size
                            else:
                                final_pips = (trade.entry_price - bar_close) / pip_size
                            
                            if trade.partial_taken:
                                mgmt_cfg_ = self.config.get("trade_management", {})
                                part_cfg_ = mgmt_cfg_.get("partials", {})
                                tp1_close_pct_ = float(part_cfg_.get("tp1_close_pct", 0.5))
                                trade.pnl_pips += final_pips * (1.0 - tp1_close_pct_)
                            else:
                                trade.pnl_pips += final_pips
                            
                            trade.rr_achieved = trade.pnl_pips * pip_size / risk_dist if risk_dist > 0 else 0
                            
                            logger.info(
                                f"GIVEBACK_GUARD EXIT: {trade.symbol} {trade.direction} "
                                f"pnl={trade.pnl_pips:+.1f} pips | peak_R={trade.peak_r:.2f} "
                                f"now_R={r_now:.2f} gb={giveback:.2f}"
                            )
                            continue  # Move to next trade

            # ─── Check SL/TP hits against the current bar ─────────────
            # Determine pip size for this symbol (for PnL calculation)
            pip_size = self._get_pip_size(trade.symbol)
            sl_hit = False
            tp_hit = False

            if trade.direction == "BUY":
                sl_hit = bar_low <= trade.sl_price
                tp_hit = bar_high >= trade.tp_price
            elif trade.direction == "SELL":
                sl_hit = bar_high >= trade.sl_price
                tp_hit = bar_low <= trade.tp_price

            if sl_hit and tp_hit:
                sl_hit = True
                tp_hit = False

            # ─── Partial TP (only if SL was NOT hit first) ─────────────
            # Reads from trade_management.partials config.
            # If partials are disabled or trade is too small, skips cleanly.
            if not sl_hit and not trade.partial_taken:
                mgmt_cfg   = self.config.get("trade_management", {})
                part_cfg   = mgmt_cfg.get("partials", {})
                partials_on = bool(part_cfg.get("enabled", False))

                if partials_on:
                    tp1_r         = float(part_cfg.get("tp1_r", 1.0))
                    tp1_close_pct = float(part_cfg.get("tp1_close_pct", 0.5))

                    # Compute the TP1 price from config R-multiple
                    risk_dist = abs(trade.entry_price - trade.sl_price)
                    if risk_dist > 0:
                        if trade.direction == "BUY":
                            tp1_price = trade.entry_price + risk_dist * tp1_r
                            tp1_hit   = bar_high >= tp1_price
                        else:
                            tp1_price = trade.entry_price - risk_dist * tp1_r
                            tp1_hit   = bar_low <= tp1_price
                    else:
                        tp1_hit = False

                    if tp1_hit:
                        trade.partial_taken = True
                        partial_pips = abs(tp1_price - trade.entry_price) / pip_size
                        trade.pnl_pips += partial_pips * tp1_close_pct

                        # Move SL per config (BE_PLUS, BREAKEVEN, or leave)
                        sl_mode = str(part_cfg.get("tp1_sl_mode", "BE_PLUS")).upper()
                        if sl_mode == "BE_PLUS":
                            be_r = float(part_cfg.get("tp1_be_plus_r", 0.05))
                            if trade.direction == "BUY":
                                new_sl = trade.entry_price + risk_dist * be_r
                                if new_sl > trade.sl_price:
                                    trade.sl_price = new_sl
                                    trade.be_applied = True
                            else:
                                new_sl = trade.entry_price - risk_dist * be_r
                                if new_sl < trade.sl_price:
                                    trade.sl_price = new_sl
                                    trade.be_applied = True
                        elif sl_mode == "BREAKEVEN":
                            if trade.direction == "BUY" and trade.entry_price > trade.sl_price:
                                trade.sl_price = trade.entry_price
                                trade.be_applied = True
                            elif trade.direction == "SELL" and trade.entry_price < trade.sl_price:
                                trade.sl_price = trade.entry_price
                                trade.be_applied = True

                        logger.debug(
                            f"PARTIAL TP1 HIT: {trade.symbol} {trade.direction} "
                            f"locked +{partial_pips * tp1_close_pct:.1f} pips "
                            f"({tp1_close_pct*100:.0f}% at {tp1_r}R) SL→{trade.sl_price:.5f}"
                        )

            if sl_hit:
                trade.exit_time = current_time
                trade.exit_price = trade.sl_price
                # If SL is above entry (BUY) or below entry (SELL), it's a trailed exit
                if trade.direction == "BUY":
                    final_pips = (trade.sl_price - trade.entry_price) / pip_size
                else:
                    final_pips = (trade.entry_price - trade.sl_price) / pip_size

                if trade.partial_taken:
                    # Remaining position size after partial
                    mgmt_cfg   = self.config.get("trade_management", {})
                    part_cfg   = mgmt_cfg.get("partials", {})
                    tp1_close_pct = float(part_cfg.get("tp1_close_pct", 0.5))
                    remaining_pct = 1.0 - tp1_close_pct
                    trade.pnl_pips += final_pips * remaining_pct
                else:
                    trade.pnl_pips += final_pips

                # Determine exit reason based on whether the SL was trailed
                if final_pips > 0 and trade.sl_price != trade.original_sl:
                    trade.exit_reason = "TRAILED_SL"  # Profitable exit via trailing
                elif trade.be_applied and abs(final_pips) < 2.0:
                    trade.exit_reason = "BREAKEVEN"    # Exited near breakeven
                else:
                    trade.exit_reason = "SL_HIT"      # Normal stop loss

                risk = abs(trade.entry_price - (trade.original_sl or trade.sl_price))
                trade.rr_achieved = trade.pnl_pips * pip_size / risk if risk > 0 else 0

                logger.debug(
                    f"EXIT {trade.exit_reason}: {trade.symbol} {trade.direction} "
                    f"{trade.setup_type} pnl={trade.pnl_pips:+.1f} pips "
                    f"(trailed {trade.trail_count}x)"
                )

            elif tp_hit:
                trade.exit_time = current_time
                trade.exit_price = trade.tp_price
                trade.exit_reason = "TP_HIT"

                if trade.direction == "BUY":
                    final_pips = (trade.tp_price - trade.entry_price) / pip_size
                else:
                    final_pips = (trade.entry_price - trade.tp_price) / pip_size

                if trade.partial_taken:
                    mgmt_cfg   = self.config.get("trade_management", {})
                    part_cfg   = mgmt_cfg.get("partials", {})
                    tp1_close_pct = float(part_cfg.get("tp1_close_pct", 0.5))
                    remaining_pct = 1.0 - tp1_close_pct
                    trade.pnl_pips += final_pips * remaining_pct
                else:
                    trade.pnl_pips += final_pips

                risk = abs(trade.entry_price - (trade.original_sl or trade.sl_price))
                trade.rr_achieved = trade.pnl_pips * pip_size / risk if risk > 0 else 0

                logger.debug(
                    f"EXIT TP: {trade.symbol} {trade.direction} {trade.setup_type} "
                    f"pnl={trade.pnl_pips:+.1f} pips"
                )

    def _get_pip_size(self, symbol: str) -> float:
        """
        Get the pip size for a trading symbol.

        Different instruments have different pip sizes:
            Forex pairs with JPY: 0.01  (e.g. USDJPY at 150.123 → pip = 0.01)
            Most forex pairs:     0.0001 (e.g. EURUSD at 1.08543 → pip = 0.0001)
            Gold (XAUUSD):        0.1    (e.g. gold at 2150.50 → pip = 0.1)
            Indices (US30, NAS):  1.0    (e.g. US30 at 39500 → pip = 1.0)
        """
        symbol_upper = symbol.upper()
        if "JPY" in symbol_upper:
            return 0.01
        if symbol_upper in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol_upper or "GOLD" in symbol_upper:
            return 0.1
        return 0.0001
