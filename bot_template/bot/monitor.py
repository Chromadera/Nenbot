"""
Hotstuff Monitor Dashboard
Dedicated view for Regime, OFI, Latency and Markout analysis.

Usage:
  python3 -m bot.monitor
"""
import sys, time, os, json
from datetime import datetime
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

console = Console()
REFRESH = 3


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S")
    except Exception:
        return "—"

def _state_file(name):
    return os.path.join(os.path.dirname(__file__), name)

def load_json(name):
    try:
        with open(_state_file(name)) as f:
            return json.load(f)
    except Exception:
        return None


# ── Regime Panel ─────────────────────────────────────────────────────────────

def build_regime_panel() -> Panel:
    states = load_json("regime_state.json")
    title = "[bold cyan]Market Regime[/bold cyan]"
    if not states:
        return Panel(Align.center(Text("No regime data — bot must be running.", style="dim")),
                     title=title, box=box.ROUNDED)

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1), expand=True)
    tbl.add_column("Market",       width=12)
    tbl.add_column("Regime",       width=16)
    tbl.add_column("Confidence",   width=12, justify="right")
    tbl.add_column("Volatility",   width=12, justify="right")
    tbl.add_column("Trend",        width=10, justify="right")
    tbl.add_column("Bid Mult",     width=10, justify="right")
    tbl.add_column("Ask Mult",     width=10, justify="right")
    tbl.add_column("Updated",      width=10, style="dim")

    colors = {"Ranging": "cyan", "Trending Up": "green", "Trending Down": "red"}

    for market, s in states.items():
        regime = s.get("regime", "Unknown")
        color  = colors.get(regime, "white")
        conf   = s.get("confidence", 0)
        vol    = s.get("volatility", 0)
        trend  = s.get("trend", 0)
        bid_m  = s.get("bid_mult", 1.0)
        ask_m  = s.get("ask_mult", 1.0)
        upd    = fmt_ts(s.get("updated_at", ""))
        tbl.add_row(
            market,
            f"[{color}]{regime}[/{color}]",
            f"{conf:.1%}",
            f"{vol:.1f}%",
            f"[{'green' if trend > 0 else 'red'}]{trend:+.2f}%[/]",
            f"[{'red' if bid_m > 1 else 'green' if bid_m < 1 else 'white'}]{bid_m}x[/]",
            f"[{'red' if ask_m > 1 else 'green' if ask_m < 1 else 'white'}]{ask_m}x[/]",
            upd,
        )

    return Panel(tbl, title=title, box=box.ROUNDED)


# ── OFI Panel ─────────────────────────────────────────────────────────────────

def build_ofi_panel() -> Panel:
    states = load_json("ofi_state.json")
    title = "[bold cyan]Order Flow Imbalance[/bold cyan]"
    if not states:
        return Panel(Align.center(Text("No OFI data — bot must be running.", style="dim")),
                     title=title, box=box.ROUNDED)

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1), expand=True)
    tbl.add_column("Market",    width=12)
    tbl.add_column("Signal",    width=14)
    tbl.add_column("OFI Raw",   width=10, justify="right")
    tbl.add_column("OFI Smooth",width=12, justify="right")
    tbl.add_column("Bid Vol",   width=12, justify="right")
    tbl.add_column("Ask Vol",   width=12, justify="right")
    tbl.add_column("B/A Ratio", width=10, justify="right")
    tbl.add_column("Updated",   width=10, style="dim")

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
        upd    = fmt_ts(s.get("updated_at", ""))
        oc = "green" if ofi > 0 else "red"
        sc = "green" if smooth > 0 else "red"
        rc = "green" if ratio > 1.1 else "red" if ratio < 0.9 else "yellow"
        tbl.add_row(
            market,
            f"[{color}]{label}[/{color}]",
            f"[{oc}]{ofi:+.3f}[/{oc}]",
            f"[{sc}]{smooth:+.3f}[/{sc}]",
            f"${bid_v:,.0f}",
            f"${ask_v:,.0f}",
            f"[{rc}]{ratio:.2f}[/{rc}]",
            upd,
        )

    return Panel(tbl, title=title, box=box.ROUNDED)


# ── Latency Panel ─────────────────────────────────────────────────────────────

def build_latency_panel() -> Panel:
    title = "[bold cyan]Order Latency[/bold cyan]"
    try:
        from bot.db import latency_stats_by_market
        from eth_account import Account
        import os
        addr = Account.from_key(os.environ["HOTSTUFF_PRIVATE_KEY"]).address
        rows = latency_stats_by_market(addr)
    except Exception:
        return Panel(Align.center(Text("No latency data — bot must be running.", style="dim")),
                     title=title, box=box.ROUNDED)

    if not rows:
        return Panel(Align.center(Text("No latency data yet.", style="dim")),
                     title=title, box=box.ROUNDED)

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1), expand=True)
    tbl.add_column("Market",    width=12)
    tbl.add_column("Fills",     width=8,  justify="right")
    tbl.add_column("Min",       width=10, justify="right")
    tbl.add_column("Avg",       width=10, justify="right")
    tbl.add_column("Max",       width=10, justify="right")
    tbl.add_column("p50",       width=10, justify="right")
    tbl.add_column("p95",       width=10, justify="right")

    for r in rows:
        avg = r.get("avg_ms", 0) or 0
        color = "green" if avg < 300 else "yellow" if avg < 700 else "red"
        tbl.add_row(
            r.get("market", ""),
            str(r.get("fills", 0)),
            f"{r.get('min_ms', 0):.0f}ms",
            f"[{color}]{avg:.0f}ms[/{color}]",
            f"{r.get('max_ms', 0):.0f}ms",
            f"{r.get('p50_ms', 0):.0f}ms",
            f"{r.get('p95_ms', 0):.0f}ms",
        )

    return Panel(tbl, title=title, box=box.ROUNDED)


# ── Markout Panel ─────────────────────────────────────────────────────────────

def build_markout_panel() -> Panel:
    title = "[bold cyan]Markout Analysis[/bold cyan]  [dim]negative = adverse selection[/dim]"
    try:
        from bot.db import markout_stats
        from eth_account import Account
        import os
        addr = Account.from_key(os.environ["HOTSTUFF_PRIVATE_KEY"]).address
        stats = markout_stats(addr)
    except Exception:
        return Panel(Align.center(Text("No markout data.", style="dim")),
                     title=title, box=box.ROUNDED)

    if not stats:
        return Panel(Align.center(Text("No markout data yet — needs 5min per fill.", style="dim")),
                     title=title, box=box.ROUNDED)

    tbl = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", padding=(0, 1), expand=True)
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
        c = "green" if v > 0 else "red"
        return f"[{c}]{v:+.2f}bps[/{c}]"

    def signal(v):
        if v is None: return "[dim]waiting[/dim]"
        if v > 2:    return "[green]Good fill ✓[/green]"
        if v > 0:    return "[yellow]Neutral[/yellow]"
        if v > -2:   return "[yellow]Slight AS[/yellow]"
        return "[red]Adverse sel. ✗[/red]"

    dir_colors = {
        "openLong": "green", "flipToLong": "cyan", "closeLong": "yellow",
        "openShort": "red",  "flipToShort": "magenta", "closeShort": "orange3",
    }

    for r in stats:
        d = r.get("direction", "")
        dc = dir_colors.get(d, "white")
        tbl.add_row(
            r.get("market", ""),
            f"[{dc}]{d}[/{dc}]",
            str(r.get("fills", 0)),
            fmt_bps(r.get("markout_10s_bps")),
            fmt_bps(r.get("markout_30s_bps")),
            fmt_bps(r.get("markout_60s_bps")),
            fmt_bps(r.get("markout_5m_bps")),
            signal(r.get("markout_5m_bps")),
        )

    return Panel(tbl, title=title, box=box.ROUNDED)


# ── Main Loop ─────────────────────────────────────────────────────────────────

def build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top",     ratio=1),
        Layout(name="bottom",  ratio=1),
    )
    layout["top"].split_row(
        Layout(build_regime_panel(), name="regime", ratio=1),
        Layout(build_ofi_panel(),    name="ofi",    ratio=1),
    )
    layout["bottom"].split_row(
        Layout(build_latency_panel(),  name="latency", ratio=1),
        Layout(build_markout_panel(),  name="markout", ratio=1),
    )
    return layout


def main():
    from bot.db import init_db
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f"[bold cyan]Hotstuff Monitor[/bold cyan]  [dim]{now}[/dim]"

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            now = datetime.now().strftime("%H:%M:%S")
            title = f"[bold cyan]Hotstuff Monitor[/bold cyan]  [dim]updated {now}[/dim]"
            live.update(Panel(build_layout(), title=title, box=box.ROUNDED))
            time.sleep(REFRESH)


if __name__ == "__main__":
    main()
