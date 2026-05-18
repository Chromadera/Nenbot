"""
Indicator engine — computes RSI, ADX (+DI / -DI), ATR, VWAP on 5m candles.
Pure Python — no numpy dependency.
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

from hotstuff import InfoClient
from hotstuff.methods.info.market import ChartParams

from bot.logger import get_logger

log = get_logger("indicators")

RESOLUTION       = "5"
LOOKBACK_CANDLES = 100
RETRAIN_INTERVAL = 60

RSI_PERIOD  = 14
ADX_PERIOD  = 14
ATR_PERIOD  = 14
VWAP_WINDOW = 48

SYMBOL_IDS = {
    "BTC-PERP":       "1",
    "ETH-PERP":       "2",
    "SOL-PERP":       "3",
    "GOLD-PERP":      "4",
    "SILVER-PERP":    "5",
    "X-PERP":         "6",
    "HYPE-PERP":      "7",
    "XRP-PERP":       "8",
    "ZEC-PERP":       "9",
    "BNB-PERP":       "10",
    "WTIOIL-PERP":    "11",
    "BRENTOIL-PERP":  "12",
    "NATGAS-PERP":    "13",
    "EURUSD-PERP": "14",
    "USDJPY-PERP": "15",
    "USA500-PERP": "16",
    "USA100-PERP": "17",
}


@dataclass
class IndicatorSnapshot:
    market:     str
    rsi:        float
    adx:        float
    plus_di:    float
    minus_di:   float
    atr:        float
    atr_pct:    float
    vwap:       float
    vwap_dist:  float
    close:      float
    updated_at: float


def _ewm(arr: list, span: int) -> list:
    alpha = 2.0 / (span + 1)
    out = [arr[0]]
    for i in range(1, len(arr)):
        out.append(alpha * arr[i] + (1.0 - alpha) * out[i - 1])
    return out


def _rsi(closes: list, period: int) -> float:
    delta  = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in delta]
    losses = [-d if d < 0 else 0.0 for d in delta]
    avg_gain = _ewm(gains,  period)[-1]
    avg_loss = _ewm(losses, period)[-1]
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _atr(highs: list, lows: list, closes: list, period: int) -> float:
    tr = []
    for i in range(1, len(highs)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]),
        ))
    return _ewm(tr, period)[-1]


def _adx(highs: list, lows: list, closes: list, period: int):
    up_move   = [highs[i] - highs[i-1] for i in range(1, len(highs))]
    down_move = [lows[i-1] - lows[i]   for i in range(1, len(lows))]

    plus_dm  = [u if u > d and u > 0 else 0.0 for u, d in zip(up_move, down_move)]
    minus_dm = [d if d > u and d > 0 else 0.0 for u, d in zip(up_move, down_move)]

    tr = []
    for i in range(1, len(highs)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1]),
        ))

    atr_s    = _ewm(tr, period)
    safe_atr = [x if x != 0 else 1e-10 for x in atr_s]
    pdm_ewm  = _ewm(plus_dm,  period)
    mdm_ewm  = _ewm(minus_dm, period)

    plus_di  = [100.0 * p / s for p, s in zip(pdm_ewm,  safe_atr)]
    minus_di = [100.0 * m / s for m, s in zip(mdm_ewm, safe_atr)]

    dx = []
    for p, m in zip(plus_di, minus_di):
        denom = p + m
        dx.append(0.0 if denom == 0 else 100.0 * abs(p - m) / denom)

    adx = _ewm(dx, period)
    return adx[-1], plus_di[-1], minus_di[-1]


def _vwap(closes: list, highs: list, lows: list, volumes: list, window: int) -> float:
    c = closes[-window:]
    h = highs[-window:]
    l = lows[-window:]
    v = volumes[-window:]
    typical = [(h[i] + l[i] + c[i]) / 3.0 for i in range(len(c))]
    total_v = sum(v)
    if total_v > 0:
        return sum(t * vol for t, vol in zip(typical, v)) / total_v
    return sum(typical) / len(typical)


class IndicatorEngine:
    def __init__(self, markets: List[str]):
        self.markets  = markets
        self._info    = InfoClient(is_testnet=False)
        self._lock    = threading.Lock()
        self._snaps:  Dict[str, IndicatorSnapshot] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        for m in self.markets:
            self._compute(m)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(f"IndicatorEngine started for {self.markets}")

    def stop(self):
        self._running = False

    def get(self, market: str) -> Optional[IndicatorSnapshot]:
        with self._lock:
            return self._snaps.get(market)

    def _fetch(self, market: str):
        symbol_id = SYMBOL_IDS.get(market)
        if not symbol_id:
            log.warning(f"No symbol ID for {market}")
            return None
        now   = int(time.time())
        from_ = now - LOOKBACK_CANDLES * int(RESOLUTION) * 60
        try:
            candles = self._info.chart(ChartParams(
                symbol=symbol_id, resolution=RESOLUTION,
                from_=from_, to=now, chart_type="mark",
            ))
            if len(candles) < 30:
                log.warning(f"{market}: only {len(candles)} candles -- skipping")
                return None
            return candles
        except Exception as e:
            log.error(f"{market} fetch error: {e}")
            if "timeout" in str(e).lower():
                self._info = InfoClient(is_testnet=False)
            return None

    def _compute(self, market: str):
        candles = self._fetch(market)
        if not candles:
            return

        closes  = [c.close  for c in candles]
        highs   = [c.high   for c in candles]
        lows    = [c.low    for c in candles]
        volumes = [c.volume for c in candles]

        rsi_val           = _rsi(closes, RSI_PERIOD)
        atr_val           = _atr(highs, lows, closes, ATR_PERIOD)
        adx_val, pdi, mdi = _adx(highs, lows, closes, ADX_PERIOD)
        vwap_val          = _vwap(closes, highs, lows, volumes, VWAP_WINDOW)
        vwap_dist         = (closes[-1] - vwap_val) / vwap_val * 100 if vwap_val else 0.0

        snap = IndicatorSnapshot(
            market    = market,
            rsi       = round(rsi_val, 2),
            adx       = round(adx_val, 2),
            plus_di   = round(pdi, 2),
            minus_di  = round(mdi, 2),
            atr       = round(atr_val, 2),
            atr_pct   = round(atr_val / closes[-1] * 100, 4),
            vwap      = round(vwap_val, 2),
            vwap_dist = round(vwap_dist, 3),
            close     = round(closes[-1], 2),
            updated_at= time.time(),
        )

        with self._lock:
            self._snaps[market] = snap

        log.info(
            f"{market} indicators: close={snap.close} rsi={snap.rsi:.1f} "
            f"adx={snap.adx:.1f}(+DI={snap.plus_di:.1f}/-DI={snap.minus_di:.1f}) "
            f"atr={snap.atr:.1f}({snap.atr_pct:.3f}%) "
            f"vwap={snap.vwap:.1f}(dist={snap.vwap_dist:+.2f}%)"
        )

    def _loop(self):
        while self._running:
            try:
                for m in self.markets:
                    self._compute(m)
            except Exception as e:
                log.error(f"Indicator loop error: {e}")
            time.sleep(RETRAIN_INTERVAL)
