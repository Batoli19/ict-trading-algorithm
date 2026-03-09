"""
Backtest Report Generator
══════════════════════════
Generates comprehensive performance reports from backtest results.

Takes a list of SimulatedTrade objects from the BacktestEngine and produces:
    • Per-setup stats (win rate, expectancy, avg RR for each ICT setup)
    • Per-symbol stats (best/worst performing pairs)
    • Per-kill-zone stats (London vs NY vs London Close)
    • Overall metrics (total PnL, max drawdown, profit factor)
    • Filtered signal analysis (what the sniper filter blocked)
    • CSV export of all trade results

Usage:
    from backtest_report import BacktestReport
    report = BacktestReport(trades, filtered_signals)
    report.print_report()
    report.export_csv("results.csv")
"""

import csv
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Fix Windows console encoding for unicode box-drawing characters
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass  # Fallback: some environments don't support reconfigure

from backtester import SimulatedTrade

logger = logging.getLogger("REPORT")


# ═══════════════════════════════════════════════════════════════════════════
# SetupStats — aggregated performance metrics for one setup type
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SetupStats:
    """
    Aggregated performance statistics for a specific setup type,
    symbol, or kill zone.
    """
    name: str                        # e.g. "FVG", "EURUSD", "LONDON_OPEN"
    total_trades: int = 0            # Total number of trades
    wins: int = 0                    # Number of winning trades
    losses: int = 0                  # Number of losing trades
    total_pnl_pips: float = 0.0      # Sum of all trade PnL in pips
    avg_win_pips: float = 0.0        # Average winning trade PnL
    avg_loss_pips: float = 0.0       # Average losing trade PnL
    max_win_pips: float = 0.0        # Largest single win
    max_loss_pips: float = 0.0       # Largest single loss
    avg_rr: float = 0.0              # Average risk-reward ratio achieved
    win_rate: float = 0.0            # Win rate as percentage (0-100)
    expectancy_pips: float = 0.0     # Expected PnL per trade in pips
    profit_factor: float = 0.0       # Gross profit / gross loss (>1 is good)
    max_consecutive_losses: int = 0  # Longest losing streak
    max_drawdown_pips: float = 0.0   # Maximum peak-to-trough drawdown in pips


# ═══════════════════════════════════════════════════════════════════════════
# BacktestReport — generates and displays the full performance report
# ═══════════════════════════════════════════════════════════════════════════

class BacktestReport:
    """
    Generates comprehensive performance reports from backtest results.

    Call print_report() for console output, or export_csv() to save
    detailed trade-by-trade results.
    """

    def __init__(
        self,
        trades: List[SimulatedTrade],
        filtered_signals: List[dict] = None,
    ):
        """
        Args:
            trades:            List of completed SimulatedTrade objects
            filtered_signals:  List of signals that were blocked by SniperFilter
        """
        # Only include trades that actually closed (not END_OF_DATA)
        self.all_trades = trades
        self.closed_trades = [
            t for t in trades
            if t.exit_reason in ("TP_HIT", "SL_HIT", "TRAILED_SL", "BREAKEVEN")
        ]
        self.filtered_signals = filtered_signals or []

    def _compute_stats(self, trades: List[SimulatedTrade], name: str) -> SetupStats:
        """
        Calculate aggregated statistics for a group of trades.

        This is the core math behind the report. For each group of trades
        (e.g. all FVG trades, or all EURUSD trades), it computes:

        Win Rate = wins / total × 100
        Expectancy = (win_rate × avg_win) - (loss_rate × avg_loss)
        Profit Factor = total_winning_pips / |total_losing_pips|
        Max Drawdown = largest peak-to-trough decline in cumulative PnL
        """
        stats = SetupStats(name=name)

        if not trades:
            return stats

        stats.total_trades = len(trades)
        stats.wins = sum(1 for t in trades if t.pnl_pips > 0)
        stats.losses = sum(1 for t in trades if t.pnl_pips <= 0)

        # ─── PnL calculations ─────────────────────────────────────────
        stats.total_pnl_pips = sum(t.pnl_pips for t in trades)

        winning_pips = [t.pnl_pips for t in trades if t.pnl_pips > 0]
        losing_pips = [t.pnl_pips for t in trades if t.pnl_pips <= 0]

        if winning_pips:
            stats.avg_win_pips = sum(winning_pips) / len(winning_pips)
            stats.max_win_pips = max(winning_pips)

        if losing_pips:
            stats.avg_loss_pips = sum(losing_pips) / len(losing_pips)
            stats.max_loss_pips = min(losing_pips)  # Most negative

        # ─── Win rate ──────────────────────────────────────────────────
        stats.win_rate = (stats.wins / stats.total_trades * 100) if stats.total_trades > 0 else 0

        # ─── Average RR ───────────────────────────────────────────────
        rr_values = [t.rr_achieved for t in trades if t.rr_achieved != 0]
        stats.avg_rr = sum(rr_values) / len(rr_values) if rr_values else 0

        # ─── Expectancy (expected PnL per trade) ──────────────────────
        # Formula: (win_rate × avg_win) + (loss_rate × avg_loss)
        # Note: avg_loss is already negative, so this gives correct result
        if stats.total_trades > 0:
            stats.expectancy_pips = stats.total_pnl_pips / stats.total_trades

        # ─── Profit Factor ─────────────────────────────────────────────
        # Gross profit / |gross loss|  →  >1 means profitable
        gross_profit = sum(winning_pips) if winning_pips else 0
        gross_loss = abs(sum(losing_pips)) if losing_pips else 0
        stats.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # ─── Max consecutive losses ───────────────────────────────────
        streak = 0
        max_streak = 0
        for t in trades:
            if t.pnl_pips <= 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        stats.max_consecutive_losses = max_streak

        # ─── Max drawdown ──────────────────────────────────────────────
        # Track cumulative PnL and find the worst peak-to-trough decline
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += t.pnl_pips
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        stats.max_drawdown_pips = max_dd

        return stats

    def print_report(self):
        """
        Print a comprehensive, formatted performance report to the console.

        Sections:
            1. Overall summary
            2. Per-setup breakdown
            3. Per-symbol breakdown
            4. Per-kill-zone breakdown
            5. Filtered signals analysis
        """
        print("\n")
        print("═" * 72)
        print("  📊  BACKTEST PERFORMANCE REPORT")
        print("═" * 72)

        # ─── 1. Overall Summary ───────────────────────────────────────
        overall = self._compute_stats(self.closed_trades, "OVERALL")
        self._print_section("OVERALL RESULTS", overall)

        if not self.closed_trades:
            print("\n  No completed trades found.\n")
            return

        # ─── 2. Per-Setup Breakdown ───────────────────────────────────
        print("\n" + "─" * 72)
        print("  📋  PER-SETUP BREAKDOWN")
        print("─" * 72)

        # Group trades by setup type
        by_setup: Dict[str, List[SimulatedTrade]] = defaultdict(list)
        for t in self.closed_trades:
            by_setup[t.setup_type].append(t)

        # Sort setups by total trades (most active first)
        for setup_name in sorted(by_setup.keys(), key=lambda k: len(by_setup[k]), reverse=True):
            stats = self._compute_stats(by_setup[setup_name], setup_name)
            self._print_row(stats)

        # ─── 3. Per-Symbol Breakdown ──────────────────────────────────
        print("\n" + "─" * 72)
        print("  💱  PER-SYMBOL BREAKDOWN")
        print("─" * 72)

        by_symbol: Dict[str, List[SimulatedTrade]] = defaultdict(list)
        for t in self.closed_trades:
            by_symbol[t.symbol].append(t)

        for sym in sorted(by_symbol.keys()):
            stats = self._compute_stats(by_symbol[sym], sym)
            self._print_row(stats)

        # ─── 4. Per-Kill-Zone Breakdown ───────────────────────────────
        print("\n" + "─" * 72)
        print("  ⏰  PER-KILL-ZONE BREAKDOWN")
        print("─" * 72)

        by_kz: Dict[str, List[SimulatedTrade]] = defaultdict(list)
        for t in self.closed_trades:
            by_kz[t.killzone].append(t)

        for kz in sorted(by_kz.keys()):
            stats = self._compute_stats(by_kz[kz], kz)
            self._print_row(stats)

        # ─── 5. Equity Curve Summary ──────────────────────────────────
        print("\n" + "─" * 72)
        print("  📈  EQUITY CURVE")
        print("─" * 72)

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.closed_trades:
            cumulative += t.pnl_pips
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        print(f"  Final PnL:          {cumulative:+.1f} pips")
        print(f"  Peak PnL:           {peak:+.1f} pips")
        print(f"  Max Drawdown:       {max_dd:.1f} pips")
        if peak > 0:
            print(f"  DD as % of peak:    {max_dd/peak*100:.1f}%")

        # ─── 6. Filtered Signals Analysis ─────────────────────────────
        if self.filtered_signals:
            print("\n" + "─" * 72)
            print("  🚫  SNIPER FILTER ANALYSIS")
            print("─" * 72)

            # Count filter reasons
            reason_counts: Dict[str, int] = defaultdict(int)
            for sig in self.filtered_signals:
                reason_counts[sig.get("skip_reason", "UNKNOWN")] += 1

            print(f"  Total signals filtered: {len(self.filtered_signals)}")
            print(f"  Filter reasons:")
            for reason, count in sorted(reason_counts.items(), key=lambda x: -x[1]):
                print(f"    {reason:<35} {count:>5} ({count/len(self.filtered_signals)*100:.0f}%)")

        print("\n" + "═" * 72)
        print()

    def _print_section(self, title: str, stats: SetupStats):
        """Print a full stats section with all metrics."""
        print(f"\n  {title}")
        print(f"  {'─' * 50}")
        print(f"  Total trades:           {stats.total_trades}")
        print(f"  Winners:                {stats.wins}  |  Losers: {stats.losses}")
        print(f"  Win rate:               {stats.win_rate:.1f}%")
        print(f"  Total PnL:              {stats.total_pnl_pips:+.1f} pips")
        print(f"  Expectancy:             {stats.expectancy_pips:+.2f} pips/trade")
        print(f"  Profit factor:          {stats.profit_factor:.2f}")
        print(f"  Avg win / Avg loss:     {stats.avg_win_pips:+.1f} / {stats.avg_loss_pips:+.1f} pips")
        print(f"  Max win / Max loss:     {stats.max_win_pips:+.1f} / {stats.max_loss_pips:+.1f} pips")
        print(f"  Avg RR achieved:        {stats.avg_rr:.2f}")
        print(f"  Max consec. losses:     {stats.max_consecutive_losses}")
        print(f"  Max drawdown:           {stats.max_drawdown_pips:.1f} pips")

    def _print_row(self, stats: SetupStats):
        """Print a compact one-line + detail row for a group."""
        # Color indicators for win rate
        if stats.win_rate >= 55:
            indicator = "🟢"
        elif stats.win_rate >= 45:
            indicator = "🟡"
        else:
            indicator = "🔴"

        print(
            f"  {indicator} {stats.name:<20} "
            f"trades={stats.total_trades:<4} "
            f"WR={stats.win_rate:>5.1f}%  "
            f"PnL={stats.total_pnl_pips:>+8.1f}  "
            f"exp={stats.expectancy_pips:>+6.2f}  "
            f"PF={stats.profit_factor:>5.2f}  "
            f"maxDD={stats.max_drawdown_pips:>6.1f}"
        )

    def export_csv(self, filepath: str | Path = None):
        """
        Export all trade results to a CSV file for external analysis
        (e.g. in Excel or Google Sheets).

        Args:
            filepath: Output CSV path (default: backtest_results.csv in project root)
        """
        if filepath is None:
            filepath = Path(__file__).resolve().parent.parent / "backtest_results.csv"
        filepath = Path(filepath)

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)

            # Header row
            writer.writerow([
                "symbol", "direction", "setup_type", "entry_time", "exit_time",
                "entry_price", "exit_price", "sl_price", "tp_price",
                "pnl_pips", "rr_achieved", "exit_reason",
                "confidence", "killzone", "htf_bias",
                "sniper_passed", "sniper_skip_reason",
            ])

            # Data rows (sorted by entry time)
            for t in sorted(self.all_trades, key=lambda x: x.entry_time):
                writer.writerow([
                    t.symbol,
                    t.direction,
                    t.setup_type,
                    t.entry_time.strftime("%Y-%m-%d %H:%M") if t.entry_time else "",
                    t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "",
                    f"{t.entry_price:.5f}",
                    f"{t.exit_price:.5f}" if t.exit_price else "",
                    f"{t.sl_price:.5f}",
                    f"{t.tp_price:.5f}",
                    f"{t.pnl_pips:.2f}",
                    f"{t.rr_achieved:.2f}",
                    t.exit_reason or "",
                    f"{t.confidence:.3f}",
                    t.killzone,
                    t.htf_bias,
                    t.sniper_passed,
                    t.sniper_skip_reason,
                ])

        logger.info(f"Trade results exported to: {filepath}")
        print(f"\n  📄 Results exported to: {filepath}")

    def get_stats_dict(self) -> dict:
        """
        Return all stats as a dictionary (useful for programmatic access).

        Returns a dict with keys: overall, by_setup, by_symbol, by_killzone
        """
        result = {}

        # Overall
        overall = self._compute_stats(self.closed_trades, "OVERALL")
        result["overall"] = {
            "total_trades": overall.total_trades,
            "wins": overall.wins,
            "losses": overall.losses,
            "win_rate": overall.win_rate,
            "total_pnl_pips": overall.total_pnl_pips,
            "expectancy_pips": overall.expectancy_pips,
            "profit_factor": overall.profit_factor,
            "max_drawdown_pips": overall.max_drawdown_pips,
            "max_consecutive_losses": overall.max_consecutive_losses,
        }

        # Per-setup
        result["by_setup"] = {}
        by_setup: Dict[str, List] = defaultdict(list)
        for t in self.closed_trades:
            by_setup[t.setup_type].append(t)
        for name, trades in by_setup.items():
            s = self._compute_stats(trades, name)
            result["by_setup"][name] = {
                "total_trades": s.total_trades,
                "win_rate": s.win_rate,
                "total_pnl_pips": s.total_pnl_pips,
                "expectancy_pips": s.expectancy_pips,
                "profit_factor": s.profit_factor,
            }

        return result
