"""
Notifier
─────────
Sends trade alerts via Telegram and/or Email.
Configure in settings.json → notifications section.
"""

import logging
import asyncio
import smtplib
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger("NOTIFY")


class Notifier:
    def __init__(self, config: dict):
        self.enabled   = config.get("enabled", False)
        self.tg_cfg    = config.get("telegram", {})
        self.email_cfg = config.get("email", {})

    async def send(self, message: str):
        if not self.enabled:
            return
        tasks = []
        if self.tg_cfg.get("enabled"):
            tasks.append(self._send_telegram(message))
        if self.email_cfg.get("enabled"):
            tasks.append(self._send_email(message))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_telegram(self, message: str):
        try:
            import aiohttp
            url  = f"https://api.telegram.org/bot{self.tg_cfg['bot_token']}/sendMessage"
            data = {
                "chat_id": self.tg_cfg["chat_id"],
                "text":    message,
                "parse_mode": "Markdown"
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
        try:
            cfg = self.email_cfg
            msg = MIMEText(message)
            msg["Subject"] = "ICT Bot Alert"
            msg["From"]    = cfg["username"]
            msg["To"]      = cfg["recipient"]

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._smtp_send, cfg, msg)
            logger.info("✉️  Email alert sent")
        except Exception as e:
            logger.error(f"Email error: {e}")

    def _smtp_send(self, cfg: dict, msg):
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
