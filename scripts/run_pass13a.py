"""
Pass 13A: Personality A High Frequency Backtest (Nov 2024 - Feb 2026)
=====================================================================
Evaluates the "Personality A" high frequency filter criteria over 16 months.
Runs using data from data/m5_xm/.
"""
import sys
import logging
from pathlib import Path
from datetime import datetime, timezone
import json
from collections import defaultdict
import pandas as pd

# Add python dir to path so we can import modules
sys.path.append(str(Path(__file__).resolve().parent.parent / "python"))

from backtester import BacktestEngine
from backtest_report import BacktestReport
from config_loader import (
    _normalize_execution_gates,
    _normalize_trailing_structure,
    _normalize_trade_management,
)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger("PASS_13A")

# For logging only when absolutely needed to avoid clutter
for noisy in ("ICT", "ICT_SETUPS", "TRAIL", "SNIPER"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

def run():
    print("=" * 60)
    print("PASS 13A: PERSONALITY A BACKTEST (Nov 2024 - Feb 2026)")
    print("=====================================================================")
    
    root_dir = Path(__file__).resolve().parent.parent
    config_path = root_dir / "config" / "backtest_config_personality_a.json"
    main_config_path = root_dir / "config" / "settings.json"
    
    with open(config_path, "r") as f:
        bt_cfg = json.load(f)
        
    with open(main_config_path, "r") as f:
        main_cfg = json.load(f)
        
    # Merge configs
    config = main_cfg.copy()
    for k, v in bt_cfg.items():
        if isinstance(v, dict) and k in config:
            config[k].update(v)
        else:
            config[k] = v
            
    # Normalize settings
    _normalize_execution_gates(config)
    _normalize_trailing_structure(config)
    _normalize_trade_management(config)
    
    print("\nPERSONALITY A CONFIG:")
    print(f"  Pairs:         {config.get('pairs')}")
    disabled = config.get('disabled_setups', [])
    print(f"  Disabled Setups: {len(disabled)} ({', '.join(disabled)})")
    print(f"  Kill zones:    {config.get('allowed_kill_zones')}")
    print(f"  Direction:     NONE (both ways all pairs)")
    print(f"  Daily limit:   DISABLED")
    giveback = config.get('trade_management', {}).get('giveback_guard', {})
    print(f"  Giveback:      {giveback.get('max_giveback_pct', 0.01)*100}% at {giveback.get('activate_at_r', 0.5)}R")
    print(f"  Date range:    {config.get('date_from')} -> {config.get('date_to')}")
    print(f"  Data dir:      {config.get('data_dir')}")
    print("=" * 60)
    
    # ─── RUN BACKTEST ───
    engine = BacktestEngine(
        config=config,
        data_dir=root_dir / config.get("data_dir", "data/m5_xm"),
        use_sniper_filter=True,
        max_open_trades=3,
        disabled_setups=config.get("disabled_setups", []),
        killzone_only=config.get("execution", {}).get("enforce_killzones", True),
        use_trailing=True,
    )
    
    # Hack the same_regime_limit for open trades by monkeypatching
    original_check = engine._run_symbol
    def _run_symbol_with_regime_limit(*args, **kwargs):
        # The backtester processes per symbol sequentially, so global state across
        # symbols requires an external check. We'll post-process trades.
        return original_check(*args, **kwargs)
    
    # Start/End dates
    start_date = datetime.strptime(config["date_from"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_date = datetime.strptime(config["date_to"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    
    trades = engine.run(
        symbols=config.get("pairs"),
        start_date=start_date,
        end_date=end_date,
    )
    
    # ─── POST PROCESS SAME-REGIME FILTER ───
    # Sort all trades globally by entry time
    trades.sort(key=lambda t: t.entry_time)
    
    filtered_trades = []
    for t in trades:
        # Check if we already have an open trade in this direction at this entry time
        # simulated open window is from entry_time to exit_time
        same_dir_open = 0
        for opened in filtered_trades:
            if opened.exit_time is None or opened.exit_time > t.entry_time:
                # Trade 'opened' is active exactly at the moment 't' is entered
                if opened.direction == t.direction:
                    same_dir_open += 1
        
        if same_dir_open < config.get("same_regime_limit", 1):
            filtered_trades.append(t)
        else:
            engine.signals_filtered += 1
            engine.filtered_signals.append({
                "symbol": t.symbol, "time": t.entry_time.isoformat(),
                "setup_type": t.setup_type, "direction": t.direction,
                "skip_reason": "SAME_REGIME_LIMIT",
            })
            
    print(f"\nFiltered total trades from {len(trades)} -> {len(filtered_trades)} using same_regime_limit.")
    trades = [t for t in filtered_trades if t.exit_reason and t.exit_reason != "END_OF_DATA"]
    
    generate_comprehensive_report(trades, engine.filtered_signals, start_date, end_date)


def generate_comprehensive_report(trades, filtered_signals, start_date, end_date):
    print("\n\n" + "=" * 60)
    print("PASS 13A RESULTS: PERSONALITY A (NOV 2024 - FEB 2026)")
    print("=" * 60)
    
    total = len(trades)
    if total == 0:
        print("No trades taken.")
        return
        
    winners = [t for t in trades if t.pnl_pips > 0]
    losers = [t for t in trades if t.pnl_pips < 0]
    win_rate = len(winners) / total * 100
    
    gross_profit = sum(t.pnl_pips for t in winners)
    gross_loss = abs(sum(t.pnl_pips for t in losers))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    total_pips = sum(t.pnl_pips for t in trades)
    
    span_days = max(1.0, (end_date - start_date).days)
    span_weeks = span_days / 7.0
    trades_per_week = total / span_weeks
    
    # Financial metrics assumption: 1 risk = $50
    risk_usd = 50.0
    # Average R achieved
    avg_r_win = sum(t.rr_achieved for t in winners) / len(winners) if winners else 0
    avg_r_loss = sum(t.rr_achieved for t in losers) / len(losers) if losers else 0
    total_r = sum(t.rr_achieved for t in trades)
    net_usd = total_r * risk_usd
    expectancy_usd = net_usd / total
    
    # Streak and DD
    max_consec_losses = 0
    curr_streak = 0
    running_r = 0
    max_r = 0
    max_dd_r = 0
    
    for t in trades:
        if t.pnl_pips < 0:
            curr_streak += 1
            max_consec_losses = max(max_consec_losses, curr_streak)
        else:
            curr_streak = 0
            
        running_r += t.rr_achieved
        max_r = max(max_r, running_r)
        dd = max_r - running_r
        max_dd_r = max(max_dd_r, dd)
        
    max_dd_usd = max_dd_r * risk_usd
    max_dd_pct = (max_dd_usd / 5000.0) * 100  # assuming $5K account
    largest_loss = min(t.rr_achieved * risk_usd for t in trades)
    
    print("\n1. OVERALL SUMMARY")
    print("-" * 40)
    print(f"Total Trades:     {total} ({trades_per_week:.1f}/wk)")
    print(f"Win Rate:         {win_rate:.1f}%")
    print(f"Profit Factor:    {pf:.2f}")
    print(f"Total Pips:       {total_pips:+.1f}")
    print(f"Net USD:          ${net_usd:+.2f} (Total {total_r:+.2f} R)")
    print(f"Expectancy:       ${expectancy_usd:+.2f} per trade")
    print(f"Max Consec Loss:  {max_consec_losses}")
    print(f"Max Drawdown:     {max_dd_r:.2f} R (${max_dd_usd:.2f} / {max_dd_pct:.1f}%)")
    print(f"Largest Loss:     ${largest_loss:.2f}")
    
    print("\n2. MONTHLY BREAKDOWN")
    print("-" * 65)
    print(f"{'Month':<10} | {'Trades':<8} | {'WR %':<6} | {'PF':<5} | {'Pips':<8} | {'Net USD':<8}")
    print("-" * 65)
    
    monthly = defaultdict(list)
    for t in trades:
        ym = f"{t.entry_time.year}-{t.entry_time.month:02d}"
        monthly[ym].append(t)
        
    for ym in sorted(monthly.keys()):
        mtrades = monthly[ym]
        mwins = [t for t in mtrades if t.pnl_pips > 0]
        mloss = [t for t in mtrades if t.pnl_pips < 0]
        mwr = len(mwins) / len(mtrades) * 100
        mgp = sum(t.pnl_pips for t in mwins)
        mgl = abs(sum(t.pnl_pips for t in mloss))
        mpf = mgp / mgl if mgl > 0 else 99
        mpips = sum(t.pnl_pips for t in mtrades)
        musd = sum(t.rr_achieved for t in mtrades) * risk_usd
        print(f"{ym:<10} | {len(mtrades):<8} | {mwr:5.1f}% | {mpf:4.2f} | {mpips:+7.1f} | ${musd:+7.2f}")
        
    print("\n3. PER-PAIR BREAKDOWN")
    print("-" * 75)
    print(f"{'Pair':<8} | {'Trades':<8} | {'WR %':<6} | {'PF':<5} | {'Pips':<8} | {'Avg W':<6} | {'Avg L':<6} | {'Net USD':<8}")
    print("-" * 75)
    
    pairs = defaultdict(list)
    for t in trades:
        pairs[t.symbol].append(t)
        
    for pair, ptrades in sorted(pairs.items(), key=lambda x: sum(t.rr_achieved for t in x[1]), reverse=True):
        pwins = [t for t in ptrades if t.pnl_pips > 0]
        ploss = [t for t in ptrades if t.pnl_pips < 0]
        pwr = len(pwins) / len(ptrades) * 100
        pgp = sum(t.pnl_pips for t in pwins)
        pgl = abs(sum(t.pnl_pips for t in ploss))
        ppf = pgp / pgl if pgl > 0 else 99
        ppips = sum(t.pnl_pips for t in ptrades)
        pusd = sum(t.rr_achieved for t in ptrades) * risk_usd
        pavgw = sum(t.pnl_pips for t in pwins)/len(pwins) if pwins else 0
        pavgl = sum(t.pnl_pips for t in ploss)/len(ploss) if ploss else 0
        print(f"{pair:<8} | {len(ptrades):<8} | {pwr:5.1f}% | {ppf:4.2f} | {ppips:+7.1f} | {pavgw:5.1f}  | {pavgl:5.1f}  | ${pusd:+7.2f}")
        
    print("\n4. PER-SETUP BREAKDOWN")
    print("-" * 65)
    print(f"{'Setup':<22} | {'Trades':<6} | {'WR %':<5} | {'PF':<5} | {'Pips':<7} | {'Net USD':<8}")
    print("-" * 65)
    
    setups = defaultdict(list)
    for t in trades:
        setups[t.setup_type].append(t)
        
    for setup, strades in sorted(setups.items(), key=lambda x: sum(t.rr_achieved for t in x[1]), reverse=True):
        swins = [t for t in strades if t.pnl_pips > 0]
        sloss = [t for t in strades if t.pnl_pips < 0]
        swr = len(swins) / len(strades) * 100
        sgp = sum(t.pnl_pips for t in swins)
        sgl = abs(sum(t.pnl_pips for t in sloss))
        spf = sgp / sgl if sgl > 0 else 99
        spips = sum(t.pnl_pips for t in strades)
        susd = sum(t.rr_achieved for t in strades) * risk_usd
        print(f"{setup:<22} | {len(strades):<6} | {swr:4.1f}% | {spf:4.2f} | {spips:+6.1f} | ${susd:+7.2f}")

    print("\n5. PER-SESSION BREAKDOWN")
    print("-" * 65)
    print(f"{'Session':<15} | {'Trades':<8} | {'WR %':<6} | {'PF':<5} | {'Pips':<8} | {'Net USD':<8}")
    print("-" * 65)
    
    sessions = defaultdict(list)
    for t in trades:
        sessions[t.killzone].append(t)
        
    for sess, st in sorted(sessions.items(), key=lambda x: sum(t.rr_achieved for t in x[1]), reverse=True):
        sw = [t for t in st if t.pnl_pips > 0]
        sl = [t for t in st if t.pnl_pips < 0]
        swr = len(sw) / len(st) * 100
        sgp = sum(t.pnl_pips for t in sw)
        sgl = abs(sum(t.pnl_pips for t in sl))
        spf = sgp / sgl if sgl > 0 else 99
        spips = sum(t.pnl_pips for t in st)
        susd = sum(t.rr_achieved for t in st) * risk_usd
        print(f"{sess:<15} | {len(st):<8} | {swr:5.1f}% | {spf:4.2f} | {spips:+7.1f} | ${susd:+7.2f}")

    print("\n6. EXIT REASON BREAKDOWN")
    print("-" * 55)
    reasons = defaultdict(list)
    for t in trades:
        reasons[t.exit_reason].append(t)
    for r, rtrades in sorted(reasons.items(), key=lambda x: len(x[1]), reverse=True):
        rpips = sum(t.pnl_pips for t in rtrades)
        rusd = sum(t.rr_achieved for t in rtrades) * risk_usd
        print(f"{r:<15} : {len(rtrades):<4} trades | {rpips:+7.1f} pips | ${rusd:+7.2f}")

    print("\n7. DAILY RETURN DISTRIBUTION")
    print("-" * 55)
    days = defaultdict(list)
    for t in trades:
        d = t.entry_time.date()
        days[d].append(t)
        
    daily_usd = {}
    for d, dtrades in days.items():
        daily_usd[d] = sum(t.rr_achieved for t in dtrades) * risk_usd
        
    if daily_usd:
        best_day = max(daily_usd.items(), key=lambda x: x[1])
        worst_day = min(daily_usd.items(), key=lambda x: x[1])
        days_over_200 = sum(1 for v in daily_usd.values() if v > 200)
        days_under_200 = sum(1 for v in daily_usd.values() if v < -200)
        win_days = [v for v in daily_usd.values() if v > 0]
        loss_days = [v for v in daily_usd.values() if v < 0]
        avg_win_day = sum(win_days) / len(win_days) if win_days else 0
        avg_loss_day = sum(loss_days) / len(loss_days) if loss_days else 0
        
        print(f"Total Trading Days: {len(days)}")
        print(f"Best Day:           {best_day[0]} (${best_day[1]:+.2f})")
        print(f"Worst Day:          {worst_day[0]} (${worst_day[1]:+.2f})")
        print(f"Days > $200 Profit: {days_over_200}")
        print(f"Days > $200 Loss:   {days_under_200}")
        print(f"Average Win Day:    ${avg_win_day:+.2f}")
        print(f"Average Loss Day:   ${avg_loss_day:+.2f}")


    print("\n8. WEEKLY INCOME ESTIMATE")
    print("-" * 55)
    weekly_avg = net_usd / span_weeks if span_weeks > 0 else 0
    print(f"Personality A Avg Weekly Income: ${weekly_avg:.2f}")
    
    print("\n=====================================================================")
    print("PERSONALITY A vs PERSONALITY B (Pass 1-12 Baseline)")
    print("=====================================================================")
    print(f"{'Metric':<22} | {'Personality B (7 wks)':<22} | {'Personality A (16 mos)'}")
    print(f"{'-'*22}-|-{'-'*22}-|-{'-'*22}")
    print(f"{'Total trades':<22} | {'39':<22} | {total}")
    print(f"{'Trades/week':<22} | {'5.6':<22} | {trades_per_week:.1f}")
    print(f"{'Win rate':<22} | {'56.4%':<22} | {win_rate:.1f}%")
    print(f"{'Profit Factor':<22} | {'1.48':<22} | {pf:.2f}")
    print(f"{'Expectancy ($/trade)':<22} | {'~$12':<22} | ${expectancy_usd:.2f}")
    print(f"{'Max consec losses':<22} | {'3':<22} | {max_consec_losses}")
    print(f"{'Max drawdown %':<22} | {'Unknown':<22} | {max_dd_pct:.1f}%")
    print(f"{'Weekly income (avg)':<22} | {'$66':<22} | ${weekly_avg:.2f}")
    
    # Save the output to a text file
    with open("pass13a_report.txt", "w") as f:
        # We redirect stdout so this is easiest done by copying everything manually...
        # Just write basic stuff for now, user can see the stdout
        f.write(f"Personality A Backtest: PF={pf:.2f}, WR={win_rate:.1f}%, Net=${net_usd:.2f}, Weekly=${weekly_avg:.2f}")

    # Export to CSV for deeper analysis
    csv_file = "pass13a_results.csv"
    df_trades = pd.DataFrame([{
        "symbol": t.symbol,
        "entry_time": t.entry_time,
        "exit_time": t.exit_time,
        "direction": t.direction,
        "setup": t.setup_type,
        "killzone": t.killzone,
        "pnl_pips": t.pnl_pips,
        "rr_achieved": t.rr_achieved,
        "usd_pnl": t.rr_achieved * 50.0,
        "exit_reason": t.exit_reason
    } for t in trades])
    df_trades.to_csv(csv_file, index=False)
    print(f"\nSaved raw trades to {csv_file}")


if __name__ == "__main__":
    run()
