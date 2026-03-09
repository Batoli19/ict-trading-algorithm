"""
News Filter — Economic Calendar Integration
══════════════════════════════════════════════
Fetches economic calendar data and blocks trading around high-impact news events.

Why this matters:
    High-impact news (NFP, CPI, FOMC, etc.) causes massive volatility spikes
    that can blow through stop-losses in milliseconds. The bot pauses trading
    around these events to protect capital.

How it works:
    1. Fetches today's economic calendar from ForexFactory JSON API
    2. Filters for HIGH-impact events affecting traded currencies (USD, EUR, GBP)
    3. Before each trade entry, checks if we're in a "blackout window":
       - 30 minutes BEFORE a high-impact event → blocked (price starts positioning)
       - 15 minutes AFTER a high-impact event  → blocked (spread still wide)

Configuration (settings.json → "news"):
    "news": {
        "enabled": true,
        "provider": "forexfactory",
        "avoid_minutes_before": 30,    ← block window before event
        "avoid_minutes_after": 15,     ← block window after event
        "impact_levels": ["HIGH"],     ← which impact levels to block on
        "currencies": ["USD", "EUR", "GBP"]  ← which currencies matter
    }
"""

import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger("NEWS")


@dataclass
class NewsEvent:
    """
    Represents a single economic calendar event.

    Example:
        NewsEvent(
            time=datetime(2025, 3, 7, 13, 30),
            currency="USD",
            impact="HIGH",
            title="Non-Farm Payrolls",
            forecast="180K",
            previous="143K"
        )
    """
    time:     datetime   # When the news is released (UTC)
    currency: str        # Which currency is affected (e.g. "USD", "EUR")
    impact:   str        # Impact level: "HIGH", "MEDIUM", or "LOW"
    title:    str        # Event name (e.g. "Non-Farm Payrolls")
    forecast: str = ""   # Market forecast value
    previous: str = ""   # Previous release value


class NewsFilter:
    """
    Economic calendar filter that blocks trading around high-impact news events.

    Lifecycle:
        1. Created at bot startup with config from settings.json
        2. update() is called every hour by the engine's news loop
        3. is_blocked(symbol) is called before every trade entry
    """

    def __init__(self, config: dict):
        """
        Args:
            config: The "news" section from settings.json
        """
        self.cfg           = config
        self.events:       list[NewsEvent] = []     # Today's filtered news events
        self.last_update:  Optional[datetime] = None  # When we last fetched the calendar
        self.update_interval = 3600                    # Refresh interval: 1 hour

    async def update(self):
        """
        Fetch today's economic calendar events from the data provider.

        Called by the engine's _news_loop() every hour. Filters events
        to only keep HIGH-impact ones affecting our traded currencies.

        This is async because it makes an HTTP request to ForexFactory's API.
        """
        if not self.cfg.get("enabled", True):
            return

        try:
            now = datetime.utcnow()

            # Don't re-fetch if we updated less than 1 hour ago
            if (self.last_update and
                (now - self.last_update).seconds < self.update_interval):
                return  # Still fresh

            # Fetch events from the calendar API
            events = await self._fetch_forexfactory()

            if events:
                self.events      = events
                self.last_update = now
                high_count = sum(1 for e in events if e.impact == "HIGH")
                logger.info(f"📰  News updated: {len(events)} events today "
                            f"({high_count} HIGH impact)")
            else:
                logger.warning("News fetch returned no events — trading without filter")

        except Exception as e:
            logger.error(f"News fetch error: {e} — continuing without filter")

    async def _fetch_forexfactory(self) -> list[NewsEvent]:
        """
        Fetch economic calendar from ForexFactory's free JSON API.

        Data source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
        This returns the full week's calendar as JSON.

        In production, you might want to use a paid provider like:
            - Benzinga (more reliable, real-time)
            - FX Street (comprehensive)
            - Investing.com API

        Returns:
            List of NewsEvent objects for today's HIGH-impact events.
        """
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        events = []

        try:
            # Use aiohttp for async HTTP (doesn't block the event loop)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

            today = datetime.utcnow().date()

            for item in data:
                # Parse the event time from ISO format
                try:
                    event_time = datetime.strptime(item["date"], "%Y-%m-%dT%H:%M:%S%z")
                    event_time = event_time.replace(tzinfo=None)  # Strip tz for naive comparison
                except:
                    continue

                # Only keep today's events
                if event_time.date() != today:
                    continue

                # Filter by impact level (usually only care about HIGH)
                impact = item.get("impact", "").upper()
                currency = item.get("country", "").upper()

                if impact not in self.cfg.get("impact_levels", ["HIGH"]):
                    continue
                if currency not in self.cfg.get("currencies", ["USD","EUR","GBP"]):
                    continue

                events.append(NewsEvent(
                    time     = event_time,
                    currency = currency,
                    impact   = impact,
                    title    = item.get("title", "Unknown"),
                    forecast = str(item.get("forecast", "")),
                    previous = str(item.get("previous", "")),
                ))

        except Exception as e:
            logger.warning(f"ForexFactory fetch failed: {e}")

        return events

    def is_blocked(self, symbol: str, now: datetime = None) -> tuple[bool, str]:
        """
        Check if trading is blocked for a symbol due to an upcoming/recent
        high-impact news event.

        This is called by the engine BEFORE every trade entry. It checks:
            1. Is there a HIGH-impact event within the next 30 minutes?
            2. Did a HIGH-impact event occur in the last 15 minutes?
        If either is true → block the trade.

        Args:
            symbol: Trading instrument (e.g. "EURUSD")
            now:    Current UTC time (default: datetime.utcnow())

        Returns:
            Tuple of (is_blocked: bool, reason: str).
            If blocked, reason explains which event is blocking and when.
        """
        if not self.cfg.get("enabled", True) or not self.events:
            return False, ""

        if now is None:
            now = datetime.utcnow()

        # Get the configured blackout windows
        before = timedelta(minutes=self.cfg.get("avoid_minutes_before", 30))
        after  = timedelta(minutes=self.cfg.get("avoid_minutes_after", 15))

        # Determine which currencies this symbol is affected by
        # E.g. EURUSD is affected by both EUR and USD news
        relevant_currencies = self._get_pair_currencies(symbol)

        for event in self.events:
            # Skip events for currencies not related to this symbol
            if event.currency not in relevant_currencies:
                continue

            # Calculate the blackout window around this event
            window_start = event.time - before  # 30 min before event
            window_end   = event.time + after    # 15 min after event

            # Check if current time falls within the blackout window
            if window_start <= now <= window_end:
                minutes_to = int((event.time - now).total_seconds() / 60)
                if minutes_to > 0:
                    # Event is in the future — we're in the "before" window
                    reason = (f"NEWS BLOCK: {event.title} ({event.currency}) "
                              f"in {minutes_to} min [{event.impact}]")
                else:
                    # Event already happened — we're in the "after" window
                    elapsed = int((now - event.time).total_seconds() / 60)
                    reason  = (f"NEWS BLOCK: {event.title} ({event.currency}) "
                               f"{elapsed} min ago [{event.impact}]")
                logger.info(f"🚫  {symbol} blocked — {reason}")
                return True, reason

        return False, ""

    def get_upcoming(self, hours_ahead: float = 2.0) -> list[NewsEvent]:
        """
        Get upcoming news events within the next N hours.
        Used by the dashboard to show the user what's coming.

        Args:
            hours_ahead: How far ahead to look (default: 2 hours)

        Returns:
            List of NewsEvent objects within the window.
        """
        now = datetime.utcnow()
        cutoff = now + timedelta(hours=hours_ahead)
        return [e for e in self.events if now <= e.time <= cutoff]

    def _get_pair_currencies(self, symbol: str) -> list[str]:
        """
        Map a trading symbol to its constituent currencies.

        For forex pairs, both currencies matter:
            EURUSD → ["EUR", "USD"] (affected by both EUR and USD news)
            GBPUSD → ["GBP", "USD"]

        For commodities and indices, only USD matters:
            XAUUSD → ["USD"] (gold priced in USD)
            US30   → ["USD"] (Dow Jones is USD-denominated)
        """
        currency_map = {
            "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"],
            "AUDUSD": ["AUD", "USD"], "USDJPY": ["USD", "JPY"],
            "EURJPY": ["EUR", "JPY"], "GBPJPY": ["GBP", "JPY"],
            "USDCHF": ["USD", "CHF"],
            "XAUUSD": ["USD"],        "US30":   ["USD"],
            "NAS100": ["USD"],        "SPX500": ["USD"],
        }
        return currency_map.get(symbol.upper(), ["USD"])
