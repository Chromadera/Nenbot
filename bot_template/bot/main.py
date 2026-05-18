"""Entry point — dual-mode bot: grid (range) + directional (trend)."""
import os, time, signal, sys, threading
from dotenv import load_dotenv
load_dotenv()


from bot.config   import load_config
from bot.logger   import get_logger
from bot.pricing  import BinanceFeed
from bot.exchange import HotstuffClient
from bot.quoting  import Quoter
from bot.orders   import OrderManager, round_to_tick, BRACKET_CHECK_SECS
from bot.markout  import MarkoutTracker
from bot.indicators import IndicatorEngine
from bot.regime   import RegimeDetector, Regime
from bot.ofi      import OFISidecar
from bot.signals  import SignalPipeline
from bot.grid     import GridManager
from bot.drawdown import DrawdownMonitor
from bot.db       import init_db

log      = get_logger("main")
shutdown = False

from dataclasses import dataclass, field as dc_field

@dataclass
class GridSLState:
    """Tracks an active grid stop-loss passive close order."""
    active:       bool  = False
    placed_at:    float = 0.0
    cloid:        str   = ""
    close_side:   str   = ""   # "buy" or "sell"
    replace_count: int  = 0


def main():
    global shutdown
    cfg = load_config()
    log.info(f"Markets: {cfg.markets}")
    log.info(f"Spread: {cfg.base_spread*10000:.0f}bps  Skew: {cfg.inventory_skew*10000:.0f}bps")

    main_address = os.environ["HOTSTUFF_WALLET_ADDRESS"]
    cfg._owner_address = main_address
    init_db()

    # ── Core services ────────────────────────────────────────────────────────
    _adv_floor_bps   = float(os.environ.get("HOTSTUFF_ADVERSE_SELECTION_BPS", "0.27"))
    _adv_multiplier  = float(os.environ.get("HOTSTUFF_ADVERSE_MULTIPLIER", "0.5"))
    markout_tracker  = MarkoutTracker(
        address         = main_address,
        base_floor_bps  = _adv_floor_bps,
        base_multiplier = _adv_multiplier,
    )
    markout_tracker.start()

    indicator_engine = IndicatorEngine(markets=cfg.markets)
    indicator_engine.start()

    regime_detector  = RegimeDetector(markets=cfg.markets, indicator_engine=indicator_engine)
    regime_detector.start()

    ofi_sidecar      = OFISidecar(markets=cfg.markets)
    ofi_sidecar.start()

    signal_pipeline  = SignalPipeline()
    drawdown_monitor = DrawdownMonitor(max_loss_usd=float(os.environ.get("HOTSTUFF_MAX_DAILY_LOSS", "20.0")))

    feed   = BinanceFeed(cfg.markets)
    client = HotstuffClient(cfg.agent_key, main_address=main_address, testnet=cfg.testnet)
    quoter = Quoter(base_spread=cfg.base_spread, inventory_skew=cfg.inventory_skew,
                    allow_flips=cfg.allow_flips)
    mgr    = OrderManager(client, cfg, quoter)

    # ── Per-market grid managers ──────────────────────────────────────────────
    import os as _os
    _grid_levels = int(float(_os.environ.get("HOTSTUFF_GRID_LEVELS", "5")))
    grid_managers = {m: GridManager(market=m, cfg=cfg, num_levels=_grid_levels) for m in cfg.markets}
    _grid_overshoot_mult = float(_os.environ.get("HOTSTUFF_GRID_OVERSHOOT_MULT", "1.1"))
    _grid_max_loss_pct   = float(_os.environ.get("HOTSTUFF_GRID_MAX_LOSS_PCT",   "0.02"))
    _grid_sl_ttl_s       = 30.0
    grid_sl_states: dict = {m: GridSLState() for m in cfg.markets}

    # ── Per-market mode tracking ─────────────────────────────────────────────
    # "grid" or "directional"
    active_mode: dict = {m: "directional" for m in cfg.markets}

    client.load_instruments(cfg.markets)
    mgr.load_instrument_specs()
    client.cancel_all_on_startup()
    client.start_watchdog()

    # ── Shutdown ──────────────────────────────────────────────────────────────
    def _shutdown(sig, frame):
        global shutdown
        log.info("Shutting down — cancelling all orders...")
        shutdown = True
        client.stop_watchdog()
        mgr.cancel_all()
        sys.exit(0)
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    def _resume(sig, frame):
        log.info("SIGUSR1 received — resuming drawdown monitor")
        drawdown_monitor.resume()
        log.info(f"Drawdown monitor resumed. PnL today: ${drawdown_monitor.pnl_today:.2f}  limit: -${drawdown_monitor._max:.2f}")

    signal.signal(signal.SIGUSR1, _resume)

    def _close_and_stop(sig, frame):
        """SIGUSR2 — market-close all positions then shutdown."""
        global shutdown
        log.warning("SIGUSR2 received — market-closing all positions and stopping")
        try:
            import time as _time
            # Step 1 — cancel all open orders first
            log.warning("Close-and-stop: cancelling all open orders")
            mgr.cancel_all()
            _time.sleep(1)
            client.refresh_positions()
            # Step 2 — market-close any open positions
            for m in cfg.markets:
                pos = client.get_position(m)
                if pos and abs(float(pos.get("size", 0))) > 0:
                    size = float(pos["size"])
                    ref  = client.get_mid(m) or float(pos.get("entry_price", 0))
                    side = "sell" if size > 0 else "buy"
                    reduce_usd = abs(size) * ref
                    log.warning(f"Close-and-stop: market-closing {m} size={size} ref={ref}")
                    mgr._place_market_reduce(m, side, reduce_usd, ref, urgent=True)
            _time.sleep(2)
        except Exception as e:
            log.error(f"Close-and-stop error: {e}")
        shutdown = True
    signal.signal(signal.SIGUSR2, _close_and_stop)

    # ── BBO callback ─────────────────────────────────────────────────────────
    _state     = {"ready": False}
    _bbo_queue = {}  # latest BBO per market — only process most recent
    _bbo_call_count = [0]  # mutable counter for on_bbo calls

    def on_bbo(symbol, bbo):
        _bbo_call_count[0] += 1
        if _state["ready"] and not shutdown:
            _bbo_queue[symbol] = bbo
        elif not _state["ready"]:
            log.warning(f"on_bbo called but state not ready — symbol={symbol}")

    def _quote_thread():
        last_health = time.time()
        _stall_counts: dict = {m: 0 for m in cfg.markets}
        while not shutdown:
            if _bbo_queue:
                markets = list(_bbo_queue.keys())
                for m in markets:
                    _bbo_queue.pop(m, None)
                    if not shutdown:
                        try:
                            run_quote_cycle(triggered_market=m)
                        except Exception as e:
                            log.warning(f"Quote cycle error {m}: {e}")
            now = time.time()
            if now - last_health > 60:
                feed_st = client.feed_status()
                log.info(f"Quote thread alive — queue={list(_bbo_queue.keys())} on_bbo_calls={_bbo_call_count[0]} feed={feed_st}")
                # ── Grid watchdog ─────────────────────────────────────────
                for m in cfg.markets:
                    if active_mode.get(m) != "grid":
                        continue
                    pos = client.get_position(m)
                    if pos.get("value_usd", 0) < 10.0:
                        continue  # flat — no rebalance expected
                    gm       = grid_managers[m]
                    last_gen = gm._current.generated_at if gm._current else 0
                    age      = now - last_gen
                    bbo      = client.get_bbo(m)
                    if age > 120 and _bbo_call_count[0] > 0 and bbo:
                        log.warning(f"{m}: GRID WATCHDOG — no rebalance in {age:.0f}s, BBO active — force requeueing")
                        _bbo_queue[m] = bbo
                        _stall_counts[m] = _stall_counts.get(m, 0) + 1
                    else:
                        _stall_counts[m] = 0  # reset if stall cleared
                    if age > 180 and _bbo_call_count[0] > 0 and bbo:
                        log.warning(f"{m}: GRID WATCHDOG — stall persists {age:.0f}s (trigger #{_stall_counts.get(m,0)}) — force quoting all markets")
                        try:
                            for _wm in cfg.markets:
                                _bbo = client.get_bbo(_wm)
                                if _bbo:
                                    _bbo_queue[_wm] = _bbo
                        except Exception as e:
                            log.warning(f"{m}: grid watchdog force quote error: {e}")
                    # Tier 3 — auto-restart after 2 consecutive stall triggers
                    if _stall_counts.get(m, 0) >= 2:
                        log.warning(f"{m}: GRID WATCHDOG — Tier 3 triggered after {_stall_counts[m]} stalls — restarting bot process")
                        import os as _os2, signal as _sig2
                        _os2.kill(_os2.getpid(), _sig2.SIGTERM)
                _bbo_call_count[0] = 0
                last_health = now
            time.sleep(0.05)

    _qt = threading.Thread(target=_quote_thread, daemon=True, name="quote-cycle")
    _qt.start()

    client.subscribe_bbo(cfg.markets, bbo_callback=on_bbo)

    def on_fill(f):
        mgr.on_fill(f)
        client.refresh_positions()
        markout_tracker.on_fill(f)
        from bot.db import save_fills; save_fills([f])
        # Feed closed PnL into drawdown monitor
        pnl = float(f.get("closed_pnl", 0.0))
        fee = float(f.get("fee", 0.0))
        if pnl != 0.0 or fee != 0.0:
            drawdown_monitor.record_pnl(pnl + fee)
        # Grid fill tracking
        cloid = f.get("cloid", "")
        mkt   = f.get("instrument", "")
        if mkt in grid_managers:
            grid_managers[mkt].mark_filled(cloid)


    # ── Startup fill backfill ─────────────────────────────────────────────────
    def _backfill_fills():
        """Fetch fills from exchange API and insert any missing ones into local DB."""
        import re as _re
        from hotstuff.methods.info.account import FillsParams
        from bot.db import save_fills
        from hotstuff import InfoClient

        NENBOT_CLOID = _re.compile(r'^[A-Z]+-PERP-(bid|ask|bclose|bstop|reduce|bracket)-')
        UUID_CLOSE   = _re.compile(r'^[0-9a-f-]{36}$')
        CLOSE_DIRS   = {"closeLong","closeShort","closeLongPartial","closeShortPartial"}

        def is_nenbot_fill(f):
            cloid = f.get("cloid","") or ""
            if NENBOT_CLOID.match(cloid):
                return True
            # Market reduce orders placed by NenBot have UUID cloids
            if UUID_CLOSE.match(cloid) and f.get("direction","") in CLOSE_DIRS:
                return True
            return False

        try:
            import sqlite3 as _sq
            info = InfoClient(is_testnet=cfg.testnet if hasattr(cfg,"testnet") else False)
            conn = _sq.connect(os.path.join(os.path.dirname(__file__), "hotstuff.db"))
            existing = set(r[0] for r in conn.execute("SELECT trade_id FROM fills").fetchall())
            conn.close()
            backfilled = 0
            page       = 1
            while True:
                resp = info.fills(FillsParams(user=main_address, page=page, limit=50))
                if not resp.entries:
                    break
                to_insert = []
                for f in resp.entries:
                    trade_id = str(f.get("trade_id",""))
                    if not trade_id or trade_id in existing:
                        continue
                    if not is_nenbot_fill(f):
                        continue
                    # Normalise timestamp to ms integer
                    ts = f.get("block_timestamp","")
                    if isinstance(ts, str) and "T" in ts:
                        from datetime import datetime as _dt
                        ts = int(_dt.fromisoformat(ts.replace("Z","+00:00")).timestamp() * 1000)
                    # Normalise fill dict to match save_fills expectations
                    normalised = dict(f)
                    normalised["block_timestamp"] = ts
                    normalised["notional_value"]  = float(f.get("notional_value") or
                        abs(float(f.get("size",0)) * float(f.get("price",0))))
                    to_insert.append(normalised)
                if to_insert:
                    save_fills(to_insert)
                    backfilled += len(to_insert)
                total_pages = getattr(resp, "total_pages", None) or 1
                if page >= total_pages:
                    break
                page += 1
            if backfilled:
                log.info(f"Startup backfill: inserted {backfilled} missing fills across {page} page(s)")
            else:
                log.info("Startup backfill: no missing fills")
        except Exception as e:
            log.warning(f"Startup backfill failed: {e}")

    _backfill_fills()
    # ─────────────────────────────────────────────────────────────────────────
    client.subscribe_account(on_fill=on_fill)


    # ── Account snapshot thread ───────────────────────────────────────────────
    SNAPSHOT_INTERVAL_S = 300  # every 5 minutes
    def _snapshot_loop():
        from hotstuff import InfoClient as _IC
        from hotstuff.methods.info.account import AccountSummaryParams as _ASP
        from bot.db import save_snapshot as _save_snap
        _info = _IC(is_testnet=False)
        while not shutdown:
            try:
                summary = _info.account_summary(_ASP(user=main_address))
                _save_snap(main_address, summary)
                log.info(f"Account snapshot saved — equity=${getattr(summary,'total_account_equity',0):.2f}")
            except Exception as e:
                log.warning(f"Snapshot error: {e}")
            for _ in range(SNAPSHOT_INTERVAL_S):
                if shutdown: break
                time.sleep(1)

    import threading as _threading
    _snap_thread = _threading.Thread(target=_snapshot_loop, daemon=True, name="snapshot")
    _snap_thread.start()
    log.info("Account snapshot thread started (every 5min)")
    # ─────────────────────────────────────────────────────────────────────────
    # ── Wait for initial data ─────────────────────────────────────────────────
    log.info("Waiting for initial data...")
    for _ in range(60):
        if feed.all_ready(cfg.markets) and all(client.get_bbo(m) for m in cfg.markets):
            break
        time.sleep(0.5)

    client.refresh_positions()
    for m in cfg.markets:
        pos = client.get_position(m)
        if pos["size"] != 0:
            log.info(f"  Existing position {m}: size={pos['size']} value=${pos['value_usd']:.1f}")

    LEVERAGE          = int(os.environ.get("HOTSTUFF_LEVERAGE", "50"))
    STOP_LOSS_M       = float(os.environ.get("HOTSTUFF_STOP_LOSS_MARGIN",   "0.015"))  # margin multiplier → stop distance
    TRAIL_RETRACE_PCT = float(os.environ.get("HOTSTUFF_TRAIL_RETRACE_PCT",  "0.35"))   # retrace fraction for trailing close order
    # Fee-based TP tiers for volume competition
    TAKER_FEE_RATE    = float(os.environ.get("HOTSTUFF_TAKER_FEE_RATE", "0.00025"))  # 2.5bps taker fee
    MAKER_FEE_RATE    = float(os.environ.get("HOTSTUFF_MAKER_FEE_RATE", "0.00002"))  # 0.2bps maker rebate

    # Per-market grid inventory cap — independent of directional max_inventory
    # Falls back to cfg.max_inventories[m] if not set
    def grid_max_inv(market: str) -> float:
        prefix = market.split("-")[0].upper()
        fallback = cfg.max_inventories.get(market, 100.0)
        return float(os.environ.get(f"{prefix}_GRID_MAX_INVENTORY_USD", fallback))

    peak_price:        dict = {m: 0.0   for m in cfg.markets}
    tp_activated:      dict = {m: False for m in cfg.markets}
    last_bracket_check: dict = {m: 0.0   for m in cfg.markets}

    # ── Transition engine state ───────────────────────────────────────────────
    # Tracks markets currently in TRANSITION state (between mode switches)
    # Value: {"target_mode": str, "close_cloid": str|None, "started_at": float}
    _transition: dict = {}
    TRANSITION_TIMEOUT_S = float(os.environ.get("HOTSTUFF_TRANSITION_TIMEOUT_S", "30"))

    # ── Mode switch helper ────────────────────────────────────────────────────

    def switch_mode(market: str, new_mode: str, reason: str):
        old_mode = active_mode[market]
        if old_mode == new_mode:
            return
        # Already in transition toward this target — don't re-trigger
        if market in _transition and _transition[market]["target_mode"] == new_mode:
            return

        pos     = client.get_position(market)
        pos_usd = pos["value_usd"] * (1 if pos["size"] >= 0 else -1)
        size    = pos.get("size", 0)
        entry   = pos.get("entry_price", 0)
        mid     = client.get_bbo(market)
        mid     = mid["mid"] if mid else None
        min_notional = mgr._min_notional.get(market, 10.0)

        # ── Flat: switch immediately ──────────────────────────────────────
        if pos["value_usd"] < min_notional or not size:
            log.info(f"{market}: MODE SWITCH {old_mode} → {new_mode} ({reason}) [flat — instant]")
            active_mode[market] = new_mode
            _transition.pop(market, None)
            mgr.cancel_all()
            grid_managers[market].clear()
            signal_pipeline.clear_stop(market)
            return

        # ── Positioned: enter TRANSITION state ───────────────────────────
        log.warning(
            f"{market}: MODE SWITCH {old_mode} → {new_mode} deferred — "
            f"position ${pos['value_usd']:.1f} open, entering TRANSITION"
        )
        active_mode[market] = "transition"
        _transition[market] = {
            "target_mode": new_mode,
            "close_cloid": None,
            "started_at":  time.time(),
        }

        # Cancel all open orders (grid levels, quotes)
        mgr.cancel_all()
        grid_managers[market].clear()

        # Place passive limit close at entry ± close_spread
        if mid and entry and size:
            close_spread = cfg.bracket_close_spreads.get(market, cfg.spreads.get(market, cfg.base_spread))
            tick = mgr._tick.get(market, 1.0)
            if size > 0:
                close_price = round_to_tick(entry * (1 + close_spread), tick)
                close_side  = "ask"
            else:
                close_price = round_to_tick(entry * (1 - close_spread), tick)
                close_side  = "bid"
            live = mgr._place_limit(
                symbol      = market,
                side        = close_side,
                price       = close_price,
                reduce_only = True,
                pos_size    = abs(size),
            )
            if live:
                _transition[market]["close_cloid"] = live.cloid
                log.info(
                    f"{market}: TRANSITION close order placed — "
                    f"{close_side} @ {close_price:.4f} size={abs(size):.4f}"
                )

    def _check_transition(market: str):
        """
        Called every quote cycle for markets in TRANSITION state.
        - If flat: complete the mode switch
        - If timed out: market reduce and force switch
        - Otherwise: re-place close order if stale
        """
        if market not in _transition:
            return

        tr      = _transition[market]
        pos     = client.get_position(market)
        size    = pos.get("size", 0)
        pos_usd = pos["value_usd"]
        mid     = client.get_bbo(market)
        mid     = mid["mid"] if mid else None
        min_notional = mgr._min_notional.get(market, 10.0)
        elapsed = time.time() - tr["started_at"]

        # ── Flat: complete the switch ─────────────────────────────────────
        if pos_usd < min_notional or not size:
            target = tr["target_mode"]
            log.info(
                f"{market}: TRANSITION complete → {target} "
                f"(flat after {elapsed:.1f}s)"
            )
            active_mode[market] = target
            _transition.pop(market, None)
            mgr.cancel_all()
            grid_managers[market].clear()
            signal_pipeline.clear_stop(market)
            return

        # ── Timeout: force market reduce ──────────────────────────────────
        if elapsed > TRANSITION_TIMEOUT_S:
            target = tr["target_mode"]
            log.warning(
                f"{market}: TRANSITION timeout ({elapsed:.1f}s) — "
                f"market reducing ${pos_usd:.1f} and forcing switch to {target}"
            )
            mgr.cancel_all()
            if mid and size:
                mgr._place_market_reduce(
                    market,
                    "sell" if size > 0 else "buy",
                    abs(pos_usd), mid, urgent=True
                )
                client.refresh_positions()
            active_mode[market] = target
            _transition.pop(market, None)
            grid_managers[market].clear()
            signal_pipeline.clear_stop(market)
            return

        # ── Still waiting: re-place close order if it disappeared ─────────
        close_cloid = tr.get("close_cloid")
        entry = pos.get("entry_price", 0)
        if mid and entry and size and close_cloid:
            with mgr._lock:
                # Check if close order is still live
                still_live = any(
                    o and o.cloid == close_cloid
                    for orders in mgr._live.values()
                    for o in orders.values()
                    if o
                )
            if not still_live:
                # Order cancelled or expired — re-place
                close_spread = cfg.bracket_close_spreads.get(market, cfg.spreads.get(market, cfg.base_spread))
                tick = mgr._tick.get(market, 1.0)
                if size > 0:
                    close_price = round_to_tick(entry * (1 + close_spread), tick)
                    close_side  = "ask"
                else:
                    close_price = round_to_tick(entry * (1 - close_spread), tick)
                    close_side  = "bid"
                live = mgr._place_limit(
                    symbol      = market,
                    side        = close_side,
                    price       = close_price,
                    reduce_only = True,
                    pos_size    = abs(size),
                )
                if live:
                    _transition[market]["close_cloid"] = live.cloid
                    log.info(
                        f"{market}: TRANSITION re-placed close order — "
                        f"{close_side} @ {close_price:.4f} ({elapsed:.1f}s elapsed)"
                    )

        log.debug(f"{market}: TRANSITION waiting — {elapsed:.1f}s / {TRANSITION_TIMEOUT_S}s")

    # ── Grid quote cycle ──────────────────────────────────────────────────────

    def run_grid_cycle(market: str, mid: float, regime, indicators, ofi):
        """Place/maintain grid orders in range mode."""
        gm = grid_managers[market]
        weights = signal_pipeline.evaluate(market, regime, indicators, ofi)

        pos     = client.get_position(market)
        size    = pos.get("size", 0.0)
        pos_usd = pos.get("value_usd", 0.0)
        min_notional = mgr._min_notional.get(market, 10.0)

        conflict_block = weights.is_neutral and any("CONFLICT" in r for r in weights.reasons)
        rsi_block      = weights.is_neutral and not conflict_block

        # RSI extreme blocks both sides — skip grid entirely
        if rsi_block:
            log.debug(f"{market}: grid skipped — RSI extreme blocks both sides")
            return

        # Signal conflict + open position — place passive close, wait for flat
        if conflict_block and abs(pos_usd) >= min_notional:
            log.warning(f"{market}: grid conflict-block with open position ${pos_usd:.1f} — placing passive close")
            tick = mgr._tick.get(market, 1.0)
            close_spread = cfg.bracket_close_spreads.get(market, cfg.spreads.get(market, cfg.base_spread))
            if size > 0:
                close_price = round_to_tick(mid * (1 + close_spread), tick)
                close_side  = "ask"
            else:
                close_price = round_to_tick(mid * (1 - close_spread), tick)
                close_side  = "bid"
            mgr.cancel_all()
            mgr._place_limit(
                symbol      = market,
                side        = close_side,
                price       = close_price,
                reduce_only = True,
                pos_size    = abs(size),
            )
            return

        # Signal conflict but no position — restore sides, continue grid normally
        if conflict_block:
            weights.allow_long  = True
            weights.allow_short = True

        # Rebalance if needed
        if gm.needs_rebalance(mid):
            atr = indicators.atr if indicators else mid * 0.001
            mgr.cancel_all()
            time.sleep(1.5)   # let cancel confirms arrive before placing new grid
            gm.clear()
            grid_state = gm.generate(mid, atr, weights)
            log.info(f"{market}: grid rebalanced at mid={mid:.1f}")

            # Place all levels
            for level in grid_state.levels:
                # Skip blocked sides
                if level.side == "bid" and not weights.allow_long:
                    continue
                if level.side == "ask" and not weights.allow_short:
                    continue

                tick     = mgr._tick.get(market, 1.0)
                lot      = mgr._lot.get(market, 0.001)
                price    = round_to_tick(level.price, tick)
                size     = round(level.size - (level.size % lot), 8)

                live = mgr._place_limit(
                    symbol        = market,
                    side          = level.side,
                    price         = price,
                    reduce_only   = False,
                    override_size = level.size,
                )
                if live:
                    gm.assign_cloid(level.side, level.level_idx, live.cloid, live.placed_at)

    # ── Main quote cycle ──────────────────────────────────────────────────────

    def run_quote_cycle(triggered_market: str = None):
        if shutdown:
            return
        markets_to_quote = [triggered_market] if triggered_market else cfg.markets

        for m in markets_to_quote:

            # ── Drawdown halt check ──────────────────────────────────────
            if drawdown_monitor.is_halted:
                log.warning(f"{m}: DRAWDOWN HALT active — skipping. Call drawdown_monitor.resume() to continue.")
                continue

            binance  = feed.get_price(m)
            bbo      = client.get_bbo(m)
            bbo_mid  = bbo["mid"] if bbo else None
            pos      = client.get_position(m)
            pos_usd  = pos["value_usd"] * (1 if pos["size"] >= 0 else -1)

            # ── Drawdown stress scaling ───────────────────────────────────
            # Graduated size reduction based on how deep into daily loss budget
            _stress_mult = drawdown_monitor.size_multiplier
            max_inv  = cfg.max_inventories[m] * _stress_mult
            entry    = pos.get("entry_price", 0)
            size     = pos.get("size", 0)
            mid      = bbo_mid or binance

            regime     = regime_detector.get_regime(m)
            indicators = indicator_engine.get(m)
            ofi        = ofi_sidecar.get_signal(m)

            # ── Execution feedback — update ATR and push dynamic floors ───
            if indicators is not None:
                markout_tracker.update_atr(m, indicators.atr_pct)
                for is_long in (True, False):
                    floor = markout_tracker.get_floor(m, is_long)
                    quoter.set_adverse_floor(m, is_long, floor)

            # ── Mode selection (regime-driven) ───────────────────────────
            if regime is not None:
                if regime.is_ranging:
                    switch_mode(m, "grid", f"ADX={regime.adx:.1f} RANGING")
                else:
                    switch_mode(m, "directional", f"ADX={regime.adx:.1f} {regime.label()}")

            mode = active_mode[m]

            # ── Transition state: manage close order, skip quoting ────────
            if mode == "transition":
                _check_transition(m)
                continue

            # ── Flat state cleanup ───────────────────────────────────────
            min_notional = mgr._min_notional.get(m, 10.0)
            if pos["value_usd"] < min_notional:
                peak_price[m]   = 0.0
                tp_activated[m] = False
                if mgr.has_bracket(m):
                    mgr.cancel_bracket(m)

            # ── Dust auto-close ──────────────────────────────────────────
            # Close positions that are above the exchange minimum notional
            # but too small to be managed meaningfully.
            # Below the exchange minimum we can't place any order — ignore and
            # let the position sit until it clears naturally (funding, rounding).
            min_notional = mgr._min_notional.get(m, 10.0)
            DUST_MIN = min_notional          # must be closeable
            DUST_MAX = min_notional * 1.5    # above this it's a real position
            if DUST_MIN <= pos["value_usd"] < DUST_MAX and size != 0 and mid:
                log.warning(f"{m}: dust position ${pos['value_usd']:.2f} — auto-closing")
                mgr._place_market_reduce(m, "sell" if size > 0 else "buy", pos["value_usd"], mid, urgent=True)
                client.refresh_positions()
                continue
            elif pos["value_usd"] < DUST_MIN and size != 0:
                # Too small to close — treat as flat, allow normal quoting
                log.debug(f"{m}: sub-minimum position ${pos['value_usd']:.2f} — treating as flat")
                pos_usd = 0.0
                size    = 0

            # ── Grid mode ─────────────────────────────────────────────────
            if mode == "grid":
                if mid and pos["value_usd"] >= min_notional:
                    g_max      = grid_max_inv(m)
                    upnl       = pos.get("unrealized_pnl", 0.0)
                    sl_st      = grid_sl_states[m]
                    close_side = "sell" if size > 0 else "buy"

                    # Stale position guard
                    if client.positions_are_stale(threshold_secs=60.0):
                        log.warning(f"{m}: position data stale — forcing refresh before grid SL check")
                        client.refresh_positions()
                        pos  = client.get_position(m)
                        size = pos.get("size", 0)
                        upnl = pos.get("unrealized_pnl", 0.0)
                        if client.positions_are_stale(threshold_secs=60.0):
                            log.warning(f"{m}: position data still stale — skipping grid cycle")
                            continue

                    # Phase 3: loss-based immediate market close
                    loss_pct = upnl / g_max if g_max > 0 else 0.0
                    if loss_pct < -_grid_max_loss_pct:
                        log.warning(
                            f"{m}: grid loss {loss_pct*100:.2f}% exceeds limit "
                            f"{_grid_max_loss_pct*100:.1f}% — market closing"
                        )
                        mgr.cancel_all()
                        grid_managers[m].clear()
                        grid_sl_states[m] = GridSLState()
                        mgr._place_market_reduce(m, close_side, abs(pos_usd), mid, urgent=True)
                        client.refresh_positions()
                        continue

                    # Phase 1 / 2: overshoot passive -> escalate
                    overshot = abs(pos_usd) > g_max * _grid_overshoot_mult

                    if sl_st.active:
                        pos_flat = pos["value_usd"] < min_notional
                        if pos_flat:
                            log.info(f"{m}: grid SL — position flat, resuming grid")
                            grid_sl_states[m] = GridSLState()
                        elif not overshot:
                            log.info(f"{m}: grid SL — overshoot cleared, resuming grid")
                            grid_sl_states[m] = GridSLState()
                        elif time.time() - sl_st.placed_at > _grid_sl_ttl_s:
                            log.warning(
                                f"{m}: grid SL TTL expired (replace #{sl_st.replace_count}) "
                                f"overshoot still active — market closing"
                            )
                            mgr.cancel_all()
                            grid_managers[m].clear()
                            grid_sl_states[m] = GridSLState()
                            mgr._place_market_reduce(m, close_side, abs(pos_usd), mid, urgent=True)
                            client.refresh_positions()
                        else:
                            log.debug(f"{m}: grid SL passive active, waiting ({time.time()-sl_st.placed_at:.0f}s/{_grid_sl_ttl_s:.0f}s)")
                        continue

                    if overshot:
                        log.warning(
                            f"{m}: grid inventory overshoot ${abs(pos_usd):.2f} > "
                            f"${g_max * _grid_overshoot_mult:.2f} — placing passive SL"
                        )
                        mgr.cancel_all()
                        grid_managers[m].clear()
                        cloid = mgr._place_passive_reduce(m, close_side, abs(pos_usd), mid)
                        grid_sl_states[m] = GridSLState(
                            active        = True,
                            placed_at     = time.time(),
                            cloid         = cloid or "",
                            close_side    = close_side,
                            replace_count = 0,
                        )
                        continue

                    # No SL condition — never run directional TP/SL in grid mode
                    # fall through to grid rebalance

                # Grid cycle — place / rebalance grid orders (with or without position)
                if mid:
                    run_grid_cycle(m, mid, regime, indicators, ofi)
                continue

            # ── Directional mode: position management ────────────────────
            if entry and size and mid and pos["value_usd"] >= min_notional:
                margin   = pos["value_usd"] / LEVERAGE
                pos_side = "long" if size > 0 else "short"

                # Place bracket on first sight of an open position
                if not mgr.has_bracket(m):
                    # stop_dist as price fraction: stop_loss_margin / leverage
                    stop_dist    = cfg.stop_loss_margins[m] / LEVERAGE
                    close_spread = cfg.bracket_close_spreads.get(m, cfg.spreads.get(m, cfg.base_spread))
                    mgr.place_bracket(
                        symbol        = m,
                        entry_price   = entry,
                        position_side = pos_side,
                        pos_size      = abs(size),
                        close_spread  = close_spread,
                        stop_dist     = stop_dist,
                    )

                # Periodic bracket gap / staleness check
                now = time.time()
                if now - last_bracket_check[m] >= BRACKET_CHECK_SECS:
                    last_bracket_check[m] = now
                    mgr.check_bracket(m, mid)

                # Trailing close — trail the close order once TP activates
                upnl               = (mid - entry) * size
                take_profit_margin = cfg.take_profit_margins[m]
                take_profit        = take_profit_margin * margin

                # Tier 2 — regime-aware passive limit close
                # Ranging:   tight 2bps target — mean reversion expected, take it fast
                # Trending:  wider 6bps target — let winners run with the trend
                # Both fill as maker, no taker fee paid
                from bot.regime import Regime as _Regime
                if regime is not None and regime.regime == _Regime.RANGING:
                    TIER2_BPS = 0.000200  # 2bps in ranging — quick mean reversion close
                else:
                    TIER2_BPS = 0.000600  # 6bps in trending — let the trade breathe

                tier2_price = entry * (1 + TIER2_BPS) if size > 0 else entry * (1 - TIER2_BPS)
                tier2_price = round_to_tick(tier2_price, mgr._tick.get(m, 1.0))
                with mgr._lock:
                    b = mgr._bracket.get(m)
                bracket_close = b.close_price if b else 0.0
                tick = mgr._tick.get(m, 1.0)
                if b and abs(tier2_price - bracket_close) >= tick * 2:
                    pos_side  = "long" if size > 0 else "short"
                    stop_dist = cfg.stop_loss_margins[m] / LEVERAGE
                    mgr.cancel_bracket(m)
                    mgr.place_bracket(
                        symbol        = m,
                        entry_price   = entry,
                        position_side = pos_side,
                        pos_size      = abs(size),
                        close_spread  = TIER2_BPS,
                        stop_dist     = stop_dist,
                    )
                    regime_label = regime.label() if regime else "unknown"
                    log.info(
                        f"{m}: TIER-2 PASSIVE LIMIT @ {tier2_price:.1f} "
                        f"({TIER2_BPS*10000:.1f}bps — {regime_label})"
                    )

                # Legacy trailing TP — fires only if bracket close doesn't fill
                if upnl >= take_profit:
                    if not tp_activated[m]:
                        tp_activated[m] = True
                        log.info(f"{m}: TAKE PROFIT activated — trailing close order")
                    if size > 0:
                        peak_price[m] = max(peak_price[m], mid)
                    else:
                        peak_price[m] = min(peak_price[m], mid) if peak_price[m] > 0 else mid

                    if peak_price[m] > 0:
                        move_to_peak  = abs(peak_price[m] - entry)
                        trail_retrace = move_to_peak * TRAIL_RETRACE_PCT
                        if size > 0:
                            new_close = peak_price[m] - trail_retrace
                            trail_hit = mid <= new_close
                        else:
                            new_close = peak_price[m] + trail_retrace
                            trail_hit = mid >= new_close

                        if trail_hit:
                            log.info(
                                f"{m}: TRAILING CLOSE triggered — "
                                f"price={mid:.2f} peak={peak_price[m]:.2f} "
                                f"trail={new_close:.2f} upnl=${upnl:.3f}"
                            )
                            stop_direction = "long" if size > 0 else "short"
                            signal_pipeline.record_stop(m, stop_direction, regime)
                            peak_price[m]   = 0.0
                            tp_activated[m] = False
                            mgr.cancel_bracket(m)
                            mgr.cancel_all()
                            mgr._place_market_reduce(
                                m, "sell" if size > 0 else "buy",
                                abs(pos_usd), mid, urgent=True
                            )
                            client.refresh_positions()
                            return
                        else:
                            # Re-trail — only move bracket when peak has shifted
                            # meaningfully (> 2 ticks) to avoid cancel/replace every tick
                            tick = mgr._tick.get(m, 1.0)
                            b    = mgr._bracket.get(m) if hasattr(mgr, "_bracket") else None
                            with mgr._lock:
                                b = mgr._bracket.get(m)
                            bracket_close = b.close_price if b else 0.0
                            if size > 0:
                                new_target = round(peak_price[m] * (1 + cfg.bracket_close_spreads.get(m, cfg.spreads.get(m, cfg.base_spread))), 0)
                            else:
                                new_target = round(peak_price[m] * (1 - cfg.bracket_close_spreads.get(m, cfg.spreads.get(m, cfg.base_spread))), 0)
                            if abs(new_target - bracket_close) >= tick * 2:
                                close_spread = cfg.bracket_close_spreads.get(m, cfg.spreads.get(m, cfg.base_spread))
                                mgr.cancel_bracket(m)
                                mgr.place_bracket(
                                    symbol        = m,
                                    entry_price   = peak_price[m],
                                    position_side = pos_side,
                                    pos_size      = abs(size),
                                    close_spread  = close_spread,
                                    stop_dist     = cfg.stop_loss_margins[m] / LEVERAGE,
                                )

            # ── Directional mode: quoting ─────────────────────────────────
            if mode == "directional":
                weights = signal_pipeline.evaluate(m, regime, indicators, ofi)

                q = quoter.quote(
                    m, binance, bbo_mid, pos_usd, max_inv,
                    spread       = cfg.spreads.get(m),
                    close_spread = cfg.close_spreads.get(m),
                    regime       = regime,
                    ofi          = ofi,
                    signal_weights = weights,
                )
                # Apply stress scaling to order size after quote generated
                if q and _stress_mult < 1.0:
                    log.info(f"{m}: stress={drawdown_monitor.stress_level} size_mult={_stress_mult:.2f} — reduced quoting")
                if q:
                    log.info(
                        f"{m} [{mode}]: fair=${q.fair:.4f} "
                        f"bid=${q.bid:.4f} ask=${q.ask:.4f} "
                        f"skew={q.skew_bps:+.2f}bps inv=${pos_usd:.1f} "
                        f"weights=L{weights.long_weight:.2f}/S{weights.short_weight:.2f}"
                    )
                    mgr.update(m, q)

    _state["ready"] = True
    # Register reconnect hook — kicks quote cycle after WS reconnect or feed stale
    def _on_reconnect():
        log.info("Reconnect hook fired — triggering quote cycle for all markets")
        for m in cfg.markets:
            try:
                run_quote_cycle(triggered_market=m)
            except Exception as e:
                log.warning(f"Reconnect hook quote cycle error {m}: {e}")
    client.set_reconnect_hook(_on_reconnect)

    log.info("Starting quote loop (event-driven via BBO callback)...")
    cycle = 0
    while not shutdown:
        any_position = any(client.get_position(m)["size"] != 0 for m in cfg.markets)
        if any_position and cycle % 5 == 0:
            client.refresh_positions()
        elif not any_position and cycle % 30 == 0:
            client.refresh_positions()
        cycle += 1
        time.sleep(1)


if __name__ == "__main__":
    main()
