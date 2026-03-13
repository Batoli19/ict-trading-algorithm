"""
Step 1: Extract features from backtest results.
"""
import pandas as pd
import numpy as np
from pathlib import Path

def extract_features():
    RESULTS_DIR = Path("03_BACKTEST_RESULTS")
    BRAIN_DIR = Path("04_BRAIN")
    TRAINING_DIR = BRAIN_DIR / "training_data"
    TRAINING_DIR.mkdir(parents=True, exist_ok=True)
    
    print(f"Scanning {RESULTS_DIR.resolve()} for CSV files...")
    csv_files = list(RESULTS_DIR.rglob("pass*.csv"))
    if not csv_files:
        print("No pass CSV files found in 03_BACKTEST_RESULTS.")
        return
        
    dfs = []
    for f in csv_files:
        try:
            df = pd.read_csv(f)
            # Ensure required columns exist before appending
            req_cols = ['symbol', 'direction', 'setup_type', 'kill_zone', 'exit_reason', 'entry_time', 'pnl_pips']
            if all(c in df.columns for c in req_cols):
                dfs.append(df)
            else:
                print(f"Skipping {f} - missing required columns.")
        except Exception as e:
            print(f"Error reading {f}: {e}")
            
    if not dfs:
        print("No valid data loaded.")
        return
        
    data = pd.concat(dfs, ignore_index=True)
    
    # Feature engineering
    data['entry_time'] = pd.to_datetime(data['entry_time'])
    
    data['hour_utc'] = data['entry_time'].dt.hour
    data['day_of_week'] = data['entry_time'].dt.dayofweek
    data['week_of_month'] = (data['entry_time'].dt.day - 1) // 7 + 1
    data['month'] = data['entry_time'].dt.month
    data['is_monday'] = (data['day_of_week'] == 0).astype(int)
    data['is_friday'] = (data['day_of_week'] == 4).astype(int)
    
    data['is_gbpusd_sell'] = ((data['symbol'] == 'GBPUSD') & (data['direction'] == 'SELL')).astype(int)
    data['is_usdjpy_buy'] = ((data['symbol'] == 'USDJPY') & (data['direction'] == 'BUY')).astype(int)
    data['is_eurusd'] = (data['symbol'] == 'EURUSD').astype(int)
    
    # direction_aligned definition
    def check_alignment(row):
        if row['symbol'] == 'GBPUSD' and row['direction'] == 'SELL':
            return 1
        elif row['symbol'] == 'USDJPY' and row['direction'] == 'BUY':
            return 1
        elif row['symbol'] == 'EURUSD':
            return 1
        return 0
        
    data['direction_aligned'] = data.apply(check_alignment, axis=1)
    
    # Target variables
    data['outcome_binary'] = (data['pnl_pips'] > 0).astype(int)
    data['outcome_pips'] = data['pnl_pips']
    
    features = [
        'symbol', 'direction', 'setup_type', 'kill_zone', 'exit_reason',
        'hour_utc', 'day_of_week', 'week_of_month', 'month', 'is_monday', 'is_friday',
        'is_gbpusd_sell', 'is_usdjpy_buy', 'is_eurusd', 'direction_aligned',
        'outcome_binary', 'outcome_pips'
    ]
    
    out_df = data[features].copy()
    out_file = TRAINING_DIR / "features.csv"
    out_df.to_csv(out_file, index=False)
    
    win_rate = out_df['outcome_binary'].mean() * 100
    total_trades = len(out_df)
    
    summary = (
        f"Feature Extraction Summary\n"
        f"==========================\n"
        f"Total Trades Loaded: {total_trades}\n"
        f"Overall Win Rate: {win_rate:.2f}%\n"
        f"Features Extracted: {len(features)}\n\n"
        f"Feature List:\n" + "\n".join([f"- {f}" for f in features]) + "\n\n"
        f"Missing Values:\n{out_df.isnull().sum().to_string()}\n"
    )
    
    print(summary)
    summary_file = TRAINING_DIR / "feature_summary.txt"
    summary_file.write_text(summary)
    print(f"Features saved to {out_file}")

if __name__ == "__main__":
    extract_features()
