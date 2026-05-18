"""
Adaptive Grid — range mode strategy.

Places N limit levels on each side of mid, spaced by ATR.
Each level is $200 notional (configurable), giving $1000 total
exposure per side when fully filled — matching directional mode.

Level sizing is biased by SignalWeights:
  long_weight  scales BUY level sizes  (0.5–1.5×)
  short_weight scales SELL level sizes (0.5–1.5×)

Grid rebalances when price moves > REBALANCE_THRESHOLD × spacing
from the centre it was generated at.

Usage:
    grid = GridManager(market='BTC-PERP', cfg=cfg)
    levels = grid.generate(mid, atr, signal_weights)
    grid.needs_rebalance(current_mid)  → bool
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from bot.logger import get_logger

log = get_logger("grid")

NUM_LEVELS          = 5
LEVEL_NOTIONAL_USD  = 200.0      # $ per level
SPACING_ATR_MULT    = float(os.environ.get("HOTSTUFF_GRID_SPACING_ATR_MULT", "0.5"))  # grid spacing = ATR × this
REBALANCE_THRESHOLD = 0.30       # rebalance when price moves > 30% of spacing from centre
MIN_SPACING_PCT     = 0.0005     # minimum spacing floor (overridden per-instance from env)


@dataclass
class GridLevel:
    side:       str           # "bid" or "ask"
    price:      float
    size:       float         # in base asset
    notional:   float         # USD
    level_idx:  int           # 1 = closest to mid
    stop_loss:  float         # price at which this level should be abandoned
    cloid:      Optional[str] = None
    is_filled:  bool          = False
    placed_at:  float         = 0.0


@dataclass
class GridState:
    levels:    List[GridLevel] = field(default_factory=list)
    mid:       float = 0.0
    spacing:   float = 0.0
    atr:       float = 0.0
    generated_at: float = 0.0

    @property
    def bid_levels(self) -> List[GridLevel]:
        return [l for l in self.levels if l.side == "bid"]

    @property
    def ask_levels(self) -> List[GridLevel]:
        return [l for l in self.levels if l.side == "ask"]

    @property
    def open_levels(self) -> List[GridLevel]:
        return [l for l in self.levels if not l.is_filled and l.cloid]


class GridManager:
    def __init__(self, market: str, cfg, num_levels: int = NUM_LEVELS,
                 level_notional: float = None):
        self.market         = market
        self._cfg           = cfg
        self.num_levels     = num_levels
        # level_notional derived from GRID_MAX_INVENTORY_USD (preferred) or
        # max_inventories (fallback) divided by num_levels.
        # This keeps grid sizing independent of directional max_inventory.
        if level_notional is not None:
            self.level_notional = level_notional
        else:
            import os
            prefix  = market.split("-")[0].upper()
            fallback = cfg.max_inventories.get(market, LEVEL_NOTIONAL_USD * num_levels)
            max_inv  = float(os.environ.get(f"{prefix}_GRID_MAX_INVENTORY_USD", fallback))
            self.level_notional = max_inv / num_levels
        # Guard: ensure level_notional >= MIN_NOTIONAL_USD ($10)
        # If not, reduce num_levels until it fits. Prevents exchange rejections.
        MIN_LEVEL_NOTIONAL = 10.0
        if self.level_notional < MIN_LEVEL_NOTIONAL:
            old_levels = self.num_levels
            # Recalculate max_inv based on original level_notional × original levels
            total_inv = self.level_notional * self.num_levels
            self.num_levels = max(1, int(total_inv / MIN_LEVEL_NOTIONAL))
            self.level_notional = total_inv / self.num_levels
            log.warning(
                f"{market}: level_notional too small — reduced levels "
                f"{old_levels} → {self.num_levels}, "
                f"level_notional → ${self.level_notional:.2f}"
            )
        self._current:     Optional[GridState] = None

    def generate(self, mid: float, atr: float, signal_weights=None) -> GridState:
        """
        Generate a fresh grid centred on mid.

        Args:
            mid:            Current mid price
            atr:            Current ATR value (absolute price units)
            signal_weights: SignalWeights — biases level sizes on each side

        Returns:
            GridState with all levels
        """
        spacing_mult    = float(os.environ.get("HOTSTUFF_GRID_SPACING_ATR_MULT", str(SPACING_ATR_MULT)))
        min_spacing_pct = float(os.environ.get("HOTSTUFF_GRID_MIN_SPACING_PCT", str(MIN_SPACING_PCT)))
        spacing = max(atr * spacing_mult, mid * min_spacing_pct)

        # Stop loss at 2× spacing beyond the outermost level
        stop_atr_mult = 2.0

        levels: List[GridLevel] = []

        for i in range(1, self.num_levels + 1):
            # ── BID levels (below mid) ──────────────────────────────────
            bid_price  = mid - spacing * i
            bid_mult   = self._size_mult(signal_weights, "bid", i)
            bid_notional = self.level_notional * bid_mult
            bid_size   = bid_notional / bid_price if bid_price > 0 else 0.0
            bid_stop   = bid_price - atr * stop_atr_mult

            levels.append(GridLevel(
                side="bid", price=round(bid_price, 8), size=round(bid_size, 8),
                notional=round(bid_notional, 2), level_idx=i,
                stop_loss=round(bid_stop, 8),
            ))

            # ── ASK levels (above mid) ──────────────────────────────────
            ask_price  = mid + spacing * i
            ask_mult   = self._size_mult(signal_weights, "ask", i)
            ask_notional = self.level_notional * ask_mult
            ask_size   = ask_notional / ask_price if ask_price > 0 else 0.0
            ask_stop   = ask_price + atr * stop_atr_mult

            levels.append(GridLevel(
                side="ask", price=round(ask_price, 8), size=round(ask_size, 8),
                notional=round(ask_notional, 2), level_idx=i,
                stop_loss=round(ask_stop, 8),
            ))

        state = GridState(
            levels=levels,
            mid=mid,
            spacing=spacing,
            atr=atr,
            generated_at=time.time(),
        )
        self._current = state

        log.info(
            f"{self.market} grid generated: mid={mid:.1f} "
            f"spacing={spacing:.2f}({spacing/mid*10000:.1f}bps) "
            f"atr={atr:.2f} levels={self.num_levels}×2"
        )
        return state

    def needs_rebalance(self, current_mid: float) -> bool:
        """True if price has drifted enough from grid centre to warrant regeneration."""
        if self._current is None:
            return True
        drift = abs(current_mid - self._current.mid)
        threshold = self._current.spacing * REBALANCE_THRESHOLD
        return drift > threshold

    def mark_filled(self, cloid: str):
        """Mark a level as filled by cloid."""
        if self._current is None:
            return
        for level in self._current.levels:
            if level.cloid == cloid:
                level.is_filled = True
                log.info(f"{self.market} grid fill: {level.side} L{level.level_idx} @ {level.price:.2f}")
                return

    def assign_cloid(self, side: str, level_idx: int, cloid: str, placed_at: float):
        if self._current is None:
            return
        for level in self._current.levels:
            if level.side == side and level.level_idx == level_idx:
                level.cloid     = cloid
                level.placed_at = placed_at
                return

    def clear(self):
        """Reset grid — called on mode switch or rebalance."""
        self._current = None

    @property
    def current(self) -> Optional[GridState]:
        return self._current

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _size_mult(weights, side: str, level_idx: int) -> float:
        """
        Compute size multiplier for a level.
          - Signal weight biases the side (0.5–1.5×)
          - Distance decay: farther levels are smaller (1.0 → 0.6 at level 5)
          - Clamp to [0.2, 1.5] to avoid zero or oversized levels
        """
        if weights is None:
            base = 1.0
        elif side == "bid":
            base = weights.long_weight
        else:
            base = weights.short_weight

        # Normalise weight to [0.5, 1.5] range for sizing
        # weight=1.0 → mult=1.0, weight=2.0 → mult=1.5, weight=0.0 → mult=0.5
        bias = 0.5 + (base / 2.0) * 1.0

        # V-shape sizing — alpha=0 uniform, alpha>0 outer levels bigger
        alpha = float(os.environ.get("HOTSTUFF_GRID_VSHAPE_ALPHA", "0.4"))
        vshape = 1.0 + alpha * (level_idx - 1)
        return max(0.2, min(3.0, bias * vshape))
