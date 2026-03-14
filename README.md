# 🤖 ICT Trading Algorithm 

<div align="center">

**A fully automated Forex & Stock trading bot powered by ICT (Inner Circle Trader) methodology**

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![MetaTrader](https://img.shields.io/badge/MetaTrader-5-7B2FBE?style=for-the-badge&logoColor=white)](https://metatrader5.com)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=for-the-badge)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-22C55E?style=for-the-badge)]()

*Trades EUR/USD · GBP/USD · XAU/USD · US30 · NAS100*

</div>

---

## ⚠️ Risk Disclaimer

> **Trading involves substantial risk of loss. This bot does NOT guarantee profits. Never trade with money you cannot afford to lose. Always test on a demo account first.**

---

## 📖 What Is This?

This is a fully automated trading bot that watches the market 24/7, identifies high-probability trade setups using **ICT (Inner Circle Trader)** concepts, and automatically places, manages, and closes trades on **MetaTrader 5**.

You configure it once, and it runs on its own — no need to sit at a screen all day.

---
## documentation testing 
<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/245eb0e9-8e6e-429f-ad14-71a3c2106bba" />


## 🧠 Trading Strategy

The bot uses **pure ICT methodology** — the same concepts used by professional smart money traders:

### Setups (in order of priority)

| # | Setup | What It Does |
|---|-------|-------------|
| 1 | 🔴 **Stop Hunt** | Detects when smart money sweeps equal highs/lows to grab liquidity, then enters on the reversal |
| 2 | 🔴 **Turtle Soup** | Identifies when price breaks an N-bar high/low then reverses — classic liquidity grab fade |
| 3 | 🟡 **Fair Value Gap (FVG)** | Finds 3-candle price imbalances and enters when price returns to fill them |
| 4 | 🟡 **Order Blocks** | Detects the last opposing candle before a major impulse move — institutional entry zones |
| 5 | 🟢 **Scalping** | Fast M1 momentum entries within kill zones for quick small profits |

### Filters Applied to Every Trade

| Filter | Purpose |
|--------|---------|
| ⏰ **Kill Zones** | Only trades London Open, NY Open, London Close — highest liquidity windows |
| 📊 **HTF Bias** | Uses H4 market structure to only trade in the dominant direction |
| 📰 **News Filter** | Blocks all trading 30 min before and 15 min after high-impact news events |

---

## 🎯 Instruments Traded

| Instrument | Type | Best Session |
|-----------|------|-------------|
| **EUR/USD** | Forex Major | London + NY |
| **GBP/USD** | Forex Major | London + NY |
| **XAU/USD** | Gold | London + NY |
| **US30** | Dow Jones Index | NY Open |
| **NAS100** | Nasdaq Index | NY Open |

---

## 🕐 Kill Zones (UTC)

```
07:00 ─────────── 10:00   🇬🇧 London Open
12:00 ─────────── 15:00   🇺🇸 New York Open  
15:00 ─────────── 17:00   🔁 London Close
```
*The bot only places new trades during these windows.*

---

## 🏗️ Project Structure

```
ict_trading_bot/
│
├── 📁 python/                   ← Python brain (strategy + execution)
│   ├── main.py                  ← 🚀 Start the bot here
│   ├── bot_engine.py            ← Master loop — scans all pairs every 10s
│   ├── ict_strategy.py          ← All ICT logic (FVG, Stop Hunt, etc.)
│   ├── mt5_connector.py         ← MetaTrader 5 bridge (orders & data)
│   ├── risk_manager.py          ← Position sizing, daily limits, journal
│   ├── news_filter.py           ← Economic calendar integration
│   ├── notifier.py              ← Telegram & email alerts
│   ├── dashboard.py             ← Live terminal display
│   └── config_loader.py         ← Settings validation
│
├── 📁 mql5/
│   └── ICT_Bot_EA.mq5           ← Standalone MT5 Expert Advisor (no Python needed)
│
├── 📁 config/
│   ├── settings.json            ← ⚠️ Your credentials (gitignored, never uploaded)
│   ├── settings.example.json    ← ✅ Safe example — copy this to get started
│   └── settings.template.json   ← ✅ Safe template (legacy)
│
├── 📁 docs/
│   ├── SETUP_GUIDE.md           ← Full setup instructions
│   └── GITHUB_PUSH_GUIDE.md    ← How to push updates to GitHub
│
├── 📁 logs/                     ← Auto-generated trade logs
├── requirements.txt             ← Python dependencies
└── README.md                    ← You are here
```

---

## ⚡ Quick Start

### Prerequisites
- [Python 3.10+](https://python.org/downloads)
- [MetaTrader 5](https://metatrader5.com) installed with a broker account

### Configure
Copy `[config/settings.example.json](config/settings.example.json)` to `config/settings.json` and fill in your MT5 credentials (this file is gitignored and should never be committed).
- A live or **demo** MT5 account

### 1. Clone the repo
```bash
git clone https://github.com/Batoli19/ict-trading-bot.git
cd ict-trading-bot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure your credentials
```bash
# Windows
copy config\settings.template.json config\settings.json

# Mac/Linux
cp config/settings.template.json config/settings.json
```

Then open `config/settings.json` and fill in:
```json
{
  "mt5": {
    "login":    12345678,
    "password": "your_mt5_password",
    "server":   "YourBroker-Demo"
  }
}
```

### 4. Start the bot
```bash
cd python
python main.py
```

A live terminal dashboard appears showing your account balance, open positions, and upcoming news events.

**To stop:** Press `Ctrl+C` — shuts down cleanly without leaving open trades.

---

## 🛡️ Risk Management

The bot automatically protects your account:

```
✅ Risk per trade    →  1% of balance per trade (configurable)
✅ Daily loss limit  →  Stops trading if down 3% on the day
✅ Max open trades   →  Hard cap of 3 simultaneous positions
✅ Trailing stop     →  Moves SL automatically to lock in profits
✅ News blackout     →  No entries 30 min before high-impact events
✅ Kill zone gate    →  Only enters during London & NY sessions
✅ HTF bias filter   →  Only trades with the H4 trend direction
```

All values are configurable in `config/settings.json`.

---

## 🔔 Notifications

Get alerted on every trade entry, exit, and error:

**📱 Telegram (recommended)**
1. Message `@BotFather` on Telegram → `/newbot` → copy your token
2. Find your chat ID: `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Add both to `settings.json → notifications → telegram → enabled: true`

**📧 Email**
- Add your SMTP details to `settings.json → notifications → email → enabled: true`

---

## 🖥️ Running 24/7 (Without Leaving Your PC On)

For reliable 24/7 operation use a **VPS (Virtual Private Server)** — a cloud computer that never turns off:

| Provider | Price | Notes |
|----------|-------|-------|
| [Contabo](https://contabo.com) | ~$7/mo | Cheapest option |
| [Forex VPS](https://forexvps.net) | ~$20/mo | MT5 optimized |
| [AWS Lightsail](https://lightsail.aws.amazon.com) | ~$10/mo | Most reliable |

Install Python + MT5 on the VPS, clone this repo, and the bot runs 24/7 even when your PC is off.

---

## 🔧 Two Deployment Options

**Option A — Python Bot** *(recommended, full featured)*
```bash
cd python
python main.py
```

**Option B — MQL5 Expert Advisor** *(no Python needed, runs inside MT5)*
1. Copy `mql5/ICT_Bot_EA.mq5` to MT5's Experts folder
2. Open MetaEditor → compile with F7
3. Drag onto any chart and click the AutoTrading button ▶️

---

## 🔍 Troubleshooting

| Problem | Solution |
|---------|---------|
| `MT5 connection failed` | Double-check login, password, and server name in settings.json |
| `No candle data for symbol` | Right-click the symbol in MT5 Market Watch → Show |
| `No trades being placed` | Make sure you're running during London or NY kill zone hours |
| `Lot size too small` | Increase `risk_per_trade_pct` or fund account further |
| `News filter always blocking` | Temporarily set `"enabled": false` in news config to test |

---

## 📜 License

[MIT License](LICENSE) — free to use, modify, and distribute.

---

<div align="center">

**Built with ❤️ using ICT Smart Money Concepts**

*Not financial advice. Trade responsibly.*

</div>
