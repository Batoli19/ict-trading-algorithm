"""
run_pass16_hard_tp.py — PASS 16: Hard SL/TP, No Trailing, No Giveback Guard
Run from project root: python run_pass16_hard_tp.py

The question: what if we strip ALL exit interference and just let
price run to a clean fixed TP?

Every previous backtest had:
  - Giveback guard closing winners early at avg +$42
  - TP1 partial closing 60% of position at 1R
  - Trailing stop sometimes closing remainder at 0.7R
  - Combined: turns a potential $100+ trade into $42

This test removes ALL of that. Pure hard SL + hard TP only.

Configs:
  A — No trail, no guard, signal's natural TP (baseline comparison)
  B — Hard TP at 1.5R  (conservative, high fill probability)
  C — Hard TP at 2.0R  (standard ICT target)
  D — Hard TP at 3.0R  (full liquidity sweep)
  
All 4 configs run WITH direction filters (GBPUSD SELL only, USDJPY BUY only)
"""

import sys, gc, json, csv, copy
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent / "python"))

with open("config/settings.json", encoding="utf-8") as f:
    BASE_CONFIG = json.load(f)

PAIRS     = ["EURUSD", "USDJPY", "GBPUSD"]
DATA_DIR  = "data/m5_xm"
DATE_FROM = datetime(2024, 11, 5, tzinfo=timezone.utc)
DATE_TO   = datetime(2026, 2, 27, tzinfo=timezone.utc)
WEEKS     = (DATE_TO - DATE_FROM).days / 7

# Direction filter — confirmed bleeders from autopsy
BAD_COMBOS = {("GBPUSD", "BUY"), ("USDJPY", "SELL")}

# ── CONFIG BUILDER ────────────────────────────────────────────────────────────
def build_config():
    cfg = copy.deepcopy(BASE_CONFIG)

    cfg["disabled_setups"] = [
        "PIN_BAR", "LH_LL_CONTINUATION", "HH_HL_CONTINUATION",
        "LIQUIDITY_GRAB_CONTINUATION", "FVG_CONTINUATION",
        "CONTINUATION_OB", "ORDER_BLOCK", "FVG_ENTRY",
    ]

    cfg.setdefault("ict", {}).setdefault("kill_zones", {}).update({
        "london_open":  {"start": "06:00", "end": "09:00", "tz": "UTC"},
        "london_close": {"start": "15:00", "end": "17:00", "tz": "UTC"},
    })
    cfg.setdefault("hybrid", {})["allowed_kill_zones"] = [
        "LONDON_OPEN", "LONDON_CLOSE"
    ]

    # ── KILL ALL EXIT LOGIC ──────────────────────────────────────
    cfg["trade_management"] = {
        "giveback_guard": {"enabled": False},
        "partials": {
            "enabled": False,          # no TP1 partial close at all
            "tp1_r": 999,              # effectively disabled
            "tp1_close_pct": 0.0,
            "trail_only_after_tp1": False,
        },
        "time_exit": {"enabled": False},
    }

    cfg.setdefault("risk", {})["risk_per_trade_pct"] = 1.0
    cfg.setdefault("execution", {}).update({
        "min_rr": 1.5,
        "enforce_killzones": True,
    })

    return cfg


# ── SUBCLASSED ENGINE WITH TP OVERRIDE ───────────────────────────────────────
def make_engine(cfg, tp_r_override=None, no_trail=True):
    """
    Returns a BacktestEngine subclass that:
    1. Disables trailing entirely
    2. Optionally overrides TP to a fixed R multiple
    """
    from backtester import BacktestEngine, SimulatedTrade

    class HardTPEngine(BacktestEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._tp_r = tp_r_override  # None = use signal's natural TP
            if no_trail:
                self.use_trailing = False
                self.trailing_manager = None

        def _run_symbol(self, symbol, start_date, end_date, progress_every=500):
            """
            Override to intercept trade creation and replace TP if needed.
            We patch signal.tp before the parent method uses it.
            """
            if self._tp_r is None:
                # No override — run normally
                super()._run_symbol(symbol, start_date, end_date, progress_every)
                return

            # Monkey-patch: wrap strategy.analyze to override TP on every signal
            original_analyze = self.strategy.analyze

            def patched_analyze(*args, **kwargs):
                signal = original_analyze(*args, **kwargs)
                if signal and signal.valid:
                    risk = abs(signal.entry - signal.sl)
                    if risk > 0:
                        if signal.direction.value == "BUY":
                            signal.tp = signal.entry + risk * self._tp_r
                        else:
                            signal.tp = signal.entry - risk * self._tp_r
                return signal

            self.strategy.analyze = patched_analyze
            try:
                super()._run_symbol(symbol, start_date, end_date, progress_every)
            finally:
                self.strategy.analyze = original_analyze

    engine = HardTPEngine(
        config=cfg,
        data_dir=DATA_DIR,
        use_sniper_filter=False,
        max_open_trades=2,
        one_trade_per_symbol=True,
        signal_cooldown_bars=6,
        disabled_setups=cfg["disabled_setups"],
        killzone_only=True,
        use_trailing=not no_trail,
    )
    return engine


# ── RUNNER ────────────────────────────────────────────────────────────────────
def run(label, tp_r=None, no_trail=True):
    cfg = build_config()
    all_trades = []

    tp_label = f"{tp_r}R" if tp_r else "natural"
    print(f"\n  [{label}] TP={tp_label} | trail={'OFF' if no_trail else 'ON'} | guard=OFF")

    for pair in PAIRS:
        print(f"  [{label}] {pair} @ {datetime.now().strftime('%H:%M:%S')}", end="", flush=True)

        engine = make_engine(cfg, tp_r_override=tp_r, no_trail=no_trail)
        trades = engine.run(
            symbols=[pair],
            start_date=DATE_FROM,
            end_date=DATE_TO,
            progress_every=3000,
        )
        closed = [t for t in trades if t.exit_price is not None]

        # Direction filter — post-filter (backtester doesn't read per_symbol)
        before = len(closed)
        closed = [
            t for t in closed
            if (str(getattr(t, "symbol", "")), str(getattr(t, "direction", "")))
            not in BAD_COMBOS
        ]
        removed = before - len(closed)

        print(f" → {len(closed)} trades (dir filter: -{removed})")
        all_trades.extend(closed)
        del engine, trades
        gc.collect()

    return all_trades


def pnl(t):
    return float(getattr(t, "pnl_pips", 0) or 0)


def summarize(trades, label, risk_usd=50, avg_sl_pips=32):
    dpip = risk_usd / avg_sl_pips
    wins   = [t for t in trades if pnl(t) > 0]
    losses = [t for t in trades if pnl(t) <= 0]
    if not wins or not losses:
        print(f"  {label}: insufficient data")
        return {}

    gw  = sum(pnl(t) for t in wins)
    gl  = abs(sum(pnl(t) for t in losses))
    net = gw - gl
    pf  = gw / gl
    wr  = len(wins) / len(trades) * 100
    avg_w_p = gw / len(wins)
    avg_l_p = gl / len(losses)
    ppw = net / WEEKS
    dpw = ppw * dpip
    dpm = dpw * 4.33

    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"{'='*62}")
    print(f"  Trades:    {len(trades)} ({len(trades)/WEEKS:.1f}/week)")
    print(f"  Win Rate:  {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  PF:        {pf:.2f}")
    print(f"  Pips:      {net:+.1f}  ({ppw:+.1f}/week)")
    print(f"  Avg Win:   +{avg_w_p:.1f}p = +${avg_w_p*dpip:.0f}")
    print(f"  Avg Loss:  -{avg_l_p:.1f}p = -${avg_l_p*dpip:.0f}")
    print(f"  R:R:       {avg_w_p/avg_l_p:.2f}:1")
    print(f"  $/week:    ${dpw:+.0f}   $/month: ${dpm:+.0f}  (at $5K 1% risk)")

    # Exit reasons
    exits = defaultdict(lambda: [0, 0.0])
    for t in trades:
        r = str(getattr(t, "exit_reason", "?"))
        exits[r][0] += 1; exits[r][1] += pnl(t)
    print(f"\n  Exit reasons:")
    for r, (cnt, p) in sorted(exits.items(), key=lambda x: -x[1][0]):
        print(f"    {r:<22} {cnt:>4}tr | {p:>+8.1f}p | avg {p/cnt:>+6.1f}p")

    # By setup
    by_s = defaultdict(list)
    for t in trades: by_s[str(getattr(t, "setup_type", "?"))].append(t)
    print(f"\n  By Setup:")
    for s, st in sorted(by_s.items(), key=lambda x: -sum(pnl(t) for t in x[1])):
        sw = [t for t in st if pnl(t) > 0]
        net_s = sum(pnl(t) for t in st)
        gw_s = sum(pnl(t) for t in sw)
        gl_s = abs(sum(pnl(t) for t in st if pnl(t) <= 0))
        pf_s = gw_s/gl_s if gl_s else float("inf")
        v = "✅" if net_s > 0 else "❌"
        wr_s = len(sw)/len(st)*100
        print(f"    {v} {s:<35} {len(st):>3}tr | WR {wr_s:.0f}% | PF {pf_s:.2f} | {net_s:>+8.1f}p")

    # By pair+direction
    combos = defaultdict(list)
    for t in trades:
        combos[f"{getattr(t,'symbol','?')} {getattr(t,'direction','?')}"].append(t)
    print(f"\n  By Pair+Direction:")
    for k, kt in sorted(combos.items(), key=lambda x: -sum(pnl(t) for t in x[1])):
        kw = [t for t in kt if pnl(t) > 0]
        net_k = sum(pnl(t) for t in kt)
        gw_k = sum(pnl(t) for t in kw)
        gl_k = abs(sum(pnl(t) for t in kt if pnl(t) <= 0))
        pf_k = gw_k/gl_k if gl_k else float("inf")
        v = "✅" if net_k > 0 else "❌"
        print(f"    {v} {k:<15} {len(kt):>3}tr | WR {len(kw)/len(kt)*100:.0f}% | PF {pf_k:.2f} | {net_k:>+8.1f}p")

    # Monthly
    by_m = defaultdict(list)
    for t in trades: by_m[str(getattr(t, "entry_time", ""))[:7]].append(t)
    green = red = 0
    print(f"\n  Monthly:")
    for m in sorted(by_m.keys()):
        mt = by_m[m]; mw = [t for t in mt if pnl(t) > 0]
        net_m = sum(pnl(t) for t in mt)
        if net_m > 0: green += 1
        else: red += 1
        print(f"    {'✅' if net_m>0 else '❌'} {m}  {len(mt):>3}tr | WR {len(mw)/len(mt)*100:.0f}% | {net_m:>+8.1f}p")
    print(f"\n  Profitable months: {green}/{green+red} = {green/(green+red)*100:.0f}%")
    print(f"{'='*62}")

    return {
        "trades": len(trades), "wr": wr, "pf": pf,
        "pips": net, "ppw": ppw, "dpw": dpw, "dpm": dpm,
        "avg_w": avg_w_p, "avg_l": avg_l_p,
    }


def save_csv(trades, filename):
    if not trades: return
    fields = ["symbol","setup_type","direction","kill_zone",
              "entry_time","exit_time","pnl_pips","exit_reason"]
    with open(filename, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            row = {f: getattr(t, f, "") for f in fields}
            if not row["kill_zone"]:
                row["kill_zone"] = getattr(t, "killzone", "")
            w.writerow(row)
    print(f"  Saved → {filename}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    print("\n" + "="*62)
    print("  PASS 16 — HARD SL/TP, NO TRAILING, NO GUARD")
    print("="*62)
    print(f"  Pairs:      {PAIRS} (direction filtered)")
    print(f"  Setups:     CHOCH + LSR only")
    print(f"  Sessions:   London Open + London Close")
    print(f"  Range:      {DATE_FROM.date()} → {DATE_TO.date()}")
    print(f"  Exit logic: SL hit = full loss | TP hit = full profit")
    print(f"  No trailing. No giveback guard. No partial closes.")
    print(f"\n  Configs:")
    print(f"  A — Natural TP from signal  (CHOCH=2.5R, LSR=2.5R)")
    print(f"  B — Hard TP at 1.5R")
    print(f"  C — Hard TP at 2.0R")
    print(f"  D — Hard TP at 3.0R")
    print("="*62)

    results = {}

    print(f"\n\n>>> A — NATURAL TP (signal's own TP, no override)")
    t_A = run("A-NATURAL", tp_r=None, no_trail=True)
    results["A"] = summarize(t_A, "A — Natural TP | No trail | No guard")
    save_csv(t_A, "pass16_A_natural_tp.csv")

    print(f"\n\n>>> B — HARD TP 1.5R")
    t_B = run("B-1.5R", tp_r=1.5, no_trail=True)
    results["B"] = summarize(t_B, "B — Hard TP 1.5R | No trail | No guard")
    save_csv(t_B, "pass16_B_1r5.csv")

    print(f"\n\n>>> C — HARD TP 2.0R")
    t_C = run("C-2.0R", tp_r=2.0, no_trail=True)
    results["C"] = summarize(t_C, "C — Hard TP 2.0R | No trail | No guard")
    save_csv(t_C, "pass16_C_2r0.csv")

    print(f"\n\n>>> D — HARD TP 3.0R")
    t_D = run("D-3.0R", tp_r=3.0, no_trail=True)
    results["D"] = summarize(t_D, "D — Hard TP 3.0R | No trail | No guard")
    save_csv(t_D, "pass16_D_3r0.csv")

    # ── FINAL TABLE ───────────────────────────────────────────────────────────
    print(f"\n\n{'='*62}")
    print(f"  PASS 16 — FINAL COMPARISON")
    print(f"{'='*62}")
    print(f"\n  {'Config':<30} {'Tr':>4} {'WR':>5} {'PF':>5} {'Avg W':>7} {'Avg L':>7} {'R:R':>5} {'$/wk':>7} {'$/mo':>7}")
    print(f"  {'-'*80}")

    labels = {
        "A": "A — Natural TP (2.5R signal)",
        "B": "B — Hard 1.5R TP",
        "C": "C — Hard 2.0R TP",
        "D": "D — Hard 3.0R TP",
    }
    for k, lbl in labels.items():
        r = results.get(k, {})
        if r:
            print(
                f"  {lbl:<30} {r['trades']:>4} {r['wr']:>4.0f}% "
                f"{r['pf']:>5.2f} {r['avg_w']:>+6.1f}p {r['avg_l']:>+6.1f}p "
                f"{r['avg_w']/r['avg_l']:>5.2f} "
                f"{r['dpw']:>+6.0f}$ {r['dpm']:>+6.0f}$"
            )

    print(f"\n  Previous best (Pass 15C with guard):   PF 1.16 | +$33/week")
    print(f"\n  Key question answered by this test:")
    print(f"  → Does letting price run to a fixed target beat")
    print(f"    the trail+guard system that's cutting wins short?")

    # Best config
    if results:
        best_k = max(results.keys(), key=lambda k: results[k].get("dpw", 0))
        best = results[best_k]
        print(f"\n  ✅ Best config: {labels[best_k]}")
        print(f"     ${best['dpw']:+.0f}/week | ${best['dpm']:+.0f}/month | PF {best['pf']:.2f}")

        if best["dpw"] > 100:
            print(f"\n  🎯 BREAKTHROUGH: ${best['dpw']:.0f}/week on $5K")
            print(f"     At $25K funded: ${best['dpw']*5:.0f}/week ← exceeds $300 target")
        elif best["dpw"] > 57:
            print(f"\n  ✅ IMPROVEMENT: {best['dpw']/57*100:.0f}% better than current")
        else:
            print(f"\n  ⚠️  Hard TP does not improve on trail+guard system")
            print(f"     Trailing is capturing more pips than fixed TP would")

    print(f"\n{'='*62}")
    print(f"  Pass 16 complete.")
    print(f"{'='*62}\n")


if __name__ == "__main__":
    main()
