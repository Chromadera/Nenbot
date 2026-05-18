"""Binance WebSocket price feed."""
import json
import threading
import websocket
from typing import Dict, Optional
from bot.logger import get_logger

log = get_logger("pricing")

SYMBOL_MAP = {
    "BTC-PERP":  "btcusdt",
    "ETH-PERP":  "ethusdt",
    "SOL-PERP":  "solusdt",
}

class BinanceFeed:
    """Streams mid prices from Binance for supported symbols."""

    def __init__(self, markets: list[str]):
        self._prices: Dict[str, Optional[float]] = {}
        self._lock = threading.Lock()

        # Only subscribe to markets we have a Binance mapping for
        self._supported = [m for m in markets if m in SYMBOL_MAP]
        unsupported = [m for m in markets if m not in SYMBOL_MAP]
        if unsupported:
            log.info(f"No Binance feed for: {unsupported} — will use BBO only")

        if not self._supported:
            log.warning("No supported markets for Binance feed")
            return

        streams = [f"{SYMBOL_MAP[m]}@bookTicker" for m in self._supported]
        url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

        self._ws = websocket.WebSocketApp(
            url,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_open=self._on_open,
        )
        t = threading.Thread(target=self._ws.run_forever, kwargs={"reconnect": 5}, daemon=True)
        t.start()

    def _on_open(self, ws):
        log.info(f"Binance WS connected — tracking {self._supported}")

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
            data = msg.get("data", msg)
            symbol = data.get("s", "").lower()
            bid = float(data["b"])
            ask = float(data["a"])
            mid = (bid + ask) / 2
            for market, bs in SYMBOL_MAP.items():
                if bs == symbol:
                    with self._lock:
                        self._prices[market] = mid
                    break
        except Exception as e:
            log.warning(f"Feed parse error: {e}")

    def _on_error(self, ws, error):
        log.warning(f"Binance WS error: {error}")

    def _on_close(self, ws, code, msg):
        log.warning(f"Binance WS closed: {code} {msg}")

    def get_price(self, market: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(market)

    def ready_for(self, market: str) -> bool:
        """True if we have a price for this market (or it's not supported — use BBO)."""
        if market not in SYMBOL_MAP:
            return True  # BBO-only markets are always "ready"
        with self._lock:
            return self._prices.get(market) is not None

    def all_ready(self, markets: list[str]) -> bool:
        return all(self.ready_for(m) for m in markets)
