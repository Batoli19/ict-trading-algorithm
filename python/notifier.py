"""
Notifier — Trade Alert System
═══════════════════════════════
Sends trade alerts (entries, exits, errors) to the user via:
    • Telegram (recommended) — instant mobile notifications
    • Email (via SMTP) — backup notification channel

Both channels are optional. Configure in settings.json → notifications:

    "notifications": {
        "enabled": true,
        "telegram": {
            "enabled": true,
            "bot_token": "YOUR_BOT_TOKEN",  ← from @BotFather
            "chat_id": "YOUR_CHAT_ID"       ← from Telegram getUpdates API
        },
        "email": {
            "enabled": false,
            ...
        }
    }

The Notifier.send() method is called by the engine on trade events.
It fires both Telegram and Email concurrently using asyncio.gather().
"""

import logging
import asyncio
import smtplib
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger("NOTIFY")


class Notifier:
    """
    Sends trade alert notifications via Telegram and/or Email.

    Usage:
        notifier = Notifier(config["notifications"])
        await notifier.send("🟢 BUY EURUSD @ 1.0850 | SL: 1.0835 | TP: 1.0882")
    """

    def __init__(self, config: dict):
        """
        Args:
            config: The "notifications" section from settings.json
        """
        self.enabled   = config.get("enabled", False)    # Master on/off switch
        self.tg_cfg    = config.get("telegram", {})       # Telegram configuration
        self.email_cfg = config.get("email", {})          # Email SMTP configuration

    async def send(self, message: str):
        """
        Send a notification message via all enabled channels.

        Uses asyncio.gather() to send Telegram and Email concurrently
        (neither blocks the other). Exceptions are caught and logged,
        never propagated — notifications should never crash the bot.

        Args:
            message: The alert text to send (supports Markdown for Telegram)
        """
        if not self.enabled:
            return

        # Build list of notification tasks to run concurrently
        tasks = []
        if self.tg_cfg.get("enabled"):
            tasks.append(self._send_telegram(message))
        if self.email_cfg.get("enabled"):
            tasks.append(self._send_email(message))

        if tasks:
            # return_exceptions=True prevents one failure from cancelling others
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_telegram(self, message: str):
        """
        Send a message via Telegram Bot API.

        Uses aiohttp for async HTTP (doesn't block the event loop).
        The Telegram API endpoint accepts JSON and returns the sent message.
        """
        try:
            import aiohttp

            # Telegram Bot API endpoint for sending messages
            url  = f"https://api.telegram.org/bot{self.tg_cfg['bot_token']}/sendMessage"
            data = {
                "chat_id": self.tg_cfg["chat_id"],   # Who receives the message
                "text":    message,                    # The message content
                "parse_mode": "Markdown"               # Enable bold, italic, etc.
            }

            async with aiohttp.ClientSession() as s:
                async with s.post(url, json=data, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        logger.info("✉️  Telegram alert sent")
                    else:
                        logger.warning(f"Telegram failed: {r.status}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")

    async def _send_email(self, message: str):
        """
        Send a message via Email (SMTP).

        Uses run_in_executor() to run the synchronous SMTP call on a
        thread pool, preventing it from blocking the async event loop.
        """
        try:
            cfg = self.email_cfg

            # Build the email message
            msg = MIMEText(message)
            msg["Subject"] = "ICT Bot Alert"
            msg["From"]    = cfg["username"]
            msg["To"]      = cfg["recipient"]

            # Run SMTP on a background thread (SMTP is synchronous/blocking)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._smtp_send, cfg, msg)
            logger.info("✉️  Email alert sent")
        except Exception as e:
            logger.error(f"Email error: {e}")

    def _smtp_send(self, cfg: dict, msg):
        """
        Synchronous SMTP send — runs on a background thread via run_in_executor().
        Connects to the SMTP server, authenticates, and sends the email.
        """
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.starttls()                           # Upgrade to encrypted connection
            server.login(cfg["username"], cfg["password"])  # Authenticate
            server.send_message(msg)                    # Send the email
