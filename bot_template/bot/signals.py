"""
Signal pipeline — combines regime, indicators, and OFI into
a SignalWeights object consumed by both quoting.py (directional mode)
and grid.py (grid mode).

Weights are multipliers in [0.0, 2.0]:
  1.0 = neutral / no adjustment
  0.0 = block this side entirely
  2.0 = full aggressive sizing

Pipeline (applied in order, multiplicative):
  1. RSI extreme filter    — hard block at overbought/oversold
  2. VWAP distance         — reduce unfavourable side when far from VWAP
  3. Regime tilt           — amplify with-trend, dampen against-trend
  4. OFI confirmation      — fine-grained entry timing
  5. Post-stop cooldown    — block same-direction re-entry after a stop

Usage:
    pipeline = SignalPipeline()
    weights  = pipeline.evaluate(regime_state, indicator_snap, ofi_signal,
                                 last_stop_direction, last_stop_regime)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from bot.regime import Regime, RegimeState
from bot.logger import get_logger

log = get_logger("signals")

# RSI thresholds — only fire at genuine extremes on 5m bars
RSI_OB = 72.0
RSI_OS = 28.0

# VWAP — reduce unfavourable side when price is this far from VWAP
VWAP_MAX_DIST_PCT = 1.5   # %

# Post-stop cooldown — block same-direction re-entry until regime changes
# Set to True to enable, False to allow immediate re-entry
ENABLE_STOP_COOLDOWN = True

# Signal conflict threshold — block trading when regime and OFI disagree strongly.
# Conflict score = regime_dir × ofi_smooth (negative = disagreement)
# score ≤ -threshold      → no-trade (hard block both sides)
# score ≤ -threshold / 2  → mild dampening (weights pulled toward neutral)
# Set to 0.0 to disable conflict resolution entirely
import os as _sc_os
SIGNAL_CONFLICT_THRESHOLD = float(_sc_os.environ.get("HOTSTUFF_SIGNAL_CONFLICT_BPS", "0.3"))


@dataclass
class SignalWeights:
    long_weight:   float = 1.0   # multiplier for bid / buy side
    short_weight:  float = 1.0   # multiplier for ask / sell side
    allow_long:    bool  = True  # hard block (RSI extreme)
    allow_short:   bool  = True  # hard block (RSI extreme)
    regime:        str   = "ranging"
    reasons:       list  = field(default_factory=list)

    @property
    def is_neutral(self) -> bool:
        return not self.allow_long and not self.allow_short


class SignalPipeline:
    def __init__(self):
        # Track last stop-out direction and the regime at the time, per market
        self._last_stop: dict = {}   # market → {"direction": "long"/"short", "regime": Regime}

    def record_stop(self, market: str, direction: str, regime: RegimeState):
        """
        Call this from main.py whenever a stop-loss or profit-lock fires.
        direction: "long" if we were long (stop was a sell), "short" if we were short.
        """
        if ENABLE_STOP_COOLDOWN:
            self._last_stop[market] = {
                "direction": direction,
                "regime":    regime.regime if regime else None,
            }
            log.info(f"{market}: stop recorded — blocking {direction} re-entry until regime changes")

    def clear_stop(self, market: str):
        """Call when regime changes or manually cleared."""
        self._last_stop.pop(market, None)

    def evaluate(
        self,
        market:         str,
        regime:         Optional[RegimeState],
        indicators,                            # IndicatorSnapshot or None
        ofi=None,                              # OFISignal or None
    ) -> SignalWeights:

        w = SignalWeights()
        if regime:
            w.regime = regime.label()

        reasons = []

        # ── 1. RSI extreme filter ────────────────────────────────────────
        if indicators is not None:
            rsi = indicators.rsi
            if rsi >= RSI_OB:
                w.allow_long = False
                reasons.append(f"RSI OB {rsi:.0f}")
            if rsi <= RSI_OS:
                w.allow_short = False
                reasons.append(f"RSI OS {rsi:.0f}")

        # ── 2. VWAP distance — weight modifier ───────────────────────────
        if indicators is not None:
            dist = indicators.vwap_dist   # positive = above VWAP
            if abs(dist) > VWAP_MAX_DIST_PCT:
                if dist > 0:
                    # Price extended above VWAP — reduce long weight
                    w.long_weight  *= 0.5
                    reasons.append(f"VWAP +{dist:.1f}%")
                else:
                    # Price extended below VWAP — reduce short weight
                    w.short_weight *= 0.5
                    reasons.append(f"VWAP {dist:.1f}%")

        # ── 3. Regime tilt — scaled by confidence ────────────────────────
        # confidence=1.0 → full tilt (same as before)
        # confidence=0.5 → half tilt
        # confidence=0.0 → neutral (no bias)
        # Formula: scaled = 1.0 + (base_mult - 1.0) * confidence
        if regime is not None:
            conf = max(0.0, min(1.0, regime.confidence))
            if regime.regime == Regime.TRENDING_UP:
                long_mult  = 1.0 + (1.3 - 1.0) * conf   # 1.0 → 1.3
                short_mult = 1.0 + (0.5 - 1.0) * conf   # 1.0 → 0.5
                w.long_weight  *= long_mult
                w.short_weight *= short_mult
                reasons.append(f"TREND UP conf={conf:.2f} L×{long_mult:.2f} S×{short_mult:.2f}")
            elif regime.regime == Regime.TRENDING_DOWN:
                long_mult  = 1.0 + (0.5 - 1.0) * conf   # 1.0 → 0.5
                short_mult = 1.0 + (1.3 - 1.0) * conf   # 1.0 → 1.3
                w.long_weight  *= long_mult
                w.short_weight *= short_mult
                reasons.append(f"TREND DOWN conf={conf:.2f} L×{long_mult:.2f} S×{short_mult:.2f}")
            else:
                reasons.append("RANGE")

        # ── 3b. Signal conflict detection ───────────────────────────────
        # Only active in trending regime — ranging has no directional bias
        if (regime is not None and ofi is not None
                and SIGNAL_CONFLICT_THRESHOLD > 0
                and regime.regime != Regime.RANGING):
            regime_dir = 1.0 if regime.regime == Regime.TRENDING_UP else -1.0
            conflict   = regime_dir * ofi.ofi_smooth  # negative = disagreement
            mild_threshold = -SIGNAL_CONFLICT_THRESHOLD / 2
            hard_threshold = -SIGNAL_CONFLICT_THRESHOLD

            if conflict <= hard_threshold:
                # Strong conflict — no-trade state
                w.allow_long   = False
                w.allow_short  = False
                w.long_weight  = 0.0
                w.short_weight = 0.0
                reasons.append(
                    f"CONFLICT BLOCK regime={regime.label()} "
                    f"ofi={ofi.ofi_smooth:+.2f} score={conflict:.2f}"
                )
                log.info(
                    f"{market}: SIGNAL CONFLICT — no-trade "
                    f"(regime={regime.label()} ofi={ofi.ofi_smooth:+.2f} "
                    f"score={conflict:.2f} threshold={SIGNAL_CONFLICT_THRESHOLD})"
                )
            elif conflict <= mild_threshold:
                # Mild conflict — dampen both weights toward neutral
                dampen = 1.0 - (conflict - mild_threshold) / (hard_threshold - mild_threshold) * 0.4
                w.long_weight  = w.long_weight  * dampen + (1.0 - dampen)
                w.short_weight = w.short_weight * dampen + (1.0 - dampen)
                reasons.append(
                    f"CONFLICT MILD regime={regime.label()} "
                    f"ofi={ofi.ofi_smooth:+.2f} score={conflict:.2f} dampen={dampen:.2f}"
                )

        # ── 4. OFI confirmation ──────────────────────────────────────
        if ofi is not None:
            s = ofi.ofi_smooth
            if s > 0.2:
                w.long_weight  *= 1.2
                w.short_weight *= 0.8
            elif s > 0.05:
                w.long_weight  *= 1.1
                w.short_weight *= 0.9
            elif s < -0.2:
                w.long_weight  *= 0.8
                w.short_weight *= 1.2
            elif s < -0.05:
                w.long_weight  *= 0.9
                w.short_weight *= 1.1

        # ── 5. Post-stop cooldown ────────────────────────────────────────
        if ENABLE_STOP_COOLDOWN and market in self._last_stop:
            stop_info = self._last_stop[market]
            current_regime = regime.regime if regime else None

            # Lift cooldown if regime has changed since the stop
            if current_regime != stop_info["regime"]:
                self.clear_stop(market)
                reasons.append("stop cooldown lifted (regime changed)")
            else:
                # Still in same regime — block the direction that got stopped
                if stop_info["direction"] == "long":
                    w.allow_long  = False
                    w.long_weight = 0.0
                    reasons.append("stop cooldown: long blocked")
                elif stop_info["direction"] == "short":
                    w.allow_short  = False
                    w.short_weight = 0.0
                    reasons.append("stop cooldown: short blocked")

        # ── Clamp weights ────────────────────────────────────────────────
        w.long_weight  = max(0.0, min(2.0, w.long_weight))
        w.short_weight = max(0.0, min(2.0, w.short_weight))

        # Sync allow flags with zero weights
        if w.long_weight  == 0.0: w.allow_long  = False
        if w.short_weight == 0.0: w.allow_short = False

        w.reasons = reasons
        log.debug(
            f"{market} signals: long={w.long_weight:.2f}({w.allow_long}) "
            f"short={w.short_weight:.2f}({w.allow_short}) reasons={reasons}"
        )
        return w
