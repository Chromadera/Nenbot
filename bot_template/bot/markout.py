"""
Markout Tracker
Measures price movement after each fill at 10s, 30s, 60s, 5min intervals.
Positive markout = price moved in your favour after fill.
Negative markout = adverse selection — you were picked off.

Usage: runs as a background thread, fed fills from the bot.
"""
import math
import time
import threading
from collections import deque
from typing import List, Dict, Optional
from dataclasses import dataclass, field

from hotstuff import InfoClient
from hotstuff.methods.info.market import BBOParams

from bot.db import save_markout
from bot.logger import get_logger

log = get_logger("markout")

INTERVALS = {
    "10s":  10,
    "30s":  30,
    "60s":  60,
    "5m":   300,
}


@dataclass
class PendingMarkout:
    cloid:      str
    address:    str
    market:     str
    side:       str          # 'b' or 's'
    direction:  str          # openLong, closeShort etc
    fill_price: float
    fill_time:  float
    mid_10s:    Optional[float] = None
    mid_30s:    Optional[float] = None
    mid_60s:    Optional[float] = None
    mid_5m:     Optional[float] = None

    def is_complete(self) -> bool:
        return all([self.mid_10s, self.mid_30s, self.mid_60s, self.mid_5m])

    def needs_sample(self, label: str) -> bool:
        elapsed = time.time() - self.fill_time
        target  = INTERVALS[label]
        attr    = f"mid_{label.replace('m', 'm').replace('s', 's')}"
        return elapsed >= target and getattr(self, attr) is None


# ── Markout Adjuster ──────────────────────────────────────────────────────────

class MarkoutAdjuster:
    """
    Dynamically adjusts adverse selection floor based on recent fill quality.
    Combines v1 (bootstrap, sigmoid, rolling windows) with v2 (volatility scaling,
    ATR smoothing, dead zone, side-split).

    Sign convention: markout values passed in should be NEGATIVE for adverse
    selection (price moved against you after fill). The adjuster internally
    negates so that higher adverse selection → higher floor adjustment.
    """

    def __init__(
        self,
        base_floor_bps:      float,   # user-configured floor (bps)
        base_multiplier:     float = 0.5,    # scales max_adjustment with ATR
        baseline_window:     int   = 50,     # fills for historical baseline
        recent_window:       int   = 20,     # fills for recent comparison
        bootstrap_threshold: int   = 30,     # min fills before going live
        dead_zone_z:         float = 1.0,    # z threshold below which no adjustment
        ema_alpha:           float = 0.1,    # EMA smoothing for ATR
        epsilon:             float = 1e-8,   # prevents division by zero
    ):
        self.base_floor_bps      = base_floor_bps
        self.base_multiplier     = base_multiplier
        self.baseline_window     = baseline_window
        self.recent_window       = recent_window
        self.bootstrap_threshold = bootstrap_threshold
        self.dead_zone_z         = dead_zone_z
        self.ema_alpha           = ema_alpha
        self.epsilon             = epsilon
        self._markouts           = deque(maxlen=baseline_window)
        self._smoothed_atr_pct   = None   # EMA-smoothed ATR percentage

    def update_atr(self, atr_pct: float):
        """Update EMA-smoothed ATR. Call each quote cycle with latest atr_pct."""
        if self._smoothed_atr_pct is None:
            self._smoothed_atr_pct = atr_pct
        else:
            self._smoothed_atr_pct = (
                self.ema_alpha * atr_pct +
                (1.0 - self.ema_alpha) * self._smoothed_atr_pct
            )

    def add_fill(self, markout_bps: float, is_opening: bool, is_maker: bool):
        """
        Add a fill to the rolling window.
        markout_bps: positive = price moved in your favour, negative = adverse
        Only opening maker fills are tracked.
        """
        if not is_opening or not is_maker:
            return
        # Negate: adverse selection (negative markout) becomes positive input
        self._markouts.append(-markout_bps)

    def get_adjusted_floor(self) -> dict:
        """
        Compute the adjusted adverse selection floor.
        Returns dict with mode, floor, z_score, adjustment.
        """
        n = len(self._markouts)

        # Bootstrap phase — not enough data yet
        if n < self.bootstrap_threshold:
            return {
                "mode":       "bootstrap",
                "floor":      self.base_floor_bps,
                "z_score":    None,
                "adjustment": 0.0,
                "fills":      n,
            }

        baseline = list(self._markouts)
        recent   = baseline[-self.recent_window:]

        mu    = sum(baseline) / len(baseline)
        var   = sum((x - mu) ** 2 for x in baseline) / len(baseline)
        sigma = max(math.sqrt(var), self.epsilon)

        m = sum(recent) / len(recent)
        z = (m - mu) / sigma
        z = max(min(z, 5.0), -5.0)   # clamp at ±5

        # Dead zone — no adjustment below threshold
        if z < self.dead_zone_z:
            return {
                "mode":       "live",
                "floor":      self.base_floor_bps,
                "z_score":    round(z, 3),
                "adjustment": 0.0,
                "fills":      n,
            }

        # Volatility-scaled max adjustment
        atr_pct = self._smoothed_atr_pct or 0.001   # fallback 0.1% if no ATR yet
        max_adjustment = self.base_multiplier * atr_pct * 10000

        # Sigmoid dampening above dead zone
        z_shifted  = z - self.dead_zone_z
        d          = 1.0 / (1.0 + math.exp(-z_shifted))
        adjustment = d * max_adjustment
        new_floor  = self.base_floor_bps + adjustment

        return {
            "mode":       "live",
            "floor":      round(new_floor, 4),
            "z_score":    round(z, 3),
            "adjustment": round(adjustment, 4),
            "fills":      n,
        }


class MarkoutTracker:
    def __init__(self, address: str, base_floor_bps: float = 0.27, base_multiplier: float = 0.5):
        self.address          = address
        self._base_floor_bps  = base_floor_bps
        self._base_multiplier = base_multiplier
        self._lock            = threading.Lock()
        self._pending: List[PendingMarkout] = []
        self._info   = InfoClient(is_testnet=False)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Per-market, per-side adjusters (long=True, short=False)
        self._adjusters: Dict[str, Dict[bool, MarkoutAdjuster]] = {}

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Markout tracker started")

    def stop(self):
        self._running = False

    def on_fill(self, fill: dict):
        """Call this when a fill comes in from the bot."""
        # Handle dict, object, or nested data attribute
        if hasattr(fill, "data"):
            fill = fill.data
        if not isinstance(fill, dict):
            fill = vars(fill) if hasattr(fill, "__dict__") else {}
        d = fill

        cloid     = str(d.get("cloid", "") or getattr(fill, "cloid", ""))
        market    = d.get("instrument", "") or getattr(fill, "instrument", "")
        side      = d.get("side", "") or getattr(fill, "side", "")
        direction = d.get("direction", "") or getattr(fill, "direction", "")
        price     = float(d.get("price", 0) or getattr(fill, "price", 0))

        if not cloid or not market or not price:
            return

        pm = PendingMarkout(
            cloid=cloid, address=self.address, market=market,
            side=side, direction=direction,
            fill_price=price, fill_time=time.time(),
        )
        with self._lock:
            if any(p.cloid == cloid for p in self._pending):
                log.debug(f"Markout: duplicate fill ignored cloid={cloid[:16]}")
                return
            self._pending.append(pm)
        log.info(f"Markout tracking: {market} {direction} @ {price:.4f} cloid={cloid[:16]}")

    def _get_adjuster(self, market: str, is_long: bool) -> MarkoutAdjuster:
        """Get or create adjuster for market/side combination."""
        if market not in self._adjusters:
            self._adjusters[market] = {}
        if is_long not in self._adjusters[market]:
            self._adjusters[market][is_long] = MarkoutAdjuster(
                base_floor_bps  = self._base_floor_bps,
                base_multiplier = self._base_multiplier,
            )
        return self._adjusters[market][is_long]

    def update_atr(self, market: str, atr_pct: float):
        """Update EMA-smoothed ATR for both sides of a market. Call each quote cycle."""
        for is_long in (True, False):
            self._get_adjuster(market, is_long).update_atr(atr_pct)

    def get_floor(self, market: str, is_long: bool) -> float:
        """
        Get the current dynamic adverse selection floor for a market/side.
        Returns base_floor_bps during bootstrap, adjusted floor when live.
        """
        adj    = self._get_adjuster(market, is_long)
        result = adj.get_adjusted_floor()
        if result["mode"] == "live" and result["adjustment"] > 0:
            log.debug(
                f"AdverseFloor {market} {'long' if is_long else 'short'}: "
                f"{result['floor']:.3f}bps "
                f"(+{result['adjustment']:.3f} z={result['z_score']:.2f} "
                f"fills={result['fills']})"
            )
        return result["floor"]

    def get_floor_status(self, market: str) -> dict:
        """Return adjuster status for both sides — used by analytics."""
        return {
            "long":  self._get_adjuster(market, True).get_adjusted_floor(),
            "short": self._get_adjuster(market, False).get_adjusted_floor(),
        }

    def _get_mid(self, market: str) -> Optional[float]:
        try:
            result = self._info.bbo(BBOParams(symbol=market))
            if not result:
                return None
            bbo = result[0]   # info.bbo() returns List[BBO]
            bid = float(bbo.best_bid_price)
            ask = float(bbo.best_ask_price)
            return (bid + ask) / 2
        except Exception as e:
            if "timeout" in str(e).lower():
                self._info = InfoClient(is_testnet=False)
            return None

    def _loop(self):
        while self._running:
            with self._lock:
                pending = list(self._pending)

            completed = []
            for pm in pending:
                # Sample each interval as it becomes due
                for label, seconds in INTERVALS.items():
                    attr = f"mid_{label}"
                    if time.time() - pm.fill_time >= seconds and getattr(pm, attr) is None:
                        mid = self._get_mid(pm.market)
                        if mid:
                            setattr(pm, attr, mid)
                            elapsed_bps = (mid - pm.fill_price) / pm.fill_price * 100
                            direction_sign = 1 if pm.side == 'b' else -1
                            markout_bps = elapsed_bps * direction_sign
                            log.info(
                                f"Markout {pm.market} {pm.direction} {label}: "
                                f"{markout_bps:+.2f}bps (fill={pm.fill_price:.4f} mid={mid:.4f})"
                            )

                if pm.is_complete():
                    save_markout(
                        cloid=pm.cloid, address=pm.address,
                        market=pm.market, side=pm.side, direction=pm.direction,
                        fill_price=pm.fill_price, fill_time=pm.fill_time,
                        mid_10s=pm.mid_10s, mid_30s=pm.mid_30s,
                        mid_60s=pm.mid_60s, mid_5m=pm.mid_5m,
                    )
                    completed.append(pm)
                    log.info(f"Markout saved: {pm.market} {pm.direction} cloid={pm.cloid[:16]}")

                    # Feed into adjuster — opening maker fills only
                    is_opening = pm.direction in ("openLong", "openShort")
                    is_maker   = not pm.direction.startswith("close")  # rough maker proxy
                    is_long    = pm.direction == "openLong"
                    if is_opening and pm.mid_30s and pm.fill_price:
                        # 30s markout: positive = favourable, negative = adverse selection
                        raw_markout = (pm.mid_30s - pm.fill_price) / pm.fill_price * 100
                        direction_sign = 1 if pm.side == "b" else -1
                        markout_bps = raw_markout * direction_sign
                        adj = self._get_adjuster(pm.market, is_long)
                        adj.add_fill(markout_bps, is_opening=True, is_maker=is_maker)

            if completed:
                with self._lock:
                    for pm in completed:
                        self._pending.remove(pm)

            time.sleep(2)

    def get_pending_count(self) -> int:
        with self._lock:
            return len(self._pending)
