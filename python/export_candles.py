"""
Export Candles — MT5 Historical Data Exporter
══════════════════════════════════════════════
Downloads historical candle data from MetaTrader 5 and saves as CSV files
for use with the backtesting engine.

Usage:
    python export_candles.py                    # Export last 6 months (default)
    python export_candles.py --months 12        # Export last 12 months
    python export_candles.py --start 2024-01-01 # Export from specific date

Output:
    Creates a 'backtest_data/' folder with one CSV per symbol per timeframe:
        backtest_data/EURUSD_H4.csv
        backtest_data/EURUSD_H1.csv
        backtest_data/EURUSD_M15.csv
        backtest_data/EURUSD_M5.csv
        backtest_data/EURUSD_M1.csv
        ... (same for each pair)

Each CSV has columns: time, open, high, low, close, tick_volume
"""

import argparse
import csv
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# We need MetaTrader5 to pull historical bars.  If it's not installed on
# your machine, the script will exit with a helpful message.
# ---------------------------------------------------------------------------
try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed. Run: pip install MetaTrader5")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Set up logging so the user can see progress in the console.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("EXPORT")

# ---------------------------------------------------------------------------
# MT5 timeframe mapping — maps our human-readable names (e.g. "M5") to the
# integer constants that MetaTrader5 uses internally.
# ---------------------------------------------------------------------------
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}

# ---------------------------------------------------------------------------
# The timeframes that the ICT strategy needs for analysis.
# H4  = Higher time-frame bias (trend direction)
# H1  = Combined HTF bias used by sniper filter
# M15 = Entry timeframe (FVG, Order Block, Turtle Soup, Stop Hunt)
# M5  = Trigger timeframe (sniper entries, engulfing, pin bar)
# M1  = Scalping timeframe (scalp and manipulation setups)
# ---------------------------------------------------------------------------
REQUIRED_TIMEFRAMES = ["H4", "H1", "M15", "M5", "M1"]


def load_config() -> dict:
    """
    Load the bot's settings.json to get the list of trading pairs
    and MT5 login credentials.
    """
    import json
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "settings.json"
    if not cfg_path.exists():
        logger.error(f"Config file not found: {cfg_path}")
        sys.exit(1)
    with open(cfg_path, "r") as f:
        return json.load(f)


def connect_mt5(config: dict) -> bool:
    """
    Initialize MT5 connection using credentials from settings.json.
    Returns True if connection succeeds, False otherwise.
    """
    mt5_cfg = config.get("mt5", {})

    # Initialize the MT5 terminal
    if not mt5.initialize():
        logger.error(f"MT5 initialize() failed: {mt5.last_error()}")
        return False

    # Login to the trading account
    login = int(mt5_cfg.get("login", 0))
    password = str(mt5_cfg.get("password", ""))
    server = str(mt5_cfg.get("server", ""))

    if login and password and server:
        if not mt5.login(login, password=password, server=server):
            logger.error(f"MT5 login failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

    logger.info(f"Connected to MT5: account={mt5.account_info().login}")
    return True


def export_symbol_timeframe(
    symbol: str,
    timeframe: str,
    start_date: datetime,
    end_date: datetime,
    output_dir: Path,
) -> int:
    """
    Download candles for one symbol+timeframe combination and save to CSV.

    Args:
        symbol:     Trading instrument (e.g. "EURUSD")
        timeframe:  Timeframe string (e.g. "M5")
        start_date: Start of date range (UTC)
        end_date:   End of date range (UTC)
        output_dir: Directory to save the CSV file

    Returns:
        Number of candles exported (0 if failed)
    """
    # Convert our timeframe string to MT5's internal constant
    tf_const = TF_MAP.get(timeframe)
    if tf_const is None:
        logger.warning(f"Unknown timeframe: {timeframe}")
        return 0

    # Make sure the symbol is available in MT5's Market Watch
    # (if it's not visible, MT5 won't return data for it)
    if not mt5.symbol_select(symbol, True):
        logger.warning(f"Symbol {symbol} not available in Market Watch")
        return 0

    # Download the historical bars from MT5
    # copy_rates_range returns a numpy structured array of OHLCV data
    rates = mt5.copy_rates_range(symbol, tf_const, start_date, end_date)

    if rates is None or len(rates) == 0:
        logger.warning(f"No data returned for {symbol} {timeframe}")
        return 0

    # Build the output CSV file path
    csv_path = output_dir / f"{symbol}_{timeframe}.csv"

    # Write the candle data to CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # Header row
        writer.writerow(["time", "open", "high", "low", "close", "tick_volume"])
        # Data rows — each 'rate' is a numpy record with named fields
        for rate in rates:
            # Convert the Unix timestamp to an ISO datetime string
            dt = datetime.fromtimestamp(rate["time"], tz=timezone.utc)
            writer.writerow([
                dt.strftime("%Y-%m-%d %H:%M:%S"),
                rate["open"],
                rate["high"],
                rate["low"],
                rate["close"],
                rate["tick_volume"],
            ])

    return len(rates)


def main():
    """
    Main function — parses command-line arguments, connects to MT5,
    and exports candles for all configured pairs and timeframes.
    """
    # -----------------------------------------------------------------------
    # Parse command-line arguments
    # -----------------------------------------------------------------------
    parser = argparse.ArgumentParser(
        description="Export MT5 historical candles to CSV for backtesting"
    )
    parser.add_argument(
        "--symbol", type=str, default=None,
        help="Single symbol to export (e.g. EURUSD). If omitted, exports all pairs from settings.json"
    )
    parser.add_argument(
        "--months", type=int, default=6,
        help="Number of months of history to export (default: 6)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date in YYYY-MM-DD format (overrides --months)"
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date in YYYY-MM-DD format (default: today)"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output directory (default: backtest_data/ in project root)"
    )
    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Load config to get the list of pairs and MT5 credentials
    # -----------------------------------------------------------------------
    config = load_config()

    # If --symbol is given, only export that one pair; otherwise use settings.json
    if args.symbol:
        pairs = [args.symbol.upper()]
    else:
        pairs = config.get("pairs", [])

    if not pairs:
        logger.error("No pairs configured in settings.json and no --symbol given")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Calculate date range for the export
    # -----------------------------------------------------------------------
    # End date: use --end if provided, otherwise default to now
    if args.end:
        try:
            end_date = datetime.strptime(args.end, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.error(f"Invalid end date format: {args.end} (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        end_date = datetime.now(tz=timezone.utc)

    # Start date: use --start if provided, otherwise go back --months
    if args.start:
        try:
            start_date = datetime.strptime(args.start, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            logger.error(f"Invalid start date format: {args.start} (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        start_date = end_date - timedelta(days=args.months * 30)

    logger.info(f"Export range: {start_date.date()} to {end_date.date()}")
    logger.info(f"Pairs: {pairs}")
    logger.info(f"Timeframes: {REQUIRED_TIMEFRAMES}")

    # -----------------------------------------------------------------------
    # Create the output directory
    # -----------------------------------------------------------------------
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = Path(__file__).resolve().parent.parent / "backtest_data"

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"Output directory: {output_dir}")

    # -----------------------------------------------------------------------
    # Connect to MT5
    # -----------------------------------------------------------------------
    if not connect_mt5(config):
        sys.exit(1)

    # -----------------------------------------------------------------------
    # Export candles for every pair × timeframe combination
    # -----------------------------------------------------------------------
    total_candles = 0
    total_files = 0

    for symbol in pairs:
        for tf in REQUIRED_TIMEFRAMES:
            count = export_symbol_timeframe(
                symbol=symbol,
                timeframe=tf,
                start_date=start_date,
                end_date=end_date,
                output_dir=output_dir,
            )
            if count > 0:
                logger.info(f"  ✅ {symbol} {tf}: {count:,} candles exported")
                total_candles += count
                total_files += 1
            else:
                logger.warning(f"  ❌ {symbol} {tf}: no data")

    # -----------------------------------------------------------------------
    # Disconnect and print summary
    # -----------------------------------------------------------------------
    mt5.shutdown()

    logger.info(f"")
    logger.info(f"Export complete!")
    logger.info(f"  Files created: {total_files}")
    logger.info(f"  Total candles: {total_candles:,}")
    logger.info(f"  Output dir:    {output_dir}")
    logger.info(f"")
    logger.info(f"Next step: python run_backtest.py")


if __name__ == "__main__":
    main()
