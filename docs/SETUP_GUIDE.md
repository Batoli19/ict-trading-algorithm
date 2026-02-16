# 🤖 ICT Trading Bot — Complete Setup Guide

## What You Have

```
ict_trading_bot/
├── python/
│   ├── main.py            ← Start the bot from here
│   ├── bot_engine.py      ← Master orchestrator loop
│   ├── ict_strategy.py    ← All ICT logic (FVG, Stop Hunt, etc.)
│   ├── mt5_connector.py   ← MetaTrader 5 connection & orders
│   ├── risk_manager.py    ← Position sizing, daily limits
│   ├── news_filter.py     ← Economic calendar blocker
│   ├── notifier.py        ← Telegram / Email alerts
│   ├── dashboard.py       ← Live terminal display
│   ├── logger_setup.py    ← Colored logs + rotating file
│   └── config_loader.py   ← Loads & validates settings.json
│
├── mql5/
│   └── ICT_Bot_EA.mq5     ← Native MT5 Expert Advisor (standalone option)
│
├── config/
│   └── settings.json      ← ALL your settings live here
│
├── logs/                  ← Auto-generated trade logs
└── requirements.txt       ← Python dependencies
```

---

## STEP 1 — Prerequisites

### Software Required
- **MetaTrader 5** — Download from your broker or metatrader5.com
- **Python 3.10+** — Download from python.org (Windows 64-bit)
- A **live or demo account** with an MT5 broker

### Recommended Brokers (MT5 + good ICT spreads)
- **IC Markets** — Tight spreads on Forex & Gold
- **Pepperstone** — Great for US30/NAS100
- **FP Markets** — Good for multi-asset

---

## STEP 2 — Install Python Dependencies

Open a terminal/command prompt in the project folder:

```bash
pip install -r requirements.txt
```

---

## STEP 3 — Configure settings.json

Open `config/settings.json` and fill in:

```json
"mt5": {
    "login":    12345678,       ← Your MT5 account number
    "password": "your_password",
    "server":   "ICMarkets-Demo" ← Find this in MT5 → File → Open Account
}
```

### Adjust Risk Settings
```json
"risk": {
    "risk_per_trade_pct": 1.0,  ← 1% risk per trade (recommended)
    "max_daily_loss_pct": 3.0,  ← Stop trading if down 3% today
    "max_open_trades": 3        ← Max 3 trades at once
}
```

### Enable Notifications (Optional)

**Telegram** (recommended):
1. Message @BotFather on Telegram → `/newbot`
2. Copy the bot token
3. Message your bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   to find your chat_id
4. Fill in `settings.json → notifications → telegram`

---

## STEP 4A — Run Python Bot

```bash
cd python
python main.py
```

The live dashboard will appear in your terminal. The bot will:
- Connect to MT5 automatically
- Scan all 5 pairs every 10 seconds
- Only trade during London/NY kill zones
- Block trading 30 min before high-impact news
- Send Telegram/email alerts on every trade

**To stop:** Press `Ctrl+C` (closes cleanly, won't leave trades hanging)

---

## STEP 4B — Run MQL5 EA (Alternative / Standalone)

If you prefer the bot to run directly inside MT5:

1. Open MT5
2. Go to: **File → Open Data Folder → MQL5 → Experts**
3. Copy `ICT_Bot_EA.mq5` into that folder
4. In MT5: **Tools → MetaQuotes Language Editor** → Open the file → Compile (F7)
5. Drag `ICT_Bot_EA` from the Navigator panel onto a chart
6. Configure the input parameters
7. Enable **AutoTrading** (the green button at the top)

---

## STEP 5 — Backtest First!

**Always backtest before going live.**

### MQL5 EA Backtesting:
1. MT5 → **View → Strategy Tester**
2. Select `ICT_Bot_EA`
3. Set date range (at least 6 months)
4. Use **"Every tick based on real ticks"** for accuracy
5. Start on a **demo account** first

### Python Bot Paper Trading:
Set your MT5 account to a **demo account** — the bot will work identically but with fake money.

---

## Trading Strategy Summary

| Setup | Timeframe | Signal | Priority |
|-------|-----------|--------|----------|
| **Stop Hunt** | M15 | Equal H/L swept, displacement | ⭐⭐⭐ Highest |
| **Turtle Soup** | M15 | N-bar H/L swept, close back | ⭐⭐⭐ High |
| **FVG** | M15 | Price entering imbalance zone | ⭐⭐ Medium |
| **Order Block** | M15 | Reaction at OB zone | ⭐⭐ Medium |
| **Scalp** | M1 | Momentum in kill zone | ⭐ Lower |

### Kill Zones (UTC)
| Session | Time |
|---------|------|
| London Open | 07:00 – 10:00 |
| New York Open | 12:00 – 15:00 |
| London Close | 15:00 – 17:00 |

---

## Risk Warning

⚠️ **This bot trades real money. Never risk more than you can afford to lose.**

- Start on a **demo account** for at least 2-4 weeks
- Begin with **0.5% risk** per trade when going live
- Monitor the bot daily — no algo is 100% reliable
- Past backtesting performance does NOT guarantee future results

---

## Customizing Pairs

Edit `settings.json`:
```json
"pairs": ["EURUSD", "GBPUSD", "XAUUSD", "US30", "NAS100"]
```

Add any MT5 symbol your broker offers. Make sure the symbol name
matches exactly what appears in MT5's Market Watch panel.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `MT5 connection failed` | Check login/password/server in settings.json |
| `No candle data` | Ensure the symbol is visible in MT5 Market Watch |
| `Lot size too small` | Increase risk_per_trade_pct or account balance |
| `No trades placed` | Check kill zone timing (trade during London/NY sessions) |
| `News filter blocking` | Wait for the news window to pass, or disable in settings |

---

*Built with ICT methodology: FVG, Turtle Soup, Stop Hunt, Order Blocks, Kill Zones*
