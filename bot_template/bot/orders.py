"""Order management — place, cancel, cloid tracking, quote cycle, bracket orders."""
import time
import threading
import uuid
import math
from typing import Dict, Optional
from dataclasses import dataclass

from hotstuff.methods.exchange.trading import (
    UnitOrder,
    PlaceOrderParams,
    CancelByCloidParams,
    CancelAllParams,
    UnitCancelByClOrderId,
)

from bot.logger import get_logger
from bot.db import save_placed_order
from bot.quoting import Quoter, Quote

log = get_logger("orders")

TTL_BUFFER_MS = 5_000

# Bracket config
BRACKET_TTL_MS     = 300_000   # 5 min — passive bracket orders sit longer than quotes
GAP_BUFFER_PCT     = 0.0005    # 5bps — if mid is this far past stop, escalate to market
BRACKET_CHECK_SECS = 1.0       # how often check_bracket is called from main


def make_cloid(symbol: str, side: str) -> str:
    return f"{symbol}-{side}-{uuid.uuid4().hex[:8]}"


def round_to_tick(value: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return round(round(value / tick) * tick, decimals)


def round_to_lot(value: float, lot: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(lot)))) if lot < 1 else 0
    return round(math.floor(value / lot) * lot, decimals)


@dataclass
class LiveOrder:
    cloid: str
    symbol: str
    side: str
    price: float
    size: float
    placed_at: float


@dataclass
class BracketState:
    """Tracks the two resting passive orders placed after a directional fill."""
    close_cloid:   str
    close_price:   float
    stop_cloid:    str
    stop_price:    float
    entry_price:   float
    position_side: str    # "long" or "short"
    placed_at:     float
    stop_escalated: bool = False   # True once we've fired the market fallback


class OrderManager:
    def __init__(self, exchange_client, cfg, quoter: Quoter):
        self._ex     = exchange_client
        self._cfg    = cfg
        self._quoter = quoter
        self._lock   = threading.Lock()

        self._live: Dict[str, Dict[str, Optional[LiveOrder]]] = {
            m: {"bid": None, "ask": None} for m in cfg.markets
        }
        self._tick:         Dict[str, float] = {}
        self._lot:          Dict[str, float] = {}
        self._min_notional: Dict[str, float] = {}
        self._last_market_reduce: Dict[str, float] = {}

        # Bracket state — one per market for directional mode
        self._bracket: Dict[str, Optional[BracketState]] = {}

        # Fill cooldown — blocks same-side requotes for 2s after a fill
        self._fill_cooldown: Dict[str, Dict[str, float]] = {
            m: {"bid": 0.0, "ask": 0.0} for m in cfg.markets
        }
        self._fill_cooldown_secs = 2.0

    def load_instrument_specs(self):
        for m in self._cfg.markets:
            inst = self._ex.get_instrument(m)
            self._tick[m]         = float(inst["tick_size"] if isinstance(inst, dict) else inst.tick_size)
            self._lot[m]          = float(inst["lot_size"]  if isinstance(inst, dict) else inst.lot_size)
            self._min_notional[m] = float(inst.get("min_notional_usd", 10) if isinstance(inst, dict) else 10)
            self._last_market_reduce[m] = 0.0
            self._bracket[m] = None
            log.info(f"  {m}: tick={self._tick[m]} lot={self._lot[m]} min_notional=${self._min_notional[m]}")

    # ── Formatting helpers ────────────────────────────────────────────────────

    def _fmt_price(self, symbol: str, price: float) -> str:
        tick = self._tick[symbol]
        rounded = round_to_tick(price, tick)
        decimals = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
        return f"{rounded:.{decimals}f}"

    def _fmt_size(self, symbol: str, size: float) -> str:
        lot = self._lot[symbol]
        decimals = max(0, -int(math.floor(math.log10(lot)))) if lot < 1 else 0
        return f"{size:.{decimals}f}"

    def _clamp_price(self, symbol: str, side: str, price: float) -> float:
        bbo = self._ex.get_bbo(symbol)
        if not bbo:
            return price
        tick = self._tick[symbol]
        if side == "bid" and price >= bbo["ask"]:
            price = bbo["ask"] - tick
        elif side == "ask" and price <= bbo["bid"]:
            price = bbo["bid"] + tick
        return price

    # ── Core limit placement ──────────────────────────────────────────────────

    def _place_limit(self, symbol: str, side: str, price: float,
                     reduce_only: bool = False, pos_size: float = 0.0,
                     override_size: float = None) -> Optional[LiveOrder]:
        """Place a post-only limit order."""
        inst_id   = self._ex.get_instrument_id(symbol)
        price     = self._clamp_price(symbol, side, price)
        price_str = self._fmt_price(symbol, price)
        expires   = int(time.time() * 1000) + self._cfg.order_ttl_ms + TTL_BUFFER_MS
        api_side  = "b" if side == "bid" else "s"

        usd = self._cfg.order_sizes[symbol]
        if reduce_only and pos_size > 0:
            size = round_to_lot(pos_size, self._lot[symbol])
        elif override_size is not None and override_size > 0:
            size = round_to_lot(override_size, self._lot[symbol])
        else:
            size = round_to_lot(usd / float(price_str), self._lot[symbol])

        for attempt in range(3):
            if size * float(price_str) < self._min_notional[symbol]:
                log.warning(f"{symbol} {side} size below min notional, skipping")
                return None

            cloid    = make_cloid(symbol, side)
            size_str = self._fmt_size(symbol, size)

            order = UnitOrder(
                instrumentId=inst_id, side=api_side, positionSide="BOTH",
                price=price_str, size=size_str, tif="GTC",
                ro=reduce_only, po=True, cloid=cloid,
            )

            try:
                _sent_at = time.time()
                resp   = self._ex.exchange.place_order(PlaceOrderParams(orders=[order], expiresAfter=expires))
                _acked_at = time.time()
                _placement_ms = round((_acked_at - _sent_at) * 1000, 1)
                status = resp.get("data", {}).get("status", [{}])
                if status and "error" in status[0]:
                    err     = status[0]["error"]
                    err_str = err.get("error", "") if isinstance(err, dict) else str(err)
                    if "insufficient margin" in err_str.lower() and attempt < 2:
                        size = round_to_lot(size / 2, self._lot[symbol])
                        log.warning(f"{symbol} {side} insufficient margin — retrying sz={self._fmt_size(symbol, size)}")
                        continue
                    log.warning(f"{symbol} {side} place failed: {err}")
                    return None

                placed_at = _acked_at
                live = LiveOrder(cloid=cloid, symbol=symbol, side=side,
                                 price=float(price_str), size=size, placed_at=placed_at)
                with self._lock:
                    self._live[symbol][side] = live
                owner = getattr(self._cfg, '_owner_address', '')
                save_placed_order(cloid, owner, symbol, side, float(price_str), placed_at, _placement_ms)
                log.info(f"  PLACED {symbol} {side.upper()} @ {price_str} sz={size_str} cloid={cloid} [{_placement_ms:.0f}ms]")
                return live

            except Exception as e:
                log.warning(f"{symbol} {side} place error: {e}")
                return None

        return None

    # ── Bracket orders ────────────────────────────────────────────────────────

    def place_bracket(self, symbol: str, entry_price: float, position_side: str,
                      pos_size: float, close_spread: float, stop_dist: float):
        """
        Place two passive resting orders after a directional fill:
          close order — at entry + close_spread (profit target, earns rebate)
          stop order  — at entry - stop_dist    (loss limit, earns rebate)

        Both are ro=True, po=True. Whichever fills first wins;
        ro=True auto-rejects the orphaned order.

        position_side: "long" or "short"
        close_spread:  fractional distance for profit target (e.g. 0.0001 = 1bp)
        stop_dist:     fractional distance for stop (e.g. 0.015 = 150bps of margin,
                       caller converts margin multiplier to price offset)
        """
        if self._bracket.get(symbol):
            return   # already bracketed

        inst_id  = self._ex.get_instrument_id(symbol)
        tick     = self._tick.get(symbol, 1.0)
        lot      = self._lot.get(symbol, 0.00001)
        size     = round_to_lot(pos_size, lot)
        expires  = int(time.time() * 1000) + BRACKET_TTL_MS

        if position_side == "long":
            close_side  = "s"
            stop_side   = "s"
            close_price = round_to_tick(entry_price * (1 + close_spread), tick)
            stop_price  = round_to_tick(entry_price * (1 - stop_dist),    tick)
        else:  # short
            close_side  = "b"
            stop_side   = "b"
            close_price = round_to_tick(entry_price * (1 - close_spread), tick)
            stop_price  = round_to_tick(entry_price * (1 + stop_dist),    tick)

        # Clamp close price to ensure it's passive (won't cross spread)
        bbo = self._ex.get_bbo(symbol)
        if bbo:
            if position_side == "long" and close_price <= bbo["ask"]:
                # Close is sell — must be above bid to be passive
                close_price = max(close_price, bbo["ask"] + tick)
            elif position_side == "short" and close_price >= bbo["bid"]:
                # Close is buy — must be below ask to be passive
                close_price = min(close_price, bbo["bid"] - tick)

        close_price_str = self._fmt_price(symbol, close_price)
        stop_price_str  = self._fmt_price(symbol, stop_price)
        size_str        = self._fmt_size(symbol, size)
        close_cloid     = make_cloid(symbol, f"bclose-{close_side}")
        stop_cloid      = make_cloid(symbol, f"bstop-{stop_side}")

        # Place close order
        close_ok = False
        try:
            order = UnitOrder(
                instrumentId=inst_id, side=close_side, positionSide="BOTH",
                price=close_price_str, size=size_str, tif="GTC",
                ro=True, po=True, cloid=close_cloid,   # po=True — earn rebate at target
            )
            resp   = self._ex.exchange.place_order(PlaceOrderParams(orders=[order], expiresAfter=expires))
            status = resp.get("data", {}).get("status", [{}])
            if status and "error" in status[0]:
                log.warning(f"{symbol} bracket close failed: {status[0].get('error', status[0])}")
            else:
                close_ok = True
                log.info(f"  BRACKET CLOSE {symbol} {'SHORT' if close_side=='s' else 'LONG'} "
                         f"@ {close_price_str} sz={size_str} cloid={close_cloid}")
        except Exception as e:
            log.warning(f"{symbol} bracket close error: {e}")

        # Place stop order
        stop_ok = False
        try:
            order = UnitOrder(
                instrumentId=inst_id, side=stop_side, positionSide="BOTH",
                price=stop_price_str, size=size_str, tif="GTC",
                ro=True, po=True, cloid=stop_cloid,
            )
            resp   = self._ex.exchange.place_order(PlaceOrderParams(orders=[order], expiresAfter=expires))
            status = resp.get("data", {}).get("status", [{}])
            if not (status and "error" in status[0]):
                stop_ok = True
                log.info(f"  BRACKET STOP  {symbol} {'SHORT' if stop_side=='s' else 'LONG'} "
                         f"@ {stop_price_str} sz={size_str} cloid={stop_cloid}")
        except Exception as e:
            log.warning(f"{symbol} bracket stop error: {e}")

        if close_ok or stop_ok:
            with self._lock:
                self._bracket[symbol] = BracketState(
                    close_cloid   = close_cloid  if close_ok else "",
                    close_price   = close_price,
                    stop_cloid    = stop_cloid   if stop_ok  else "",
                    stop_price    = stop_price,
                    entry_price   = entry_price,
                    position_side = position_side,
                    placed_at     = time.time(),
                )

    def cancel_bracket(self, symbol: str):
        """Cancel both bracket orders and clear state. Returns old BracketState."""
        with self._lock:
            b = self._bracket.get(symbol)
            if not b:
                return None
            self._bracket[symbol] = None

        inst_id = self._ex.get_instrument_id(symbol)
        expires = int(time.time() * 1000) + 10_000
        for cloid in [b.close_cloid, b.stop_cloid]:
            if not cloid:
                continue
            try:
                self._ex.exchange.cancel_by_cloid(CancelByCloidParams(
                    cancels=[UnitCancelByClOrderId(cloid=cloid, instrumentId=inst_id)],
                    expiresAfter=expires,
                ))
                log.info(f"  BRACKET CANCELLED {symbol} cloid={cloid}")
            except Exception as e:
                log.warning(f"{symbol} bracket cancel error cloid={cloid}: {e}")

    def has_bracket(self, symbol: str) -> bool:
        with self._lock:
            return self._bracket.get(symbol) is not None

    def check_bracket(self, symbol: str, mid: float):
        """
        Called every BBO tick in directional mode when a position is open.
        Checks if the passive stop has been gapped — escalates to market if so.
        Also re-places bracket if TTL has expired.
        """
        with self._lock:
            b = self._bracket.get(symbol)
        if not b or b.stop_escalated:
            return

        pos_side = b.position_side

        # Gap check — has mid moved meaningfully past the stop level?
        gap_breach = False
        if pos_side == "long":
            # Stop is below entry — breach if mid drops below stop - gap_buffer
            gap_price = b.stop_price * (1 - GAP_BUFFER_PCT)
            gap_breach = mid < gap_price
        else:  # short
            # Stop is above entry — breach if mid rises above stop + gap_buffer
            gap_price = b.stop_price * (1 + GAP_BUFFER_PCT)
            gap_breach = mid > gap_price

        if gap_breach:
            log.warning(
                f"{symbol}: bracket stop GAPPED — mid={mid:.2f} stop={b.stop_price:.2f} "
                f"gap_price={gap_price:.2f} — escalating to market"
            )
            with self._lock:
                b.stop_escalated = True

            # Cancel both passive orders and fire market reduce
            self.cancel_bracket(symbol)
            pos  = self._ex.get_position(symbol)
            size = abs(pos.get("size", 0.0))
            if size > 0 and pos.get("value_usd", 0) >= self._min_notional.get(symbol, 10):
                side = "sell" if pos_side == "long" else "buy"
                self._place_market_reduce(symbol, side, pos["value_usd"], mid, urgent=True)

        # TTL re-place — if bracket is stale (expired on exchange) re-place it
        elif time.time() - b.placed_at > BRACKET_TTL_MS / 1000 - 30:
            log.info(f"{symbol}: bracket TTL expiring — re-placing with trailing stop")
            was_escalated  = b.stop_escalated
            orig_entry     = b.entry_price
            close_spread   = abs(b.close_price - b.entry_price) / b.entry_price
            stop_dist      = abs(b.stop_price  - b.entry_price) / b.entry_price
            position_side  = b.position_side
            self.cancel_bracket(symbol)
            if not was_escalated:
                pos  = self._ex.get_position(symbol)
                size = abs(pos.get("size", 0.0))
                if size > 0:
                    # Trail stop only in profitable direction — never extend against position
                    if position_side == "long":
                        stop_ref = max(mid, orig_entry)
                    else:
                        stop_ref = min(mid, orig_entry)
                    self.place_bracket(
                        symbol        = symbol,
                        entry_price   = orig_entry,
                        position_side = position_side,
                        pos_size      = size,
                        close_spread  = close_spread,
                        stop_dist     = abs(stop_ref - orig_entry) / orig_entry + stop_dist,
                    )

    # ── Market reduce (last resort) ───────────────────────────────────────────

    def _place_market_reduce(self, symbol: str, side: str, reduce_usd: float,
                             ref_price: float, urgent: bool = False):
        now      = time.time()
        throttle = 1 if urgent else 30
        if now - self._last_market_reduce.get(symbol, 0) < throttle:
            return
        self._last_market_reduce[symbol] = now

        inst_id  = self._ex.get_instrument_id(symbol)
        # Use actual position size to avoid reduce-only rejection from price movement
        pos      = self._ex.get_position(symbol)
        pos_size = abs(pos.get("size", 0.0))
        size     = round_to_lot(min(reduce_usd / ref_price, pos_size), self._lot[symbol])
        if size * ref_price < self._min_notional[symbol]:
            log.warning(f"{symbol} market reduce size too small, skipping")
            return

        size_str = self._fmt_size(symbol, size)
        price    = ref_price * 0.997 if side == "sell" else ref_price * 1.003
        api_side = "s" if side == "sell" else "b"
        price_str = self._fmt_price(symbol, price)
        cloid    = make_cloid(symbol, f"reduce-{side}")
        expires  = int(time.time() * 1000) + 30_000

        order = UnitOrder(
            instrumentId=inst_id, side=api_side, positionSide="BOTH",
            price=price_str, size=size_str, tif="IOC",
            ro=True, po=False, isMarket=True, cloid=cloid,
        )
        try:
            resp   = self._ex.exchange.place_order(PlaceOrderParams(orders=[order], expiresAfter=expires))
            status = resp.get("data", {}).get("status", [{}])
            if status and "error" in status[0]:
                log.warning(f"{symbol} market reduce failed: {status[0]['error']}")
            else:
                log.info(f"  MARKET REDUCE {symbol} {side.upper()} sz={size_str} (~${reduce_usd:.0f})")
        except Exception as e:
            log.warning(f"{symbol} market reduce error: {e}")

    def _place_passive_reduce(self, symbol: str, side: str, reduce_usd: float, ref_price: float) -> str:
        """Place a passive reduce-only limit order at mid. Returns cloid or empty string on failure."""
        inst_id  = self._ex.get_instrument_id(symbol)
        pos      = self._ex.get_position(symbol)
        pos_size = abs(pos.get("size", 0.0))
        size     = round_to_lot(min(reduce_usd / ref_price, pos_size), self._lot[symbol])
        if size * ref_price < self._min_notional[symbol]:
            log.warning(f"{symbol} passive reduce size too small, skipping")
            return ""
        size_str  = self._fmt_size(symbol, size)
        price_str = self._fmt_price(symbol, ref_price)
        api_side  = "s" if side == "sell" else "b"
        cloid     = make_cloid(symbol, f"grid-sl-{side}")
        expires   = int(time.time() * 1000) + 60_000
        order = UnitOrder(
            instrumentId=inst_id, side=api_side, positionSide="BOTH",
            price=price_str, size=size_str, tif="GTC",
            ro=True, po=True, isMarket=False, cloid=cloid,
        )
        try:
            resp   = self._ex.exchange.place_order(PlaceOrderParams(orders=[order], expiresAfter=expires))
            status = resp.get("data", {}).get("status", [{}])
            if status and "error" in status[0]:
                log.warning(f"{symbol} passive reduce failed: {status[0]['error']}")
                return ""
            log.info(f"  PASSIVE REDUCE {symbol} {side.upper()} sz={size_str} px={price_str} (~${reduce_usd:.0f})")
            return cloid
        except Exception as e:
            log.warning(f"{symbol} passive reduce error: {e}")
            return ""

    # ── Quote orders ──────────────────────────────────────────────────────────

    def _cancel_quote(self, symbol: str, side: str):
        with self._lock:
            live = self._live[symbol].get(side)
            if live is None:
                return

        inst_id = self._ex.get_instrument_id(symbol)
        expires = int(time.time() * 1000) + 10_000
        try:
            self._ex.exchange.cancel_by_cloid(CancelByCloidParams(
                cancels=[UnitCancelByClOrderId(cloid=live.cloid, instrumentId=inst_id)],
                expiresAfter=expires,
            ))
            log.info(f"  CANCELLED {symbol} {side.upper()} cloid={live.cloid}")
        except Exception as e:
            log.warning(f"{symbol} {side} cancel error: {e}")
        finally:
            with self._lock:
                if self._live[symbol].get(side) and self._live[symbol][side].cloid == live.cloid:
                    self._live[symbol][side] = None

    def cancel_all(self):
        expires = int(time.time() * 1000) + 10_000
        try:
            self._ex.exchange.cancel_all(CancelAllParams(expiresAfter=expires))
            log.info("All orders cancelled.")
        except Exception as e:
            log.warning(f"cancel_all error: {e}")
        finally:
            with self._lock:
                for symbol in self._live:
                    self._live[symbol] = {"bid": None, "ask": None}
                for symbol in self._bracket:
                    self._bracket[symbol] = None

    def _needs_requote(self, live: Optional[LiveOrder], new_price: float, symbol: str) -> bool:
        if live is None:
            return True
        age_ms = (time.time() - live.placed_at) * 1000
        if age_ms >= self._cfg.order_ttl_ms:
            return True
        tick        = self._tick.get(symbol, 1.0)
        new_rounded = round_to_tick(new_price, tick)
        price_move  = abs(new_rounded - live.price) / live.price
        return price_move >= self._cfg.price_move_threshold

    def update(self, symbol: str, quote: Quote):
        """Update resting quote orders based on new quote."""
        if quote.market_reduce:
            self._place_market_reduce(symbol, quote.market_reduce_side,
                                      quote.market_reduce_usd, quote.fair)

        pos             = self._ex.get_position(symbol)
        actual_pos_size = abs(pos.get("size", 0.0))

        for side, new_price, skip, is_closing in [
            ("bid", quote.bid, quote.skip_bid, quote.bid_is_closing),
            ("ask", quote.ask, quote.skip_ask, quote.ask_is_closing),
        ]:
            with self._lock:
                live = self._live[symbol][side]

            if skip:
                if live:
                    self._cancel_quote(symbol, side)
                continue

            if time.time() - self._fill_cooldown[symbol][side] < self._fill_cooldown_secs:
                continue

            clamped_price = self._clamp_price(symbol, side, new_price)
            if self._needs_requote(live, clamped_price, symbol):
                if live:
                    self._cancel_quote(symbol, side)
                    time.sleep(self._cfg.requote_cooldown)
                self._place_limit(symbol, side, clamped_price,
                                  reduce_only=is_closing, pos_size=actual_pos_size)

    def get_live(self, symbol: str) -> Dict[str, Optional[LiveOrder]]:
        with self._lock:
            return dict(self._live[symbol])

    def on_fill(self, fill: dict):
        """Called on fill. Clears filled order state and fill cooldown."""
        cloid  = fill.get("cloid", "")
        symbol = fill.get("instrument", "")
        if not cloid or not symbol:
            return

        filled_side = None
        with self._lock:
            for side in ("bid", "ask"):
                live = self._live.get(symbol, {}).get(side)
                if live and live.cloid == cloid:
                    self._live[symbol][side] = None
                    filled_side = side
                    self._fill_cooldown[symbol][side] = time.time()
                    log.info(f"  FILL CLEARED {symbol} {side.upper()} cloid={cloid}")
                    break

            # Clear bracket state if a bracket order filled
            b = self._bracket.get(symbol)
            if b:
                if cloid in (b.close_cloid, b.stop_cloid):
                    self._bracket[symbol] = None
                    log.info(f"  BRACKET FILLED {symbol} cloid={cloid} — bracket cleared")

        # Cancel stale same-side quote to prevent double fill
        if filled_side:
            with self._lock:
                stale = self._live.get(symbol, {}).get(filled_side)
            if stale and stale.cloid != cloid:
                log.info(f"  CANCELLING stale {filled_side} {stale.cloid} after fill")
                self._cancel_quote(symbol, filled_side)
