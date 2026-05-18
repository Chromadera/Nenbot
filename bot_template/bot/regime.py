"""
Regime Detector — ADX hysteresis classifier.

Replaces the HMM-based detector. Uses ADX with a hysteresis band to
prevent rapid regime flipping, combined with +DI/-DI for direction.

Regimes:
  RANGING       — ADX < lower threshold  (range mode → grid strategy)
  TRENDING_UP   — ADX > upper threshold, +DI > -DI
  TRENDING_DOWN — ADX > upper threshold, -DI > +DI

Hysteresis band prevents noise-driven flips:
  Enter TREND  when ADX crosses above upper_threshold (default 25)
  Exit  TREND  when ADX drops below lower_threshold  (default 22)
  In-between   — hold current regime

Usage:
    regime = RegimeDetector(markets=['BTC-PERP'])
    regime.start()
    state = regime.get_regime('BTC-PERP')  # RegimeState or None
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional
from enum import IntEnum

from bot.logger import get_logger

log = get_logger("regime")

# Configurable via env vars — lower to activate grid more often
# ADX_TREND_THRESHOLD=20 with ADX_HYSTERESIS_BAND=3 → grid below 17
import os as _os
ADX_TREND_THRESHOLD = float(_os.environ.get("HOTSTUFF_ADX_TREND_THRESHOLD", "25.0"))
ADX_HYSTERESIS_BAND = float(_os.environ.get("HOTSTUFF_ADX_HYSTERESIS_BAND", "3.0"))     # exit trend below 25 - 3 = 22
POLL_INTERVAL       = 5.0     # seconds between regime checks (reads from indicator engine)


class Regime(IntEnum):
    RANGING       = 0
    TRENDING_UP   = 1
    TRENDING_DOWN = 2


@dataclass
class RegimeState:
    regime:       Regime
    adx:          float
    plus_di:      float
    minus_di:     float
    confidence:   float     # 0–1, how far ADX is from threshold
    updated_at:   float

    def label(self) -> str:
        return {
            Regime.RANGING:       "Ranging",
            Regime.TRENDING_UP:   "Trending Up",
            Regime.TRENDING_DOWN: "Trending Down",
        }[self.regime]

    def color(self) -> str:
        return {
            Regime.RANGING:       "cyan",
            Regime.TRENDING_UP:   "green",
            Regime.TRENDING_DOWN: "red",
        }[self.regime]

    def spread_multiplier_bid(self) -> float:
        return {
            Regime.RANGING:       1.0,
            Regime.TRENDING_UP:   2.0,   # widen bid — avoid building longs into uptrend
            Regime.TRENDING_DOWN: 0.8,   # tighten bid — want to buy dip
        }[self.regime]

    def spread_multiplier_ask(self) -> float:
        return {
            Regime.RANGING:       1.0,
            Regime.TRENDING_UP:   0.8,   # tighten ask — want to sell strength
            Regime.TRENDING_DOWN: 2.0,   # widen ask — avoid building shorts into downtrend
        }[self.regime]

    def max_inventory_multiplier(self) -> float:
        return {
            Regime.RANGING:       1.0,
            Regime.TRENDING_UP:   0.5,
            Regime.TRENDING_DOWN: 0.5,
        }[self.regime]

    @property
    def is_trending(self) -> bool:
        return self.regime in (Regime.TRENDING_UP, Regime.TRENDING_DOWN)

    @property
    def is_ranging(self) -> bool:
        return self.regime == Regime.RANGING

    @property
    def trend_direction(self) -> str:
        if self.regime == Regime.TRENDING_UP:   return "UP"
        if self.regime == Regime.TRENDING_DOWN: return "DOWN"
        return "NONE"


class _HysteresisDetector:
    """Per-market hysteresis state machine."""
    def __init__(self, upper: float, lower: float):
        self.upper   = upper
        self.lower   = lower
        self._regime = Regime.RANGING   # start in range until proven otherwise

    def update(self, adx: float, plus_di: float, minus_di: float) -> RegimeState:
        # Hysteresis transitions
        if adx >= self.upper:
            self._regime = Regime.TRENDING_UP if plus_di >= minus_di else Regime.TRENDING_DOWN
        elif adx <= self.lower:
            self._regime = Regime.RANGING
        # else: keep current regime (in hysteresis band)

        # Confidence: distance from the relevant threshold, normalised 0–1
        if self._regime != Regime.RANGING:
            raw  = (adx - self.upper) / 20.0 + 0.5
        else:
            raw  = (self.lower - adx) / 20.0 + 0.5
        confidence = max(0.0, min(1.0, raw))

        return RegimeState(
            regime     = self._regime,
            adx        = adx,
            plus_di    = plus_di,
            minus_di   = minus_di,
            confidence = confidence,
            updated_at = time.time(),
        )


class RegimeDetector:
    def __init__(self, markets: List[str], indicator_engine=None):
        """
        Args:
            markets: list of market symbols
            indicator_engine: IndicatorEngine instance (injected from main.py).
                              RegimeDetector reads ADX values from it rather than
                              fetching candles itself — single data source.
        """
        self.markets   = markets
        self._engine   = indicator_engine   # set via set_engine() if not passed at init
        self._lock     = threading.Lock()
        self._states:  Dict[str, RegimeState] = {}
        self._detectors: Dict[str, _HysteresisDetector] = {
            m: _HysteresisDetector(
                upper=ADX_TREND_THRESHOLD,
                lower=ADX_TREND_THRESHOLD - ADX_HYSTERESIS_BAND,
            )
            for m in markets
        }
        self._running  = False
        self._thread:  Optional[threading.Thread] = None

    def set_engine(self, engine):
        """Inject indicator engine after construction if needed."""
        self._engine = engine

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(f"RegimeDetector started for {self.markets}")

    def stop(self):
        self._running = False

    def get_regime(self, market: str) -> Optional[RegimeState]:
        with self._lock:
            return self._states.get(market)

    # ── internal ────────────────────────────────────────────────────────────

    def _update(self, market: str):
        if self._engine is None:
            return
        snap = self._engine.get(market)
        if snap is None:
            return

        state = self._detectors[market].update(snap.adx, snap.plus_di, snap.minus_di)

        with self._lock:
            old = self._states.get(market)
            self._states[market] = state

        if old is None or old.regime != state.regime:
            log.info(
                f"{market} REGIME CHANGE → {state.label()} "
                f"(ADX={state.adx:.1f} +DI={state.plus_di:.1f} -DI={state.minus_di:.1f} "
                f"conf={state.confidence:.0%})"
            )
        else:
            log.debug(
                f"{market} regime={state.label()} ADX={state.adx:.1f} "
                f"+DI={state.plus_di:.1f} -DI={state.minus_di:.1f}"
            )

        self._write_state()

    def _loop(self):
        while self._running:
            try:
                for m in self.markets:
                    self._update(m)
            except Exception as e:
                log.error(f"Regime loop error: {e}")
            time.sleep(POLL_INTERVAL)

    def _write_state(self):
        import json, os
        path = os.path.join(os.path.dirname(__file__), "regime_state.json")
        try:
            with self._lock:
                data = {
                    m: {
                        "regime":     s.label(),
                        "adx":        s.adx,
                        "plus_di":    s.plus_di,
                        "minus_di":   s.minus_di,
                        "confidence": s.confidence,
                        "bid_mult":   s.spread_multiplier_bid(),
                        "ask_mult":   s.spread_multiplier_ask(),
                        "updated_at": s.updated_at,
                    }
                    for m, s in self._states.items()
                }
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning(f"Could not write regime state: {e}")
