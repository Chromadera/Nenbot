"""Hotstuff exchange connectivity — instruments, BBO, account streams.

BBO data is sourced from the shared NenBot feed (nenfeed.service) via Unix
socket when available. Falls back to a direct Hotstuff WS subscription
automatically if the feed socket is missing or unresponsive.

Socket path: /tmp/nenfeed_{MARKET}.sock  (e.g. /tmp/nenfeed_BTC_PERP.sock)
"""
import os
import json
import socket
import threading
import time
from typing import Dict, Optional, Callable
from eth_account import Account

from hotstuff import InfoClient, ExchangeClient, SubscriptionClient
from hotstuff.methods.info.market import InstrumentsParams
from hotstuff.methods.info.account import PositionsParams, AccountSummaryParams
from hotstuff.methods.exchange.trading import CancelAllParams
from hotstuff.methods.subscription.channels import (
    BBOSubscriptionParams,
    FillsSubscriptionParams,
)
from hotstuff.transports.websocket import WebSocketTransport
from hotstuff.types.transports import WebSocketTransportOptions

from bot.logger import get_logger

log = get_logger("exchange")

# ── Feed socket config ────────────────────────────────────────────────────────
FEED_SOCKET_DIR     = "/tmp"
FEED_SOCKET_PREFIX  = "nenfeed_"
FEED_CONNECT_TIMEOUT = 2.0   # seconds to wait for socket connection
FEED_READ_TIMEOUT   = 5.0    # seconds before considering feed dead
FEED_RETRY_INTERVAL = 30     # seconds before retrying feed after failure

# ── Direct WS config ──────────────────────────────────────────────────────────
BBO_STALE_THRESHOLD = 30
WATCHDOG_INTERVAL   = 10


def _feed_socket_path(market: str) -> str:
    safe = market.replace("-", "_")
    return os.path.join(FEED_SOCKET_DIR, f"{FEED_SOCKET_PREFIX}{safe}.sock")


def _feed_available(market: str) -> bool:
    """Check if the feed socket exists and is connectable."""
    path = _feed_socket_path(market)
    if not os.path.exists(path):
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(FEED_CONNECT_TIMEOUT)
        sock.connect(path)
        sock.close()
        return True
    except Exception:
        return False


# ── Per-market feed reader ────────────────────────────────────────────────────
class FeedReader:
    """
    Connects to the shared feed Unix socket for one market and calls
    bbo_callback(symbol, bbo_dict) on each update.
    Falls back gracefully if the socket disappears.
    """

    def __init__(self, market: str, bbo_callback: Callable):
        self.market       = market
        self._cb          = bbo_callback
        self._running     = False
        self._connected   = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(
            target=self._run, daemon=True, name=f"feed-{self.market}")
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return self._connected

    def _run(self):
        path = _feed_socket_path(self.market)
        while self._running:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.settimeout(FEED_READ_TIMEOUT)
                self._sock.connect(path)
                self._connected = True
                log.info(f"{self.market}: connected to shared feed at {path}")

                buf = ""
                while self._running:
                    try:
                        chunk = self._sock.recv(4096).decode()
                        if not chunk:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                data = json.loads(line)
                                bbo  = {
                                    "bid": float(data["bid"]),
                                    "ask": float(data["ask"]),
                                    "mid": float(data["mid"]),
                                }
                                self._cb(self.market, bbo)
                            except Exception as e:
                                log.warning(f"{self.market}: feed parse error: {e}")
                    except socket.timeout:
                        log.warning(f"{self.market}: feed read timeout — reconnecting")
                        break

            except Exception as e:
                if self._running:
                    log.warning(f"{self.market}: feed socket error: {e} — retry in {FEED_RETRY_INTERVAL}s")
            finally:
                self._connected = False
                try:
                    if self._sock:
                        self._sock.close()
                except Exception:
                    pass

            if self._running:
                time.sleep(FEED_RETRY_INTERVAL)


# ── Main client ───────────────────────────────────────────────────────────────
class HotstuffClient:
    def __init__(self, agent_key: str, main_address: str, testnet: bool = False):
        self._lock      = threading.Lock()
        self._bbo:      Dict[str, dict]  = {}
        self._bbo_ts:   Dict[str, float] = {}
        self._positions: Dict[str, dict] = {}
        self._fills_cb: Optional[Callable] = None
        self._bbo_cb:   Optional[Callable] = None
        self._markets:  list = []

        self._wallet   = Account.from_key(agent_key)
        self._testnet  = testnet
        log.info(f"Agent: {self._wallet.address}")

        self.info     = InfoClient(is_testnet=testnet)
        self.exchange = ExchangeClient(wallet=self._wallet, is_testnet=testnet)
        self._main_address  = main_address
        self._instruments: Dict[str, dict] = {}

        # WS subscription (used only as fallback)
        self.sub: Optional[SubscriptionClient] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._shutdown        = False
        self._fills_subscribed = False

        # Feed readers per market
        self._feed_readers: Dict[str, FeedReader] = {}
        self._using_feed:   Dict[str, bool]       = {}

    # ── Instruments ──────────────────────────────────────────────────────────

    def load_instruments(self, markets: list):
        self._markets = markets
        resp = self.info.instruments(InstrumentsParams(type="perps"))
        for inst in resp.perps:
            name = inst["name"] if isinstance(inst, dict) else inst.name
            self._instruments[name] = inst
        for m in markets:
            if m not in self._instruments:
                raise ValueError(f"Unknown market: {m}")
            inst = self._instruments[m]
            id_  = inst["id"] if isinstance(inst, dict) else inst.id
            log.info(f"  {m}: id={id_}")

    def get_instrument(self, symbol: str) -> dict:
        return self._instruments[symbol]

    def get_instrument_id(self, symbol: str) -> int:
        inst = self._instruments[symbol]
        return inst["id"] if isinstance(inst, dict) else inst.id

    # ── BBO callback (shared by feed and direct WS) ───────────────────────────

    def _on_bbo(self, symbol: str, data):
        """Accept BBO from either feed reader or direct WS."""
        try:
            if isinstance(data, dict) and "bid" in data:
                # From feed reader — already parsed
                bid = float(data["bid"])
                ask = float(data["ask"])
            else:
                # From direct WS — raw SDK data
                d   = data.data if hasattr(data, "data") else data
                bid = float(d.get("best_bid_price") or d.get("bid") or d.get("b") or 0)
                ask = float(d.get("best_ask_price") or d.get("ask") or d.get("a") or 0)

            if bid > 0 and ask > 0:
                bbo = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
                with self._lock:
                    self._bbo[symbol]    = bbo
                    self._bbo_ts[symbol] = time.time()
                if self._bbo_cb:
                    try:
                        self._bbo_cb(symbol, bbo)
                    except Exception as e:
                        log.warning(f"BBO callback error {symbol}: {e}")
                else:
                    log.warning(f"_on_bbo: _bbo_cb is None for {symbol}")
        except Exception as e:
            log.warning(f"BBO parse error {symbol}: {e}")

    # ── Subscription setup ────────────────────────────────────────────────────

    def subscribe_bbo(self, markets: list, bbo_callback: Optional[Callable] = None):
        self._markets = markets
        self._bbo_cb  = bbo_callback

        feed_markets   = []
        direct_markets = []

        for m in markets:
            if _feed_available(m):
                feed_markets.append(m)
                self._using_feed[m] = True
            else:
                direct_markets.append(m)
                self._using_feed[m] = False

        # Start feed readers
        for m in feed_markets:
            reader = FeedReader(m, self._on_bbo)
            reader.start()
            self._feed_readers[m] = reader
            log.info(f"{m}: using shared feed ✓")

        # Direct WS for remaining markets
        if direct_markets:
            log.info(f"Direct WS for: {direct_markets} (feed not available)")
            self.sub = self._build_sub_client()
            for m in direct_markets:
                self.sub.bbo(
                    BBOSubscriptionParams(symbol=m),
                    lambda data, mk=m: self._on_bbo(mk, data),
                )

        if not direct_markets:
            log.info("All markets on shared feed — no direct WS needed")

    def subscribe_account(self, on_fill: Optional[Callable] = None):
        """Fills subscription is always direct — account-specific."""
        self._fills_cb = on_fill
        if self.sub is None:
            self.sub = self._build_sub_client()
        self.sub.fills(
            FillsSubscriptionParams(user=self._main_address),
            self._on_fill,
        )
        self._fills_subscribed = True
        log.info(f"Account fills subscribed: {self._main_address}")

    def _build_sub_client(self) -> SubscriptionClient:
        ws = WebSocketTransport(WebSocketTransportOptions(is_testnet=self._testnet))
        return SubscriptionClient(transport=ws)

    def set_reconnect_hook(self, fn):
        """Register a callback fired after every reconnect — used to kick quote cycle."""
        self._reconnect_hook = fn

    def _resubscribe(self):
        """Rebuild direct WS subscriptions for markets not on feed."""
        direct_markets = [m for m in self._markets if not self._using_feed.get(m)]
        if not direct_markets and not self._fills_subscribed:
            return
        log.warning("Reconnecting direct WS subscriptions...")
        try:
            old_sub  = self.sub
            self.sub = self._build_sub_client()
            for m in direct_markets:
                self.sub.bbo(
                    BBOSubscriptionParams(symbol=m),
                    lambda data, mk=m: self._on_bbo(mk, data),
                )
            if self._fills_subscribed:
                self.sub.fills(
                    FillsSubscriptionParams(user=self._main_address),
                    self._on_fill,
                )
            log.info("Direct WS resubscribed.")
            try:
                if old_sub:
                    old_sub.transport.disconnect()
            except Exception:
                pass
            # Fire reconnect hook to kick quote cycle back
            hook = getattr(self, "_reconnect_hook", None)
            if hook:
                try:
                    hook()
                except Exception as he:
                    log.warning(f"Reconnect hook error: {he}")
        except Exception as e:
            log.warning(f"Resubscribe failed: {e}")

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def start_watchdog(self):
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        log.info("Watchdog started.")

    def _watchdog_loop(self):
        while not self._shutdown:
            time.sleep(WATCHDOG_INTERVAL)
            now   = time.time()
            stale = []
            with self._lock:
                for m in self._markets:
                    last = self._bbo_ts.get(m, 0)
                    if now - last > BBO_STALE_THRESHOLD:
                        stale.append(m)
            if stale:
                # Only reconnect direct-WS markets — feed handles its own reconnect
                direct_stale = [m for m in stale if not self._using_feed.get(m)]
                if direct_stale:
                    log.warning(f"Stale BBO (direct WS): {direct_stale} — resubscribing")
                    self._resubscribe()
                feed_stale = [m for m in stale if self._using_feed.get(m)]
                if feed_stale:
                    log.warning(f"Stale BBO (feed): {feed_stale} — feed may be down, check nenfeed.service")
                    # Fire reconnect hook to kick quote cycle back after feed stale
                    hook = getattr(self, "_reconnect_hook", None)
                    if hook:
                        try:
                            hook()
                        except Exception as he:
                            log.warning(f"Reconnect hook error: {he}")
            # Fills WS health check - independent of BBO staleness
            if self._fills_subscribed and self.sub and not self.sub.transport.is_connected():
                log.warning("Fills WS disconnected - resubscribing")
                self._resubscribe()


    def stop_watchdog(self):
        self._shutdown = True

    # ── BBO getters ───────────────────────────────────────────────────────────

    def get_bbo(self, symbol: str) -> Optional[dict]:
        with self._lock:
            return self._bbo.get(symbol)

    def get_mid(self, symbol: str) -> Optional[float]:
        bbo = self.get_bbo(symbol)
        return bbo["mid"] if bbo else None

    # ── Positions ─────────────────────────────────────────────────────────────

    def refresh_positions(self):
        try:
            positions = self.info.positions(PositionsParams(user=self._main_address))
            # Pull upnl per market from account_summary — positions endpoint lacks upnl
            upnl_map = {}
            try:
                summary = self.info.account_summary(AccountSummaryParams(user=self._main_address))
                perp    = getattr(summary, "perp_positions", {}) or {}
                for sym, data in perp.items():
                    upnl_map[sym] = float(data.get("upnl", 0.0))
            except Exception as e:
                log.debug(f"account_summary upnl fetch failed: {e}")
            with self._lock:
                self._positions = {}
                self._positions_updated_at = time.time()
                for p in positions:
                    symbol = p["instrument"]
                    size   = float(p["size"])
                    price  = float(p["entry_price"])
                    upnl   = upnl_map.get(symbol, 0.0)
                    self._positions[symbol] = {
                        "size":           size,
                        "value_usd":      abs(size) * price,
                        "entry_price":    price,
                        "unrealized_pnl": upnl,
                    }
        except Exception as e:
            log.warning(f"Position refresh error: {e}")

    def positions_are_stale(self, threshold_secs: float = 60.0) -> bool:
        """Returns True if positions haven't been refreshed recently."""
        return time.time() - getattr(self, "_positions_updated_at", 0) > threshold_secs

    def get_position(self, symbol: str) -> dict:
        with self._lock:
            return self._positions.get(symbol, {"size": 0.0, "value_usd": 0.0, "entry_price": 0.0, "mark_price": 0.0, "unrealized_pnl": 0.0})

    # ── Fills ─────────────────────────────────────────────────────────────────

    def _on_fill(self, data):
        d = data.data if hasattr(data, "data") else data
        log.info(f"Fill: {d}")
        if self._fills_cb:
            self._fills_cb(d)

    # ── Startup cleanup ───────────────────────────────────────────────────────

    def cancel_all_on_startup(self):
        try:
            expires = int(time.time() * 1000) + 10_000
            self.exchange.cancel_all(CancelAllParams(expiresAfter=expires))
            log.info("Startup: cancelled all stale orders.")
        except Exception as e:
            log.warning(f"Startup cancel_all error: {e}")

    # ── Status ────────────────────────────────────────────────────────────────

    def feed_status(self) -> dict:
        """Return feed/direct WS status per market — useful for admin commands."""
        status = {}
        for m in self._markets:
            using_feed = self._using_feed.get(m, False)
            reader     = self._feed_readers.get(m)
            status[m]  = {
                "source":    "feed" if using_feed else "direct",
                "connected": reader.connected if reader else False,
                "bbo_age":   round(time.time() - self._bbo_ts.get(m, 0), 1),
                "bbo":       self._bbo.get(m),
            }
        return status
