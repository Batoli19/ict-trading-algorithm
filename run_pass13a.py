"""
run_pass13a.py — Personality A Sequential Backtest
Run from project root: python run_pass13a.py
Runs one pair at a time, prints live progress, saves CSV after each pair.
"""

import sys
import os
import csv
import json
import gc
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "python"))

# ── CONFIG ────────────────────────────────────────────────────────────────────

PAIRS = ["GBPUSD", "USDJPY", "AUDUSD", "EURUSD", "GBPJPY"]
DATA_DIR = "data/m5_xm"
OUTPUT_DIR = "."   # saves pass13a_GBPUSD.csv etc in project root

DATE_FROM = datetime(2024, 11, 5, tzinfo=timezone.utc)
DATE_TO   = datetime(2026, 2, 27, tzinfo=timezone.utc)

# Load base config from settings.json then override for Personality A
import json
with open("config/settings.json", encoding="utf-8") as f:
    CONFIG = json.load(f)

# Personality A overrides
CONFIG["pairs"] = PAIRS
CONFIG["disabled_setups"] = [
    "PIN_BAR",
    "LH_LL_CONTINUATION",
    "HH_HL_CONTINUATION",
    "LIQUIDITY_GRAB_CONTINUATION",
]

# Kill zone times — corrected ICT times
if "ict" not in CONFIG:
    CONFIG["ict"] = {}
if "kill_zones" not in CONFIG["ict"]:
    CONFIG["ict"]["kill_zones"] = {}

CONFIG["ict"]["kill_zones"]["london_open"]  = {"start": "06:00", "end": "09:00", "tz": "UTC"}
CONFIG["ict"]["kill_zones"]["ny_open"]      = {"start": "13:30", "end": "16:00", "tz": "UTC"}
CONFIG["ict"]["kill_zones"]["london_close"] = {"start": "15:00", "end": "17:00", "tz": "UTC"}

# All kill zones enabled
if "hybrid" not in CONFIG:
    CONFIG["hybrid"] = {}
CONFIG["hybrid"]["allowed_kill_zones"] = ["LONDON_OPEN", "NY_OPEN", "LONDON_CLOSE"]

# No direction filters
CONFIG["direction_filters"] = {}

# Giveback guard — 99% (lock almost all profit)
if "trade_management" not in CONFIG:
    CONFIG["trade_management"] = {}
CONFIG["trade_management"]["giveback_guard"] = {
    "enabled": True,
    "activate_at_r": 0.5,
    "max_giveback_pct": 0.01,
}

# Partials
CONFIG["trade_management"]["partials"] = {
    "enabled": True,
    "tp1_r": 1.0,
    "tp1_close_pct": 0.6,
    "tp1_sl_mode": "BE_PLUS",
    "tp1_be_plus_r": 0.5,
    "trail_only_after_tp1": True,
}

# Risk — 1%, no daily limit
if "risk" not in CONFIG:
    CONFIG["risk"] = {}
CONFIG["risk"]["risk_per_trade_pct"] = 1.0
CONFIG["risk"]["daily_loss_limit_pct"] = None

# Min RR 1.5
if "execution" not in CONFIG:
    CONFIG["execution"] = {}
CONFIG["execution"]["min_rr"] = 1.5
CONFIG["execution"]["enforce_killzones"] = True

# ── HELPERS ───────────────────────────────────────────────────────────────────

def print_pair_summary(pair, trades):
    if not trades:
        print(f"  {pair}: 0 trades")
        return

    closed = [t for t in trades if t.exit_price is not None]
    if not closed:
        print(f"  {pair}: 0 closed trades")
        return

    wins   = [t for t in closed if t.pnl_pips > 0]
    losses = [t for t in closed if t.pnl_pips <= 0]
    total_pips = sum(t.pnl_pips for t in closed)
    wr = len(wins) / len(closed) * 100
    avg_win  = sum(t.pnl_pips for t in wins)  / len(wins)  if wins   else 0
    avg_loss = sum(t.pnl_pips for t in losses) / len(losses) if losses else 0
    gross_win  = sum(t.pnl_pips for t in wins)
    gross_loss = abs(sum(t.pnl_pips for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

    print(f"\n{'='*55}")
    print(f"  {pair} RESULT")
    print(f"{'='*55}")
    print(f"  Trades:      {len(closed)}")
    print(f"  Win Rate:    {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Total Pips:  {total_pips:+.1f}")
    print(f"  Avg Win:     +{avg_win:.1f} pips")
    print(f"  Avg Loss:    {avg_loss:.1f} pips")
    print(f"  Net USD:     ${total_pips * 50 / max(abs(avg_loss), 1):.2f} (approx)")

    # Setup breakdown
    from collections import defaultdict
    by_setup = defaultdict(list)
    for t in closed:
        by_setup[t.setup_type].append(t)
    print(f"\n  Setup Breakdown:")
    for setup, st in sorted(by_setup.items()):
        sw = [x for x in st if x.pnl_pips > 0]
        sp = sum(x.pnl_pips for x in st)
        sl = abs(sum(x.pnl_pips for x in st if x.pnl_pips <= 0))
        spf = sum(x.pnl_pips for x in sw) / sl if sl > 0 else float('inf')
        print(f"    {setup:<30} {len(st):>3} trades | WR {len(sw)/len(st)*100:.0f}% | PF {spf:.2f} | {sp:+.1f} pips")

    # Session breakdown
    by_session = defaultdict(list)
    for t in closed:
        by_session[getattr(t, 'kill_zone', 'UNKNOWN')].append(t)
    print(f"\n  Session Breakdown:")
    for sess, st in sorted(by_session.items()):
        sw = [x for x in st if x.pnl_pips > 0]
        sp = sum(x.pnl_pips for x in st)
        sl = abs(sum(x.pnl_pips for x in st if x.pnl_pips <= 0))
        spf = sum(x.pnl_pips for x in sw) / sl if sl > 0 else float('inf')
        print(f"    {sess:<20} {len(st):>3} trades | WR {len(sw)/len(st)*100:.0f}% | PF {spf:.2f} | {sp:+.1f} pips")

    print(f"{'='*55}\n")


def save_pair_csv(pair, trades, output_dir):
    closed = [t for t in trades if t.exit_price is not None]
    if not closed:
        print(f"  No closed trades to save for {pair}")
        return

    filepath = os.path.join(output_dir, f"pass13a_{pair}.csv")
    fields = [
        'symbol', 'setup_type', 'direction', 'kill_zone',
        'entry_time', 'exit_time', 'entry_price', 'exit_price',
        'sl_price', 'tp1_price', 'pnl_pips', 'exit_reason',
        'partial_taken',
    ]

    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for t in closed:
            row = {}
            for field in fields:
                row[field] = getattr(t, field, '')
            writer.writerow(row)

    print(f"  Saved {len(closed)} trades → {filepath}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  PASS 13A — PERSONALITY A SEQUENTIAL BACKTEST")
    print("="*60)
    print(f"  Pairs:      {PAIRS}")
    print(f"  Date range: {DATE_FROM.date()} → {DATE_TO.date()}")
    print(f"  Data dir:   {DATA_DIR}")
    print(f"  Setups:     ALL except confirmed bleeders")
    print(f"  Kill zones: LONDON_OPEN + NY_OPEN(13:30) + LONDON_CLOSE")
    print(f"  Direction:  BOTH (no filters)")
    print(f"  Giveback:   99% at 0.5R")
    print(f"  Min RR:     1.5")
    print("="*60 + "\n")

    from backtester import BacktestEngine

    all_results = {}

    for i, pair in enumerate(PAIRS, 1):
        print(f"\n>>> RUN {i}/5: {pair}")
        print(f"    Started: {datetime.now().strftime('%H:%M:%S')}")

        try:
            engine = BacktestEngine(
                config=CONFIG,
                data_dir=DATA_DIR,
                use_sniper_filter=False,   # off for speed
                max_open_trades=2,         # 1 buy + 1 sell
                one_trade_per_symbol=True,
                signal_cooldown_bars=6,
                disabled_setups=CONFIG["disabled_setups"],
                killzone_only=True,
                use_trailing=True,
            )

            trades = engine.run(
                symbols=[pair],
                start_date=DATE_FROM,
                end_date=DATE_TO,
                progress_every=2000,       # print progress every 2000 bars
            )

            print(f"    Finished: {datetime.now().strftime('%H:%M:%S')}")
            print_pair_summary(pair, trades)
            save_pair_csv(pair, trades, OUTPUT_DIR)
            all_results[pair] = trades

        except Exception as e:
            print(f"\n  ERROR on {pair}: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # Free memory before next pair
            if 'engine' in locals():
                del engine
            if 'trades' in locals():
                del trades
            gc.collect()
            print(f"  Memory freed. Moving to next pair...\n")

    # ── GRAND SUMMARY ─────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  PASS 13A — GRAND SUMMARY (ALL PAIRS)")
    print("="*60)

    all_trades = []
    for pair, trades in all_results.items():
        closed = [t for t in trades if t.exit_price is not None]
        all_trades.extend(closed)

    if not all_trades:
        print("  No trades to summarize.")
        return

    wins   = [t for t in all_trades if t.pnl_pips > 0]
    losses = [t for t in all_trades if t.pnl_pips <= 0]
    total_pips = sum(t.pnl_pips for t in all_trades)
    wr  = len(wins) / len(all_trades) * 100
    gw  = sum(t.pnl_pips for t in wins)
    gl  = abs(sum(t.pnl_pips for t in losses))
    pf  = gw / gl if gl > 0 else float('inf')
    weeks = (DATE_TO - DATE_FROM).days / 7

    print(f"\n  Total trades:    {len(all_trades)}")
    print(f"  Win Rate:        {wr:.1f}%")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Total Pips:      {total_pips:+.1f}")
    print(f"  Trades/week:     {len(all_trades)/weeks:.1f}")

    print(f"\n  Per-Pair Summary:")
    print(f"  {'Pair':<10} {'Trades':>7} {'WR':>7} {'PF':>6} {'Pips':>10}")
    print(f"  {'-'*45}")
    for pair in PAIRS:
        if pair not in all_results:
            continue
        closed = [t for t in all_results[pair] if t.exit_price is not None]
        if not closed:
            print(f"  {pair:<10} {'0':>7}")
            continue
        w = [t for t in closed if t.pnl_pips > 0]
        tp = sum(t.pnl_pips for t in closed)
        gl2 = abs(sum(t.pnl_pips for t in closed if t.pnl_pips <= 0))
        gw2 = sum(t.pnl_pips for t in w)
        pf2 = gw2/gl2 if gl2 > 0 else float('inf')
        print(f"  {pair:<10} {len(closed):>7} {len(w)/len(closed)*100:>6.1f}% {pf2:>6.2f} {tp:>+10.1f}")

    print(f"\n  Personality A vs Personality B:")
    print(f"  {'Metric':<25} {'Pers B':>10} {'Pers A':>10}")
    print(f"  {'-'*47}")
    print(f"  {'Profit Factor':<25} {'1.48':>10} {pf:>10.2f}")
    print(f"  {'Win Rate':<25} {'56.4%':>10} {wr:>9.1f}%")
    print(f"  {'Total Trades':<25} {'39 (7wk)':>10} {len(all_trades):>10}")
    print(f"  {'Trades/week':<25} {'5.6':>10} {len(all_trades)/weeks:>10.1f}")
    print(f"  {'Total Pips':<25} {'+220.5':>10} {total_pips:>+10.1f}")

    print("\n" + "="*60)
    print("  Pass 13A complete.")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()