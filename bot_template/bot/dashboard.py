"""
Hotstuff Terminal Dashboard with persistent analytics
Usage:
  python3 -m bot.dashboard                          # your own wallet
  python3 -m bot.dashboard 0xABCD...               # any wallet
  python3 -m bot.dashboard 0xABCD... 0xEFGH...     # multiple wallets
  python3 -m bot.dashboard --leaderboard            # top traders by volume
  python3 -m bot.dashboard --analytics              # your PnL analytics
  python3 -m bot.dashboard --analytics 0xABCD...   # analytics for any wallet
Press TAB to switch views when monitoring a wallet.
"""
import sys, time, os, threading
from datetime import datetime
from typing import Dict, List, Optional
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich.layout import Layout
from rich import box
from rich.align import Align
from rich.columns import Columns

from hotstuff import InfoClient
from hotstuff.methods.info.account import (
    FillsParams, AccountSummaryParams, OpenOrdersParams
)
from hotstuff.methods.info.market import TradesParams

from bot.db import (
    init_db, save_fills, save_snapshot, save_leaderboard,
    pnl_by_market, pnl_by_direction, pnl_over_time, fee_analysis, equity_history,
    latency_stats_by_market, markout_stats,
)

console = Console()
REFRESH_INTERVAL    = 5
SNAPSHOT_INTERVAL   = 1800   # 30 minutes
LEADERBOARD_MARKETS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP"]
LEADERBOARD_TRADES  = 200

# ── Time-window state ─────────────────────────────────────────────────────────
TIME_WINDOWS    = ["1hr", "12hr", "24hr", "all"]
_win_idx        = 2          # default → "24hr"
_win_lock       = threading.Lock()

def get_window() -> str:
    with _win_lock:
        return TIME_WINDOWS[_win_idx]

def set_window_idx(i: int):
    global _win_idx
    with _win_lock:
        _win_idx = i % len(TIME_WINDOWS)

def window_hours() -> Optional[int]:
    return {"1hr": 1, "12hr": 12, "24hr": 24}.get(get_window())


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_side(side: str) -> Text:
    s = "BUY" if side in ("b", "buy", "BUY") else "SELL"
    return Text(s, style="bold green" if s == "BUY" else "bold red")

def fmt_pnl(val: float) -> Text:
    return Text(f"${val:+.4f}", style="green" if val >= 0 else "red")

def fmt_pnl_r(val: float) -> str:
    return f"[green]${val:+.4f}[/green]" if val >= 0 else f"[red]${val:+.4f}[/red]"

def fmt_dir(direction: str) -> Text:
    colors = {"openLong":"green","flipToLong":"cyan","closeLong":"yellow",
              "openShort":"red","flipToShort":"magenta","closeShort":"yellow"}
    return Text(direction, style=colors.get(direction, "white"))

def fmt_ts(ts) -> str:
    if isinstance(ts, str) and "T" in ts:
        return ts[11:19]
    if isinstance(ts, (int, float)):
        v = int(ts)
        return datetime.fromtimestamp(v/1000 if v > 1e10 else v).strftime("%H:%M:%S")
    return str(ts)[:8]

def short_addr(addr: str) -> str:
    return addr[:6] + "..." + addr[-4:] if len(addr) > 12 else addr

def bar(val: float, max_val: float, width: int = 20, positive: bool = True) -> str:
    if max_val == 0:
        return " " * width
    ratio = min(abs(val) / abs(max_val), 1.0)
    filled = int(ratio * width)
    char = "█"
    color = "green" if (val >= 0 and positive) or (val < 0 and not positive) else "red"
    return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"


# ── Wallet monitor ────────────────────────────────────────────────────────────

class WalletMonitor:
    def __init__(self, address: str, label: str = "", is_own: bool = False):
        self.address  = address
        self.label    = label or short_addr(address)
        self.is_own   = is_own
        self.info     = InfoClient(is_testnet=False)
        self._lock    = threading.Lock()
        self._summary = None
        self._fills: List[dict] = []
        self._orders: List[dict] = []
        self._last_update   = 0.0
        self._last_snapshot = 0.0
        self._last_fill_save= 0.0
        self._error: Optional[str] = None

    def refresh(self):
        max_retries = 3
        for attempt in range(max_retries):
            try:
                summary = self.info.account_summary(AccountSummaryParams(user=self.address))
                fills   = self.info.fills(FillsParams(user=self.address, limit=50))
                orders  = self.info.open_orders(OpenOrdersParams(user=self.address, limit=50))

                entries = fills.entries if hasattr(fills, "entries") else []
                raw_ord = orders.orders if hasattr(orders, "orders") else (orders if isinstance(orders, list) else [])

                now = time.time()

                # Save fills to DB
                save_fills(entries)

                # Feed fills into latency tracker
                if _latency_tracker is not None:
                    _latency_tracker.ingest_fills(entries)

                # Snapshot equity every 30 min
                if now - self._last_snapshot >= SNAPSHOT_INTERVAL:
                    save_snapshot(self.address, summary)
                    self._last_snapshot = now

                with self._lock:
                    self._summary     = summary
                    self._fills       = entries
                    self._orders      = raw_ord
                    self._last_update = now
                    self._error       = None
                return  # success

            except Exception as e:
                err_str = str(e)
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s backoff
                    time.sleep(wait)
                    # Recreate InfoClient on timeout to reset connection
                    if "timeout" in err_str.lower() or "connect" in err_str.lower():
                        self.info = InfoClient(is_testnet=False)
                else:
                    with self._lock:
                        self._error = err_str

    def get(self):
        with self._lock:
            return self._summary, list(self._fills), list(self._orders), self._last_update, self._error


# ── Regime display (read from log file or bot state file) ────────────────────

def build_regime_panel() -> Panel:
    """Read regime state from bot/regime_state.json if available."""
    import json, os
    state_file = os.path.join(os.path.dirname(__file__), "regime_state.json")
    try:
        with open(state_file) as f:
            states = json.load(f)
    except Exception:
        return Panel(
            Align.center(Text("Regime data not available — bot must be running.", style="dim")),
            title="[bold cyan]Market Regime[/bold cyan]", box=box.ROUNDED
        )

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    tbl.add_column("Market",  width=10)
    tbl.add_column("Regime",  width=14)
    tbl.add_column("Conf",    width=7,  justify="right")
    tbl.add_column("Vol",     width=7,  justify="right")
    tbl.add_column("Trend",   width=8,  justify="right")
    tbl.add_column("Bid×",    width=5,  justify="right")
    tbl.add_column("Ask×",    width=5,  justify="right")

    regime_colors = {"Ranging": "cyan", "Trending Up": "green", "Trending Down": "red"}

    for market, s in states.items():
        regime  = s.get("regime", "Unknown")
        color   = regime_colors.get(regime, "white")
        conf    = s.get("confidence", 0)
        vol     = s.get("volatility", 0)
        trend   = s.get("trend", 0)
        bid_m   = s.get("bid_mult", 1.0)
        ask_m   = s.get("ask_mult", 1.0)
        tbl.add_row(
            market,
            f"[{color}]{regime}[/{color}]",
            f"{conf:.0%}",
            f"{vol:.0f}%",
            f"[{'green' if trend > 0 else 'red'}]{trend:+.1f}%[/]",
            f"[{'red' if bid_m > 1 else 'green' if bid_m < 1 else 'white'}]{bid_m}[/]",
            f"[{'red' if ask_m > 1 else 'green' if ask_m < 1 else 'white'}]{ask_m}[/]",
        )

    return Panel(tbl, title="[bold cyan]Market Regime[/bold cyan]", box=box.ROUNDED)


# ── OFI panel ────────────────────────────────────────────────────────────────

def build_ofi_panel() -> Panel:
    import json, os
    state_file = os.path.join(os.path.dirname(__file__), "ofi_state.json")
    try:
        with open(state_file) as f:
            states = json.load(f)
    except Exception:
        return Panel(
            Align.center(Text("OFI data not available — bot must be running.", style="dim")),
            title="[bold cyan]Order Flow Imbalance[/bold cyan]", box=box.ROUNDED
        )

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    tbl.add_column("Market",  width=10)
    tbl.add_column("Signal",  width=12)
    tbl.add_column("Raw",     width=7,  justify="right")
    tbl.add_column("Smooth",  width=8,  justify="right")
    tbl.add_column("Bid$",    width=9,  justify="right")
    tbl.add_column("Ask$",    width=9,  justify="right")
    tbl.add_column("B/A",     width=5,  justify="right")

    label_colors = {
        "Strong Buy": "green", "Buy": "green",
        "Strong Sell": "red",  "Sell": "red",
        "Neutral": "yellow"
    }

    for market, s in states.items():
        label  = s.get("label", "Neutral")
        color  = label_colors.get(label, "white")
        ofi    = s.get("ofi", 0)
        smooth = s.get("ofi_smooth", 0)
        bid_v  = s.get("bid_volume", 0)
        ask_v  = s.get("ask_volume", 0)
        ratio  = bid_v / ask_v if ask_v > 0 else 0
        ofi_color    = "green" if ofi > 0 else "red"
        smooth_color = "green" if smooth > 0 else "red"
        ratio_color  = "green" if ratio > 1.1 else "red" if ratio < 0.9 else "yellow"
        tbl.add_row(
            market,
            f"[{color}]{label}[/{color}]",
            f"[{ofi_color}]{ofi:+.3f}[/{ofi_color}]",
            f"[{smooth_color}]{smooth:+.3f}[/{smooth_color}]",
            f"${bid_v/1000:.1f}k",
            f"${ask_v/1000:.1f}k",
            f"[{ratio_color}]{ratio:.2f}[/{ratio_color}]",
        )

    return Panel(tbl, title="[bold cyan]Order Flow Imbalance[/bold cyan]", box=box.ROUNDED)


# ── Latency tracker ───────────────────────────────────────────────────────────

class LatencyTracker:
    """
    Measures two kinds of latency:
    - Quote latency: how long a BBO REST fetch takes (price staleness)
    - Fill latency:  time between order placement and fill (from timestamps)
    """
    MARKETS  = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP"]
    HISTORY  = 10   # rolling window

    def __init__(self):
        self.info   = InfoClient(is_testnet=False)
        self._lock  = threading.Lock()
        # quote latency per market: list of ms values
        self._quote_lat: Dict[str, List[float]] = {m: [] for m in self.MARKETS}
        # fill latency per market: list of seconds between order ts and fill ts
        self._fill_lat:  Dict[str, List[float]] = {m: [] for m in self.MARKETS}
        self._last_run  = 0.0

    def measure_quote(self):
        from hotstuff.methods.info.market import BBOParams
        for market in self.MARKETS:
            for attempt in range(2):
                try:
                    t0 = time.perf_counter()
                    self.info.bbo(BBOParams(symbol=market))
                    ms = (time.perf_counter() - t0) * 1000
                    with self._lock:
                        lst = self._quote_lat[market]
                        lst.append(ms)
                        if len(lst) > self.HISTORY:
                            lst.pop(0)
                    break
                except Exception as e:
                    if "timeout" in str(e).lower() or "connect" in str(e).lower():
                        self.info = InfoClient(is_testnet=False)
                    if attempt == 1:
                        pass  # silently skip on persistent failure

    def ingest_fills(self, fills: List[dict]):
        """Compute fill latency from fill data: fill_ts - order_ts."""
        for f in fills:
            d = f if isinstance(f, dict) else vars(f)
            market = d.get("instrument", "")
            if market not in self._fill_lat:
                continue
            try:
                # block_timestamp = when fill was confirmed on chain
                # timestamp field on order = when order was placed
                # We use the fill's own timestamp fields available
                fill_ts_str = d.get("block_timestamp", "")
                ord_ts_str  = d.get("timestamp", fill_ts_str)
                if not fill_ts_str or not ord_ts_str:
                    continue
                def parse_ts(s):
                    from datetime import timezone
                    s = str(s)
                    if "T" in s:
                        s = s.replace("Z", "+00:00")
                        try:
                            from datetime import datetime as dt
                            return dt.fromisoformat(s).timestamp()
                        except Exception:
                            return None
                    try:
                        v = float(s)
                        return v / 1000 if v > 1e10 else v
                    except Exception:
                        return None

                ft = parse_ts(fill_ts_str)
                ot = parse_ts(ord_ts_str)
                if ft and ot and ft >= ot:
                    diff_ms = (ft - ot) * 1000
                    if diff_ms < 300_000:   # ignore outliers > 5 min
                        with self._lock:
                            lst = self._fill_lat[market]
                            lst.append(diff_ms)
                            if len(lst) > self.HISTORY:
                                lst.pop(0)
            except Exception:
                pass

    def get(self):
        with self._lock:
            return (
                {m: list(v) for m, v in self._quote_lat.items()},
                {m: list(v) for m, v in self._fill_lat.items()},
            )


_latency_tracker: Optional[LatencyTracker] = None


def latency_refresh_loop():
    while True:
        _latency_tracker.measure_quote()
        time.sleep(10)


def build_latency_panel() -> Panel:
    if _latency_tracker is None:
        return Panel(Text("Latency tracker not running", style="dim"), title="Latency", box=box.ROUNDED)

    quote_lat, fill_lat = _latency_tracker.get()

    def stats(lst):
        if not lst:
            return "—", "—", "—"
        import statistics as st
        return f"{min(lst):.0f}ms", f"{st.mean(lst):.0f}ms", f"{max(lst):.0f}ms"

    def lat_color(avg_str):
        try:
            v = float(avg_str.replace("ms", ""))
            return "green" if v < 300 else "yellow" if v < 700 else "red"
        except Exception:
            return "white"

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1))
    tbl.add_column("Market",   width=10)
    tbl.add_column("Q.Min",    width=7,  justify="right")
    tbl.add_column("Q.Avg",    width=7,  justify="right")
    tbl.add_column("Q.Max",    width=7,  justify="right")
    tbl.add_column("F.Min",    width=7,  justify="right")
    tbl.add_column("F.Avg",    width=7,  justify="right")
    tbl.add_column("F.Max",    width=7,  justify="right")
    tbl.add_column("n",        width=6,  justify="right", style="dim")

    active_markets = [m for m in LatencyTracker.MARKETS
                      if quote_lat.get(m) or fill_lat.get(m)]

    for market in active_markets:
        q_min, q_avg, q_max = stats(quote_lat.get(market, []))
        f_min, f_avg, f_max = stats(fill_lat.get(market, []))
        q_samples = len(quote_lat.get(market, []))
        f_samples = len(fill_lat.get(market, []))
        color = lat_color(q_avg)
        tbl.add_row(
            market,
            q_min, f"[{color}]{q_avg}[/{color}]", q_max,
            f_min, f_avg, f_max,
            f"{q_samples}/{f_samples}",
        )

    if not active_markets:
        return Panel(Align.center(Text("Measuring...", style="dim")),
                     title="Latency", box=box.ROUNDED)

    # DB-backed fill latency (bot must be running to populate)
    db_stats = {}
    try:
        from bot.db import latency_stats_by_market
        own = get_own_address()
        if own:
            for row in latency_stats_by_market(own):
                db_stats[row["market"]] = row
    except Exception:
        pass

    if db_stats:
        db_tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1))
        db_tbl.add_column("Market",   width=12)
        db_tbl.add_column("Fills",    width=8,  justify="right")
        db_tbl.add_column("Min",      width=10, justify="right")
        db_tbl.add_column("Avg",      width=10, justify="right")
        db_tbl.add_column("Max",      width=10, justify="right")
        for market, r in db_stats.items():
            def fmt_s(v):
                if v is None: return "—"
                return f"{v*1000:.0f}ms" if abs(v) < 3600 else "—"
            avg_ms = r["avg_latency_sec"] * 1000 if r["avg_latency_sec"] else 0
            color = "green" if avg_ms < 2000 else "yellow" if avg_ms < 5000 else "red"
            db_tbl.add_row(
                market, str(r["fills"]),
                fmt_s(r["min_latency_sec"]),
                f"[{color}]{fmt_s(r['avg_latency_sec'])}[/{color}]",
                fmt_s(r["max_latency_sec"]),
            )

        layout = Layout()
        layout.split_row(
            Layout(Panel(tbl,    title="Quote Latency (live)",      box=box.SIMPLE), ratio=3),
            Layout(Panel(db_tbl, title="Fill Latency (placed→fill)", box=box.SIMPLE), ratio=2),
        )
        return Panel(layout, title="[bold cyan]Latency[/bold cyan]", box=box.ROUNDED)

    return Panel(tbl, title="[bold cyan]Latency[/bold cyan]  [dim]quote=BBO REST  fill=order→confirmation  rolling 10 samples[/dim]",
                 box=box.ROUNDED)



# ── Wallet panel ──────────────────────────────────────────────────────────────

def build_wallet_panel(monitor: WalletMonitor) -> Panel:
    summary, fills, orders, last_update, error = monitor.get()
    updated = datetime.fromtimestamp(last_update).strftime("%H:%M:%S") if last_update else "—"
    color   = "cyan" if monitor.is_own else "yellow"
    title   = f"[bold {color}]{monitor.label}[/bold {color}]  [dim]{monitor.address[:20]}...  updated {updated}[/dim]"

    if error:
        return Panel(Text(f"Error: {error}", style="red"), title=title, box=box.ROUNDED)
    if summary is None:
        return Panel(Align.center(Text("Loading...", style="dim")), title=title, box=box.ROUNDED)

    equity   = getattr(summary, "total_account_equity", 0) or 0
    balance  = getattr(summary, "margin_balance", 0) or 0
    upnl     = getattr(summary, "upnl", 0) or 0
    total_pnl= getattr(summary, "total_pnl", 0) or 0
    avail    = getattr(summary, "available_balance", 0) or 0
    im_util  = (getattr(summary, "initial_margin_utilization", 0) or 0) * 100
    volume   = getattr(summary, "total_volume", 0) or 0

    acc = Table(box=None, show_header=False, padding=(0,1))
    acc.add_column(style="dim", width=12)
    acc.add_column(justify="right", width=16)
    acc.add_row("Equity",    f"[bold]${equity:,.4f}[/bold]")
    acc.add_row("Balance",   f"${balance:,.4f}")
    acc.add_row("Available", f"${avail:,.4f}")
    acc.add_row("uPnL",      fmt_pnl_r(upnl))
    acc.add_row("Total PnL", fmt_pnl_r(total_pnl))
    acc.add_row("IM Util",   f"[{'red' if im_util>90 else 'yellow' if im_util>70 else 'green'}]{im_util:.1f}%[/]")
    acc.add_row("Volume",    f"${volume:,.0f}")

    pos_tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    for col, w, j in [("Market",12,"left"),("Size",10,"right"),("Entry",12,"right"),
                       ("Value",10,"right"),("uPnL",12,"right"),("Liq",10,"right")]:
        pos_tbl.add_column(col, width=w, justify=j, style="bold" if col=="Market" else "")

    for market, pdata in (getattr(summary,"perp_positions",{}) or {}).items():
        legs = pdata.get("legs", [])
        if not legs: continue
        leg = legs[0]
        size = leg.get("size", 0)
        pos_tbl.add_row(
            market,
            Text(f"{size:+.4f}", style="green" if size>0 else "red"),
            f"${leg.get('entry_price',0):,.4f}",
            f"${leg.get('position_value',0):,.2f}",
            fmt_pnl(pdata.get("upnl",0)),
            f"${pdata.get('liquidation_price',0):,.2f}",
        )

    ord_tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    for col, w, j in [("Market",12,"left"),("Side",6,"center"),("Price",12,"right"),
                       ("Size",10,"right"),("Cloid",32,"left")]:
        ord_tbl.add_column(col, width=w, justify=j)
    for o in orders[:12]:
        d = o if isinstance(o,dict) else vars(o)
        ord_tbl.add_row(
            d.get("instrument",""), fmt_side(d.get("side","")),
            f"${float(d.get('limit_price', d.get('price', 0))):,.4f}", f"{float(d.get('unfilled', d.get('size', 0))):.5f}",
            str(d.get("cloid","")),
        )

    fill_tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    for col, w, j in [("Time",10,"left"),("Market",12,"left"),("Side",6,"center"),
                       ("Price",12,"right"),("Size",10,"right"),("Direction",14,"left"),
                       ("PnL",12,"right"),("Fee",10,"right")]:
        fill_tbl.add_column(col, width=w, justify=j)
    for f in fills[:15]:
        d = f if isinstance(f,dict) else vars(f)
        fill_tbl.add_row(
            fmt_ts(d.get("block_timestamp","")), d.get("instrument",""),
            fmt_side(d.get("side","")),
            f"${float(d.get('limit_price', d.get('price', 0))):,.4f}", f"{float(d.get('unfilled', d.get('size', 0))):.5f}",
            fmt_dir(d.get("direction","")),
            fmt_pnl(float(d.get("closed_pnl",0))),
            f"${float(d.get('fee',0)):.4f}",
        )

    layout = Layout()
    layout.split_column(
        Layout(name="top",     size=9),
        Layout(name="orders",  size=min(len(orders)+4, 14)),
        Layout(name="latency", size=7),
        Layout(name="fills"),
    )
    layout["top"].split_row(
        Layout(Panel(acc,     title="Account",   box=box.SIMPLE), ratio=1),
        Layout(Panel(pos_tbl, title="Positions", box=box.SIMPLE), ratio=3),
    )
    layout["orders"].update(Panel(ord_tbl,  title=f"Open Orders ({len(orders)})", box=box.SIMPLE))
    layout["latency"].update(build_latency_panel())
    layout["fills"].update(Panel(fill_tbl,  title="Recent Fills", box=box.SIMPLE))

    return Panel(layout, title=title, box=box.ROUNDED)


# ── Markout panel ────────────────────────────────────────────────────────────

def build_markout_panel(address: str) -> Panel:
    stats = markout_stats(address)
    title = "[bold cyan]Markout Analysis[/bold cyan]  [dim]price move after fill — negative = adverse selection[/dim]"

    if not stats:
        return Panel(
            Align.center(Text("No markout data yet — fills are tracked as the bot runs.", style="dim")),
            title=title, box=box.ROUNDED
        )

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1))
    tbl.add_column("Market",    width=12)
    tbl.add_column("Direction", width=14)
    tbl.add_column("Fills",     width=6,  justify="right")
    tbl.add_column("10s",       width=10, justify="right")
    tbl.add_column("30s",       width=10, justify="right")
    tbl.add_column("60s",       width=10, justify="right")
    tbl.add_column("5min",      width=10, justify="right")
    tbl.add_column("Signal",    width=16)

    def fmt_bps(v):
        if v is None: return "—"
        color = "green" if v > 0 else "red"
        return f"[{color}]{v:+.2f}bps[/{color}]"

    def signal(v_5m):
        if v_5m is None: return "[dim]waiting[/dim]"
        if v_5m > 2:    return "[green]Good fill ✓[/green]"
        if v_5m > 0:    return "[yellow]Neutral[/yellow]"
        if v_5m > -2:   return "[yellow]Slight AS[/yellow]"
        return "[red]Adverse sel. ✗[/red]"

    for r in stats:
        tbl.add_row(
            r["market"], fmt_dir(r["direction"]),
            str(r["fills"]),
            fmt_bps(r["markout_10s_bps"]),
            fmt_bps(r["markout_30s_bps"]),
            fmt_bps(r["markout_60s_bps"]),
            fmt_bps(r["markout_5m_bps"]),
            signal(r["markout_5m_bps"]),
        )

    return Panel(tbl, title=title, box=box.ROUNDED)


# ── Analytics panel ───────────────────────────────────────────────────────────

def build_analytics_panel(address: str) -> Panel:
    window = get_window()
    hours  = window_hours()

    def _q(fn, *args, **kwargs):
        try:
            return fn(*args, hours=hours, **kwargs)
        except TypeError:
            return fn(*args, **kwargs)

    by_market    = _q(pnl_by_market,    address)
    by_direction = _q(pnl_by_direction, address)
    over_time    = _q(pnl_over_time,    address, "hour")
    fees         = _q(fee_analysis,     address)
    equity_hist  = equity_history(address, 20)

    # Window selector bar
    bar_parts = []
    for i, w in enumerate(TIME_WINDOWS):
        if w == window:
            bar_parts.append(f"[bold yellow on dark_orange]  {i+1}:{w}  [/bold yellow on dark_orange]")
        else:
            bar_parts.append(f"[dim]  {i+1}:{w}  [/dim]")
    win_bar = "".join(bar_parts) + "  [dim](press 1-4 to switch)[/dim]"

    title = f"[bold cyan]Analytics[/bold cyan]  [dim]{short_addr(address)}[/dim]   {win_bar}"

    # ── PnL by market ─────────────────────────────────────────────
    mkt_tbl = Table(title="PnL by Market", box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    mkt_tbl.add_column("Market",   width=12)
    mkt_tbl.add_column("Gross PnL",width=12, justify="right")
    mkt_tbl.add_column("Fees",     width=10, justify="right")
    mkt_tbl.add_column("Net PnL",  width=12, justify="right")
    mkt_tbl.add_column("Trades",   width=8,  justify="right")
    mkt_tbl.add_column("Volume",   width=12, justify="right")
    mkt_tbl.add_column("",         width=22)

    max_abs = max((abs(r["net_pnl"]) for r in by_market), default=1)
    for r in by_market:
        mkt_tbl.add_row(
            r["market"], fmt_pnl_r(r["total_pnl"]),
            f"[red]${r['total_fee']:.4f}[/red]",
            fmt_pnl_r(r["net_pnl"]),
            str(r["trades"]), f"${r['volume']:,.0f}",
            bar(r["net_pnl"], max_abs),
        )

    # ── PnL by direction ──────────────────────────────────────────
    dir_tbl = Table(title="PnL by Direction", box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    dir_tbl.add_column("Direction", width=14)
    dir_tbl.add_column("Gross PnL", width=12, justify="right")
    dir_tbl.add_column("Fees",      width=10, justify="right")
    dir_tbl.add_column("Net PnL",   width=12, justify="right")
    dir_tbl.add_column("Trades",    width=8,  justify="right")
    dir_tbl.add_column("",          width=22)

    max_abs_d = max((abs(r["net_pnl"]) for r in by_direction), default=1)
    for r in by_direction:
        dir_tbl.add_row(
            fmt_dir(r["direction"]),
            fmt_pnl_r(r["total_pnl"]),
            f"[red]${r['total_fee']:.4f}[/red]",
            fmt_pnl_r(r["net_pnl"]),
            str(r["trades"]),
            bar(r["net_pnl"], max_abs_d),
        )

    # ── Fee analysis ──────────────────────────────────────────────
    fee_tbl = Table(title="Fee Drag by Market", box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    fee_tbl.add_column("Market",   width=12)
    fee_tbl.add_column("Total Fee",width=12, justify="right")
    fee_tbl.add_column("Gross PnL",width=12, justify="right")
    fee_tbl.add_column("Net PnL",  width=12, justify="right")
    fee_tbl.add_column("Avg Fee%", width=10, justify="right")
    fee_tbl.add_column("Trades",   width=8,  justify="right")

    for r in fees:
        fee_tbl.add_row(
            r["market"],
            f"[red]${r['total_fee']:.4f}[/red]",
            fmt_pnl_r(r["gross_pnl"]),
            fmt_pnl_r(r["net_pnl"]),
            f"{r['avg_fee_pct']:.4f}%",
            str(r["trades"]),
        )

    # ── PnL over time ─────────────────────────────────────────────
    time_tbl = Table(title=f"PnL by Hour [{window}]", box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    time_tbl.add_column("Period",  width=16)
    time_tbl.add_column("Net PnL", width=12, justify="right")
    time_tbl.add_column("Trades",  width=8,  justify="right")
    time_tbl.add_column("",        width=22)

    recent = over_time[-20:] if len(over_time) > 20 else over_time
    max_t = max((abs(r["net_pnl"]) for r in recent), default=1)
    for r in recent:
        time_tbl.add_row(
            r["period"], fmt_pnl_r(r["net_pnl"]),
            str(r["trades"]), bar(r["net_pnl"], max_t),
        )

    # ── Equity history ────────────────────────────────────────────
    eq_tbl = Table(title="Equity Snapshots (30min)", box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    eq_tbl.add_column("Time",     width=20)
    eq_tbl.add_column("Equity",   width=14, justify="right")
    eq_tbl.add_column("uPnL",     width=12, justify="right")
    eq_tbl.add_column("Total PnL",width=12, justify="right")
    eq_tbl.add_column("IM Util",  width=10, justify="right")

    for r in equity_hist[-15:]:
        eq_tbl.add_row(
            r["timestamp"][:19],
            f"[bold]${r['equity']:,.4f}[/bold]",
            fmt_pnl_r(r["upnl"]),
            fmt_pnl_r(r["total_pnl"]),
            f"[{'red' if r['im_utilization']>90 else 'yellow' if r['im_utilization']>70 else 'green'}]{r['im_utilization']:.1f}%[/]",
        )

    markout = build_markout_panel(address)

    if not by_market:
        return Panel(
            Align.center(Text(
                f"No fills in the last {window} — try a wider window (press 1-4) or wait for fills.",
                style="dim"
            )),
            title=title, box=box.ROUNDED
        )

    layout = Layout()
    layout.split_column(
        Layout(name="row1", ratio=2),
        Layout(name="row2", ratio=2),
        Layout(name="row3", ratio=3),
    )
    layout["row1"].split_row(
        Layout(Panel(mkt_tbl, box=box.SIMPLE), ratio=3),
        Layout(Panel(eq_tbl,  box=box.SIMPLE), ratio=2),
    )
    layout["row2"].split_row(
        Layout(Panel(dir_tbl, box=box.SIMPLE), ratio=3),
        Layout(Panel(fee_tbl, box=box.SIMPLE), ratio=2),
    )
    layout["row3"].split_row(
        Layout(Panel(time_tbl, box=box.SIMPLE), ratio=2),
        Layout(markout, ratio=3),
    )

    return Panel(layout, title=title, box=box.ROUNDED)


# ── Leaderboard ───────────────────────────────────────────────────────────────

class Leaderboard:
    def __init__(self):
        self.info = InfoClient(is_testnet=False)
        self._lock = threading.Lock()
        self._traders: Dict[str, dict] = {}
        self._last_update = 0.0
        self._error: Optional[str] = None

    def refresh(self):
        try:
            traders: Dict[str, dict] = {}
            for market in LEADERBOARD_MARKETS:
                trades = self.info.trades(TradesParams(symbol=market, limit=LEADERBOARD_TRADES))
                for t in trades:
                    d = vars(t) if not isinstance(t, dict) else t
                    for role in ("maker", "taker"):
                        addr = d.get(role, "")
                        if not addr: continue
                        if addr not in traders:
                            traders[addr] = {"volume": 0.0, "trades": 0, "markets": set()}
                        traders[addr]["volume"] += float(d.get("price",0)) * float(d.get("size",0))
                        traders[addr]["trades"] += 1
                        traders[addr]["markets"].add(market)
            save_leaderboard(traders)
            with self._lock:
                self._traders     = traders
                self._last_update = time.time()
                self._error       = None
        except Exception as e:
            with self._lock:
                self._error = str(e)

    def get(self):
        with self._lock:
            return dict(self._traders), self._last_update, self._error


_leaderboard: Optional[Leaderboard] = None


def build_leaderboard_panel() -> Panel:
    traders, last_update, error = _leaderboard.get()
    updated = datetime.fromtimestamp(last_update).strftime("%H:%M:%S") if last_update else "—"

    if error:
        return Panel(Text(f"Error: {error}", style="red"), title="Leaderboard")
    if not traders:
        return Panel(Align.center(Text("Loading...", style="dim")), title="Leaderboard")

    ranked = sorted(traders.items(), key=lambda x: x[1]["volume"], reverse=True)[:30]

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0,1))
    tbl.add_column("#",       width=4,  justify="right", style="dim")
    tbl.add_column("Address", width=44)
    tbl.add_column("Volume",  width=14, justify="right")
    tbl.add_column("Trades",  width=8,  justify="right")
    tbl.add_column("Markets", width=36)

    for i, (addr, data) in enumerate(ranked, 1):
        tbl.add_row(str(i), addr, f"${data['volume']:,.0f}",
                    str(data["trades"]), ", ".join(sorted(data["markets"])))

    return Panel(
        tbl,
        title=f"[bold cyan]Leaderboard[/bold cyan]  [dim]by volume · {LEADERBOARD_TRADES} trades/market · updated {updated}[/dim]",
        box=box.ROUNDED,
    )


# ── Background loops ──────────────────────────────────────────────────────────

def wallet_refresh_loop(monitors):
    while True:
        for m in monitors:
            threading.Thread(target=m.refresh, daemon=True).start()
        time.sleep(REFRESH_INTERVAL)

def leaderboard_refresh_loop():
    while True:
        threading.Thread(target=_leaderboard.refresh, daemon=True).start()
        time.sleep(30)


# ── Entry points ──────────────────────────────────────────────────────────────

def get_own_address() -> str:
    try:
        from eth_account import Account as A
        pk = os.environ.get("HOTSTUFF_PRIVATE_KEY", "")
        return A.from_key(pk).address if pk else ""
    except Exception:
        return ""


def run_wallet_dashboard(addresses: List[str]):
    own = get_own_address().lower()
    monitors = []
    for addr in addresses:
        is_own = addr.lower() == own
        label  = f"[YOU] {short_addr(addr)}" if is_own else short_addr(addr)
        monitors.append(WalletMonitor(addr, label, is_own=is_own))

    global _latency_tracker
    _latency_tracker = LatencyTracker()

    console.print("[dim]Loading...[/dim]")
    for m in monitors:
        m.refresh()

    threading.Thread(target=wallet_refresh_loop, args=(monitors,), daemon=True).start()
    threading.Thread(target=latency_refresh_loop, daemon=True).start()

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            if len(monitors) == 1:
                live.update(build_wallet_panel(monitors[0]))
            else:
                layout = Layout()
                layout.split_column(*[Layout(build_wallet_panel(m)) for m in monitors])
                live.update(layout)
            time.sleep(1)


def run_analytics(address: str):
    """Analytics with 1/2/3/4 key switching for time windows."""
    import sys, tty, termios

    def _key_listener():
        """Runs in a daemon thread; reads single keypresses raw."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("1", "2", "3", "4"):
                    set_window_idx(int(ch) - 1)
                elif ch in ("q", "\x03"):   # q or Ctrl-C
                    import os
                    os.kill(os.getpid(), 2)  # SIGINT → clean exit
                # swallow arrow keys (ESC sequences) without crashing
                elif ch == "\x1b":
                    sys.stdin.read(2)        # consume [ + A/B/C/D
        except Exception:
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    t = threading.Thread(target=_key_listener, daemon=True)
    t.start()

    console.print("[dim]Loading analytics...[/dim]")
    with Live(console=console, refresh_per_second=1, screen=False) as live:
        while True:
            live.update(build_analytics_panel(address))
            time.sleep(2)


def run_leaderboard():
    global _leaderboard
    _leaderboard = Leaderboard()
    console.print("[dim]Loading leaderboard...[/dim]")
    _leaderboard.refresh()
    threading.Thread(target=leaderboard_refresh_loop, daemon=True).start()

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            live.update(build_leaderboard_panel())
            time.sleep(1)


if __name__ == "__main__":
    init_db()
    args = sys.argv[1:]

    if "--leaderboard" in args:
        run_leaderboard()

    elif "--analytics" in args:
        remaining = [a for a in args if a != "--analytics"]
        address = remaining[0] if remaining else get_own_address()
        if not address:
            console.print("[red]No address. Set HOTSTUFF_PRIVATE_KEY or pass an address.[/red]")
            sys.exit(1)
        run_analytics(address)

    else:
        if not args:
            own = get_own_address()
            if not own:
                console.print("[red]No address provided and HOTSTUFF_PRIVATE_KEY not set.[/red]")
                sys.exit(1)
            args = [own]
        run_wallet_dashboard(args)

@app.route("/")
def index():
    with open("/root/hotstuff-flow/bot/index.html") as f:
        return f.read(), 200, {"Content-Type": "text/html"}
