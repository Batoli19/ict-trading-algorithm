"""
Compare Backtest — Multi-Configuration Performance Comparison
═══════════════════════════════════════════════════════════
Runs the same backtest data through multiple configurations and prints
a side-by-side comparison table to identify the best settings.

Usage:
    python compare_backtest.py
    python compare_backtest.py --start 2025-01-01 --end 2025-03-01
    python compare_backtest.py --symbols USDJPY GBPUSD

Configurations tested:
    1. Baseline      — All setups, no KZ enforcement
    2. No PIN_BAR    — PIN_BAR disabled
    3. KZ Only       — Kill zone enforcement ON
    4. Optimized     — No PIN_BAR + KZ only
    5. Adaptive      — Optimized + adaptive learning from losses
"""

import argparse
import json
import logging
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,  # Quiet mode — only show comparison results
    format="%(asctime)s  %(levelname)-8s  [%(name)s]  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("COMPARE")


def load_config() -> dict:
    """Load and merge config (same logic as run_backtest.py)."""
    project_root = Path(__file__).resolve().parent.parent
    bt_config_path = project_root / "config" / "backtest_config.json"
    main_config_path = project_root / "config" / "settings.json"

    if not main_config_path.exists():
        print("ERROR: config/settings.json not found")
        sys.exit(1)

    with open(main_config_path, "r") as f:
        main_cfg = json.load(f)

    if bt_config_path.exists():
        with open(bt_config_path, "r") as f:
            bt_cfg = json.load(f)
        if bt_cfg.get("inherit_from_main", False):
            main_cfg = _deep_merge(main_cfg, bt_cfg)

    return main_cfg


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts, override takes precedence."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def run_single_config(
    config: dict,
    name: str,
    symbols: list = None,
    start_date: datetime = None,
    end_date: datetime = None,
    disabled_setups: list = None,
    killzone_only: bool = False,
    learner=None,
) -> dict:
    """
    Run a single backtest configuration and return summary stats.
    """
    from backtester import BacktestEngine

    engine = BacktestEngine(
        config=config,
        use_sniper_filter=True,
        max_open_trades=3,
        signal_cooldown_bars=6,
        disabled_setups=disabled_setups or [],
        killzone_only=killzone_only,
        learner=learner,
    )

    trades = engine.run(
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        progress_every=999999,  # Suppress progress logging
    )

    # Calculate stats
    closed = [t for t in trades if t.exit_reason in ("SL_HIT", "TP_HIT")]
    total = len(closed)
    winners = sum(1 for t in closed if t.is_winner)
    losers = total - winners
    total_pnl = sum(t.pnl_pips for t in closed)
    win_rate = (winners / total * 100) if total > 0 else 0.0

    # Profit factor
    gross_profit = sum(t.pnl_pips for t in closed if t.is_winner)
    gross_loss = abs(sum(t.pnl_pips for t in closed if not t.is_winner))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0

    # Max drawdown
    max_dd = 0.0
    peak = 0.0
    running = 0.0
    for t in sorted(closed, key=lambda x: x.exit_time or x.entry_time):
        running += t.pnl_pips
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    # Expectancy
    expectancy = (total_pnl / total) if total > 0 else 0.0

    # Max consecutive losses
    max_consec = 0
    current_streak = 0
    for t in sorted(closed, key=lambda x: x.exit_time or x.entry_time):
        if not t.is_winner:
            current_streak += 1
            max_consec = max(max_consec, current_streak)
        else:
            current_streak = 0

    return {
        "name": name,
        "trades": total,
        "winners": winners,
        "losers": losers,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "max_dd": max_dd,
        "max_consec_losses": max_consec,
        "signals_generated": engine.signals_generated,
        "signals_filtered": engine.signals_filtered,
        "all_trades": trades,
    }


def print_comparison(results: list):
    """Print a formatted comparison table of all configurations."""
    print(f"\n{'═'*90}")
    print(f"  📊  MULTI-CONFIGURATION BACKTEST COMPARISON")
    print(f"{'═'*90}")

    # Header
    print(f"\n  {'Configuration':<22} {'Trades':>7} {'WR%':>7} {'PnL':>10} "
          f"{'Exp':>8} {'PF':>6} {'MaxDD':>8} {'MCL':>5}")
    print(f"  {'─'*22} {'─'*7} {'─'*7} {'─'*10} {'─'*8} {'─'*6} {'─'*8} {'─'*5}")

    best_pnl = max(r["total_pnl"] for r in results)

    for r in results:
        icon = "🏆" if r["total_pnl"] == best_pnl else "  "
        pnl_str = f"{r['total_pnl']:+.1f}"
        print(
            f"{icon}{r['name']:<22} {r['trades']:>7} {r['win_rate']:>6.1f}% "
            f"{pnl_str:>10} {r['expectancy']:>+7.2f} {r['profit_factor']:>5.2f} "
            f"{r['max_dd']:>7.1f} {r['max_consec_losses']:>5}"
        )

    # Improvement summary
    baseline = results[0]
    best = max(results, key=lambda r: r["total_pnl"])

    if best["name"] != baseline["name"]:
        improvement = best["total_pnl"] - baseline["total_pnl"]
        print(f"\n  {'─'*82}")
        print(f"  🏆 Best: {best['name']}")
        print(f"     Improvement over baseline: {improvement:+.1f} pips")
        if baseline["total_pnl"] < 0 and best["total_pnl"] > 0:
            print(f"     ✅ Turned a LOSING strategy into a WINNER!")
        elif best["total_pnl"] > baseline["total_pnl"]:
            pct = abs(improvement / abs(baseline["total_pnl"]) * 100) if baseline["total_pnl"] != 0 else 0
            print(f"     📈 {pct:.0f}% better than baseline")

    print(f"\n{'═'*90}\n")

    # Per-config signal pipeline breakdown
    print(f"  📋 SIGNAL PIPELINE BREAKDOWN")
    print(f"  {'─'*60}")
    for r in results:
        taken = r["signals_generated"] - r["signals_filtered"]
        filt_pct = (r["signals_filtered"] / r["signals_generated"] * 100) if r["signals_generated"] > 0 else 0
        print(f"  {r['name']:<22} signals={r['signals_generated']:>5}  "
              f"filtered={r['signals_filtered']:>5} ({filt_pct:.0f}%)  "
              f"taken={taken:>5}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Compare backtest performance across multiple configurations"
    )
    parser.add_argument("--symbols", nargs="+", default=None, help="Symbols to test")
    parser.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="End date YYYY-MM-DD")
    parser.add_argument(
        "--learn-threshold", type=float, default=0.65,
        help="Loss rate threshold for adaptive rules (default: 0.65)"
    )
    parser.add_argument(
        "--learn-min-samples", type=int, default=5,
        help="Minimum trades per pattern for rule creation (default: 5)"
    )
    args = parser.parse_args()

    # Parse dates
    start_date = None
    end_date = None
    if args.start:
        start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if args.end:
        end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    # Load base config
    config = load_config()

    # Normalize config
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
    except ImportError:
        pass

    symbols = args.symbols or config.get("pairs", [])

    print(f"\n  🔬 Running comparison for: {symbols}")
    print(f"  📅 Date range: {args.start or 'all'} to {args.end or 'all'}")
    print(f"  ⏳ This will take a few minutes...\n")

    results = []

    # ─── Config 1: Baseline (all setups, no KZ enforcement) ────────────
    print("  [1/5] Running Baseline...")
    r1 = run_single_config(
        config=config, name="Baseline (all)",
        symbols=symbols, start_date=start_date, end_date=end_date,
        disabled_setups=[], killzone_only=False,
    )
    results.append(r1)

    # ─── Config 2: No PIN_BAR ──────────────────────────────────────────
    print("  [2/5] Running No PIN_BAR...")
    r2 = run_single_config(
        config=config, name="No PIN_BAR",
        symbols=symbols, start_date=start_date, end_date=end_date,
        disabled_setups=["PIN_BAR"], killzone_only=False,
    )
    results.append(r2)

    # ─── Config 3: Kill zones only ─────────────────────────────────────
    print("  [3/5] Running KZ Only...")
    r3 = run_single_config(
        config=config, name="Kill Zones Only",
        symbols=symbols, start_date=start_date, end_date=end_date,
        disabled_setups=[], killzone_only=True,
    )
    results.append(r3)

    # ─── Config 4: Optimized (no PIN_BAR + KZ) ─────────────────────────
    print("  [4/5] Running Optimized...")
    r4 = run_single_config(
        config=config, name="No PIN_BAR + KZ",
        symbols=symbols, start_date=start_date, end_date=end_date,
        disabled_setups=["PIN_BAR"], killzone_only=True,
    )
    results.append(r4)

    # ─── Config 5: Adaptive learning ───────────────────────────────────
    print("  [5/5] Running Adaptive Learning (2-pass)...")

    # Pass 1: Run baseline to collect trade data
    from backtest_learner import BacktestLearner

    learner = BacktestLearner(
        loss_threshold=args.learn_threshold,
        min_samples=args.learn_min_samples,
    )

    # Use the optimized config trades for learning (Pass 1 data)
    optimized_trades = r4.get("all_trades", [])
    if optimized_trades:
        learner.analyze_losses(optimized_trades)

    # Pass 2: Re-run with learned rules
    r5 = run_single_config(
        config=config, name="Adaptive Learning",
        symbols=symbols, start_date=start_date, end_date=end_date,
        disabled_setups=["PIN_BAR"], killzone_only=True,
        learner=learner if learner.rules else None,
    )
    results.append(r5)

    # ─── Print comparison ──────────────────────────────────────────────
    print_comparison(results)

    # Print adaptive learning details
    if learner.rules:
        learner.print_summary()


if __name__ == "__main__":
    main()
