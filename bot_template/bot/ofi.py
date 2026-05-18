"""
Order Flow Imbalance (OFI) Sidecar
Measures real-time buy vs sell pressure from the order book.

OFI = (bid_volume - ask_volume) / (bid_volume + ask_volume)
  +1.0 = pure buy pressure  → price likely to rise
  -1.0 = pure sell pressure → price likely to fall
   0.0 = balanced

Combined with HMM regime:
  - Trending Down + OFI negative → strong sell signal → widen ask aggressively
  - Trending Up   + OFI positive → strong buy signal  → widen bid aggressively
  - Regime and OFI disagree      → reduce multipliers, be cautious

Usage:
  ofi = OFISidecar(markets=['BTC-PERP', 'ETH-PERP'])
  ofi.start()
  signal = ofi.get_signal('BTC-PERP')  # returns OFISignal
"""
import time
import threading
import collections
from dataclasses import dataclass
from typing import Dict, Optional, List, Deque

from hotstuff import InfoClient
from hotstuff.methods.info.market import OrderbookParams

from bot.logger import get_logger

log = get_logger("ofi")

# How many levels deep to look in the order book
DEPTH_LEVELS = 10
# How often to sample the order book (seconds)
SAMPLE_INTERVAL = 2
# Rolling window size for OFI smoothing
WINDOW_SIZE = 20


@dataclass
class OFISignal:
    market:       str
    ofi:          float    # raw OFI [-1, +1]
    ofi_smooth:   float    # smoothed OFI over rolling window
    bid_volume:   float    # total bid volume in top DEPTH_LEVELS
    ask_volume:   float    # total ask volume in top DEPTH_LEVELS
    bid_ask_ratio: float   # bid_vol / ask_vol
    updated_at:   float

    def label(self) -> str:
        if self.ofi_smooth > 0.2:   return "Strong Buy"
        if self.ofi_smooth > 0.05:  return "Buy"
        if self.ofi_smooth < -0.2:  return "Strong Sell"
        if self.ofi_smooth < -0.05: return "Sell"
        return "Neutral"

    def color(self) -> str:
        if self.ofi_smooth > 0.2:   return "green"
        if self.ofi_smooth > 0.05:  return "green"
        if self.ofi_smooth < -0.2:  return "red"
        if self.ofi_smooth < -0.05: return "red"
        return "yellow"

    def combined_bid_multiplier(self, regime=None) -> float:
        """
        Combine OFI with HMM regime to get final bid spread multiplier.
        OFI positive (buy pressure) = widen bid (harder for buyers to lift us)
        OFI negative (sell pressure) = tighten bid (we want to buy the dip)
        """
        ofi_mult = 1.0
        if self.ofi_smooth > 0.2:   ofi_mult = 1.8   # strong buy — widen bid
        elif self.ofi_smooth > 0.05: ofi_mult = 1.3
        elif self.ofi_smooth < -0.2: ofi_mult = 0.7   # strong sell — tighten bid
        elif self.ofi_smooth < -0.05: ofi_mult = 0.9

        if regime is None:
            return ofi_mult

        regime_mult = regime.spread_multiplier_bid()
        # If both agree, amplify. If they disagree, dampen.
        if (ofi_mult > 1 and regime_mult > 1) or (ofi_mult < 1 and regime_mult < 1):
            combined = (ofi_mult + regime_mult) / 2 * 1.2   # amplify agreement
        else:
            combined = (ofi_mult + regime_mult) / 2 * 0.8   # dampen disagreement
        return min(combined, 1.4)

    def combined_ask_multiplier(self, regime=None) -> float:
        """
        OFI negative (sell pressure) = widen ask (harder for sellers to hit us)
        OFI positive (buy pressure) = tighten ask (we want to sell into strength)
        """
        ofi_mult = 1.0
        if self.ofi_smooth < -0.2:   ofi_mult = 1.8   # strong sell — widen ask
        elif self.ofi_smooth < -0.05: ofi_mult = 1.3
        elif self.ofi_smooth > 0.2:   ofi_mult = 0.7   # strong buy — tighten ask
        elif self.ofi_smooth > 0.05:  ofi_mult = 0.9

        if regime is None:
            return ofi_mult

        regime_mult = regime.spread_multiplier_ask()
        if (ofi_mult > 1 and regime_mult > 1) or (ofi_mult < 1 and regime_mult < 1):
            combined = (ofi_mult + regime_mult) / 2 * 1.2
        else:
            combined = (ofi_mult + regime_mult) / 2 * 0.8
        return min(combined, 1.4)


class OFISidecar:
    def __init__(self, markets: List[str]):
        self.markets  = markets
        self._info    = InfoClient(is_testnet=False)
        self._lock    = threading.Lock()
        self._signals: Dict[str, OFISignal] = {}
        self._history: Dict[str, Deque[float]] = {
            m: collections.deque(maxlen=WINDOW_SIZE) for m in markets
        }
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info(f"OFI sidecar started for {self.markets}")

    def stop(self):
        self._running = False

    def get_signal(self, market: str) -> Optional[OFISignal]:
        with self._lock:
            return self._signals.get(market)

    def _sample(self, market: str):
        try:
            ob = self._info.orderbook(OrderbookParams(symbol=market))

            bids = ob.bids[:DEPTH_LEVELS]
            asks = ob.asks[:DEPTH_LEVELS]

            # bids/asks are raw dicts from the API (OrderbookResponse does not
            # auto-construct nested OrderbookLevel objects from the response)
            bid_vol = sum(
                float(b['size'] if isinstance(b, dict) else b.size) *
                float(b['price'] if isinstance(b, dict) else b.price)
                for b in bids
            )
            ask_vol = sum(
                float(a['size'] if isinstance(a, dict) else a.size) *
                float(a['price'] if isinstance(a, dict) else a.price)
                for a in asks
            )
            total   = bid_vol + ask_vol

            if total == 0:
                return

            ofi = (bid_vol - ask_vol) / total
            self._history[market].append(ofi)

            history = list(self._history[market])
            ofi_smooth = sum(history) / len(history)

            signal = OFISignal(
                market=market,
                ofi=ofi,
                ofi_smooth=ofi_smooth,
                bid_volume=bid_vol,
                ask_volume=ask_vol,
                bid_ask_ratio=bid_vol / ask_vol if ask_vol > 0 else 999,
                updated_at=time.time(),
            )

            with self._lock:
                self._signals[market] = signal

            log.debug(
                f"OFI {market}: raw={ofi:+.3f} smooth={ofi_smooth:+.3f} "
                f"bid=${bid_vol:,.0f} ask=${ask_vol:,.0f} -> {signal.label()}"
            )
            self._write_state()

        except Exception as e:
            log.error(f"OFI sample error {market}: {e}")
            if "timeout" in str(e).lower():
                self._info = InfoClient(is_testnet=False)

    def _write_state(self):
        import json, os
        state_file = os.path.join(os.path.dirname(__file__), "ofi_state.json")
        try:
            data = self.summary()
            with open(state_file, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.warning(f"Could not write OFI state: {e}")

    def _loop(self):
        while self._running:
            try:
                for market in self.markets:
                    self._sample(market)
            except Exception as e:
                log.error(f"OFI loop error: {e}")
            time.sleep(SAMPLE_INTERVAL)

    def summary(self) -> Dict[str, dict]:
        with self._lock:
            return {
                m: {
                    "ofi": s.ofi,
                    "ofi_smooth": s.ofi_smooth,
                    "label": s.label(),
                    "bid_volume": s.bid_volume,
                    "ask_volume": s.ask_volume,
                    "updated_at": s.updated_at,
                }
                for m, s in self._signals.items()
            }
