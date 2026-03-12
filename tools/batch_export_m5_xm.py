"""
Batch Export M5 + Higher TF Data from XM MT5
=============================================
Exports M5, M1, M15, H1, H4 data for Personality A backtest.
Nov 2024 → Feb 2026, 5 pairs.
"""
import sys, os
# Redirect all output to a log file since terminal doesn't capture stdout
_log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "export_log.txt")
sys.stdout = open(_log_path, "w", buffering=1, encoding="utf-8")
sys.stderr = sys.stdout

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timezone
from calendar import monthrange
import os
import sys
import time
import glob

ACCOUNT = 1301048395
PASSWORD = "yv#fhV&pG4Kn,6L"
SERVER = "XMGlobal-MT5 6"

PAIRS = ["GBPUSD", "USDJPY", "AUDUSD", "EURUSD", "GBPJPY"]
TIMEFRAMES = {
    "M5":  mt5.TIMEFRAME_M5,
    "M1":  mt5.TIMEFRAME_M1,
    "M15": mt5.TIMEFRAME_M15,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
}

# Only export M5 first, then higher TFs
PRIORITY_TFS = {"M5": mt5.TIMEFRAME_M5}  # Export M5 first

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "m5_xm")

# Nov 2024 - Feb 2026
MONTHS = [
    (2024, 11), (2024, 12),
    (2025, 1),  (2025, 2),  (2025, 3),  (2025, 4),
    (2025, 5),  (2025, 6),  (2025, 7),  (2025, 8),
    (2025, 9),  (2025, 10), (2025, 11), (2025, 12),
    (2026, 1),  (2026, 2),
]


def init_mt5():
    """Initialize MT5 and login to XM account."""
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)

    authorized = mt5.login(ACCOUNT, password=PASSWORD, server=SERVER)
    if not authorized:
        print(f"Login failed: {mt5.last_error()}")
        mt5.shutdown()
        sys.exit(1)

    info = mt5.account_info()
    print(f"Connected to XM MT5 — Account: {info.login}, Server: {info.server}")
    print(f"Balance: {info.balance}, Equity: {info.equity}")
    return True


def export_data():
    """Export all pairs × all timeframes × all months."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    failed = []
    total_exported = 0

    for symbol in PAIRS:
        if not mt5.symbol_select(symbol, True):
            print(f"  WARNING: Could not select {symbol}")
            continue

        for tf_name, tf_const in TIMEFRAMES.items():
            symbol_tf_dir = os.path.join(OUTPUT_DIR, symbol)
            os.makedirs(symbol_tf_dir, exist_ok=True)

            for year, month in MONTHS:
                _, last_day = monthrange(year, month)
                date_from = datetime(year, month, 1, tzinfo=timezone.utc)
                date_to = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
                filename = os.path.join(symbol_tf_dir, f"{symbol}_{tf_name}_{year:04d}-{month:02d}.csv")

                if os.path.exists(filename):
                    # Check file is not empty
                    if os.path.getsize(filename) > 100:
                        continue

                print(f"  {symbol} {tf_name} {year}-{month:02d}...", end=" ", flush=True)

                rates = mt5.copy_rates_range(symbol, tf_const, date_from, date_to)

                if rates is None or len(rates) == 0:
                    print("NO DATA")
                    failed.append(f"{symbol} {tf_name} {year}-{month:02d}")
                    time.sleep(0.5)
                    continue

                df = pd.DataFrame(rates)
                df['time'] = pd.to_datetime(df['time'], unit='s', utc=True)
                df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
                # Format time as expected by the backtester
                df['time'] = df['time'].dt.strftime('%Y-%m-%d %H:%M:%S')
                df.columns = ['time', 'open', 'high', 'low', 'close', 'tick_volume']
                df.to_csv(filename, index=False)
                print(f"OK ({len(df)} bars)")
                total_exported += 1
                time.sleep(0.3)

    print(f"\nTotal files exported: {total_exported}")
    if failed:
        print(f"Failed exports: {failed}")

    return failed


def merge_files():
    """Merge monthly CSVs into single files per symbol × timeframe."""
    print("\n=== MERGING ===")
    for symbol in PAIRS:
        for tf_name in TIMEFRAMES:
            pattern = os.path.join(OUTPUT_DIR, symbol, f"{symbol}_{tf_name}_*.csv")
            files = sorted(glob.glob(pattern))
            if not files:
                print(f"{symbol}_{tf_name}: NO FILES")
                continue
            dfs = [pd.read_csv(f) for f in files]
            merged = pd.concat(dfs).drop_duplicates('time').sort_values('time')
            out = os.path.join(OUTPUT_DIR, f"{symbol}_{tf_name}.csv")
            merged.to_csv(out, index=False)
            print(f"{symbol}_{tf_name}: {len(merged):,} bars | {merged['time'].iloc[0]} -> {merged['time'].iloc[-1]}")


def verify_data():
    """Verify data coverage."""
    print("\n=== VERIFICATION ===")
    all_ok = True
    for symbol in PAIRS:
        m5_file = os.path.join(OUTPUT_DIR, f"{symbol}_M5.csv")
        if not os.path.exists(m5_file):
            print(f"  MISSING: {m5_file}")
            all_ok = False
            continue
        df = pd.read_csv(m5_file)
        print(f"  {symbol} M5: {len(df):,} bars | {df['time'].iloc[0]} -> {df['time'].iloc[-1]}")
        if len(df) < 5000:
            print(f"    WARNING: < 5000 bars — may need to scroll back in MT5 chart")
            all_ok = False

    # Also check higher TFs
    for symbol in PAIRS:
        for tf in ["M1", "M15", "H1", "H4"]:
            tf_file = os.path.join(OUTPUT_DIR, f"{symbol}_{tf}.csv")
            if not os.path.exists(tf_file):
                print(f"  MISSING: {tf_file}")
                all_ok = False
                continue
            df = pd.read_csv(tf_file)
            print(f"  {symbol} {tf}: {len(df):,} bars")

    return all_ok


if __name__ == "__main__":
    print("=" * 60)
    print("BATCH EXPORT: XM MT5 Data for Personality A Backtest")
    print(f"Pairs: {PAIRS}")
    print(f"Timeframes: {list(TIMEFRAMES.keys())}")
    print(f"Months: {len(MONTHS)} ({MONTHS[0]} -> {MONTHS[-1]})")
    print(f"Output: {OUTPUT_DIR}")
    print("=" * 60)

    init_mt5()
    failed = export_data()
    merge_files()
    verify_data()
    mt5.shutdown()

    print("\nFlushing and closing log...")
    if not failed:
        print("\nAll exports successful.")
    else:
        print(f"\nWARNING: Some exports failed: {len(failed)} items")
        print("   Open the pair's chart in MT5 (scroll to Nov 2024), wait for data to load, re-run.")
    
    sys.stdout.flush()
    sys.stdout.close()
