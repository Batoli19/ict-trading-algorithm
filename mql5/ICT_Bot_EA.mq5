//+------------------------------------------------------------------+
//|  ICT_Bot_EA.mq5                                                  |
//|  ICT Trading Bot — MetaTrader 5 Expert Advisor                   |
//|  Execution Layer: Receives signals, manages orders               |
//+------------------------------------------------------------------+
#property copyright "ICT Trading Bot"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include <Trade\OrderInfo.mqh>

//+------------------------------------------------------------------+
//| Input Parameters                                                  |
//+------------------------------------------------------------------+

// Risk
input double  InpRiskPercent     = 1.0;    // Risk per trade (%)
input double  InpMaxDailyLoss    = 3.0;    // Max daily loss (%)
input int     InpMaxOpenTrades   = 3;      // Max concurrent trades
input double  InpRR              = 2.0;    // Risk:Reward ratio
input bool    InpTrailingStop    = true;   // Enable trailing stop
input int     InpTrailPips       = 15;     // Trailing stop distance (pips)

// ICT Strategy
input bool    InpFVG             = true;   // Enable FVG setups
input bool    InpTurtleSoup      = true;   // Enable Turtle Soup
input bool    InpStopHunt        = true;   // Enable Stop Hunt
input bool    InpOrderBlock      = true;   // Enable Order Block
input bool    InpKillZones       = true;   // Only trade in kill zones
input int     InpFVGMinPips      = 5;      // Min FVG gap (pips)
input int     InpLookback        = 20;     // Lookback for Turtle Soup

// Scalping
input bool    InpScalping        = true;   // Enable M1 scalping
input double  InpMaxSpreadPips   = 2.0;    // Max spread for scalping
input int     InpScalpTP         = 10;     // Scalp TP (pips)
input int     InpScalpSL         = 6;      // Scalp SL (pips)

// General
input ulong   InpMagic           = 20250101; // Magic number
input string  InpPairs           = "EURUSD,GBPUSD,XAUUSD,US30,NAS100"; // Pairs

//+------------------------------------------------------------------+
//| Global Variables                                                  |
//+------------------------------------------------------------------+
CTrade        g_trade;
CPositionInfo g_pos;

datetime      g_last_scan      = 0;
int           g_scan_interval  = 10;   // seconds
double        g_daily_pnl      = 0;
int           g_daily_trades   = 0;
datetime      g_today          = 0;

string        g_pairs[];

//+------------------------------------------------------------------+
//| Expert Initialization                                             |
//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(10);
   g_trade.SetTypeFilling(ORDER_FILLING_IOC);

   // Parse pairs
   StringSplit(InpPairs, ',', g_pairs);

   Print("✅ ICT Bot EA initialized | Pairs: ", InpPairs);
   Print("   Risk: ", InpRiskPercent, "% | Max trades: ", InpMaxOpenTrades,
         " | Magic: ", InpMagic);
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert Deinitialization                                           |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("🛑 ICT Bot EA stopped | Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Expert tick function                                              |
//+------------------------------------------------------------------+
void OnTick()
{
   datetime now = TimeCurrent();

   // Throttle scanning
   if (now - g_last_scan < g_scan_interval) return;
   g_last_scan = now;

   // Reset daily counters
   CheckDailyReset(now);

   // Manage open positions (trailing stop)
   ManagePositions();

   // Check daily limits
   if (!CanTrade()) return;

   // Scan each pair
   for (int i = 0; i < ArraySize(g_pairs); i++)
   {
      string sym = StringTrimRight(StringTrimLeft(g_pairs[i]));
      if (sym == "") continue;
      ScanSymbol(sym);
   }
}

//+------------------------------------------------------------------+
//| Daily reset                                                       |
//+------------------------------------------------------------------+
void CheckDailyReset(datetime now)
{
   MqlDateTime dt;
   TimeToStruct(now, dt);
   datetime today = (datetime)(now - dt.hour*3600 - dt.min*60 - dt.sec);

   if (today != g_today)
   {
      Print("📅 New day — Daily P&L: ", g_daily_pnl, " | Trades: ", g_daily_trades);
      g_daily_pnl    = 0;
      g_daily_trades = 0;
      g_today        = today;
   }
}

//+------------------------------------------------------------------+
//| Can we trade? (risk guards)                                       |
//+------------------------------------------------------------------+
bool CanTrade()
{
   // Max open trades
   int open = CountOpenTrades();
   if (open >= InpMaxOpenTrades)
   {
      return false;
   }

   // Daily loss limit
   double balance  = AccountInfoDouble(ACCOUNT_BALANCE);
   double max_loss = balance * InpMaxDailyLoss / 100.0;
   if (g_daily_pnl <= -max_loss)
   {
      Print("⛔ Daily loss limit hit: ", g_daily_pnl, " / -", max_loss);
      return false;
   }

   return true;
}

//+------------------------------------------------------------------+
//| Count bot's open trades                                           |
//+------------------------------------------------------------------+
int CountOpenTrades()
{
   int count = 0;
   for (int i = 0; i < PositionsTotal(); i++)
   {
      if (g_pos.SelectByIndex(i) && g_pos.Magic() == InpMagic)
         count++;
   }
   return count;
}

//+------------------------------------------------------------------+
//| Kill zone check                                                   |
//+------------------------------------------------------------------+
bool InKillZone(string &zone_name)
{
   if (!InpKillZones)
   {
      zone_name = "ALWAYS";
      return true;
   }

   MqlDateTime dt;
   datetime now_utc = TimeGMT();
   TimeToStruct(now_utc, dt);
   int h = dt.hour;
   int m = dt.min;
   int total_min = h * 60 + m;

   // London Open: 07:00 – 10:00 UTC
   if (total_min >= 420 && total_min <= 600)  { zone_name = "LONDON_OPEN"; return true; }
   // NY Open: 12:00 – 15:00 UTC
   if (total_min >= 720 && total_min <= 900)  { zone_name = "NY_OPEN"; return true; }
   // London Close: 15:00 – 17:00 UTC
   if (total_min >= 900 && total_min <= 1020) { zone_name = "LONDON_CLOSE"; return true; }

   zone_name = "DEAD_ZONE";
   return false;
}

//+------------------------------------------------------------------+
//| Get pip size for a symbol                                         |
//+------------------------------------------------------------------+
double GetPipSize(string symbol)
{
   if (StringFind(symbol, "JPY") >= 0) return 0.01;
   if (symbol == "US30" || symbol == "NAS100" || symbol == "SPX500") return 1.0;
   if (StringFind(symbol, "XAU") >= 0) return 0.1;
   return 0.0001;
}

//+------------------------------------------------------------------+
//| HTF bias from H4 structure                                        |
//+------------------------------------------------------------------+
int GetHTFBias(string symbol)
{
   // Returns: 1=Bullish, -1=Bearish, 0=Neutral
   int bars = 20;
   double highs[], lows[];
   ArraySetAsSeries(highs, true);
   ArraySetAsSeries(lows, true);

   if (CopyHigh(symbol, PERIOD_H4, 0, bars, highs) < bars) return 0;
   if (CopyLow(symbol,  PERIOD_H4, 0, bars, lows)  < bars) return 0;

   double max_high = highs[ArrayMaximum(highs, 1, bars-1)];
   double min_low  = lows[ArrayMinimum(lows,   1, bars-1)];

   bool hh = highs[0] > max_high;
   bool hl = lows[0]  > min_low;
   bool lh = highs[0] < max_high;
   bool ll = lows[0]  < min_low;

   if (hh && hl) return  1;
   if (lh && ll) return -1;
   return 0;
}

//+------------------------------------------------------------------+
//| Detect Fair Value Gap                                             |
//+------------------------------------------------------------------+
bool FindFVG(string symbol, ENUM_TIMEFRAMES tf, int bias,
             double &fvg_top, double &fvg_bot, double &entry_price)
{
   if (!InpFVG) return false;

   double open[], high[], low[], close[];
   ArraySetAsSeries(open,  true);
   ArraySetAsSeries(high,  true);
   ArraySetAsSeries(low,   true);
   ArraySetAsSeries(close, true);

   int count = 50;
   if (CopyOpen(symbol,  tf, 0, count, open)  < count) return false;
   if (CopyHigh(symbol,  tf, 0, count, high)  < count) return false;
   if (CopyLow(symbol,   tf, 0, count, low)   < count) return false;
   if (CopyClose(symbol, tf, 0, count, close) < count) return false;

   double pip   = GetPipSize(symbol);
   double min_g = InpFVGMinPips * pip;
   double price = close[0];

   for (int i = 2; i < count - 2; i++)
   {
      // Bullish FVG: gap between candle[i+2].high and candle[i].low
      if (bias == 1)
      {
         double gap = low[i] - high[i+2];
         if (gap >= min_g && price >= high[i+2] && price <= low[i])
         {
            fvg_top   = low[i];
            fvg_bot   = high[i+2];
            entry_price = price;
            return true;
         }
      }
      // Bearish FVG
      if (bias == -1)
      {
         double gap = low[i+2] - high[i];
         if (gap >= min_g && price >= high[i] && price <= low[i+2])
         {
            fvg_top   = low[i+2];
            fvg_bot   = high[i];
            entry_price = price;
            return true;
         }
      }
   }
   return false;
}

//+------------------------------------------------------------------+
//| Detect Stop Hunt                                                  |
//+------------------------------------------------------------------+
bool FindStopHunt(string symbol, ENUM_TIMEFRAMES tf, int bias,
                  double &entry, double &sl, double &tp)
{
   if (!InpStopHunt) return false;

   double high[], low[], close[];
   ArraySetAsSeries(high,  true);
   ArraySetAsSeries(low,   true);
   ArraySetAsSeries(close, true);

   int count = 20;
   if (CopyHigh(symbol,  tf, 0, count, high)  < count) return false;
   if (CopyLow(symbol,   tf, 0, count, low)   < count) return false;
   if (CopyClose(symbol, tf, 0, count, close) < count) return false;

   double pip  = GetPipSize(symbol);
   double tol  = 2 * pip;
   double disp = 8 * pip;

   // Find equal lows (bullish hunt)
   if (bias == 1)
   {
      double min_low = low[ArrayMinimum(low, 2, count-2)];
      int eq_count = 0;
      for (int i = 2; i < count; i++)
         if (MathAbs(low[i] - min_low) <= tol) eq_count++;

      if (eq_count >= 2 && low[1] < min_low - tol &&
          close[0] - low[1] >= disp)
      {
         entry = close[0];
         sl    = low[1] - 5*pip;
         tp    = entry + (entry - sl) * InpRR;
         return true;
      }
   }

   // Find equal highs (bearish hunt)
   if (bias == -1)
   {
      double max_high = high[ArrayMaximum(high, 2, count-2)];
      int eq_count = 0;
      for (int i = 2; i < count; i++)
         if (MathAbs(high[i] - max_high) <= tol) eq_count++;

      if (eq_count >= 2 && high[1] > max_high + tol &&
          high[1] - close[0] >= disp)
      {
         entry = close[0];
         sl    = high[1] + 5*pip;
         tp    = entry - (sl - entry) * InpRR;
         return true;
      }
   }
   return false;
}

//+------------------------------------------------------------------+
//| Calculate lot size based on risk %                                |
//+------------------------------------------------------------------+
double CalcLotSize(string symbol, double entry, double sl_price)
{
   double balance   = AccountInfoDouble(ACCOUNT_BALANCE);
   double risk_usd  = balance * InpRiskPercent / 100.0;
   double sl_dist   = MathAbs(entry - sl_price);
   double pip       = GetPipSize(symbol);
   double sl_pips   = sl_dist / pip;

   // Simplified pip value (USD account)
   double pip_value = 10.0; // per standard lot for most USD pairs
   if (StringFind(symbol, "JPY") >= 0) pip_value = 9.1;
   if (StringFind(symbol, "XAU") >= 0) pip_value = 100.0;
   if (symbol == "US30" || symbol == "NAS100") pip_value = 1.0;

   if (sl_pips <= 0) return 0.01;

   double lot = risk_usd / (sl_pips * pip_value);
   lot = MathMax(0.01, MathMin(lot, 10.0));

   // Round to 2 decimal places
   return NormalizeDouble(MathRound(lot * 100.0) / 100.0, 2);
}

//+------------------------------------------------------------------+
//| Place a market order                                              |
//+------------------------------------------------------------------+
bool PlaceOrder(string symbol, int direction, double lot,
                double sl, double tp, string comment)
{
   ENUM_ORDER_TYPE otype = (direction == 1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;

   MqlTick tick;
   if (!SymbolInfoTick(symbol, tick)) return false;
   double price = (direction == 1) ? tick.ask : tick.bid;

   bool result = g_trade.PositionOpen(symbol, otype, lot, price,
                                       NormalizeDouble(sl, 5),
                                       NormalizeDouble(tp, 5),
                                       comment);
   if (result)
   {
      g_daily_trades++;
      PrintFormat("✅ ORDER | %s %s %.2f lots | Price:%.5f SL:%.5f TP:%.5f | %s",
                  (direction==1?"BUY":"SELL"), symbol, lot, price, sl, tp, comment);
   }
   else
   {
      PrintFormat("❌ ORDER FAIL | %s | Error: %d | %s",
                  symbol, GetLastError(), g_trade.ResultComment());
   }
   return result;
}

//+------------------------------------------------------------------+
//| Main symbol scanner                                               |
//+------------------------------------------------------------------+
void ScanSymbol(string symbol)
{
   // Skip if already have position on this pair
   for (int i = 0; i < PositionsTotal(); i++)
   {
      if (g_pos.SelectByIndex(i) &&
          g_pos.Symbol() == symbol &&
          g_pos.Magic() == InpMagic) return;
   }

   // Kill zone check
   string kz_name;
   if (!InKillZone(kz_name)) return;

   // HTF bias
   int bias = GetHTFBias(symbol);
   if (bias == 0) return;

   double pip   = GetPipSize(symbol);
   double entry = 0, sl = 0, tp = 0;
   string comment = "";
   bool   found = false;

   // 1. Stop Hunt (highest priority)
   if (!found && FindStopHunt(symbol, PERIOD_M15, bias, entry, sl, tp))
   {
      comment = "ICT_STOP_HUNT";
      found   = true;
   }

   // 2. FVG
   if (!found)
   {
      double fvg_top, fvg_bot;
      if (FindFVG(symbol, PERIOD_M15, bias, fvg_top, fvg_bot, entry))
      {
         sl = (bias == 1) ? fvg_bot - 5*pip : fvg_top + 5*pip;
         tp = entry + (MathAbs(entry - sl) * InpRR) * bias;
         comment = "ICT_FVG";
         found   = true;
      }
   }

   if (!found) return;

   // Validate SL/TP
   if (sl == 0 || tp == 0) return;
   if (bias == 1  && tp <= entry) return;
   if (bias == -1 && tp >= entry) return;

   // Calculate lot
   double lot = CalcLotSize(symbol, entry, sl);
   if (lot < 0.01) return;

   // Place trade
   PlaceOrder(symbol, bias, lot, sl, tp, comment + "_" + kz_name);
}

//+------------------------------------------------------------------+
//| Manage open positions (trailing stop)                             |
//+------------------------------------------------------------------+
void ManagePositions()
{
   if (!InpTrailingStop) return;

   for (int i = PositionsTotal()-1; i >= 0; i--)
   {
      if (!g_pos.SelectByIndex(i)) continue;
      if (g_pos.Magic() != InpMagic) continue;

      string sym   = g_pos.Symbol();
      double pip   = GetPipSize(sym);
      double trail = InpTrailPips * pip;

      MqlTick tick;
      if (!SymbolInfoTick(sym, tick)) continue;

      double current_sl = g_pos.StopLoss();
      double new_sl     = 0;

      if (g_pos.PositionType() == POSITION_TYPE_BUY)
      {
         new_sl = tick.bid - trail;
         if (new_sl > current_sl + trail * 0.5)
         {
            g_trade.PositionModify(g_pos.Ticket(),
                                    NormalizeDouble(new_sl, 5),
                                    g_pos.TakeProfit());
         }
      }
      else if (g_pos.PositionType() == POSITION_TYPE_SELL)
      {
         new_sl = tick.ask + trail;
         if (new_sl < current_sl - trail * 0.5)
         {
            g_trade.PositionModify(g_pos.Ticket(),
                                    NormalizeDouble(new_sl, 5),
                                    g_pos.TakeProfit());
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Trade transaction handler (track closed trades for P&L)           |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction& trans,
                         const MqlTradeRequest& request,
                         const MqlTradeResult&  result)
{
   if (trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      if (trans.deal_type == DEAL_TYPE_BUY || trans.deal_type == DEAL_TYPE_SELL)
      {
         // This fires on close — add profit to daily tracking
         // (MT5 doesn't give profit here directly; use history)
         HistoryDealSelect(trans.deal);
         double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT);
         if (profit != 0)
         {
            g_daily_pnl += profit;
            string emoji = (profit > 0) ? "✅" : "❌";
            PrintFormat("%s Trade closed | Profit: %.2f | Daily P&L: %.2f",
                        emoji, profit, g_daily_pnl);
         }
      }
   }
}
//+------------------------------------------------------------------+
