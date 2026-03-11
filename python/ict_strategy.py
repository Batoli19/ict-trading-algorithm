"""
ICT Strategy Engine — MERGED (Base + Enhanced)
──────────────────────────────────────────────
Implements ICT (Inner Circle Trader) concepts + enhanced price-action/sniper tools.

CORE ICT:
  1) Market Structure (placeholder for future: BOS/CHoCH)
  2) Fair Value Gaps (FVG) — 3-candle imbalance zones, mitigation detection
  3) Turtle Soup — liquidity sweep + reversal
  4) Stop Hunt — equal highs/lows sweep + displacement
  5) Order Blocks — last opposing candle before impulse
  6) Kill Zones — London Open, NY Open, London Close sessions
  7) HTF Bias — H4 multi-factor bias filter

ENHANCED ADDITIONS:
  • Engulfing + Pin Bar pattern detection (M5)
  • Sniper entries (M5 premium/discount pullback confirmation)
  • Manipulation scalp (M1 counter-trend exhaustion before main move)
  • Confidence bonus during kill zones (+10%)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from enum import Enum
from typing import Optional, List, Tuple

from ict_advanced_setups import ICTSetupsLibrary
from market_math import pip_size as _pip_size, to_pips as _to_pips, from_pips as _from_pips

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

    # Enhanced
    SNIPER       = "SNIPER"
    MANIPULATION = "MANIPULATION"
    ENGULFING    = "ENGULFING"
    PIN_BAR      = "PIN_BAR"


@dataclass
class Zone:
    top: float
    bottom: float
    direction: Direction
    zone_type: str
    time: datetime
    mitigated: bool = False


@dataclass
class Signal:
    symbol: str
    direction: Direction
    setup_type: SetupType
    entry: float
    sl: float
    tp: float
    confidence: float        # 0.0 – 1.0
    reason: str
    time: datetime = field(default_factory=datetime.utcnow)
    valid: bool = True
    sniper_entry: bool = False  # marks precise entries (enhanced)

    @property
    def rr(self) -> float:
        if abs(self.entry - self.sl) == 0:
            return 0.0
        return round(abs(self.tp - self.entry) / abs(self.entry - self.sl), 2)


# ─── ICT Strategy Engine ──────────────────────────────────────────────────────

class ICTStrategy:
    def __init__(self, config: dict):
        self.root_config = config
        self.cfg = config.get("ict", {})
        self.scalp_cfg = config.get("scalping", {})
        self.pip_size = {}  # per symbol cache
        self.advanced_setups = ICTSetupsLibrary(config)

    # ── Utilities ────────────────────────────────────────────────────────────


    def get_min_rr(self, symbol: str, default: float = 2.0) -> float:
        exec_cfg = self.root_config.get("execution", {})
        per_sym = exec_cfg.get("per_symbol", {}).get(symbol, {})
        return float(per_sym.get("min_rr", exec_cfg.get("min_rr", default)))

    def get_pip_size(self, symbol: str) -> float:
        if symbol in self.pip_size:
            return self.pip_size[symbol]
        ps = _pip_size(symbol)
        self.pip_size[symbol] = ps
        return ps

    def to_pips(self, price_diff: float, symbol: str) -> float:
        return _to_pips(price_diff, symbol)

    def from_pips(self, pips: float, symbol: str) -> float:
        return _from_pips(pips, symbol)

    # ── 1) HTF Bias (H4 multi-factor) ────────────────────────────────────────

    def get_htf_bias(self, candles_h4: list) -> Direction:
        """
        Multi-factor HTF bias using 3 independent methods voted together:

          1) EMA trend (8 vs 21)
          2) Price vs 20-candle range midpoint (with neutral band)
          3) Swing structure across segments (HH/HL vs LH/LL)

        Score >= +2 -> BULLISH
        Score <= -2 -> BEARISH
        else        -> NEUTRAL
        """
        if len(candles_h4) < 30:
            return Direction.NEUTRAL

        closes = [c["close"] for c in candles_h4[-30:]]
        highs = [c["high"] for c in candles_h4[-30:]]
        lows = [c["low"] for c in candles_h4[-30:]]

        score = 0

        # Method 1: EMA crossover
        def ema(values: list, period: int) -> float:
            k = 2 / (period + 1)
            result = values[0]
            for v in values[1:]:
                result = v * k + result * (1 - k)
            return result

        fast_ema = ema(closes, 8)
        slow_ema = ema(closes, 21)

        if fast_ema > slow_ema * 1.0001:
            score += 1
        elif fast_ema < slow_ema * 0.9999:
            score -= 1

        # Method 2: price vs midpoint (20-candle range)
        range_high = max(highs[-20:])
        range_low = min(lows[-20:])
        midpoint = (range_high + range_low) / 2
        last_close = closes[-1]
        band = (range_high - range_low) * 0.1  # 10% neutral band around midpoint

        if last_close > midpoint + band:
            score += 1
        elif last_close < midpoint - band:
            score -= 1

        # Method 3: swing structure via segments
        seg_size = 5
        seg_highs = [max(highs[i:i + seg_size]) for i in range(0, 20, seg_size)]
        seg_lows = [min(lows[i:i + seg_size]) for i in range(0, 20, seg_size)]

        bull_struct = (seg_highs[-1] > seg_highs[-2] and seg_lows[-1] > seg_lows[-2])
        bear_struct = (seg_highs[-1] < seg_highs[-2] and seg_lows[-1] < seg_lows[-2])

        if bull_struct:
            score += 1
        elif bear_struct:
            score -= 1

        logger.debug(
            f"HTF bias score={score:+d} | "
            f"EMA fast={fast_ema:.5f} slow={slow_ema:.5f} | "
            f"Close={last_close:.5f} mid={midpoint:.5f} | "
            f"SegH={seg_highs} SegL={seg_lows}"
        )

        if score >= 2:
            return Direction.BULLISH
        if score <= -2:
            return Direction.BEARISH
        return Direction.NEUTRAL

    # ── 2) Kill Zone Check ───────────────────────────────────────────────────

    def in_kill_zone(self, now: datetime = None) -> Tuple[bool, str]:
        """Returns (is_in_kill_zone, zone_name). Times are UTC unless you pass localized 'now'."""
        kz_cfg = self.cfg.get("kill_zones", {})
        if not kz_cfg.get("enabled", True):
            return True, "ALWAYS"

        if now is None:
            now = datetime.now(timezone.utc)

        t = now.time()

        def parse_time(t_str, default_h):
            try:
                h, m = map(int, t_str.split(":"))
                return dtime(h, m)
            except:
                return dtime(default_h, 0)

        # Build dynamic zones from config
        zones = {
            "LONDON_OPEN": (
                parse_time(kz_cfg.get("london_open", {}).get("start", "07:00"), 7),
                parse_time(kz_cfg.get("london_open", {}).get("end", "10:00"), 10)
            ),
            "NY_OPEN": (
                parse_time(kz_cfg.get("ny_open", {}).get("start", "12:00"), 12),
                parse_time(kz_cfg.get("ny_open", {}).get("end", "15:00"), 15)
            ),
            "LONDON_CLOSE": (
                parse_time(kz_cfg.get("london_close", {}).get("start", "15:00"), 15),
                parse_time(kz_cfg.get("london_close", {}).get("end", "17:00"), 17)
            ),
        }

        for name, (start, end) in zones.items():
            if start <= t <= end:
                return True, name
        return False, "DEAD_ZONE"

    # ── 3) Fair Value Gaps (FVG) ─────────────────────────────────────────────

    def find_fvg(self, candles: list, symbol: str) -> List[Zone]:
        """
        Bullish FVG: candle[i-2].high < candle[i].low
        Bearish FVG: candle[i-2].low  > candle[i].high
        """
        if not self.cfg["fvg"]["enabled"] or len(candles) < 3:
            return []

        min_gap = self.from_pips(self.cfg["fvg"]["min_gap_pips"], symbol)
        zones: List[Zone] = []

        for i in range(2, len(candles)):
            c0, c1, c2 = candles[i - 2], candles[i - 1], candles[i]

            # Bullish gap
            gap_bull = c2["low"] - c0["high"]
            if gap_bull >= min_gap:
                zones.append(Zone(
                    top=c2["low"],
                    bottom=c0["high"],
                    direction=Direction.BULLISH,
                    zone_type="FVG_BULL",
                    time=c1["time"],
                ))

            # Bearish gap
            gap_bear = c0["low"] - c2["high"]
            if gap_bear >= min_gap:
                zones.append(Zone(
                    top=c0["low"],
                    bottom=c2["high"],
                    direction=Direction.BEARISH,
                    zone_type="FVG_BEAR",
                    time=c1["time"],
                ))

        # Mark mitigated (simple rule using last close)
        last_close = candles[-1]["close"]
        for z in zones:
            if z.direction == Direction.BULLISH and last_close <= z.bottom:
                z.mitigated = True
            elif z.direction == Direction.BEARISH and last_close >= z.top:
                z.mitigated = True

        return [z for z in zones if not z.mitigated]

    def fvg_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """Entry: price returns into FVG aligned with HTF bias."""
        zones = self.find_fvg(candles[-50:], symbol)
        if not zones:
            return None

        current_price = candles[-1]["close"]
        rr = self.get_min_rr(symbol, 2.0)

        for z in reversed(zones):
            if z.direction != bias:
                continue

            if z.bottom <= current_price <= z.top:
                if z.direction == Direction.BULLISH:
                    sl_price = z.bottom - self.from_pips(5, symbol)
                    risk = current_price - sl_price
                    tp_price = current_price + (risk * rr)
                    sig = Signal(
                        symbol=symbol,
                        direction=Direction.BULLISH,
                        setup_type=SetupType.FVG,
                        entry=current_price,
                        sl=sl_price,
                        tp=tp_price,
                        confidence=0.75,
                        reason=f"Price mitigating Bullish FVG at {z.bottom:.5f}–{z.top:.5f}",
                    )
                    setattr(sig, "zone_midpoint", (z.bottom + z.top) / 2.0)
                    return sig
                else:
                    sl_price = z.top + self.from_pips(5, symbol)
                    risk = sl_price - current_price
                    tp_price = current_price - (risk * rr)
                    sig = Signal(
                        symbol=symbol,
                        direction=Direction.BEARISH,
                        setup_type=SetupType.FVG,
                        entry=current_price,
                        sl=sl_price,
                        tp=tp_price,
                        confidence=0.75,
                        reason=f"Price mitigating Bearish FVG at {z.bottom:.5f}–{z.top:.5f}",
                    )
                    setattr(sig, "zone_midpoint", (z.bottom + z.top) / 2.0)
                    return sig

        return None

    # ── 4) Turtle Soup ───────────────────────────────────────────────────────

    def turtle_soup_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Turtle Soup: sweep N-bar high/low then close back inside -> reversal.
        """
        if not self.cfg["turtle_soup"]["enabled"] or len(candles) < 25:
            return None

        lookback = self.cfg["turtle_soup"]["lookback_candles"]
        confirm = self.from_pips(self.cfg["turtle_soup"]["confirmation_pips"], symbol)

        prev = candles[-(lookback + 1):-1]
        last = candles[-1]
        prev_2 = candles[-2]

        n_high = max(c["high"] for c in prev)
        n_low = min(c["low"] for c in prev)

        # Bullish: swept low, closed back above
        if (
            prev_2["low"] < n_low and
            last["close"] > n_low + confirm and
            bias == Direction.BULLISH
        ):
            sl_price = prev_2["low"] - self.from_pips(3, symbol)
            risk = last["close"] - sl_price
            tp_price = last["close"] + (risk * self.get_min_rr(symbol, 2.0))
            return Signal(
                symbol=symbol,
                direction=Direction.BULLISH,
                setup_type=SetupType.TURTLE_SOUP,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.80,
                reason=f"Turtle Soup: Swept {lookback}-bar low at {n_low:.5f}, reversed",
            )

        # Bearish: swept high, closed back below
        if (
            prev_2["high"] > n_high and
            last["close"] < n_high - confirm and
            bias == Direction.BEARISH
        ):
            sl_price = prev_2["high"] + self.from_pips(3, symbol)
            risk = sl_price - last["close"]
            tp_price = last["close"] - (risk * self.get_min_rr(symbol, 2.0))
            return Signal(
                symbol=symbol,
                direction=Direction.BEARISH,
                setup_type=SetupType.TURTLE_SOUP,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.80,
                reason=f"Turtle Soup: Swept {lookback}-bar high at {n_high:.5f}, reversed",
            )

        return None

    # ── 5) Stop Hunt ─────────────────────────────────────────────────────────

    def stop_hunt_signal(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Equal Highs/Lows -> resting liquidity.
        Stop hunt: spike through equal H/L then displacement back.
        """
        if not self.cfg["stop_hunt"]["enabled"] or len(candles) < 10:
            return None

        tol = self.from_pips(self.cfg["stop_hunt"]["equal_hl_tolerance"], symbol)
        disp = self.from_pips(self.cfg["stop_hunt"]["displacement_min_pips"], symbol)

        recent = candles[-15:]
        last = recent[-1]
        prev = recent[-2]

        highs = [c["high"] for c in recent[:-2]]
        lows = [c["low"] for c in recent[:-2]]

        if not highs or not lows:
            return None

        equal_highs = [h for h in highs if abs(h - max(highs)) <= tol]
        equal_lows = [l for l in lows if abs(l - min(lows)) <= tol]

        # Bullish: swept equal lows then displaced up
        if (
            len(equal_lows) >= 2 and
            prev["low"] <= min(equal_lows) - tol and
            (last["close"] - prev["low"]) >= disp and
            bias == Direction.BULLISH
        ):
            sl_price = prev["low"] - self.from_pips(5, symbol)
            risk = last["close"] - sl_price
            tp_price = last["close"] + (risk * self.get_min_rr(symbol, 2.5))
            return Signal(
                symbol=symbol,
                direction=Direction.BULLISH,
                setup_type=SetupType.STOP_HUNT,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.85,
                reason=(
                    f"Stop Hunt: Equal lows swept at {min(equal_lows):.5f}, "
                    f"displacement {self.to_pips(disp, symbol):.1f} pips up"
                ),
            )

        # Bearish: swept equal highs then displaced down
        if (
            len(equal_highs) >= 2 and
            prev["high"] >= max(equal_highs) + tol and
            (prev["high"] - last["close"]) >= disp and
            bias == Direction.BEARISH
        ):
            sl_price = prev["high"] + self.from_pips(5, symbol)
            risk = sl_price - last["close"]
            tp_price = last["close"] - (risk * self.get_min_rr(symbol, 2.5))
            return Signal(
                symbol=symbol,
                direction=Direction.BEARISH,
                setup_type=SetupType.STOP_HUNT,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.85,
                reason=(
                    f"Stop Hunt: Equal highs swept at {max(equal_highs):.5f}, "
                    f"displacement {self.to_pips(disp, symbol):.1f} pips down"
                ),
            )

        return None

    # ── 6) Order Blocks ──────────────────────────────────────────────────────

    def find_order_blocks(self, candles: list, symbol: str) -> List[Zone]:
        """
        Bullish OB: last bearish candle before bullish impulse candle.
        Bearish OB: last bullish candle before bearish impulse candle.
        """
        if not self.cfg["order_blocks"]["enabled"] or len(candles) < 5:
            return []

        min_impulse = self.from_pips(self.cfg["order_blocks"]["min_impulse_pips"], symbol)
        blocks: List[Zone] = []

        for i in range(1, len(candles) - 2):
            c = candles[i]
            nxt = candles[i + 1]

            # Bullish OB
            if (
                c["close"] < c["open"] and
                (nxt["close"] - nxt["open"]) >= min_impulse
            ):
                blocks.append(Zone(
                    top=c["open"],
                    bottom=c["close"],
                    direction=Direction.BULLISH,
                    zone_type="OB_BULL",
                    time=c["time"],
                ))

            # Bearish OB
            if (
                c["close"] > c["open"] and
                (nxt["open"] - nxt["close"]) >= min_impulse
            ):
                blocks.append(Zone(
                    top=c["close"],
                    bottom=c["open"],
                    direction=Direction.BEARISH,
                    zone_type="OB_BEAR",
                    time=c["time"],
                ))

        return blocks[-5:]

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
                    tp = price + (price - sl) * self.get_min_rr(symbol, 2.0)
                    sig = Signal(
                        symbol=symbol,
                        direction=Direction.BULLISH,
                        setup_type=SetupType.ORDER_BLOCK,
                        entry=price,
                        sl=sl,
                        tp=tp,
                        confidence=0.78,
                        reason=f"Bullish OB reaction at {ob.bottom:.5f}–{ob.top:.5f}",
                    )
                    setattr(sig, "zone_midpoint", (ob.bottom + ob.top) / 2.0)
                    return sig
                else:
                    sl = ob.top + self.from_pips(5, symbol)
                    tp = price - (sl - price) * self.get_min_rr(symbol, 2.0)
                    sig = Signal(
                        symbol=symbol,
                        direction=Direction.BEARISH,
                        setup_type=SetupType.ORDER_BLOCK,
                        entry=price,
                        sl=sl,
                        tp=tp,
                        confidence=0.78,
                        reason=f"Bearish OB reaction at {ob.bottom:.5f}–{ob.top:.5f}",
                    )
                    setattr(sig, "zone_midpoint", (ob.bottom + ob.top) / 2.0)
                    return sig

        return None

    # ── 7) Regular Scalp (M1) ────────────────────────────────────────────────

    def scalp_signal(self, candles_m1: list, symbol: str, bias: Direction, spread_pips: float) -> Optional[Signal]:
        """Fast scalp within kill zone using basic micro-momentum."""
        if not self.scalp_cfg.get("enabled", False):
            return None
        if spread_pips > self.scalp_cfg.get("max_spread_pips", 2.0):
            return None
        if len(candles_m1) < 10:
            return None

        quick_tp = self.from_pips(self.scalp_cfg["quick_tp_pips"], symbol)
        quick_sl = self.from_pips(self.scalp_cfg["quick_sl_pips"], symbol)

        recent = candles_m1[-5:]
        last = recent[-1]
        closes = [c["close"] for c in recent]

        # Bullish: 3 consecutive higher closes
        if all(closes[i] > closes[i - 1] for i in range(-3, 0)) and bias == Direction.BULLISH:
            return Signal(
                symbol=symbol,
                direction=Direction.BULLISH,
                setup_type=SetupType.SCALP,
                entry=last["close"],
                sl=last["close"] - quick_sl,
                tp=last["close"] + quick_tp,
                confidence=0.60,
                reason="Scalp: M1 bullish momentum in kill zone",
            )

        # Bearish: 3 consecutive lower closes
        if all(closes[i] < closes[i - 1] for i in range(-3, 0)) and bias == Direction.BEARISH:
            return Signal(
                symbol=symbol,
                direction=Direction.BEARISH,
                setup_type=SetupType.SCALP,
                entry=last["close"],
                sl=last["close"] + quick_sl,
                tp=last["close"] - quick_tp,
                confidence=0.60,
                reason="Scalp: M1 bearish momentum in kill zone",
            )

        return None

    # ── ENHANCED: Price Action Patterns (M5) ─────────────────────────────────

    def detect_engulfing(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Bullish Engulfing: bearish candle then larger bullish candle engulfing body.
        Bearish Engulfing: bullish candle then larger bearish candle engulfing body.
        """
        if len(candles) < 2:
            return None

        prev = candles[-2]
        last = candles[-1]

        prev_body = abs(prev["close"] - prev["open"])
        last_body = abs(last["close"] - last["open"])

        # Bullish engulfing
        if (
            bias == Direction.BULLISH and
            prev["close"] < prev["open"] and
            last["close"] > last["open"] and
            last["open"] <= prev["close"] and
            last["close"] >= prev["open"] and
            last_body > prev_body * 1.2
        ):
            sl_price = last["low"] - self.from_pips(3, symbol)
            risk = last["close"] - sl_price
            tp_price = last["close"] + (risk * self.get_min_rr(symbol, 2.5))
            return Signal(
                symbol=symbol,
                direction=Direction.BULLISH,
                setup_type=SetupType.ENGULFING,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.82,
                reason=f"Bullish engulfing at {last['close']:.5f}",
                sniper_entry=True,
            )

        # Bearish engulfing
        if (
            bias == Direction.BEARISH and
            prev["close"] > prev["open"] and
            last["close"] < last["open"] and
            last["open"] >= prev["close"] and
            last["close"] <= prev["open"] and
            last_body > prev_body * 1.2
        ):
            sl_price = last["high"] + self.from_pips(3, symbol)
            risk = sl_price - last["close"]
            tp_price = last["close"] - (risk * self.get_min_rr(symbol, 2.5))
            return Signal(
                symbol=symbol,
                direction=Direction.BEARISH,
                setup_type=SetupType.ENGULFING,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.82,
                reason=f"Bearish engulfing at {last['close']:.5f}",
                sniper_entry=True,
            )

        return None

    def detect_pin_bar(self, candles: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """Pin bar / hammer / shooting star rejection candle."""
        if len(candles) < 1:
            return None

        last = candles[-1]
        body = abs(last["close"] - last["open"])
        full_range = last["high"] - last["low"]
        if full_range == 0:
            return None

        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]

        # Bullish pin (hammer)
        if (
            bias == Direction.BULLISH and
            lower_wick > body * 2.5 and
            upper_wick < body * 0.5 and
            lower_wick > full_range * 0.6
        ):
            sl_price = last["low"] - self.from_pips(2, symbol)
            risk = last["close"] - sl_price
            tp_price = last["close"] + (risk * self.get_min_rr(symbol, 2.0))
            return Signal(
                symbol=symbol,
                direction=Direction.BULLISH,
                setup_type=SetupType.PIN_BAR,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.78,
                reason=f"Bullish pin bar rejection at {last['low']:.5f}",
                sniper_entry=True,
            )

        # Bearish pin (shooting star)
        if (
            bias == Direction.BEARISH and
            upper_wick > body * 2.5 and
            lower_wick < body * 0.5 and
            upper_wick > full_range * 0.6
        ):
            sl_price = last["high"] + self.from_pips(2, symbol)
            risk = sl_price - last["close"]
            tp_price = last["close"] - (risk * self.get_min_rr(symbol, 2.0))
            return Signal(
                symbol=symbol,
                direction=Direction.BEARISH,
                setup_type=SetupType.PIN_BAR,
                entry=last["close"],
                sl=sl_price,
                tp=tp_price,
                confidence=0.78,
                reason=f"Bearish pin bar rejection at {last['high']:.5f}",
                sniper_entry=True,
            )

        return None

    # ── ENHANCED: Manipulation Scalp (M1) ────────────────────────────────────

    def manipulation_scalp(self, candles_m1: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Catch counter-trend manipulation exhaustion before HTF continuation.
        Uses tight stops; best used inside kill zones.
        """
        if len(candles_m1) < 10:
            return None

        recent = candles_m1[-5:]
        last = recent[-1]
        closes = [c["close"] for c in recent]

        # Bullish bias -> catch bearish dip exhaustion
        if bias == Direction.BULLISH:
            if all(closes[i] < closes[i - 1] for i in range(1, len(closes))):
                body = abs(last["close"] - last["open"])
                lower_wick = min(last["open"], last["close"]) - last["low"]

                if lower_wick > body * 1.5:
                    sl_price = last["low"] - self.from_pips(5, symbol)
                    tp_price = last["close"] + self.from_pips(12, symbol)
                    return Signal(
                        symbol=symbol,
                        direction=Direction.BULLISH,
                        setup_type=SetupType.MANIPULATION,
                        entry=last["close"],
                        sl=sl_price,
                        tp=tp_price,
                        confidence=0.72,
                        reason="Bearish manipulation exhaustion → bullish reversal",
                        sniper_entry=True,
                    )

        # Bearish bias -> catch bullish pop exhaustion
        if bias == Direction.BEARISH:
            if all(closes[i] > closes[i - 1] for i in range(1, len(closes))):
                body = abs(last["close"] - last["open"])
                upper_wick = last["high"] - max(last["open"], last["close"])

                if upper_wick > body * 1.5:
                    sl_price = last["high"] + self.from_pips(5, symbol)
                    tp_price = last["close"] - self.from_pips(12, symbol)
                    return Signal(
                        symbol=symbol,
                        direction=Direction.BEARISH,
                        setup_type=SetupType.MANIPULATION,
                        entry=last["close"],
                        sl=sl_price,
                        tp=tp_price,
                        confidence=0.72,
                        reason="Bullish manipulation exhaustion → bearish reversal",
                        sniper_entry=True,
                    )

        return None

    # ── ENHANCED: Sniper Entry (M5 premium/discount) ─────────────────────────

    def sniper_entry(self, candles_m5: list, symbol: str, bias: Direction) -> Optional[Signal]:
        """
        Wait for M5 pullback into discount/premium zone + rejection confirmation.
        Tight stops (~7 pips by default config here).
        """
        if len(candles_m5) < 20:
            return None

        recent = candles_m5[-20:]
        last = recent[-1]

        swing_high = max(c["high"] for c in recent)
        swing_low = min(c["low"] for c in recent)
        range_size = swing_high - swing_low
        if range_size <= 0:
            return None

        discount_zone = swing_low + (range_size * 0.3)
        premium_zone = swing_high - (range_size * 0.3)

        # Bullish sniper
        if bias == Direction.BULLISH and last["close"] <= discount_zone:
            body = abs(last["close"] - last["open"])
            lower_wick = min(last["open"], last["close"]) - last["low"]

            if lower_wick > body * 1.2 and last["close"] > last["open"]:
                sl_price = last["low"] - self.from_pips(7, symbol)
                risk = last["close"] - sl_price
                tp_price = last["close"] + (risk * self.get_min_rr(symbol, 2.2))
                return Signal(
                    symbol=symbol,
                    direction=Direction.BULLISH,
                    setup_type=SetupType.SNIPER,
                    entry=last["close"],
                    sl=sl_price,
                    tp=tp_price,
                    confidence=0.88,
                    reason=f"Sniper entry at discount zone {last['close']:.5f}",
                    sniper_entry=True,
                )

        # Bearish sniper
        if bias == Direction.BEARISH and last["close"] >= premium_zone:
            body = abs(last["close"] - last["open"])
            upper_wick = last["high"] - max(last["open"], last["close"])

            if upper_wick > body * 1.2 and last["close"] < last["open"]:
                sl_price = last["high"] + self.from_pips(7, symbol)
                risk = sl_price - last["close"]
                tp_price = last["close"] - (risk * self.get_min_rr(symbol, 2.2))
                return Signal(
                    symbol=symbol,
                    direction=Direction.BEARISH,
                    setup_type=SetupType.SNIPER,
                    entry=last["close"],
                    sl=sl_price,
                    tp=tp_price,
                    confidence=0.88,
                    reason=f"Sniper entry at premium zone {last['close']:.5f}",
                    sniper_entry=True,
                )

        return None

    # ── 8) Master Aggregator (Merged + Enhanced Priority) ────────────────────

    def analyze(
        self,
        symbol: str,
        candles_h4: list,
        candles_m15: list,
        candles_m5: list,
        candles_m1: list,
        spread_pips: float,
        adaptive_evaluator=None,
    ) -> Optional[Signal]:
        """
        Priority (highest to lowest confidence):
          1) Sniper Entry (M5 precision)
          2) Stop Hunt
          3) Engulfing (M5)
          4) Turtle Soup
          5) Pin Bar (M5)
          6) Order Block
          7) FVG
          8) Manipulation Scalp (M1)
          9) Regular Scalp (M1)

        Kill zone bonus: +10% confidence when in kill zones (no hard filter).
        """
        in_kz, kz_name = self.in_kill_zone()
        kz_bonus = 0.10 if in_kz else 0.0

        if in_kz:
            logger.debug(f"{symbol}: Trading in {kz_name} kill zone — confidence bonus applied")

        bias = self.get_htf_bias(candles_h4)
        if bias == Direction.NEUTRAL:
            logger.debug(f"{symbol}: Neutral HTF bias — lower confidence trades only")

        logger.debug(f"{symbol}: HTF Bias={bias.value} | Kill Zone={kz_name}")

        candidates: List[Signal] = []

        # Phase 3: Pure Execution confirmation patterns
        # Identify if any local M5 patterns are pointing in our HTF bias directions.
        # These are no longer standalone trades, but confirmations.
        local_engulfing_bull = self.detect_engulfing(candles_m5, symbol, Direction.BULLISH)
        local_engulfing_bear = self.detect_engulfing(candles_m5, symbol, Direction.BEARISH)
        local_sniper_bull = self.sniper_entry(candles_m5, symbol, Direction.BULLISH)
        local_sniper_bear = self.sniper_entry(candles_m5, symbol, Direction.BEARISH)

        # 1) ADVANCED SETUPS SCORING ENGINE (Deep Institutional Logic)
        adv_signals = self.advanced_setups.scan_all_setups(
            candles_h4=candles_h4,
            candles_m15=candles_m15,
            candles_m5=candles_m5,
            symbol=symbol,
        )
        
        for adv_signal in adv_signals:
            direction = Direction.BULLISH if adv_signal.direction == "BUY" else Direction.BEARISH
            mapped_sig = Signal(
                symbol=symbol,
                direction=direction,
                setup_type=adv_signal.setup_type,
                entry=adv_signal.entry_price,
                sl=adv_signal.sl_price,
                tp=adv_signal.tp_price,
                confidence=adv_signal.confidence,
                reason=adv_signal.reason,
                time=adv_signal.detected_at,
                valid=adv_signal.valid,
                sniper_entry=False
            )
            
            # Phase 3 Execution Pipeline confirmation 
            # Inject tight M5 logic into the valid HTF macro setups
            if direction == Direction.BULLISH:
                if local_sniper_bull:
                    mapped_sig.entry = local_sniper_bull.entry
                    mapped_sig.sl = local_sniper_bull.sl
                    mapped_sig.sniper_entry = True
                    mapped_sig.confidence = min(mapped_sig.confidence + 0.15, 1.0)
                    mapped_sig.reason += " + [M5 Sniper Confirmed]"
                elif local_engulfing_bull:
                    mapped_sig.entry = local_engulfing_bull.entry
                    mapped_sig.sl = local_engulfing_bull.sl
                    mapped_sig.sniper_entry = True
                    mapped_sig.confidence = min(mapped_sig.confidence + 0.10, 1.0)
                    mapped_sig.reason += " + [M5 Engulfing Confirmed]"

            elif direction == Direction.BEARISH:
                if local_sniper_bear:
                    mapped_sig.entry = local_sniper_bear.entry
                    mapped_sig.sl = local_sniper_bear.sl
                    mapped_sig.sniper_entry = True
                    mapped_sig.confidence = min(mapped_sig.confidence + 0.15, 1.0)
                    mapped_sig.reason += " + [M5 Sniper Confirmed]"
                elif local_engulfing_bear:
                    mapped_sig.entry = local_engulfing_bear.entry
                    mapped_sig.sl = local_engulfing_bear.sl
                    mapped_sig.sniper_entry = True
                    mapped_sig.confidence = min(mapped_sig.confidence + 0.10, 1.0)
                    mapped_sig.reason += " + [M5 Engulfing Confirmed]"

            candidates.append(mapped_sig)

        if not candidates:
            return None

        # PRE-FILTER: Discard invalid signals explicitly labeled by the Confluence Engine (e.g. against HTF)
        candidates = [c for c in candidates if getattr(c, 'valid', True)]
        
        if not candidates:
            return None

        for s in candidates:
            if adaptive_evaluator:
                setup_name = getattr(s.setup_type, "value", s.setup_type)
                s.confidence = adaptive_evaluator(setup_name) / 100.0
            s.confidence = min(s.confidence + kz_bonus, 1.0)

        best = max(candidates, key=lambda s: s.confidence)

        kz_indicator = f"[{kz_name}]" if in_kz else "[OUTSIDE_KZ]"
        sniper_tag = " 🎯 SNIPER" if best.sniper_entry else ""
        
        # Add blocked warning flag to log if there are blocked candidates around
        logger.info(
            f"📊  {symbol} {kz_indicator}{sniper_tag} | {best.setup_type.value} | "
            f"{best.direction.value} | Conf: {best.confidence:.0%} | "
            f"RR: {best.rr} | {best.reason}"
        )
        return best
