import json
import csv
import math
import subprocess
from pathlib import Path
from datetime import datetime

CONFIG_PATH = Path("../config/backtest_config.json")
SETTINGS_PATH = Path("../config/settings.json")
RESULTS_CSV = Path("../backtest_results.csv")

def get_config_val(conf, keys, default=None):
    curr = conf
    for k in keys:
        if isinstance(curr, dict) and k in curr:
            curr = curr[k]
        else:
            return default
    return curr

def run_pass12():
    print("\n" + "="*50)
    print("      PASS 12: FINAL VALIDATION RUN")
    print("="*50)

    # 1. Load configs for confirmation
    with open(CONFIG_PATH, "r") as f:
        bt_conf = json.load(f)
    with open(SETTINGS_PATH, "r") as f:
        main_conf = json.load(f)

    # Resolve "inheritance" for confirmation
    tp1_be = bt_conf.get("trade_management", {}).get("partials", {}).get("tp1_be_plus_r")
    trail_only = bt_conf.get("trade_management", {}).get("partials", {}).get("trail_only_after_tp1")
    allowed_kz = bt_conf.get("allowed_kill_zones")
    ny_start = main_conf.get("ict", {}).get("kill_zones", {}).get("ny_open", {}).get("start")
    pairs = bt_conf.get("pairs")
    disables = bt_conf.get("disabled_setups", [])

    print("\nCONFIG LOCK:")
    print(f"  tp1_be_plus_r    = {tp1_be}       {'[OK]' if tp1_be == 0.5 else '[FAIL]'}")
    print(f"  trail_only_after = {trail_only}       {'[OK]' if trail_only == True else '[FAIL]'}")
    print(f"  allowed_kz       = {allowed_kz}       {'[OK]' if allowed_kz == ['NY_OPEN', 'LONDON_CLOSE'] else '[FAIL]'}")
    print(f"  ny_open_start    = {ny_start}       {'[OK]' if ny_start == '13:30' else '[FAIL]'}")
    print(f"  pairs            = {pairs}       {'[OK]' if pairs == ['USDJPY', 'GBPUSD', 'AUDUSD'] else '[FAIL]'}")
    print(f"  disabled_setups  = {len(disables)} items   {'[OK]' if 'LH_LL_CONTINUATION' in disables else '[FAIL]'}")

    v_fails = []
    if tp1_be != 0.5: v_fails.append("V1")
    if trail_only != True: v_fails.append("V2")
    if ny_start != "13:30": v_fails.append("NY_START")
    
    if v_fails:
        print(f"\n[CRITICAL] Basic config verification failed: {v_fails}")
        # return

    # 2. Run Backtest
    print("\n>>> Launching Backtest Engine (Jan 1 - Feb 28)...")
    subprocess.run(["python", "run_backtest.py", "--start", "2025-01-01", "--end", "2025-03-01"], check=True)

    if not RESULTS_CSV.exists():
        print("[ERROR] No results generated.")
        return

    # 3. Analyze Results
    trades = []
    with open(RESULTS_CSV, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)

    if not trades:
        print("[WARNING] No trades fired.")
        return

    # Ported analysis logic from Pass 11 + advanced stats
    wins = [float(t["pnl_pips"]) for t in trades if float(t["pnl_pips"]) > 0]
    losses = [abs(float(t["pnl_pips"])) for t in trades if float(t["pnl_pips"]) <= 0]
    total_pips = sum(float(t["pnl_pips"]) for t in trades)
    wr = (len(wins) / len(trades))
    
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
    
    # Drawdown
    peak = 0
    curr = 0
    max_dd = 0
    consec_losses = 0
    max_consec_losses = 0
    worst_trade = min(float(t["pnl_pips"]) for t in trades)
    
    for t in trades:
        pnl = float(t["pnl_pips"])
        curr += pnl
        if curr > peak: peak = curr
        dd = peak - curr
        if dd > max_dd: max_dd = dd
        
        if pnl <= 0:
            consec_losses += 1
            if consec_losses > max_consec_losses: max_consec_losses = consec_losses
        else:
            consec_losses = 0

    # Sessions
    session_data = {}
    for t in trades:
        kz = t["killzone"]
        if kz not in session_data: session_data[kz] = {"wins": [], "losses": []}
        pnl = float(t["pnl_pips"])
        if pnl > 0: session_data[kz]["wins"].append(pnl)
        else: session_data[kz]["losses"].append(abs(pnl))

    # Stats
    # 95% WR CI lower bound (Simple Approximation)
    z = 1.96
    lower_ci = wr - z * math.sqrt((wr * (1 - wr)) / len(trades))

    # Deployment Projections ($50 / trade)
    risk = 50
    avg_pips_win = sum(wins)/len(wins) if wins else 0
    avg_pips_loss = sum(losses)/len(losses) if losses else 15
    # Assume 1R = 15 pips average for calc
    risk_pips = 15 
    exp_win_usd = (avg_pips_win / risk_pips) * risk
    exp_loss_usd = (avg_pips_loss / risk_pips) * risk
    expectancy_usd = (wr * exp_win_usd) - ((1-wr) * exp_loss_usd)
    
    trades_per_week = len(trades) / 8.4 # 2 months
    
    print("\n" + "-"*30)
    print("FINAL VALIDATION RESULTS")
    print("-"*30)
    print(f"Total Trades: {len(trades)}")
    print(f"Win Rate:     {wr*100:.1f}% (CI Lower: {max(0, lower_ci)*100:.1f}%)")
    print(f"Profit Factor: {pf:.2f}")
    print(f"Total Pips:    {total_pips:+.1f}")
    print(f"Avg Win/Loss:  {avg_pips_win:.1f} / {avg_pips_loss:.1f}")
    print(f"Max DD:        {max_dd:.1f} pips")
    print(f"Max Consec Ls: {max_consec_losses}")
    
    print("\nSESSION STATS:")
    for kz, data in session_data.items():
        kz_p = sum(data["wins"]) - sum(data["losses"])
        kz_pf = sum(data["wins"]) / sum(data["losses"]) if sum(data["losses"]) > 0 else 999
        print(f"  {kz:15} | PF: {kz_pf:.2f} | Pips: {kz_p:+.1f}")

    print("\nDEPLOYMENT PROJECTIONS ($50 Risk):")
    print(f"  Expected $/trade: ${expectancy_usd:.2f}")
    print(f"  Expected $/week:  ${expectancy_usd * trades_per_week:.2f}")
    print(f"  Expected $/month: ${expectancy_usd * trades_per_week * 4.3:.2f}")
    
    # V-Checks
    v3 = all("11:00" > t["entry_time"].split()[1] or t["entry_time"].split()[1] > "13:30" for t in trades if t["killzone"] == "NY_OPEN")
    v4_jpy = all(t["direction"] == "SELL" for t in trades if t["symbol"] == "USDJPY")
    v4_aud = all(t["direction"] == "BUY" for t in trades if t["symbol"] == "AUDUSD")
    v5 = all(t["killzone"] != "LONDON_OPEN" for t in trades)
    v6 = all(t["setup_type"] not in ["LH_LL_CONTINUATION", "HH_HL_CONTINUATION", "LIQUIDITY_GRAB_CONTINUATION"] for t in trades)
    
    print("\nVERIFICATION CHECKLIST:")
    print(f"  V3 (Judas Swing Clean): {'PASS' if v3 else 'FAIL'}")
    print(f"  V4 (Pair Directions):  {'PASS' if v4_jpy and v4_aud else 'FAIL'}")
    print(f"  V5 (No London Open):   {'PASS' if v5 else 'FAIL'}")
    print(f"  V6 (No Bleeder Setups):{'PASS' if v6 else 'FAIL'}")
    print(f"  V7 (NY PF > 1.0):      {'PASS' if session_data.get('NY_OPEN', {}).get('wins') and (sum(session_data['NY_OPEN']['wins'])/sum(session_data['NY_OPEN']['losses']) if sum(session_data['NY_OPEN']['losses'])>0 else 9)>1.0 else 'FAIL/NO TRADES'}")
    print(f"  V8 (Overall PF > 1.3): {'PASS' if pf > 1.3 else 'FAIL'}")

    # Generate persistent metadata for artifact
    report_data = {
        "summary": {"trades": len(trades), "wr": wr, "pf": pf, "pips": total_pips, "dd": max_dd, "consec": max_consec_losses},
        "sessions": session_data,
        "projections": {"usd_per_trade": expectancy_usd, "usd_per_week": expectancy_usd * trades_per_week},
        "verification": {"v3": v3, "v4": v4_jpy and v4_aud, "v5": v5, "v6": v6, "v8": pf > 1.3}
    }
    with Path("pass12_stats.json").open("w") as f:
        json.dump(report_data, f, indent=2)

if __name__ == "__main__":
    run_pass12()
