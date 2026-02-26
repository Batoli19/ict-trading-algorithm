from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple


@dataclass
class GateDecision:
    allowed: bool
    reason: str = "OK"


class HybridGate:
    """
    Hybrid (Sniper + Session Momentum) gate:
    - Trade only in selected kill zones (optional)
    - Enforce cooldowns after a close (win/loss)
    - Cap trades per day total / per symbol / per killzone
    - Prevent immediate re-entry same direction unless new setup
    """

    def __init__(self, cfg: dict, memory):
        self.cfg = cfg
        self.memory = memory

        self._cooldown_until_by_symbol: Dict[str, datetime] = {}
        self._last_close_by_symbol: Dict[str, Dict] = {}

    def on_trade_closed(self, symbol: str, pnl: float, direction: Optional[str], setup_type: Optional[str]):
        hcfg = self.cfg.get("hybrid", {})
        now = datetime.utcnow()

        cd_close = int(hcfg.get("cooldown_after_close_seconds", 0))
        cd_loss = int(hcfg.get("cooldown_after_loss_seconds", 0))
        cd_win = int(hcfg.get("cooldown_after_win_seconds", 0))

        if pnl < 0:
            cooldown = max(cd_close, cd_loss)
        elif pnl > 0:
            cooldown = max(cd_close, cd_win)
        else:
            cooldown = cd_close

        if cooldown > 0:
            self._cooldown_until_by_symbol[symbol] = now + timedelta(seconds=cooldown)

        self._last_close_by_symbol[symbol] = {
            "time": now,
            "pnl": pnl,
            "direction": direction,
            "setup_type": setup_type,
        }

    def _cooldown_active(self, symbol: str) -> Tuple[bool, str]:
        until = self._cooldown_until_by_symbol.get(symbol)
        if not until:
            return False, ""
        now = datetime.utcnow()
        if now < until:
            return True, f"COOLDOWN_ACTIVE until={until.isoformat()}"
        return False, ""

    def _counts_ok(self, symbol: str, kz_name: str) -> Tuple[bool, str]:
        hcfg = self.cfg.get("hybrid", {})

        max_total = int(hcfg.get("max_trades_per_day_total", 0))
        max_sym = int(hcfg.get("max_trades_per_symbol_per_day", 0))
        max_kzsym = int(hcfg.get("max_trades_per_killzone_per_symbol", 0))

        total_today = self.memory.count_trades_today_total()
        sym_today = self.memory.count_trades_today_symbol(symbol)
        kzsym_today = self.memory.count_trades_today_symbol_kz(symbol, kz_name)

        if max_total > 0 and total_today >= max_total:
            return False, f"MAX_DAILY_TOTAL reached {total_today}/{max_total}"
        if max_sym > 0 and sym_today >= max_sym:
            return False, f"MAX_DAILY_SYMBOL reached {sym_today}/{max_sym}"
        if max_kzsym > 0 and kzsym_today >= max_kzsym:
            return False, f"MAX_KZ_SYMBOL reached {kzsym_today}/{max_kzsym}"

        return True, "OK"

    def allow_entry(
        self,
        symbol: str,
        kz_name: str,
        direction: str,
        setup_type: str,
        rr: float,
        confidence: float,
    ) -> GateDecision:
        hcfg = self.cfg.get("hybrid", {})
        if not hcfg.get("enabled", False):
            return GateDecision(True, "HYBRID_DISABLED")

        min_rr = float(hcfg.get("min_rr", 0.0))
        min_conf = float(hcfg.get("min_confidence", 0.0))
        if rr < min_rr:
            return GateDecision(False, f"RR_TOO_LOW rr={rr:.2f} min={min_rr:.2f}")
        if confidence < min_conf:
            return GateDecision(False, f"CONF_TOO_LOW conf={confidence:.2f} min={min_conf:.2f}")

        active, reason = self._cooldown_active(symbol)
        if active:
            return GateDecision(False, reason)

        if hcfg.get("block_reentry_same_direction", True):
            last = self._last_close_by_symbol.get(symbol)
            if last and last.get("direction") == direction:
                if hcfg.get("reentry_requires_new_setup", True) and last.get("setup_type") == setup_type:
                    return GateDecision(False, "REENTRY_BLOCKED same_direction_and_setup")

        ok, reason = self._counts_ok(symbol, kz_name)
        if not ok:
            return GateDecision(False, reason)

        return GateDecision(True, "OK")
