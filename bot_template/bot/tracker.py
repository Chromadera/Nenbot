"""
Wallet Tracker — fetch all fills for target wallets, analyse strategy, export CSV.

Usage:
  python3 -m bot.tracker 0xABC... 0xDEF...          # analyse wallets
  python3 -m bot.tracker 0xABC... --hours 48        # last 48 hours (default 24)
  python3 -m bot.tracker 0xABC... --csv             # export CSV to current dir
  python3 -m bot.tracker 0xABC... --csv --hours 48  # export 48hr CSV
"""

import sys
import os
import csv
import time
import threading
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich import box

from hotstuff.apis.info import InfoClient
from hotstuff.methods.info.account import FillsParams, Fill

console = Console()

FILLS_PER_PAGE = 500  # max per request


# ── Data fetching ─────────────────────────────────────────────────────────────

def fetch_all_fills(info: InfoClient, address: str, since: Optional[datetime] = None) -> List[dict]:
    """Paginate through all fills for a wallet, optionally filtering by time."""
    all_fills = []
    page = 1
    total_pages = None

    console.print(f"[dim]Fetching fills for {address[:10]}...[/dim]", end="")

    while True:
        resp = None
        for attempt in range(3):
            try:
                resp = info.fills(FillsParams(user=address, limit=FILLS_PER_PAGE, page=page))
                break
            except Exception as e:
                if attempt < 2:
                    console.print(f"[yellow]Page {page} attempt {attempt+1} failed: {e} — retrying...[/yellow]")
                    time.sleep(2)
                else:
                    console.print(f"\n[red]Error fetching page {page}: {e}[/red]")
        if resp is None:
            break

        entries = getattr(resp, "entries", []) or []
        if total_pages is None:
            total_pages = getattr(resp, "total_pages", 1) or 1
            console.print(f" {getattr(resp, 'total_count', '?')} total fills across {total_pages} pages")

        for f in entries:
            fill = f if isinstance(f, dict) else (f.__dict__ if hasattr(f, '__dict__') else vars(f))
            # Parse timestamp
            ts_raw = fill.get("timestamp") or fill.get("block_timestamp")
            if ts_raw:
                try:
                    if isinstance(ts_raw, (int, float)) and ts_raw > 1e10:
                        ts = datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)
                    elif isinstance(ts_raw, (int, float)):
                        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                    fill["_ts"] = ts
                except:
                    fill["_ts"] = None
            else:
                fill["_ts"] = None

            # If since filter — stop once we've gone past the time window
            if since and fill["_ts"] and fill["_ts"] < since:
                console.print(f"[dim] Reached time boundary at page {page}[/dim]")
                return all_fills

            all_fills.append(fill)

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.15)  # rate limit

    return all_fills


# ── Strategy analysis ─────────────────────────────────────────────────────────

def analyse(fills: List[dict], address: str, hours: int) -> dict:
    """Infer strategy characteristics from fill history."""
    if not fills:
        return {}

    now = datetime.now(tz=timezone.utc)
    since = now - timedelta(hours=hours)
    recent = [f for f in fills if f.get("_ts") and f["_ts"] >= since]
    window = recent if recent else fills

    total_notional  = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in window)
    total_trades    = len(window)
    maker_fills     = sum(1 for f in window if not f.get("crossed", True))
    taker_fills     = sum(1 for f in window if f.get("crossed", False))
    maker_pct       = maker_fills / total_trades * 100 if total_trades else 0

    # Direction breakdown
    directions: Dict[str, int] = {}
    for f in window:
        d = f.get("direction") or "unknown"
        directions[d] = directions.get(d, 0) + 1

    # PnL
    total_pnl  = sum(float(f.get("closed_pnl") or 0) for f in window)
    total_fees = sum(float(f.get("fee") or 0) for f in window)
    net_pnl    = total_pnl + total_fees

    # Avg trade size
    avg_size_usd = total_notional / total_trades if total_trades else 0

    # Hold time estimation — pair opens with closes by direction
    open_times: Dict[str, List[datetime]] = {}
    hold_times = []
    for f in sorted(window, key=lambda x: x.get("_ts") or now):
        d   = f.get("direction", "")
        ts  = f.get("_ts")
        if not ts:
            continue
        mkt = f.get("instrument") or f.get("market", "")

        if d == "flipToLong":
            short_key = f"{mkt}_short"
            if open_times.get(short_key):
                hold_times.append((ts - open_times[short_key].pop(0)).total_seconds())
            open_times.setdefault(f"{mkt}_long", []).append(ts)
        elif d == "flipToShort":
            long_key = f"{mkt}_long"
            if open_times.get(long_key):
                hold_times.append((ts - open_times[long_key].pop(0)).total_seconds())
            open_times.setdefault(f"{mkt}_short", []).append(ts)
        elif d.startswith("open"):
            key = f"{mkt}_long" if "Long" in d else f"{mkt}_short" if "Short" in d else None
            if key: open_times.setdefault(key, []).append(ts)
        elif d.startswith("close"):
            key = f"{mkt}_long" if "Long" in d else f"{mkt}_short" if "Short" in d else None
            if key and open_times.get(key):
                hold_times.append((ts - open_times[key].pop(0)).total_seconds())

    # Cap outliers — holds >4h are likely bot-stopped sessions, not normal trades
    # Report both the raw avg and the capped avg
    normal_holds = [h for h in hold_times if h <= 14400]  # ≤4h
    avg_hold_secs = sum(normal_holds) / len(normal_holds) if normal_holds else (
                    sum(hold_times) / len(hold_times) if hold_times else None)

    # Market preference
    markets: Dict[str, float] = {}
    for f in window:
        mkt = f.get("instrument") or f.get("market", "unknown")
        markets[mkt] = markets.get(mkt, 0) + float(f.get("price", 0)) * float(f.get("size", 0))

    # Time of day bias (UTC hour buckets)
    hour_buckets: Dict[int, int] = {}
    for f in window:
        ts = f.get("_ts")
        if ts:
            hour_buckets[ts.hour] = hour_buckets.get(ts.hour, 0) + 1
    peak_hour = max(hour_buckets, key=hour_buckets.get) if hour_buckets else None

    # Directional bias
    long_vol  = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in window if "Long" in (f.get("direction") or ""))
    short_vol = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in window if "Short" in (f.get("direction") or ""))
    if long_vol + short_vol > 0:
        long_bias = long_vol / (long_vol + short_vol) * 100
    else:
        long_bias = 50.0

    # Strategy classification
    if maker_pct >= 70:
        style = "Market Maker"
    elif maker_pct <= 30:
        style = "Directional / Aggressive"
    else:
        style = "Mixed"

    if avg_hold_secs is not None:
        if avg_hold_secs < 30:
            hold_style = "Scalper (<30s)"
        elif avg_hold_secs < 300:
            hold_style = "Short-term (30s–5m)"
        elif avg_hold_secs < 3600:
            hold_style = "Swing (5m–1hr)"
        else:
            hold_style = "Position (>1hr)"
    else:
        hold_style = "Unknown"

    return {
        "address":       address,
        "hours":         hours,
        "total_trades":  total_trades,
        "total_notional": total_notional,
        "avg_size_usd":  avg_size_usd,
        "maker_pct":     maker_pct,
        "taker_pct":     100 - maker_pct,
        "net_pnl":       net_pnl,
        "total_pnl":     total_pnl,
        "total_fees":    total_fees,
        "directions":    directions,
        "markets":       markets,
        "avg_hold_secs": avg_hold_secs,
        "hold_style":    hold_style,
        "style":         style,
        "long_bias":     long_bias,
        "peak_hour_utc": peak_hour,
        "fills":         window,
    }


# ── Display ───────────────────────────────────────────────────────────────────

def build_analysis_panel(result: dict) -> Panel:
    addr    = result["address"]
    hours   = result["hours"]
    style   = result["style"]
    hold    = result["hold_style"]
    bias    = result["long_bias"]
    mkr     = result["maker_pct"]
    tkr     = result["taker_pct"]
    trades  = result["total_trades"]
    notional= result["total_notional"]
    avg_sz  = result["avg_size_usd"]
    net     = result["net_pnl"]
    fees    = result["total_fees"]
    peak    = result["peak_hour_utc"]
    markets = result["markets"]
    dirs    = result["directions"]
    hold_s  = result["avg_hold_secs"]

    color = "green" if net >= 0 else "red"

    tbl = Table(box=box.SIMPLE, show_header=False, padding=(0, 1))
    tbl.add_column("Field", style="dim", width=22)
    tbl.add_column("Value", style="bold")

    tbl.add_row("Address",        f"{addr[:12]}...{addr[-6:]}")
    tbl.add_row("Window",         f"Last {hours}h")
    tbl.add_row("Strategy",       f"[cyan]{style}[/cyan]")
    tbl.add_row("Hold Style",     f"[cyan]{hold}[/cyan]")
    tbl.add_row("Avg Hold Time",  f"{hold_s:.0f}s" if hold_s else "N/A")
    tbl.add_row("Trades",         f"{trades:,}")
    tbl.add_row("Volume",         f"${notional:,.0f}")
    tbl.add_row("Avg Trade Size", f"${avg_sz:,.0f}")
    tbl.add_row("Maker %",        f"{mkr:.1f}%")
    tbl.add_row("Taker %",        f"{tkr:.1f}%")
    tbl.add_row("Long Bias",      f"{'🟢' if bias > 55 else '🔴' if bias < 45 else '⚪'} {bias:.1f}% long")
    tbl.add_row("Net PnL",        f"[{color}]${net:+.4f}[/{color}]")
    tbl.add_row("Fees Paid",      f"${abs(fees):.4f}")
    tbl.add_row("Peak Hour UTC",  f"{peak:02d}:00" if peak is not None else "N/A")

    # Markets
    top_markets = sorted(markets.items(), key=lambda x: x[1], reverse=True)[:5]
    tbl.add_row("Top Markets",    ", ".join(f"{m}(${v:,.0f})" for m, v in top_markets))

    # Direction breakdown
    top_dirs = sorted(dirs.items(), key=lambda x: x[1], reverse=True)[:5]
    tbl.add_row("Directions",     ", ".join(f"{d}:{c}" for d, c in top_dirs))

    return Panel(tbl, title=f"[bold cyan]Wallet Analysis[/bold cyan]  [dim]{addr[:20]}...[/dim]",
                 border_style="cyan")


# ── CSV export ────────────────────────────────────────────────────────────────

def export_csv(fills: List[dict], address: str, hours: int):
    filename = f"fills_{address[:10]}_{hours}h_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    fieldnames = ["timestamp", "market", "side", "direction", "price", "size",
                  "notional", "closed_pnl", "fee", "crossed", "tx_hash"]

    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for fill in sorted(fills, key=lambda x: x.get("_ts") or datetime.min.replace(tzinfo=timezone.utc)):
            row = {
                "timestamp":  fill.get("_ts").isoformat() if fill.get("_ts") else "",
                "market":     fill.get("instrument") or fill.get("market", ""),
                "side":       fill.get("side", ""),
                "direction":  fill.get("direction", ""),
                "price":      fill.get("price", ""),
                "size":       fill.get("size", ""),
                "notional":   float(fill.get("price", 0)) * float(fill.get("size", 0)),
                "closed_pnl": fill.get("closed_pnl", ""),
                "fee":        fill.get("fee", ""),
                "crossed":    fill.get("crossed", ""),
                "tx_hash":    fill.get("tx_hash", ""),
            }
            writer.writerow(row)

    console.print(f"[green]CSV exported → {filename}[/green]")
    return filename


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        console.print(__doc__)
        sys.exit(0)

    export = "--csv" in args
    args   = [a for a in args if a != "--csv"]

    hours = 24
    if "--hours" in args:
        idx   = args.index("--hours")
        hours = int(args[idx + 1])
        args  = [a for a in args if a not in ("--hours", str(hours))]

    addresses = [a for a in args if a.startswith("0x")]
    if not addresses:
        console.print("[red]No wallet addresses provided.[/red]")
        sys.exit(1)

    info = InfoClient(is_testnet=False)
    info.transport.timeout = 30.0
    since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)

    for addr in addresses:
        console.rule(f"[cyan]{addr}[/cyan]")
        try:
            fills  = fetch_all_fills(info, addr, since=since)
            if not fills:
                console.print(f"[yellow]No fills found for {addr}[/yellow]")
                continue
            result = analyse(fills, addr, hours)
            if not result:
                console.print(f"[yellow]No data to analyse for {addr}[/yellow]")
                continue
            console.print(build_analysis_panel(result))

            if export:
                export_csv(result["fills"], addr, hours)

        except Exception as e:
            console.print(f"[red]Error processing {addr}: {e}[/red]")
            import traceback; traceback.print_exc()

    console.print("\n[dim]Done.[/dim]")


if __name__ == "__main__":
    main()
