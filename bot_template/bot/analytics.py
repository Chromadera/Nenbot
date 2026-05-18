"""
Standalone analytics dashboard with time-window switching.
Usage:
  python3 -m bot.analytics                  # your own wallet
  python3 -m bot.analytics 0xABCD...        # any wallet

Keys:  1=1hr  2=12hr  3=24hr  4=all   q=quit
"""
import os, sys, time, threading, tty, termios
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich.layout import Layout
from rich import box

from bot.db import (
    init_db, pnl_by_market, pnl_by_direction,
    pnl_over_time, fee_analysis, equity_history,
)

console = Console()

# ── Time window ───────────────────────────────────────────────────────────────
WINDOWS  = ["1hr", "12hr", "24hr", "all"]
_win_idx = 2
_win_lock = threading.Lock()

def get_window() -> str:
    with _win_lock:
        return WINDOWS[_win_idx]

def set_window(i: int):
    global _win_idx
    with _win_lock:
        _win_idx = i % len(WINDOWS)

def get_hours() -> Optional[int]:
    return {"1hr": 1, "12hr": 12, "24hr": 24}.get(get_window())

def _q(fn, *args, **kwargs):
    """Call DB fn with hours= kwarg, fall back if unsupported."""
    try:
        return fn(*args, hours=get_hours(), **kwargs)
    except TypeError:
        return fn(*args, **kwargs)


# ── Formatting ────────────────────────────────────────────────────────────────
def pnl_str(v: float) -> str:
    return f"[green]${v:+.4f}[/green]" if v >= 0 else f"[red]${v:+.4f}[/red]"

def short_addr(a: str) -> str:
    return a[:6] + "..." + a[-4:] if len(a) > 12 else a

def spark(val: float, max_val: float, width: int = 18) -> str:
    if not max_val:
        return " " * width
    filled = int(min(abs(val) / abs(max_val), 1.0) * width)
    color  = "green" if val >= 0 else "red"
    return f"[{color}]{'█' * filled}{'░' * (width - filled)}[/{color}]"

def fmt_dir(d: str) -> str:
    c = {"openLong":"green","flipToLong":"cyan","closeLong":"yellow",
         "openShort":"red","flipToShort":"magenta","closeShort":"yellow"}
    return f"[{c.get(d,'white')}]{d}[/]"

def window_bar() -> Text:
    t = Text()
    w = get_window()
    for i, name in enumerate(WINDOWS):
        if name == w:
            t.append(f" {i+1}:{name} ", style="bold black on yellow")
        else:
            t.append(f" {i+1}:{name} ", style="dim")
        t.append(" ")
    t.append("  (1-4 to switch  q to quit)", style="dim")
    return t


# ── Table builders ────────────────────────────────────────────────────────────
def tbl_pnl_market(address: str) -> Table:
    rows = _q(pnl_by_market, address)
    t = Table(title="PnL by Market", box=box.SIMPLE_HEAD,
              header_style="bold cyan", padding=(0,1))
    t.add_column("Market",  width=12)
    t.add_column("Gross",   width=12, justify="right")
    t.add_column("Fees",    width=10, justify="right")
    t.add_column("Net PnL", width=12, justify="right")
    t.add_column("Trades",  width=7,  justify="right")
    t.add_column("Volume",  width=12, justify="right")
    t.add_column("",        width=20)
    if not rows:
        t.add_row("[dim]no data[/dim]","","","","","",""); return t
    mx = max(abs(r["net_pnl"]) for r in rows) or 1
    for r in rows:
        t.add_row(r["market"], pnl_str(r["total_pnl"]),
                  f"[red]${r['total_fee']:.4f}[/red]",
                  pnl_str(r["net_pnl"]), str(r["trades"]),
                  f"${r['volume']:,.0f}", spark(r["net_pnl"], mx))
    return t

def tbl_pnl_direction(address: str) -> Table:
    rows = _q(pnl_by_direction, address)
    t = Table(title="PnL by Direction", box=box.SIMPLE_HEAD,
              header_style="bold cyan", padding=(0,1))
    t.add_column("Direction", width=14)
    t.add_column("Gross",     width=12, justify="right")
    t.add_column("Fees",      width=10, justify="right")
    t.add_column("Net PnL",   width=12, justify="right")
    t.add_column("Trades",    width=7,  justify="right")
    t.add_column("",          width=20)
    if not rows:
        t.add_row("[dim]no data[/dim]","","","","",""); return t
    mx = max(abs(r["net_pnl"]) for r in rows) or 1
    for r in rows:
        t.add_row(fmt_dir(r["direction"]), pnl_str(r["total_pnl"]),
                  f"[red]${r['total_fee']:.4f}[/red]",
                  pnl_str(r["net_pnl"]), str(r["trades"]),
                  spark(r["net_pnl"], mx))
    return t

def tbl_fees(address: str) -> Table:
    rows = _q(fee_analysis, address)
    t = Table(title="Fee Drag", box=box.SIMPLE_HEAD,
              header_style="bold cyan", padding=(0,1))
    t.add_column("Market",    width=12)
    t.add_column("Total Fee", width=12, justify="right")
    t.add_column("Gross",     width=12, justify="right")
    t.add_column("Net PnL",   width=12, justify="right")
    t.add_column("Avg Fee%",  width=10, justify="right")
    t.add_column("Trades",    width=7,  justify="right")
    if not rows:
        t.add_row("[dim]no data[/dim]","","","","",""); return t
    for r in rows:
        t.add_row(r["market"], f"[red]${r['total_fee']:.4f}[/red]",
                  pnl_str(r["gross_pnl"]), pnl_str(r["net_pnl"]),
                  f"{r['avg_fee_pct']:.4f}%", str(r["trades"]))
    return t

def tbl_pnl_time(address: str) -> Table:
    rows = _q(pnl_over_time, address, "hour")
    t = Table(title=f"PnL by Hour  [{get_window()}]", box=box.SIMPLE_HEAD,
              header_style="bold cyan", padding=(0,1))
    t.add_column("Period",  width=16)
    t.add_column("Net PnL", width=12, justify="right")
    t.add_column("Trades",  width=7,  justify="right")
    t.add_column("",        width=20)
    if not rows:
        t.add_row("[dim]no data[/dim]","","",""); return t
    recent = rows[-20:]
    mx = max(abs(r["net_pnl"]) for r in recent) or 1
    for r in recent:
        t.add_row(r["period"], pnl_str(r["net_pnl"]),
                  str(r["trades"]), spark(r["net_pnl"], mx))
    return t

def tbl_equity(address: str) -> Table:
    rows = equity_history(address, 12)
    t = Table(title="Equity Snapshots (30min)", box=box.SIMPLE_HEAD,
              header_style="bold cyan", padding=(0,1))
    t.add_column("Time",      width=19)
    t.add_column("Equity",    width=14, justify="right")
    t.add_column("uPnL",      width=12, justify="right")
    t.add_column("Total PnL", width=12, justify="right")
    t.add_column("IM Util",   width=9,  justify="right")
    if not rows:
        t.add_row("[dim]no data[/dim]","","","",""); return t
    for r in rows:
        im = r["im_utilization"]
        t.add_row(r["timestamp"][:19],
                  f"[bold]${r['equity']:,.4f}[/bold]",
                  pnl_str(r["upnl"]), pnl_str(r["total_pnl"]),
                  f"[{'red' if im>90 else 'yellow' if im>70 else 'green'}]{im:.1f}%[/]")
    return t


# ── Key listener (daemon thread) ──────────────────────────────────────────────
_quit = threading.Event()

def key_listener():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not _quit.is_set():
            ch = sys.stdin.read(1)
            if ch in ("1","2","3","4"):
                set_window(int(ch) - 1)
            elif ch in ("q", "\x03"):
                _quit.set()
            elif ch == "\x1b":
                rest = sys.stdin.read(2)
                if rest == "[C":   set_window(_win_idx + 1)
                elif rest == "[D": set_window(_win_idx - 1)
    except Exception:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Main loop ─────────────────────────────────────────────────────────────────
def run(address: str):
    threading.Thread(target=key_listener, daemon=True).start()

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )

    with Live(layout, console=console, refresh_per_second=2, screen=True) as live:
        while not _quit.is_set():
            now = datetime.now().strftime("%H:%M:%S")

            # Header — pinned at top, always visible
            hdr = Text()
            hdr.append("  Hotstuff Analytics  ", style="bold cyan")
            hdr.append(f"{short_addr(address)}  ", style="dim")
            hdr.append(f"updated {now}\n  ", style="dim")
            hdr.append(window_bar())
            layout["header"].update(Panel(hdr, box=box.HORIZONTALS))

            # Body — tables stacked, cropped at terminal bottom
            layout["body"].update(Panel(
                Group(
                    tbl_pnl_market(address),
                    Text(""),
                    tbl_pnl_direction(address),
                    Text(""),
                    tbl_fees(address),
                    Text(""),
                    tbl_pnl_time(address),
                    Text(""),
                    tbl_equity(address),
                ),
                box=box.SIMPLE,
            ))

            time.sleep(3)


# ── Entry ─────────────────────────────────────────────────────────────────────
def get_own_address() -> str:
    try:
        from eth_account import Account as A
        pk = os.environ.get("HOTSTUFF_PRIVATE_KEY", "")
        return A.from_key(pk).address if pk else ""
    except Exception:
        return ""

if __name__ == "__main__":
    init_db()
    args = sys.argv[1:]
    address = args[0] if args else get_own_address()
    if not address:
        console.print("[red]No address. Pass one or set HOTSTUFF_PRIVATE_KEY.[/red]")
        sys.exit(1)
    run(address)
