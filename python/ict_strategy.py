"""
ICT Strategy Engine
────────────────────
Implements full ICT (Inner Circle Trader) methodology:

  1. Market Structure  — Highs/Lows, BOS (Break of Structure), CHoCH
  2. Fair Value Gaps   — 3-candle imbalance zones, mitigation detection
  3. Turtle Soup       — Liquidity sweep + reversal setup
  4. Stop Hunt         — Equal H/L sweep, displacement entry
  5. Order Blocks      — Last opposing candle before impulse
  6. Kill Zones        — London Open, NY Open, London Close sessions
  7. HTF Bias          — H4 directional bias filter
"""

import logging
from datetime import datetime, time as dtime, timezone
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

logger = logging.getLogger("ICT")


# ─── Data Structures ──────────────────────────────────────────────────────────

class Direction(Enum):
    BULLISH = "BUY"
    BEARISH = "SELL"
    NEUTRAL = "NEUTRAL"

class SetupType(Enum):
    FVG          = "FVG"
    TURTLE_SOUP  = "TURTLE_SOUP"
    STOP_HUNT    = "STOP_HUNT"
    ORDER_BLOCK  = "ORDER_BLOCK"
    SCALP        = "SCALP"

@dataclass
class Zone:
    top:    float
    bottom: float
    direction: Direction
    zone_type: str
    time:   datetime
    mitigated: bool = False

@dataclass
class Signal:
    symbol:     str
    direction:  Direction
    setup_type: SetupType
    entry:      float
    sl:         float
    tp:         float
    confidence: float        # 0.0 – 1.0
    reason:     str
    time:       datetime = field(default_factory=datetime.utcnow)
    valid:      bool = True

    @property
    def rr(self) -> float:
        if abs(self.entry - self.sl) == 0:
            return 0.0
        return round(abs(self.tp - self.entry) / abs(self.entry - self.sl), 2)


# ─── ICT Strategy Engine ──────────────────────────────────────────────────────

class ICTStrategy:
    def __init__(self, config: dict):
        self.cfg      = config["ict"]
        self.scalp_cfg = config.get("scalping", {})
        self.pip_size  = {}  # populated per symbol

    def get_pip_size(self, symbol: str) -> float:
        if symbol in self.pip_size:
            return self.pip_size[symbol]
        if "JPY" in symbol:
            return 0.01
        if symbol in ("US30", "NAS100", "SPX500"):
            return 1.0
        if "XAU" in symbol or "GOLD" in symbol:
            return 0.1
        return 0.0001

    def to_pips(self, price_diff: float, symbol: str) -> float:
        return abs(price_diff) / self.get_pip_size(symbol)

    def from_pips(self, pips: float, symbol: str) -> float:
        return pips * self.get_pip_size(symbol)

    # ── 1. HTF Bias (H4 / D1) ────────────────────────────────────────────────
    def get_htf_bias(self, candles_h4: list) -> Direction:
        """
        Simple structure-based bias:
        Bullish if price is making higher highs & higher lows.
        Bearish if lower highs & lower lows.
        """
        if len(candles_h4) < 20:
            return Direction.NEUTRAL

        recent = candles_h4[-20:]
        highs  = [c["high"]  for c in recent]
        lows   = [c["low"]   for c in recent]

        # Check last 3 swing points
        hh = highs[-1] > max(highs[-10:-1])
        hl = lows[-1]  > min(lows[-10:-1])
        lh = highs[-1] < max(highs[-10:-1])
        ll = lows[-1]  < min(lows[-10:-1])

        if hh and hl:
            return Direction.BULLISH
        if lh and ll:
            return Direction.BEARISH
        return Direction.NEUTRAL

    # ── 2. Kill Zone Check ────────────────────────────────────────────────────
    def in_kill_zone(self, now: datetime = None) -> tuple[bool, str]:
        """Returns (is_in_kill_zone, zone_name)"""
        if not self.cfg["kill_zones"]["enabled"]:
            return True, "ALWAYS"

        if now is None:
            now = datetime.utcnow()
        t = now.time()

        kz = self.cfg["kill_zones"]
        zones = {
            "LONDON_OPEN":  (dtime(7, 0),  dtime(10, 0)),
            "NY_OPEN":      (dtime(12, 0), dtime(15, 0)),
            "LONDON_CLOSE": (dtime(15, 0), dtime(17, 0)),
        }
        for name, (start, end) in zones.items():
            if start <= t <= end:
                return True, name
        return False, "DEAD_ZONE"

    # ── 3. Fair Value Gaps (FVG) ──────────────────────────────────────────────
    def find_fvg(self, candles: list, symbol: str) -> list[Zone]:
        """
        Bullish FVG: candle[i-2].high < candle[i].low  (gap between)
        Bearish FVG: candle[i-2].low  > candle[i].high (gap between)
        """
        if not self.cfg["fvg"]["enabled"] or len(candles) < 3:
            return []

        min_gap = self.from_pips(self.cfg["fvg"]["min_gap_pips"], symbol)
        zones   = []

        for i in range(2, len(candles)):
            c0, c1, c2 = candles[i-2], candles[i-1], candles[i]

            # Bullish FVG
            gap = c2["low"] - c0["high"]
            if gap >= min_gap:
                zones.append(Zone(
                    top=c2["low"], bottom=c0["high"],
                    direction=Direction.BULLISH,
                    zone_type="FVG_BULL",
                    time=c1["time"]
                ))

            # Bearish FVG
            gap = c0["low"] - c2["high"]
            if gap >= min_gap:
                zones.append(Zone(
                    top=c0["low"], bottom=c2["high"],
                    direction=Direction.BEARISH,
                    zone_type="FVG_BEAR",
                    time=c1["time"]
                ))

        # Mark mitigated zones
        last_close = candles[-1]["close"]
        for z in zones:
            if z.direction == Direction.BULLISH and last_close <= z.bottom:
                z.mitigated = True
            elif z.direction == Direction.BEARISH and last_close >= z.top:
                z.mitigated = True

        return [z for z in zones if not z.mitigated]

    def fvg_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Entry: Price returns to FVG zone that aligns with HTF bias.
        """
        zones = self.find_fvg(candles[-50:], symbol)
        if not zones:
            return None

        current_price = candles[-1]["close"]
        rr = self.cfg.get("rr_ratio", 2.0) if "rr_ratio" in self.cfg else 2.0

        for z in reversed(zones):
            if z.direction != bias:
                continue

            # Price must be entering the zone
            if z.direction == Direction.BULLISH:
                if z.bottom <= current_price <= z.top:
                    sl_price = z.bottom - self.from_pips(5, symbol)
                    risk     = current_price - sl_price
                    tp_price = current_price + (risk * rr)
                    return Signal(
                        symbol=symbol, direction=Direction.BULLISH,
                        setup_type=SetupType.FVG,
                        entry=current_price, sl=sl_price, tp=tp_price,
                        confidence=0.75,
                        reason=f"Price mitigating Bullish FVG at {z.bottom:.5f}–{z.top:.5f}"
                    )

            elif z.direction == Direction.BEARISH:
                if z.bottom <= current_price <= z.top:
                    sl_price = z.top + self.from_pips(5, symbol)
                    risk     = sl_price - current_price
                    tp_price = current_price - (risk * rr)
                    return Signal(
                        symbol=symbol, direction=Direction.BEARISH,
                        setup_type=SetupType.FVG,
                        entry=current_price, sl=sl_price, tp=tp_price,
                        confidence=0.75,
                        reason=f"Price mitigating Bearish FVG at {z.bottom:.5f}–{z.top:.5f}"
                    )
        return None

    # ── 4. Turtle Soup (Liquidity Sweep + Reversal) ───────────────────────────
    def turtle_soup_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Turtle Soup: Price sweeps above N-bar high / below N-bar low,
        then closes back below/above → reversal entry.
        Classic smart money liquidity grab.
        """
        if not self.cfg["turtle_soup"]["enabled"] or len(candles) < 25:
            return None

        lookback  = self.cfg["turtle_soup"]["lookback_candles"]
        confirm   = self.from_pips(self.cfg["turtle_soup"]["confirmation_pips"], symbol)
        prev      = candles[-(lookback+1):-1]
        last      = candles[-1]
        prev_2    = candles[-2]

        n_high = max(c["high"] for c in prev)
        n_low  = min(c["low"]  for c in prev)

        # Bullish Turtle Soup: swept low, closed back above
        if (prev_2["low"] < n_low and        # wick swept below
            last["close"] > n_low + confirm and  # closed back above
            bias == Direction.BULLISH):
            sl_price = prev_2["low"] - self.from_pips(3, symbol)
            risk     = last["close"] - sl_price
            tp_price = last["close"] + (risk * 2.0)
            return Signal(
                symbol=symbol, direction=Direction.BULLISH,
                setup_type=SetupType.TURTLE_SOUP,
                entry=last["close"], sl=sl_price, tp=tp_price,
                confidence=0.80,
                reason=f"Turtle Soup: Swept {lookback}-bar low at {n_low:.5f}, reversed"
            )

        # Bearish Turtle Soup: swept high, closed back below
        if (prev_2["high"] > n_high and
            last["close"] < n_high - confirm and
            bias == Direction.BEARISH):
            sl_price = prev_2["high"] + self.from_pips(3, symbol)
            risk     = sl_price - last["close"]
            tp_price = last["close"] - (risk * 2.0)
            return Signal(
                symbol=symbol, direction=Direction.BEARISH,
                setup_type=SetupType.TURTLE_SOUP,
                entry=last["close"], sl=sl_price, tp=tp_price,
                confidence=0.80,
                reason=f"Turtle Soup: Swept {lookback}-bar high at {n_high:.5f}, reversed"
            )

        return None

    # ── 5. Stop Hunt Detection ────────────────────────────────────────────────
    def stop_hunt_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Equal Highs/Lows: detect resting liquidity.
        Stop Hunt: price spikes through equal H/L then displaces back.
        This IS the kill shot setup.
        """
        if not self.cfg["stop_hunt"]["enabled"] or len(candles) < 10:
            return None

        tol  = self.from_pips(self.cfg["stop_hunt"]["equal_hl_tolerance"], symbol)
        disp = self.from_pips(self.cfg["stop_hunt"]["displacement_min_pips"], symbol)

        recent = candles[-15:]
        last   = recent[-1]
        prev   = recent[-2]

        # Find equal highs (sell-side liquidity above)
        highs = [c["high"] for c in recent[:-2]]
        equal_highs = [h for h in highs if abs(h - max(highs)) <= tol]

        # Find equal lows (buy-side liquidity below)
        lows = [c["low"] for c in recent[:-2]]
        equal_lows = [l for l in lows if abs(l - min(lows)) <= tol]

        # Bullish stop hunt: swept equal lows, closed back up with displacement
        if (len(equal_lows) >= 2 and
            prev["low"] <= min(equal_lows) - tol and   # spike below
            last["close"] - prev["low"] >= disp and     # strong close back up
            bias == Direction.BULLISH):
            sl_price = prev["low"] - self.from_pips(5, symbol)
            risk     = last["close"] - sl_price
            tp_price = last["close"] + (risk * 2.5)
            return Signal(
                symbol=symbol, direction=Direction.BULLISH,
                setup_type=SetupType.STOP_HUNT,
                entry=last["close"], sl=sl_price, tp=tp_price,
                confidence=0.85,
                reason=f"Stop Hunt: Equal lows swept at {min(equal_lows):.5f}, "
                       f"displacement {self.to_pips(disp, symbol):.1f} pips up"
            )

        # Bearish stop hunt: swept equal highs, closed back down with displacement
        if (len(equal_highs) >= 2 and
            prev["high"] >= max(equal_highs) + tol and
            prev["high"] - last["close"] >= disp and
            bias == Direction.BEARISH):
            sl_price = prev["high"] + self.from_pips(5, symbol)
            risk     = sl_price - last["close"]
            tp_price = last["close"] - (risk * 2.5)
            return Signal(
                symbol=symbol, direction=Direction.BEARISH,
                setup_type=SetupType.STOP_HUNT,
                entry=last["close"], sl=sl_price, tp=tp_price,
                confidence=0.85,
                reason=f"Stop Hunt: Equal highs swept at {max(equal_highs):.5f}, "
                       f"displacement {self.to_pips(disp, symbol):.1f} pips down"
            )

        return None

    # ── 6. Order Block Detection ──────────────────────────────────────────────
    def find_order_blocks(self, candles: list, symbol: str) -> list[Zone]:
        """
        Bullish OB:  Last bearish candle before a bullish impulse move.
        Bearish OB:  Last bullish candle before a bearish impulse move.
        """
        if not self.cfg["order_blocks"]["enabled"] or len(candles) < 5:
            return []

        min_impulse = self.from_pips(self.cfg["order_blocks"]["min_impulse_pips"], symbol)
        blocks = []

        for i in range(1, len(candles) - 2):
            c = candles[i]
            nxt = candles[i+1]

            # Bullish OB: bearish candle followed by strong bullish move
            if (c["close"] < c["open"] and  # bearish
                nxt["close"] - nxt["open"] >= min_impulse):  # bullish impulse
                blocks.append(Zone(
                    top=c["open"], bottom=c["close"],
                    direction=Direction.BULLISH,
                    zone_type="OB_BULL",
                    time=c["time"]
                ))

            # Bearish OB: bullish candle followed by strong bearish move
            if (c["close"] > c["open"] and  # bullish
                nxt["open"] - nxt["close"] >= min_impulse):  # bearish impulse
                blocks.append(Zone(
                    top=c["close"], bottom=c["open"],
                    direction=Direction.BEARISH,
                    zone_type="OB_BEAR",
                    time=c["time"]
                ))

        return blocks[-5:]  # Keep last 5 most relevant OBs

    def order_block_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        blocks = self.find_order_blocks(candles[-30:], symbol)
        if not blocks:
            return None

        price = candles[-1]["close"]
        for ob in reversed(blocks):
            if ob.direction != bias:
                continue
            if ob.bottom <= price <= ob.top:
                if ob.direction == Direction.BULLISH:
                    sl = ob.bottom - self.from_pips(5, symbol)
                    tp = price + (price - sl) * 2.0
                    return Signal(
                        symbol=symbol, direction=Direction.BULLISH,
                        setup_type=SetupType.ORDER_BLOCK,
                        entry=price, sl=sl, tp=tp,
                        confidence=0.78,
                        reason=f"Bullish OB reaction at {ob.bottom:.5f}–{ob.top:.5f}"
                    )
                else:
                    sl = ob.top + self.from_pips(5, symbol)
                    tp = price - (sl - price) * 2.0
                    return Signal(
                        symbol=symbol, direction=Direction.BEARISH,
                        setup_type=SetupType.ORDER_BLOCK,
                        entry=price, sl=sl, tp=tp,
                        confidence=0.78,
                        reason=f"Bearish OB reaction at {ob.bottom:.5f}–{ob.top:.5f}"
                    )
        return None

    # ── 7. Scalp Signal (M1/M5) ───────────────────────────────────────────────
    def scalp_signal(self, candles_m1: list, symbol: str, bias: Direction,
                     spread_pips: float) -> Optional[Signal]:
        """Fast scalp within kill zone using momentum and micro-structure."""
        if not self.scalp_cfg.get("enabled", False):
            return None
        if spread_pips > self.scalp_cfg.get("max_spread_pips", 2.0):
            return None
        if len(candles_m1) < 10:
            return None

        quick_tp = self.from_pips(self.scalp_cfg["quick_tp_pips"], symbol)
        quick_sl = self.from_pips(self.scalp_cfg["quick_sl_pips"], symbol)
        recent   = candles_m1[-5:]
        last     = recent[-1]

        # Bullish scalp: 3 consecutive higher closes
        closes = [c["close"] for c in recent]
        if all(closes[i] > closes[i-1] for i in range(-3, 0)) and bias == Direction.BULLISH:
            return Signal(
                symbol=symbol, direction=Direction.BULLISH,
                setup_type=SetupType.SCALP,
                entry=last["close"],
                sl=last["close"] - quick_sl,
                tp=last["close"] + quick_tp,
                confidence=0.60,
                reason="Scalp: M1 bullish momentum in kill zone"
            )

        # Bearish scalp: 3 consecutive lower closes
        if all(closes[i] < closes[i-1] for i in range(-3, 0)) and bias == Direction.BEARISH:
            return Signal(
                symbol=symbol, direction=Direction.BEARISH,
                setup_type=SetupType.SCALP,
                entry=last["close"],
                sl=last["close"] + quick_sl,
                tp=last["close"] - quick_tp,
                confidence=0.60,
                reason="Scalp: M1 bearish momentum in kill zone"
            )

        return None

    # ── 8. Master Signal Aggregator ───────────────────────────────────────────
    def analyze(self, symbol: str, candles_h4: list, candles_m15: list,
                candles_m5: list, candles_m1: list, spread_pips: float) -> Optional[Signal]:
        """
        Full ICT analysis pipeline.
        Returns highest-confidence signal or None.
        Priority: Stop Hunt > Turtle Soup > FVG > Order Block > Scalp
        """
        # Step 1: Kill zone gate
        in_kz, kz_name = self.in_kill_zone()
        if self.cfg["kill_zones"].get("trade_only_in_kill_zones", True) and not in_kz:
            return None

        # Step 2: HTF bias
        bias = self.get_htf_bias(candles_h4)
        if bias == Direction.NEUTRAL:
            logger.debug(f"{symbol}: Neutral HTF bias — no trade")
            return None

        logger.debug(f"{symbol}: HTF Bias={bias.value} | Kill Zone={kz_name}")

        # Step 3: Try setups in priority order
        candidates = []

        sig = self.stop_hunt_signal(candles_m15, symbol, bias)
        if sig:
            candidates.append(sig)

        sig = self.turtle_soup_signal(candles_m15, symbol, bias)
        if sig:
            candidates.append(sig)

        sig = self.fvg_signal(candles_m15, symbol, bias)
        if sig:
            candidates.append(sig)

        sig = self.order_block_signal(candles_m15, symbol, bias)
        if sig:
            candidates.append(sig)

        sig = self.scalp_signal(candles_m1, symbol, bias, spread_pips)
        if sig:
            candidates.append(sig)

        if not candidates:
            return None

        # Return highest confidence signal
        best = max(candidates, key=lambda s: s.confidence)
        logger.info(f"📊  {symbol} | {best.setup_type.value} | "
                    f"{best.direction.value} | Conf: {best.confidence:.0%} | "
                    f"RR: {best.rr} | {best.reason}")
        return best
