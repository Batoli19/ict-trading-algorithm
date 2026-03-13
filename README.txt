─────────────────────────────────────────────────────────────
ICT TRADING BOT — PROJECT OVERVIEW
====================================

This is an automated trading bot that trades Forex on MetaTrader 5.
It uses ICT (Inner Circle Trader) price action methods.

HOW TO USE THIS PROJECT:
─────────────────────────
  To START the bot:      run START_BOT.py
  To STOP the bot:       run STOP_BOT.py
  To check performance:  run CHECK_PERFORMANCE.py

FOLDER GUIDE:
─────────────
  01_LIVE_BOT/        — the actual bot code (do not edit unless sure)
  02_BACKTESTER/      — testing the bot on historical data
  03_BACKTEST_RESULTS — all test results saved here
  04_BRAIN/           — the learning system (AI upgrade)
  05_DATA/            — historical price data
  06_CONFIG/          — bot settings and rules
  07_LOGS/            — daily activity logs
  08_DOCS/            — explanations and guides

WHAT THE BOT TRADES:
────────────────────
  Pairs:    EURUSD, GBPUSD (sell only), USDJPY (buy only)
  Sessions: London Open (8am-11am Gaborone time)
             London Close (5pm-7pm Gaborone time)
  Risk:     1% per trade ($50 on a $5,000 account)

CURRENT STATUS:
───────────────
  Strategy:  Validated across 16 months of data
  Live demo: Running on MetaQuotes demo account
  Go-live:   March 28, 2026
─────────────────────────────────────────────────────────────
