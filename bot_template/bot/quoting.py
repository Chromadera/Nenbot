"""Quoting logic — fair price, spread, tiered inventory skew."""
from dataclasses import dataclass
from typing import Optional
from bot.logger import get_logger

log = get_logger("quoting")

TIER_NORMAL   = 0.50
TIER_SKEW     = 1.00
TIER_ONE_SIDE = 1.50

import os as _qt_os
ADVERSE_SELECTION_BPS = float(_qt_os.environ.get("HOTSTUFF_ADVERSE_SELECTION_BPS", "0.27"))
PROFIT_TARGET_BPS     = float(_qt_os.environ.get("HOTSTUFF_PROFIT_TARGET_BPS", "5.0"))
MIN_SPREAD_BPS        = 2 * ADVERSE_SELECTION_BPS + PROFIT_TARGET_BPS

OFI_SHIFT_FACTOR = 0.0008   # 8bps max shift at full OFI signal (was 3bps — too small)

INVENTORY_PENALTY = 0.00015

TREND_BLOCK_ON  = 0.15
TREND_BLOCK_OFF = 0.05


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    fair: float
    skew_bps: float
    inv_usd: float
    skip_bid: bool = False
    skip_ask: bool = False
    bid_is_closing: bool = False  # True when bid closes a short position
    ask_is_closing: bool = False  # True when ask closes a long position
    market_reduce: bool = False
    market_reduce_side: str = ""
    market_reduce_usd: float = 0.0


class Quoter:
    def __init__(self, base_spread: float, inventory_skew: float, allow_flips: bool = True):
        self.base_spread    = base_spread
        self.inventory_skew = inventory_skew
        self.allow_flips    = allow_flips
        self._bid_blocked: dict = {}
        self._ask_blocked: dict = {}
        # Dynamic adverse selection floors — updated by main.py from MarkoutTracker
        # Keys: (market, is_long) → floor_bps. Falls back to module-level constant.
        self._dynamic_floors: dict = {}

    def set_adverse_floor(self, market: str, is_long: bool, floor_bps: float):
        """Update dynamic adverse selection floor for a market/side."""
        self._dynamic_floors[(market, is_long)] = floor_bps

    def get_adverse_floor(self, market: str, is_long: bool) -> float:
        """Get current adverse selection floor in bps."""
        return self._dynamic_floors.get((market, is_long), ADVERSE_SELECTION_BPS)

    def fair_price(self, binance_mid: Optional[float], bbo_mid: Optional[float]) -> Optional[float]:
        if bbo_mid:
            if binance_mid and abs(binance_mid - bbo_mid) / bbo_mid > 0.005:
                log.warning(f"Binance {binance_mid:.2f} diverges >50bps from BBO {bbo_mid:.2f}")
            return bbo_mid
        return binance_mid

    def quote(
        self,
        symbol: str,
        binance_mid: Optional[float],
        bbo_mid: Optional[float],
        pos_usd: float,
        max_inventory_usd: float,
        spread: Optional[float] = None,
        close_spread: Optional[float] = None,
        regime=None,
        ofi=None,
        signal_weights=None,
    ) -> Optional[Quote]:

        fair = self.fair_price(binance_mid, bbo_mid)
        if fair is None:
            log.warning(f"{symbol}: no fair price")
            return None

        effective_spread = spread if spread is not None else self.base_spread
        _is_long_tmp = pos_usd >= 0
        _floor_bps_tmp = self.get_adverse_floor(symbol, _is_long_tmp)
        _dynamic_min_spread_bps = 2 * _floor_bps_tmp + PROFIT_TARGET_BPS
        min_spread = _dynamic_min_spread_bps / 10_000
        if effective_spread < min_spread:
            log.debug(f"{symbol}: spread {effective_spread*10000:.2f}bps below adverse selection floor {_dynamic_min_spread_bps:.2f}bps — raising to floor")
            effective_spread = min_spread
        effective_close_spread = close_spread if close_spread is not None else effective_spread * 0.3

        ofi_fair_shift = 0.0
        if ofi is not None:
            ofi_fair_shift = ofi.ofi_smooth * OFI_SHIFT_FACTOR
            fair = fair * (1 + ofi_fair_shift)

        bid_spread_mult = 1.0
        ask_spread_mult = 1.0
        skip_bid = False
        skip_ask = False

        if signal_weights is not None:
            bid_spread_mult = signal_weights.long_weight
            ask_spread_mult = signal_weights.short_weight
            if regime is not None:
                max_inventory_usd = max_inventory_usd * regime.max_inventory_multiplier()
            if not signal_weights.allow_long:
                skip_bid = True
            if not signal_weights.allow_short:
                skip_ask = True
            log.info(
                f"{symbol} signal: L={bid_spread_mult:.2f} S={ask_spread_mult:.2f} "
                f"regime={regime.label() if regime else 'none'} "
                f"reasons={signal_weights.reasons}"
            )
        elif ofi is not None:
            bid_spread_mult = ofi.combined_bid_multiplier(regime)
            ask_spread_mult = ofi.combined_ask_multiplier(regime)
            if regime is not None:
                max_inventory_usd = max_inventory_usd * regime.max_inventory_multiplier()
            log.info(
                f"{symbol} ofi={ofi.ofi_smooth:+.3f}({ofi.label()}) "
                f"regime={regime.label() if regime else 'none'} "
                f"fair_shift={ofi_fair_shift*10000:+.2f}bps "
                f"bid_mult={bid_spread_mult:.2f} ask_mult={ask_spread_mult:.2f}"
            )
        elif regime is not None:
            bid_spread_mult = regime.spread_multiplier_bid()
            ask_spread_mult = regime.spread_multiplier_ask()
            max_inventory_usd = max_inventory_usd * regime.max_inventory_multiplier()

        abs_inv   = abs(pos_usd)
        inv_ratio = abs_inv / max_inventory_usd
        is_long   = pos_usd >= 0

        # skip_bid/skip_ask already initialised above — preserve signal_weights decisions
        market_reduce = False
        market_reduce_side = ""
        market_reduce_usd  = 0.0

        if regime is not None and ofi is not None:
            from bot.regime import Regime
            bid_was_blocked = self._bid_blocked.get(symbol, False)
            ask_was_blocked = self._ask_blocked.get(symbol, False)

            if regime.regime == Regime.TRENDING_DOWN:
                if ofi.ofi_smooth < -TREND_BLOCK_ON:
                    self._bid_blocked[symbol] = True
                elif ofi.ofi_smooth > -TREND_BLOCK_OFF:
                    self._bid_blocked[symbol] = False
            else:
                self._bid_blocked[symbol] = False

            if regime.regime == Regime.TRENDING_UP:
                if ofi.ofi_smooth > TREND_BLOCK_ON:
                    self._ask_blocked[symbol] = True
                elif ofi.ofi_smooth < TREND_BLOCK_OFF:
                    self._ask_blocked[symbol] = False
            else:
                self._ask_blocked[symbol] = False

            if self._bid_blocked.get(symbol):
                skip_bid = True
                if not bid_was_blocked:
                    log.info(f"{symbol}: trend block ON — pulling bid (trending down, ofi={ofi.ofi_smooth:+.3f})")
            elif bid_was_blocked:
                log.info(f"{symbol}: trend block OFF — restoring bid (ofi={ofi.ofi_smooth:+.3f})")

            if self._ask_blocked.get(symbol):
                skip_ask = True
                if not ask_was_blocked:
                    log.info(f"{symbol}: trend block ON — pulling ask (trending up, ofi={ofi.ofi_smooth:+.3f})")
            elif ask_was_blocked:
                log.info(f"{symbol}: trend block OFF — restoring ask (ofi={ofi.ofi_smooth:+.3f})")

        # ── Flip prevention ──────────────────────────────────────────────────
        # When allow_flips=False, never post the adding side when positioned.
        # Only post the closing side — flips can only happen from flat.
        if not self.allow_flips and pos_usd != 0:
            if pos_usd > 0:
                skip_bid = True   # long — suppress bid (would add to long), only close via ask
            else:
                skip_ask = True   # short — suppress ask (would add to short), only close via bid

        if inv_ratio <= TIER_NORMAL:
            skew_factor = inv_ratio / TIER_NORMAL
            skew_bps    = -skew_factor * self.inventory_skew * 10_000 * (1 if is_long else -1)

        elif inv_ratio <= TIER_SKEW:
            skew_factor = (inv_ratio - TIER_NORMAL) / (TIER_SKEW - TIER_NORMAL)
            base_skew   = self.inventory_skew * 10_000
            phi_correction = INVENTORY_PENALTY * 10_000 * skew_factor
            extra_skew  = base_skew * 2 * skew_factor + phi_correction
            skew_bps    = -(base_skew + extra_skew) * (1 if is_long else -1)

        elif inv_ratio <= TIER_ONE_SIDE:
            skew_factor = (inv_ratio - TIER_SKEW) / (TIER_ONE_SIDE - TIER_SKEW)
            base_skew   = self.inventory_skew * 10_000 * 3
            extra_skew  = base_skew * skew_factor
            skew_bps    = -(base_skew + extra_skew) * (1 if is_long else -1)
            if is_long: skip_bid = True
            else:       skip_ask = True

        else:
            skew_bps = -self.inventory_skew * 10_000 * 5 * (1 if is_long else -1)
            if is_long:
                skip_bid           = True
                market_reduce      = True
                market_reduce_side = "sell"
                market_reduce_usd  = abs_inv - max_inventory_usd
            else:
                skip_ask           = True
                market_reduce      = True
                market_reduce_side = "buy"
                market_reduce_usd  = abs_inv - max_inventory_usd

        if inv_ratio > TIER_NORMAL:
            log.info(f"{symbol}: inv_ratio={inv_ratio:.2f} skew={skew_bps:+.1f}bps spread={effective_spread*10000:.0f}bps skip_bid={skip_bid} skip_ask={skip_ask}")

        # ── Calculate half spreads first so skew cap can reference them ─────
        half_spread = effective_spread / 2
        half_close  = effective_close_spread / 2
        # Use dynamic floor if available, fall back to static constant
        _is_long    = pos_usd >= 0
        _floor_bps  = self.get_adverse_floor(symbol, _is_long)
        adv_sel_adj = _floor_bps / 10_000

        if is_long and pos_usd > 0:
            bid_half = half_spread * bid_spread_mult + adv_sel_adj
            ask_half = half_close  * 1.0            + adv_sel_adj  # closing side — no OFI mult, always tight
        elif not is_long and pos_usd < 0:
            bid_half = half_close  * 1.0            + adv_sel_adj  # closing side — no OFI mult, always tight
            ask_half = half_spread * ask_spread_mult + adv_sel_adj
        else:
            bid_half = half_spread * bid_spread_mult + adv_sel_adj
            ask_half = half_spread * ask_spread_mult + adv_sel_adj

        # ── Skew cap — asymmetric, based on closing half only ────────────────
        # Cap skew so the CLOSING side never crosses fair (bid < fair when short,
        # ask > fair when long). The OPENING side is allowed to go wide naturally —
        # this makes it unattractive to fill, protecting against adverse opens.
        # For bid < fair:  skewed_mid * (1 - bid_half) < fair → skew_bps < bid_half * 10000
        # For ask > fair:  skewed_mid * (1 + ask_half) > fair → skew_bps > -ask_half * 10000
        if pos_usd < 0:
            # Short — closing side is bid, cap positive skew at bid_half
            closing_half = bid_half
        elif pos_usd > 0:
            # Long — closing side is ask, cap negative skew at ask_half
            closing_half = ask_half
        else:
            # Flat — use tighter of both sides
            closing_half = min(bid_half, ask_half)
        max_skew = max(closing_half * 10_000 - 1.0, 1.0)
        if abs(skew_bps) > max_skew:
            capped = max_skew * (1 if skew_bps > 0 else -1)
            log.debug(f"{symbol}: skew capped {skew_bps:+.1f}bps -> {capped:+.1f}bps")
            skew_bps = capped

        skewed_mid = fair * (1 + skew_bps / 10_000)
        bid = skewed_mid * (1 - bid_half)
        ask = skewed_mid * (1 + ask_half)

        return Quote(
            symbol=symbol, bid=bid, ask=ask, fair=fair,
            skew_bps=skew_bps, inv_usd=pos_usd,
            skip_bid=skip_bid, skip_ask=skip_ask,
            bid_is_closing=(pos_usd < 0),   # bid closes a short
            ask_is_closing=(pos_usd > 0),   # ask closes a long
            market_reduce=market_reduce,
            market_reduce_side=market_reduce_side,
            market_reduce_usd=market_reduce_usd,
        )
