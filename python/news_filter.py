"""
News Filter
────────────
Fetches economic calendar from ForexFactory (or similar) and:
  • Blocks trading X minutes before/after high-impact events
  • Identifies currencies affected
  • Logs upcoming events
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
    time:     datetime
    currency: str
    impact:   str      # HIGH, MEDIUM, LOW
    title:    str
    forecast: str = ""
    previous: str = ""


class NewsFilter:
    def __init__(self, config: dict):
        self.cfg           = config
        self.events:       list[NewsEvent] = []
        self.last_update:  Optional[datetime] = None
        self.update_interval = 3600  # Refresh every hour

    async def update(self):
        """Fetch today's news events."""
        if not self.cfg.get("enabled", True):
            return

        try:
            now = datetime.utcnow()
            if (self.last_update and
                (now - self.last_update).seconds < self.update_interval):
                return  # Still fresh

            # Try ForexFactory scraping (or use a paid API for production)
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
        Fetch from ForexFactory JSON API.
        In production: replace with a paid provider like Benzinga or FX Street.
        """
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        events = []

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()

            today = datetime.utcnow().date()
            for item in data:
                try:
                    event_time = datetime.strptime(item["date"], "%Y-%m-%dT%H:%M:%S%z")
                    event_time = event_time.replace(tzinfo=None)  # strip tz for comparison
                except:
                    continue

                if event_time.date() != today:
                    continue

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
        Returns (blocked, reason).
        Blocked if a HIGH-impact event is within the configured window.
        """
        if not self.cfg.get("enabled", True) or not self.events:
            return False, ""

        if now is None:
            now = datetime.utcnow()

        before = timedelta(minutes=self.cfg.get("avoid_minutes_before", 30))
        after  = timedelta(minutes=self.cfg.get("avoid_minutes_after", 15))

        # Extract currencies relevant to this pair
        relevant_currencies = self._get_pair_currencies(symbol)

        for event in self.events:
            if event.currency not in relevant_currencies:
                continue

            window_start = event.time - before
            window_end   = event.time + after

            if window_start <= now <= window_end:
                minutes_to = int((event.time - now).total_seconds() / 60)
                if minutes_to > 0:
                    reason = (f"NEWS BLOCK: {event.title} ({event.currency}) "
                              f"in {minutes_to} min [{event.impact}]")
                else:
                    elapsed = int((now - event.time).total_seconds() / 60)
                    reason  = (f"NEWS BLOCK: {event.title} ({event.currency}) "
                               f"{elapsed} min ago [{event.impact}]")
                logger.info(f"🚫  {symbol} blocked — {reason}")
                return True, reason

        return False, ""

    def get_upcoming(self, hours_ahead: float = 2.0) -> list[NewsEvent]:
        now = datetime.utcnow()
        cutoff = now + timedelta(hours=hours_ahead)
        return [e for e in self.events if now <= e.time <= cutoff]

    def _get_pair_currencies(self, symbol: str) -> list[str]:
        currency_map = {
            "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"],
            "AUDUSD": ["AUD", "USD"], "USDJPY": ["USD", "JPY"],
            "EURJPY": ["EUR", "JPY"], "GBPJPY": ["GBP", "JPY"],
            "XAUUSD": ["USD"],        "US30":   ["USD"],
            "NAS100": ["USD"],        "SPX500": ["USD"],
        }
        return currency_map.get(symbol.upper(), ["USD"])
