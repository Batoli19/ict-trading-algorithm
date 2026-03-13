import csv
from collections import defaultdict

def pip_size(symbol):
    return 0.01 if symbol.endswith('JPY') else 0.0001

rows=[]
with open('backtest_results.csv', newline='') as f:
    rows=list(csv.DictReader(f))

mismatches=[]
for r in rows:
    sym=r['symbol']
    entry=float(r['entry_price']) if r.get('entry_price') else None
    exitp=float(r['exit_price']) if r.get('exit_price') else None
    if entry is None or exitp is None:
        continue
    direction=r['direction'].upper()
    ps=pip_size(sym)
    raw=(exitp-entry)/ps
    if direction=='SELL':
        raw=-raw
    pnl=float(r['pnl_pips']) if r.get('pnl_pips') else 0.0
    diff=raw-pnl
    # flag big diffs
    if abs(diff) > 0.5:  # half pip
        mismatches.append((sym, direction, entry, exitp, pnl, raw, diff, r.get('exit_reason')))

# summarize
print('TOTAL', len(rows))
print('MISMATCHES', len(mismatches))

# show worst 20
mismatches.sort(key=lambda x: abs(x[6]), reverse=True)
for m in mismatches[:20]:
    print(m)

# by symbol avg abs diff
by=defaultdict(list)
for m in mismatches:
    by[m[0]].append(abs(m[6]))
print('\nAVG_ABS_DIFF_BY_SYMBOL')
for sym, vals in sorted(by.items()):
    print(sym, sum(vals)/len(vals), 'count', len(vals))
