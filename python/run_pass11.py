import json
import subprocess
import csv
from pathlib import Path
from datetime import datetime

CONFIG_PATH = Path("../config/backtest_config.json")
RESULTS_PATH = Path("../backtest_results.csv")

def run_backtest(name, allowed_kzs, disabled_setups):
    print(f"\n>>> STARTING SUB-RUN: {name}")
    
    # 1. Update config
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    
    config["hybrid"]["allowed_kill_zones"] = allowed_kzs
    config["disabled_setups"] = disabled_setups
    
    # Ensure direction filters are correct
    config["direction_filters"] = {
        "USDJPY": ["SELL"],
        "AUDUSD": ["BUY"]
    }
    
    # Block other pairs from sessions if needed (though using allowed_kill_zones is cleaner)
    # The requirement says: Sub-A (LO only), Sub-B (NY only), Sub-C (LC only)
    
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    
    # 2. Run backtest
    subprocess.run(["python", "run_backtest.py"], check=True)
    
    # 3. Rename results
    output_csv = f"pass11_{name.replace(' ', '_')}_results.csv"
    if RESULTS_PATH.exists():
        RESULTS_PATH.replace(output_csv)
    return output_csv

def analyze_results(csv_file):
    trades = []
    if not Path(csv_file).exists():
        return None
        
    with open(csv_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)
            
    if not trades:
        return {
            "count": 0, "wr": 0, "pf": 0, "pips": 0, "expectancy": 0,
            "avg_win": 0, "avg_loss": 0, "setups": {}, "exits": {}
        }
        
    wins = [float(t["pnl_pips"]) for t in trades if float(t["pnl_pips"]) > 0]
    losses = [abs(float(t["pnl_pips"])) for t in trades if float(t["pnl_pips"]) <= 0]
    
    total_pips = sum(float(t["pnl_pips"]) for t in trades)
    wr = (len(wins) / len(trades)) * 100
    
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    pf = gross_profit / gross_loss if gross_loss > 0 else 999.0
    
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    
    # Setup breakdown
    setups = {}
    for t in trades:
        s = t["setup_type"]
        if s not in setups: setups[s] = {"count": 0, "pips": 0}
        setups[s]["count"] += 1
        setups[s]["pips"] += float(t["pnl_pips"])
        
    # Exit reasons
    exits = {}
    for t in trades:
        e = t["exit_reason"]
        if e not in exits: exits[e] = {"count": 0, "pips": 0}
        exits[e]["count"] += 1
        exits[e]["pips"] += float(t["pnl_pips"])

    # Jan/Feb breakdown
    months = {"01": 0, "02": 0}
    for t in trades:
        m = t["entry_time"].split("-")[1]
        if m in months: months[m] += float(t["pnl_pips"])

    return {
        "count": len(trades),
        "wr": wr,
        "pf": pf,
        "pips": total_pips,
        "expectancy": total_pips / len(trades),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "setups": setups,
        "exits": exits,
        "months": months,
        "earliest": min(t["entry_time"].split()[1] for t in trades) if trades else "N/A"
    }

def main():
    base_disables = ["PIN_BAR", "FVG", "CONTINUATION_OB"]
    
    # Sub-run A: LONDON_OPEN (06:00-09:00), CHOCH + LSR only
    disables_a = base_disables + ["LH_LL_CONTINUATION", "HH_HL_CONTINUATION", "LIQUIDITY_GRAB_CONTINUATION"]
    res_a_file = run_backtest("London Open", ["LONDON_OPEN"], disables_a)
    analysis_a = analyze_results(res_a_file)
    
    # Sub-run B: NY_OPEN (13:30-16:00), All except base disables
    res_b_file = run_backtest("True NY", ["NY_OPEN"], base_disables)
    analysis_b = analyze_results(res_b_file)
    
    # Sub-run C: LONDON_CLOSE (15:00-17:00), CHOCH + LSR only
    disables_c = base_disables + ["LH_LL_CONTINUATION", "HH_HL_CONTINUATION", "LIQUIDITY_GRAB_CONTINUATION"]
    res_c_file = run_backtest("London Close", ["LONDON_CLOSE"], disables_c)
    analysis_c = analyze_results(res_c_file)
    
    # Generate report
    with open("pass11_raw_report.json", "w") as f:
        json.dump({"A": analysis_a, "B": analysis_b, "C": analysis_c}, f, indent=2)
        
    print("\n>>> PASS 11 COMPLETE. Generating summary report...")
    
    with open("pass11_summary.md", "w") as f:
        f.write("# Pass 11: Corrected Kill Zones Analysis\n\n")
        
        for name, data in [("London Open", analysis_a), ("True NY", analysis_b), ("London Close", analysis_c)]:
            f.write(f"## {name} Sub-Run\n")
            if not data or data["count"] == 0:
                f.write("No trades fired.\n\n")
                continue
            f.write(f"- Total Trades: {data['count']}\n")
            f.write(f"- Win Rate: {data['wr']:.1f}%\n")
            f.write(f"- Profit Factor: {data['pf']:.2f}\n")
            f.write(f"- Total Pips: {data['pips']:.1f}\n")
            f.write(f"- Expectancy: {data['expectancy']:.2f} pips\n")
            f.write(f"- Avg Win: {data['avg_win']:.1f} | Avg Loss: {data['avg_loss']:.1f}\n")
            f.write(f"- Earliest Entry: {data['earliest']}\n\n")
            
            f.write("### Setup Breakdown\n")
            for s, sdata in data["setups"].items():
                f.write(f"- {s}: {sdata['count']} trades | {sdata['pips']:.1f} pips\n")
            f.write("\n")
            
            f.write("### Exit Breakdown\n")
            for e, edata in data["exits"].items():
                f.write(f"- {e}: {edata['count']} trades | {edata['pips']:.1f} pips\n")
            f.write("\n")

if __name__ == "__main__":
    main()
