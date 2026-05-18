"""
feed.py — Shared market data feed for NenMMBot SaaS.

One process, one WebSocket connection per market to Hotstuff.
Broadcasts BBO updates to all user bots via Unix domain sockets.

Socket per market: /tmp/nenfeed_{MARKET}.sock
  e.g. /tmp/nenfeed_BTC-PERP.sock

Protocol (newline-delimited JSON):
  {"bid": 1.23, "ask": 1.24, "mid": 1.235, "ts": 1234567890.123}

Usage:
  python3 feed.py                          # all markets from NENFEED_MARKETS env
  python3 feed.py BTC-PERP ETH-PERP       # specific markets
"""

import os
import sys
import json
import time
import signal
import socket
import threading
import logging
from typing import Dict, Set, Optional

from dotenv import load_dotenv
load_dotenv("/root/saas/.env")

from hotstuff import InfoClient, SubscriptionClient
from hotstuff.methods.info.market import InstrumentsParams
from hotstuff.methods.subscription.channels import BBOSubscriptionParams
from hotstuff.transports.websocket import WebSocketTransport
from hotstuff.types.transports import WebSocketTransportOptions

# ── Config ────────────────────────────────────────────────────────────────────
SOCKET_DIR        = "/tmp"
SOCKET_PREFIX     = "nenfeed_"
TESTNET           = os.environ.get("HOTSTUFF_TESTNET", "false").lower() == "true"
RECONNECT_DELAY   = 5       # seconds between reconnect attempts
BBO_STALE_SECS    = 30      # seconds before BBO considered stale
WATCHDOG_INTERVAL = 10      # seconds between watchdog checks
MAX_CLIENTS       = 200     # max concurrent client connections per market
DEFAULT_MARKETS   = os.environ.get(
    "NENFEED_MARKETS",
    "BTC-PERP,ETH-PERP,SOL-PERP,HYPE-PERP"
).split(",")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [feed] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("feed")


def socket_path(market: str) -> str:
    safe = market.replace("-", "_")
    return os.path.join(SOCKET_DIR, f"{SOCKET_PREFIX}{safe}.sock")


# ── Per-market broadcaster ─────────────────────────────────────────────────────
class MarketFeed:
    """
    Manages one Hotstuff BBO subscription and broadcasts to connected Unix
    socket clients for a single market.
    """

    def __init__(self, market: str, testnet: bool = False):
        self.market   = market
        self.testnet  = testnet
        self._lock    = threading.Lock()
        self._clients: Set[socket.socket] = set()
        self._latest: Optional[dict] = None
        self._latest_ts: float = 0.0
        self._shutdown = False

        self._sub: Optional[SubscriptionClient] = None
        self._reconnect_flag: bool = False
        self._server: Optional[socket.socket] = None
        self._server_thread: Optional[threading.Thread] = None
        self._sub_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None

    # ── Unix socket server ────────────────────────────────────────────────────

    def _start_server(self):
        path = socket_path(self.market)
        if os.path.exists(path):
            os.unlink(path)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(MAX_CLIENTS)
        self._server.settimeout(1.0)
        os.chmod(path, 0o666)
        log.info(f"{self.market}: Unix socket listening at {path}")

    def _accept_loop(self):
        while not self._shutdown:
            try:
                conn, _ = self._server.accept()
                conn.settimeout(5.0)
                with self._lock:
                    self._clients.add(conn)
                log.debug(f"{self.market}: client connected ({len(self._clients)} total)")
                # Send latest BBO immediately on connect
                if self._latest:
                    self._send_to(conn, self._latest)
            except socket.timeout:
                continue
            except Exception as e:
                if not self._shutdown:
                    log.warning(f"{self.market}: accept error: {e}")

    def _send_to(self, conn: socket.socket, data: dict):
        try:
            msg = json.dumps(data) + "\n"
            conn.sendall(msg.encode())
        except Exception:
            with self._lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _broadcast(self, data: dict):
        with self._lock:
            clients = list(self._clients)
        dead = []
        for conn in clients:
            try:
                msg = json.dumps(data) + "\n"
                conn.sendall(msg.encode())
            except Exception:
                dead.append(conn)
        if dead:
            with self._lock:
                for conn in dead:
                    self._clients.discard(conn)
                    try:
                        conn.close()
                    except Exception:
                        pass
            log.debug(f"{self.market}: removed {len(dead)} dead client(s)")

    # ── Hotstuff subscription ─────────────────────────────────────────────────

    def _build_sub(self) -> SubscriptionClient:
        ws = WebSocketTransport(WebSocketTransportOptions(is_testnet=self.testnet))
        return SubscriptionClient(transport=ws)

    def _on_bbo(self, data):
        try:
            d = data.data if hasattr(data, "data") else data
            bid = float(d.get("best_bid_price") or d.get("bid") or d.get("b") or 0)
            ask = float(d.get("best_ask_price") or d.get("ask") or d.get("a") or 0)
            if bid <= 0 or ask <= 0:
                return
            payload = {
                "market": self.market,
                "bid": bid,
                "ask": ask,
                "mid": round((bid + ask) / 2, 8),
                "ts": time.time(),
            }
            self._latest    = payload
            self._latest_ts = payload["ts"]
            self._broadcast(payload)
        except Exception as e:
            log.warning(f"{self.market}: BBO parse error: {e}")

    def _subscribe(self):
        self._reconnect_flag = False
        while not self._shutdown:
            self._reconnect_flag = False
            try:
                log.info(f"{self.market}: connecting to Hotstuff WS...")
                self._sub = self._build_sub()
                self._sub.bbo(
                    BBOSubscriptionParams(symbol=self.market),
                    lambda data: self._on_bbo(data),
                )
                log.info(f"{self.market}: WS connected and subscribed")
                # Block here — break out if reconnect flagged or shutdown
                while not self._shutdown and not self._reconnect_flag:
                    time.sleep(1)
                if self._reconnect_flag and not self._shutdown:
                    log.warning(f"{self.market}: reconnect flag set -- rebuilding WS")
                    try:
                        self._sub.transport.disconnect()
                    except Exception:
                        pass
                    time.sleep(RECONNECT_DELAY)
            except Exception as e:
                if not self._shutdown:
                    log.warning(f"{self.market}: WS error: {e} — reconnecting in {RECONNECT_DELAY}s")
                    time.sleep(RECONNECT_DELAY)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def _watchdog(self):
        while not self._shutdown:
            time.sleep(WATCHDOG_INTERVAL)
            age = time.time() - self._latest_ts
            if self._latest_ts > 0 and age > BBO_STALE_SECS:
                log.warning(f"{self.market}: BBO stale ({age:.0f}s) — setting reconnect flag")
                self._reconnect_flag = True
            with self._lock:
                n = len(self._clients)
            log.info(f"{self.market}: {n} client(s) connected | last BBO {age:.1f}s ago")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        self._start_server()

        self._server_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name=f"accept-{self.market}")
        self._server_thread.start()

        self._sub_thread = threading.Thread(
            target=self._subscribe, daemon=True, name=f"sub-{self.market}")
        self._sub_thread.start()

        self._watchdog_thread = threading.Thread(
            target=self._watchdog, daemon=True, name=f"watchdog-{self.market}")
        self._watchdog_thread.start()

        log.info(f"{self.market}: feed started")

    def stop(self):
        self._shutdown = True
        try:
            if self._sub:
                self._sub.transport.disconnect()
        except Exception:
            pass
        try:
            if self._server:
                self._server.close()
        except Exception:
            pass
        path = socket_path(self.market)
        if os.path.exists(path):
            os.unlink(path)
        with self._lock:
            for conn in self._clients:
                try:
                    conn.close()
                except Exception:
                    pass
            self._clients.clear()
        log.info(f"{self.market}: feed stopped")

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def last_bbo(self) -> Optional[dict]:
        return self._latest

    @property
    def bbo_age(self) -> float:
        return time.time() - self._latest_ts if self._latest_ts > 0 else float("inf")


# ── Main feed process ─────────────────────────────────────────────────────────
class FeedServer:
    def __init__(self, markets: list):
        self.markets: Dict[str, MarketFeed] = {}
        self._shutdown = False
        for m in markets:
            m = m.strip()
            if m:
                self.markets[m] = MarketFeed(m, testnet=TESTNET)

    def start(self):
        # Verify markets exist on exchange
        try:
            info = InfoClient(is_testnet=TESTNET)
            resp = info.instruments(InstrumentsParams(type="perps"))
            known = set()
            for inst in resp.perps:
                name = inst["name"] if isinstance(inst, dict) else inst.name
                known.add(name)
            for m in list(self.markets.keys()):
                if m not in known:
                    log.error(f"Unknown market: {m} — removing from feed")
                    del self.markets[m]
        except Exception as e:
            log.warning(f"Could not verify instruments: {e} — proceeding anyway")

        for feed in self.markets.values():
            feed.start()

        log.info(f"Feed server started — markets: {list(self.markets.keys())}")

    def stop(self):
        self._shutdown = True
        for feed in self.markets.values():
            feed.stop()
        log.info("Feed server stopped")

    def status(self):
        lines = ["── Feed Status ──────────────────────"]
        for m, feed in self.markets.items():
            bbo = feed.last_bbo
            lines.append(
                f"  {m}: {feed.client_count} clients | "
                f"age={feed.bbo_age:.1f}s | "
                + (f"bid={bbo['bid']} ask={bbo['ask']}" if bbo else "no data yet")
            )
        return "\n".join(lines)

    def run_forever(self):
        self.start()
        try:
            while not self._shutdown:
                time.sleep(30)
                log.info("\n" + self.status())
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    markets = sys.argv[1:] if len(sys.argv) > 1 else DEFAULT_MARKETS
    markets = [m.strip() for m in markets if m.strip()]

    if not markets:
        log.error("No markets specified. Set NENFEED_MARKETS in .env or pass as args.")
        sys.exit(1)

    log.info(f"Starting NenBot shared feed — markets: {markets}")
    server = FeedServer(markets)

    def _shutdown(sig, frame):
        log.info("Shutdown signal received")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.run_forever()


if __name__ == "__main__":
    main()
