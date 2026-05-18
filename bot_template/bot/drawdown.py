"""
Daily drawdown monitor — halts all quoting when cumulative loss
exceeds MAX_DAILY_LOSS_USD. Resets at UTC midnight or on manual resume.

Usage:
    dd = DrawdownMonitor(max_loss_usd=20.0)
    dd.record_pnl(-1.50)      # call on every closed trade
    if dd.is_halted:
        ...                   # skip quote cycle
    dd.resume()               # manual override to continue trading
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from bot.logger import get_logger

log = get_logger("drawdown")


@dataclass
class DrawdownState:
    pnl_today:       float = 0.0
    max_loss_usd:    float = 20.0
    is_halted:       bool  = False
    halt_reason:     str   = ""
    day_start_time:  float = 0.0
    trades_today:    int   = 0


class DrawdownMonitor:
    def __init__(self, max_loss_usd: float = 20.0):
        self._max   = max_loss_usd
        self._state = DrawdownState(
            max_loss_usd   = max_loss_usd,
            day_start_time = self._day_start(),
        )

    @property
    def is_halted(self) -> bool:
        self._maybe_reset()
        return self._state.is_halted

    @property
    def pnl_today(self) -> float:
        return self._state.pnl_today

    @property
    def stress_level(self) -> int:
        """
        Returns how deep into the daily loss budget we are.
          0 — loss < 50% of limit  (full size)
          1 — loss 50-75% of limit (half size)
          2 — loss 75-100% of limit (quarter size)
        """
        loss = -self._state.pnl_today
        if loss <= 0:
            return 0
        ratio = loss / self._max
        if ratio >= 0.75:
            return 2
        if ratio >= 0.50:
            return 1
        return 0

    @property
    def size_multiplier(self) -> float:
        """Size multiplier based on stress level. 1.0 = full, 0.5 = half, 0.25 = quarter."""
        return {0: 1.0, 1: 0.5, 2: 0.25}[self.stress_level]

    def record_pnl(self, pnl: float):
        """
        Call after every closed trade with the realised PnL (fees included).
        Triggers halt if cumulative loss exceeds threshold.
        """
        self._maybe_reset()
        if self._state.is_halted:
            return

        self._state.pnl_today   += pnl
        self._state.trades_today += 1

        loss = -self._state.pnl_today   # positive = loss
        if loss >= self._max:
            self._state.is_halted  = True
            self._state.halt_reason = (
                f"Daily loss ${loss:.2f} reached max ${self._max:.2f} "
                f"after {self._state.trades_today} trades"
            )
            log.warning(f"DRAWDOWN HALT — {self._state.halt_reason}")
        else:
            remaining = self._max - loss
            stress = self.stress_level
            mult   = self.size_multiplier
            if stress > 0:
                log.warning(
                    f"DRAWDOWN STRESS {stress} — PnL ${self._state.pnl_today:.2f} "
                    f"({loss/self._max*100:.0f}% of limit) — size reduced to {mult*100:.0f}%"
                )
            else:
                log.debug(f"PnL today: ${self._state.pnl_today:.2f}  remaining buffer: ${remaining:.2f}")

    def resume(self):
        """
        Manual override — continue trading after halt.
        Preserves today's PnL counter so the halt threshold is not reset.
        """
        if self._state.is_halted:
            log.info(
                f"DRAWDOWN MONITOR: manual resume. "
                f"PnL today remains ${self._state.pnl_today:.2f}. "
                f"Trading continues — next halt at cumulative -${self._max:.2f}."
            )
            self._state.is_halted   = False
            self._state.halt_reason = ""
        else:
            log.info("DrawdownMonitor: not halted — nothing to resume")

    def status(self) -> dict:
        return {
            "pnl_today":    round(self._state.pnl_today, 4),
            "max_loss_usd": self._state.max_loss_usd,
            "is_halted":    self._state.is_halted,
            "halt_reason":  self._state.halt_reason,
            "trades_today": self._state.trades_today,
        }

    # ── internal ────────────────────────────────────────────────────────────

    @staticmethod
    def _day_start() -> float:
        """Unix timestamp of UTC midnight today."""
        t = time.gmtime()
        return time.mktime(time.strptime(
            f"{t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d}",
            "%Y-%m-%d"
        )) - time.timezone

    def _maybe_reset(self):
        """Reset at UTC midnight."""
        if time.time() >= self._day_start() + 86400:
            prev_pnl = self._state.pnl_today
            self._state = DrawdownState(
                max_loss_usd   = self._max,
                day_start_time = self._day_start(),
            )
            log.info(f"Drawdown monitor reset for new day. Yesterday PnL: ${prev_pnl:.2f}")
