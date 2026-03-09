"""
Run Backtest — CLI Entry Point
════════════════════════════════
Simple command-line interface to run the ICT strategy backtester.

Usage:
    # Run with default settings (uses settings.json, data from backtest_data/)
    python run_backtest.py

    # Run with a custom config file
    python run_backtest.py --config ../config/backtest_config.json

    # Run without the sniper filter (to see raw signal performance)
    python run_backtest.py --no-sniper

    # Run on specific symbols only
    python run_backtest.py --symbols EURUSD GBPUSD

    # Run with a specific date range
    python run_backtest.py --start 2025-01-01 --end 2025-06-01

    # Export results to a specific CSV file
    python run_backtest.py --output my_results.csv

Full Pipeline:
    Step 1: Export candles (only needed once)
        python export_candles.py --months 6

    Step 2: Run backtest
        python run_backtest.py

    Step 3: Analyze results
        Open backtest_results.csv in Excel or Google Sheets
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─── Setup logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("RUN_BT")


def load_config(config_path: str = None) -> dict:
    """
    Load configuration for the backtest.

    Priority:
        1. Custom config file (if --config flag is provided)
        2. Backtest-specific config (config/backtest_config.json)
        3. Main settings.json (same config as live trading)

    The idea is that backtest_config.json can override specific settings
    (like disabling news filter) while inheriting everything else from
    the main settings.json.
    """
    # If a specific config path was provided, use that
    if config_path:
        path = Path(config_path)
        if not path.exists():
            logger.error(f"Config file not found: {path}")
            sys.exit(1)
        with open(path, "r") as f:
            return json.load(f)

    # Try backtest-specific config first
    project_root = Path(__file__).resolve().parent.parent
    bt_config_path = project_root / "config" / "backtest_config.json"
    main_config_path = project_root / "config" / "settings.json"

    if bt_config_path.exists():
        logger.info(f"Using backtest config: {bt_config_path}")
        with open(bt_config_path, "r") as f:
            bt_cfg = json.load(f)

        # If the backtest config has an "inherit_from" key, merge with main config
        if bt_cfg.get("inherit_from_main", False) and main_config_path.exists():
            with open(main_config_path, "r") as f:
                main_cfg = json.load(f)
            # Deep merge: backtest config overrides main config
            merged = _deep_merge(main_cfg, bt_cfg)
            return merged

        return bt_cfg

    # Fall back to main settings.json
    if main_config_path.exists():
        logger.info(f"Using main config: {main_config_path}")
        with open(main_config_path, "r") as f:
            return json.load(f)

    logger.error("No config file found!")
    sys.exit(1)


def _deep_merge(base: dict, override: dict) -> dict:
    """
    Deep merge two dictionaries. Values from `override` take precedence
    over values in `base`. Nested dicts are merged recursively.

    Example:
        base     = {"risk": {"max_open": 3, "trailing": true}}
        override = {"risk": {"max_open": 5}}
        result   = {"risk": {"max_open": 5, "trailing": true}}
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def main():
    """
    Main function — parses arguments, loads config, runs backtest,
    and prints/exports the performance report.
    """
    # ─── Parse command-line arguments ──────────────────────────────────
    parser = argparse.ArgumentParser(
        description="Run ICT Strategy Backtest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_backtest.py                          # Default run
  python run_backtest.py --no-sniper              # Skip sniper filter
  python run_backtest.py --symbols EURUSD XAUUSD  # Specific symbols
  python run_backtest.py --start 2025-01-01       # From date
        """
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Path to config JSON file (default: auto-detect)"
    )
    parser.add_argument(
        "--data-dir", type=str, default=None,
        help="Path to candle data directory (default: backtest_data/)"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Symbols to backtest (default: from config)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date in YYYY-MM-DD format"
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date in YYYY-MM-DD format"
    )
    parser.add_argument(
        "--no-sniper", action="store_true",
        help="Disable sniper filter (test raw signal performance)"
    )
    parser.add_argument(
        "--max-open", type=int, default=3,
        help="Maximum simultaneous open trades (default: 3)"
    )
    parser.add_argument(
        "--cooldown", type=int, default=6,
        help="Signal cooldown in M5 bars (default: 6 = 30 min)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output CSV file path (default: backtest_results.csv)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug-level logging for detailed trade-by-trade output"
    )
    parser.add_argument(
        "--learn", action="store_true",
        help="Enable 2-pass adaptive learning (analyze losses → re-run with rules)"
    )
    parser.add_argument(
        "--learn-threshold", type=float, default=0.65,
        help="Loss rate threshold for generating avoidance rules (default: 0.65)"
    )
    parser.add_argument(
        "--learn-min-samples", type=int, default=5,
        help="Minimum trades per pattern to create a rule (default: 5)"
    )
    parser.add_argument(
        "--disabled-setups", nargs="+", default=None,
        help="Setup types to disable (e.g. PIN_BAR ORDER_BLOCK)"
    )
    parser.add_argument(
        "--killzone-only", action="store_true", default=None,
        help="Only allow trades inside kill zones"
    )
    parser.add_argument(
        "--no-trailing", action="store_true",
        help="Disable trailing stops (test with fixed SL/TP only)"
    )

    args = parser.parse_args()

    # Set debug logging if verbose mode
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # ─── Load configuration ────────────────────────────────────────────
    config = load_config(args.config)

    # Normalize config (apply defaults) using the config_loader if available
    try:
        from config_loader import (
            _normalize_execution_gates,
            _normalize_trailing_structure,
            _normalize_trade_management,
            _normalize_adaptive_learning,
        )
        _normalize_execution_gates(config)
        _normalize_trailing_structure(config)
        _normalize_trade_management(config)
        _normalize_adaptive_learning(config)
        logger.info("Config normalization applied")
    except ImportError:
        logger.warning("config_loader not available — using raw config")

    # ─── Parse date range ──────────────────────────────────────────────
    start_date = None
    end_date = None

    if args.start:
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.error(f"Invalid start date: {args.start}")
            sys.exit(1)

    if args.end:
        try:
            end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.error(f"Invalid end date: {args.end}")
            sys.exit(1)

    # ─── Initialize and run the backtester ─────────────────────────────
    from backtester import BacktestEngine
    from backtest_report import BacktestReport

    engine = BacktestEngine(
        config=config,
        data_dir=args.data_dir,
        use_sniper_filter=not args.no_sniper,
        max_open_trades=args.max_open,
        signal_cooldown_bars=args.cooldown,
        disabled_setups=args.disabled_setups,
        killzone_only=args.killzone_only if args.killzone_only else None,
        use_trailing=not args.no_trailing,
    )

    logger.info("")
    logger.info("🚀 Starting backtest...")
    logger.info("")

    # Run the simulation
    trades = engine.run(
        symbols=args.symbols,
        start_date=start_date,
        end_date=end_date,
    )

    # ─── Generate and display the report ───────────────────────────────
    report = BacktestReport(
        trades=trades,
        filtered_signals=engine.filtered_signals,
    )

    report.print_report()

    # Export results to CSV
    output_path = args.output
    report.export_csv(output_path)

    # ─── 2-Pass Adaptive Learning ───────────────────────────────────
    if args.learn:
        from backtest_learner import BacktestLearner

        logger.info("")
        logger.info("🧠 Starting adaptive learning (Pass 2)...")
        logger.info("")

        # Analyze losses from Pass 1
        learner = BacktestLearner(
            loss_threshold=args.learn_threshold,
            min_samples=args.learn_min_samples,
        )
        learner.analyze_losses(trades)
        learner.print_summary()

        if learner.rules:
            # Re-run with learned rules
            print("\n  ⏳ Re-running backtest with avoidance rules...\n")

            engine2 = BacktestEngine(
                config=config,
                data_dir=args.data_dir,
                use_sniper_filter=not args.no_sniper,
                max_open_trades=args.max_open,
                signal_cooldown_bars=args.cooldown,
                disabled_setups=args.disabled_setups,
                killzone_only=args.killzone_only if args.killzone_only else None,
                use_trailing=not args.no_trailing,
                learner=learner,
            )

            trades2 = engine2.run(
                symbols=args.symbols,
                start_date=start_date,
                end_date=end_date,
            )

            report2 = BacktestReport(
                trades=trades2,
                filtered_signals=engine2.filtered_signals,
            )

            print("\n  🧠─── PASS 2: WITH ADAPTIVE RULES ───")
            report2.print_report()

            # Compare
            closed1 = [t for t in trades if t.exit_reason in ("SL_HIT", "TP_HIT")]
            closed2 = [t for t in trades2 if t.exit_reason in ("SL_HIT", "TP_HIT")]
            pnl1 = sum(t.pnl_pips for t in closed1)
            pnl2 = sum(t.pnl_pips for t in closed2)
            print(f"\n  📊 Adaptive Learning Impact:")
            print(f"     Pass 1 PnL: {pnl1:+.1f} pips ({len(closed1)} trades)")
            print(f"     Pass 2 PnL: {pnl2:+.1f} pips ({len(closed2)} trades)")
            print(f"     Improvement: {pnl2 - pnl1:+.1f} pips")
            print(f"     Trades blocked: {learner.skipped_count}")
            print()
        else:
            print("\n  No avoidance rules generated — loss patterns too diverse.")
            print("  Try lowering --learn-threshold or --learn-min-samples.\n")
    if not args.no_sniper and engine.signals_filtered > 0:
        total_signals = engine.signals_generated
        filtered = engine.signals_filtered
        taken = total_signals - filtered
        print(f"\n  📊 Signal Pipeline Summary:")
        print(f"     Signals generated:  {total_signals}")
        print(f"     Filtered by sniper: {filtered} ({filtered/total_signals*100:.0f}%)")
        print(f"     Trades taken:       {taken} ({taken/total_signals*100:.0f}%)")
        print()
        print(f"  💡 Tip: Run with --no-sniper to compare raw vs filtered performance")
        print()


if __name__ == "__main__":
    main()
