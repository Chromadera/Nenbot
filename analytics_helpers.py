import os
import sqlite3
from datetime import datetime, timezone


def _analytics_since(period: str) -> float:
    now = datetime.now(timezone.utc)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elif period == "7d":
        return now.timestamp() - 7 * 86400
    else:
        return 0.0


def _analytics_db(bot_dir: str, wallet_address: str, since: float) -> dict:
    db_path = os.path.join(bot_dir, "bot", "hotstuff.db")
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        ts_filter = (
            "CASE WHEN timestamp LIKE '%T%' "
            "THEN CAST(strftime('%s', timestamp) AS REAL) >= ? "
            "ELSE CAST(timestamp AS REAL) / 1000 >= ? END"
        )

        r = conn.execute(
            "SELECT COUNT(*) trades, SUM(notional) volume, "
            "SUM(closed_pnl) pnl, SUM(fee) fees, "
            "SUM(closed_pnl + fee) net_pnl "
            "FROM fills WHERE address=? AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        best = conn.execute(
            "SELECT closed_pnl + fee as net, timestamp FROM fills "
            "WHERE address=? AND " + ts_filter + " ORDER BY net DESC LIMIT 1",
            (wallet_address, since, since)
        ).fetchone()

        worst = conn.execute(
            "SELECT closed_pnl + fee as net, timestamp FROM fills "
            "WHERE address=? AND " + ts_filter + " ORDER BY net ASC LIMIT 1",
            (wallet_address, since, since)
        ).fetchone()

        wins = conn.execute(
            "SELECT COUNT(*) wins FROM fills "
            "WHERE address=? AND direction IN ('closeShort','closeLong') "
            "AND closed_pnl + fee > 0 AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        total_closes = conn.execute(
            "SELECT COUNT(*) total FROM fills "
            "WHERE address=? AND direction IN ('closeShort','closeLong') "
            "AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        recent = conn.execute(
            "SELECT closed_pnl + fee as net FROM fills "
            "WHERE address=? AND direction IN ('closeShort','closeLong') "
            "AND " + ts_filter + " ORDER BY timestamp DESC LIMIT 20",
            (wallet_address, since, since)
        ).fetchall()

        streak = 0
        if recent:
            sign = 1 if recent[0]["net"] > 0 else -1
            for row in recent:
                if (1 if row["net"] > 0 else -1) == sign:
                    streak += 1
                else:
                    break

        directions = conn.execute(
            "SELECT direction, SUM(closed_pnl + fee) net_pnl, COUNT(*) trades "
            "FROM fills WHERE address=? AND " + ts_filter + " "
            "GROUP BY direction ORDER BY direction",
            (wallet_address, since, since)
        ).fetchall()

        fee_eff = conn.execute(
            "SELECT SUM(fee) total_fee, SUM(closed_pnl) gross_pnl, "
            "AVG(ABS(fee) / NULLIF(notional, 0) * 100) avg_fee_pct "
            "FROM fills WHERE address=? AND notional > 0 AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        hourly = conn.execute(
            "SELECT CAST(CASE "
            "WHEN timestamp LIKE '%T%' THEN SUBSTR(timestamp, 12, 2) "
            "ELSE CAST(CAST(timestamp AS REAL)/1000/3600%24 AS INTEGER) "
            "END AS INTEGER) AS hour, "
            "SUM(notional) vol, SUM(closed_pnl + fee) net_pnl, COUNT(*) trades "
            "FROM fills WHERE address=? AND " + ts_filter + " "
            "GROUP BY hour ORDER BY hour",
            (wallet_address, since, since)
        ).fetchall()

        markouts = conn.execute(
            "SELECT "
            "AVG((mid_10s - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) m10, "
            "AVG((mid_30s - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) m30, "
            "AVG((mid_60s - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) m60, "
            "AVG((mid_5m  - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) m5m, "
            "COUNT(*) cnt "
            "FROM markouts WHERE address=? AND mid_10s IS NOT NULL",
            (wallet_address,)
        ).fetchone()

        spread_capture = None
        if markouts and markouts["cnt"] > 5 and markouts["m60"] is not None:
            spread_capture = markouts["m60"]

        latency = conn.execute(
            "SELECT AVG(CAST(SUBSTR(f.timestamp,1,10) AS REAL) - p.placed_at) avg_lat, "
            "MIN(CAST(SUBSTR(f.timestamp,1,10) AS REAL) - p.placed_at) min_lat, "
            "MAX(CAST(SUBSTR(f.timestamp,1,10) AS REAL) - p.placed_at) max_lat, "
            "COUNT(*) cnt "
            "FROM fills f JOIN placed_orders p ON f.cloid = p.cloid "
            "WHERE f.address=?",
            (wallet_address,)
        ).fetchone()

        equity_rows = conn.execute(
            "SELECT equity, timestamp FROM account_snapshots "
            "WHERE address=? ORDER BY timestamp ASC",
            (wallet_address,)
        ).fetchall()

        conn.close()

        equity_start = equity_rows[0]["equity"] if equity_rows else None
        equity_peak  = max(row["equity"] for row in equity_rows) if equity_rows else None
        equity_now   = equity_rows[-1]["equity"] if equity_rows else None
        max_dd = None
        if equity_peak and equity_now and equity_peak > 0:
            max_dd = (equity_now - equity_peak) / equity_peak * 100

        return {
            "trades":         r["trades"] or 0,
            "volume":         r["volume"] or 0,
            "pnl":            r["pnl"] or 0,
            "fees":           r["fees"] or 0,
            "net_pnl":        r["net_pnl"] or 0,
            "gross_pnl":      fee_eff["gross_pnl"] or 0 if fee_eff else 0,
            "fee_total":      fee_eff["total_fee"] or 0 if fee_eff else 0,
            "fee_pct":        fee_eff["avg_fee_pct"] or 0 if fee_eff else 0,
            "best_trade":     {"net": best["net"], "ts": best["timestamp"]} if best and best["net"] else None,
            "worst_trade":    {"net": worst["net"], "ts": worst["timestamp"]} if worst and worst["net"] else None,
            "win_rate":       round(wins["wins"] / total_closes["total"] * 100) if total_closes and total_closes["total"] > 0 else None,
            "streak":         (streak, "W" if recent and recent[0]["net"] > 0 else "L") if streak else None,
            "directions":     [dict(d) for d in directions],
            "hourly":         [dict(h) for h in hourly],
            "markout":        dict(markouts) if markouts and markouts["cnt"] > 0 else None,
            "spread_capture": spread_capture,
            "latency":        dict(latency) if latency and latency["cnt"] > 0 else None,
            "equity_start":   equity_start,
            "equity_peak":    equity_peak,
            "equity_now":     equity_now,
            "max_drawdown":   max_dd,
        }
    except Exception as e:
        import logging
        logging.getLogger("telegram_bot").error("Analytics DB error: " + str(e))
        return {}


def _fmt_ts(ts_str) -> str:
    try:
        if "T" in str(ts_str):
            dt = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        else:
            dt = datetime.utcfromtimestamp(int(ts_str) / 1000)
        return dt.strftime("%d %b %H:%M UTC")
    except Exception:
        return str(ts_str)[:16]


def _markout_interpretation(m10, m30, m60, m5m) -> str:
    if m60 is None:
        return "Insufficient data."
    if m10 < -0.3 and m60 < -0.1:
        return "Getting picked off consistently. Widen spread by 0.3-0.5bps."
    elif m10 < -0.1 and m60 >= 0:
        return "Picked off short-term but price recovers. Spread slightly tight. Widen by 0.1-0.2bps."
    elif m10 >= 0 and m5m >= 0:
        return "Good spread capture. Minimal adverse selection detected."
    elif m5m < -0.3:
        return "Strong adverse selection at 5m. Consider raising ADX threshold or widening spread."
    else:
        return "Mixed signal. Monitor over more fills before adjusting."


def _latency_interpretation(avg: float) -> str:
    if avg < 1.0:
        return "Excellent. Sub-second fill latency."
    elif avg < 3.0:
        return "Healthy. No exchange lag detected."
    elif avg < 8.0:
        return "Moderate lag. Check VPS load and feed health."
    else:
        return "High latency detected. Check nenfeed.service and VPS."


def _hour_bar(vol: float, max_vol: float, width: int = 16) -> str:
    if max_vol == 0:
        return chr(9617) * width
    filled = round(vol / max_vol * width)
    return chr(9608) * filled + " " * (width - filled)


def _build_performance_text(label: str, market: str, period_label: str, d: dict, pts: dict) -> str:
    if not d or d.get("trades", 0) == 0:
        return "*" + label.upper() + "* - " + market + "\n\nNo trade data for this period."

    ul = label.upper()
    lines = [chr(128202) + " *" + ul + "* - `" + market + "` - " + period_label + "\n"]

    win_str = (" - Win Rate " + str(d["win_rate"]) + "%") if d["win_rate"] is not None else ""
    streak_str = ""
    if d["streak"]:
        cnt, sw = d["streak"]
        icon = chr(128293) if sw == "W" else chr(10052)
        streak_str = " - " + icon + " " + str(cnt) + sw

    lines.append(chr(128176) + " *PnL and Volume*")
    lines.append("  Gross `$" + "{:+.2f}".format(d["gross_pnl"]) + "` - Fees `$" + "{:.2f}".format(d["fees"]) + "` - Net `$" + "{:+.2f}".format(d["net_pnl"]) + "`")
    lines.append("  Vol `$" + "{:,.0f}".format(d["volume"]) + "` - Trades `" + str(d["trades"]) + "`" + win_str + streak_str)

    if d.get("best_trade"):
        lines.append("\n" + chr(128200) + " Best Trade:  `$" + "{:+.2f}".format(d["best_trade"]["net"]) + "` - " + _fmt_ts(d["best_trade"]["ts"]))
    if d.get("worst_trade"):
        lines.append(chr(128201) + " Worst Trade: `$" + "{:+.2f}".format(d["worst_trade"]["net"]) + "` - " + _fmt_ts(d["worst_trade"]["ts"]))

    if d.get("hourly"):
        hs = sorted(d["hourly"], key=lambda x: x["net_pnl"] or 0, reverse=True)
        bh = hs[0]
        wh = hs[-1]
        lines.append("\n" + chr(127942) + " Best Hour:  `" + "{:02d}:00 UTC".format(int(bh["hour"])) + "` - `$" + "{:+.2f}".format(bh["net_pnl"] or 0) + "` net")
        lines.append(chr(128128) + " Worst Hour: `" + "{:02d}:00 UTC".format(int(wh["hour"])) + "` - `$" + "{:+.2f}".format(wh["net_pnl"] or 0) + "` net")

    dir_map = {"openLong": "Open Long", "closeLong": "Close Long", "openShort": "Open Short", "closeShort": "Close Short"}
    if d.get("directions"):
        lines.append("\n" + chr(128260) + " *Direction Breakdown* _\\(net after fees\\)_")
        for drow in d["directions"]:
            name = dir_map.get(drow["direction"], drow["direction"])
            lines.append("  " + "{:<12}".format(name) + " `$" + "{:+.2f}".format(drow["net_pnl"] or 0) + "` - " + str(drow["trades"]) + " trades")

    lines.append("\n" + chr(128184) + " *Fee Efficiency*")
    lines.append("  Total `$" + "{:.2f}".format(d["fee_total"]) + "` - Avg `" + "{:.4f}".format(d["fee_pct"]) + "%`/trade")
    if d.get("gross_pnl") and d["gross_pnl"] != 0:
        fee_ratio = abs(d["fee_total"]) / abs(d["gross_pnl"]) * 100
        lines.append("  Fees as pct of Gross PnL: `" + "{:.1f}".format(fee_ratio) + "%`")

    total_pts = pts.get("total_points", 0)
    if total_pts > 0 and d["volume"] > 0:
        usd_per_pt = d["volume"] / total_pts
        lines.append("\n" + chr(11088) + " *Points Efficiency*")
        lines.append("  `" + "{:,}".format(total_pts) + "` pts - `$" + "{:,.0f}".format(d["volume"]) + "` vol - `$" + "{:,.0f}".format(usd_per_pt) + "`/pt")

    if d.get("hourly"):
        max_vol = max(h["vol"] or 0 for h in d["hourly"])
        lines.append("\n" + chr(128336) + " *Activity by Hour \\(UTC\\)*")
        lines.append("```")
        for h in d["hourly"]:
            bar = _hour_bar(h["vol"] or 0, max_vol)
            net = h["net_pnl"] or 0
            vol_k = (h["vol"] or 0) / 1000
            lines.append("{:02d} |{}| ${:.1f}k  ${:+.2f}".format(int(h["hour"]), bar, vol_k, net))
        lines.append("```")

    return "\n".join(lines)


def _build_quality_text(label: str, market: str, period_label: str, d: dict) -> str:
    if not d:
        return chr(127919) + " *" + label.upper() + "* - `" + market + "`\n\nNo data available."

    lines = [chr(127919) + " *" + label.upper() + "* - `" + market + "` - " + period_label + "\n"]

    m = d.get("markout")
    if m and m.get("cnt", 0) > 5:
        m10 = m.get("m10") or 0
        m30 = m.get("m30") or 0
        m60 = m.get("m60") or 0
        m5m = m.get("m5m") or 0
        lines.append(chr(128225) + " *Adverse Selection*")
        lines.append("  10s `" + "{:+.3f}".format(m10) + "bps`  30s `" + "{:+.3f}".format(m30) + "bps`")
        lines.append("  60s `" + "{:+.3f}".format(m60) + "bps`   5m `" + "{:+.3f}".format(m5m) + "bps`")
        lines.append("\n  -> _" + _markout_interpretation(m10, m30, m60, m5m) + "_")
    else:
        lines.append(chr(128225) + " *Adverse Selection*\n  _Insufficient markout data \\(need 5\\+ fills\\)_")

    sc = d.get("spread_capture")
    if sc is not None:
        lines.append("\n" + chr(127919) + " *Spread Capture Rate*")
        if sc > 0:
            lines.append("  Avg 60s markout `" + "{:+.3f}".format(sc) + "bps` -> capturing spread")
            lines.append("  -> _Good. Spread calibrated correctly._")
        elif sc > -0.2:
            lines.append("  Avg 60s markout `" + "{:+.3f}".format(sc) + "bps` -> marginal capture")
            lines.append("  -> _Borderline. Monitor adverse selection._")
        else:
            lines.append("  Avg 60s markout `" + "{:+.3f}".format(sc) + "bps` -> losing to toxic flow")
            lines.append("  -> _Widen spread or raise Price Move Threshold._")

    lat = d.get("latency")
    if lat and lat.get("cnt", 0) > 0:
        avg = lat.get("avg_lat") or 0
        mn  = lat.get("min_lat") or 0
        mx  = lat.get("max_lat") or 0
        lines.append("\n" + chr(9889) + " *Fill Latency*")
        lines.append("  Avg `" + "{:.1f}".format(avg) + "s` - Min `" + "{:.1f}".format(mn) + "s` - Max `" + "{:.1f}".format(mx) + "s`")
        lines.append("  -> _" + _latency_interpretation(avg) + "_")

    es  = d.get("equity_start")
    ep  = d.get("equity_peak")
    en  = d.get("equity_now")
    mdd = d.get("max_drawdown")
    if es is not None and en is not None:
        lines.append("\n" + chr(128200) + " *Equity Curve*")
        lines.append("  Start `$" + "{:.2f}".format(es) + "` - Current `$" + "{:.2f}".format(en) + "` - Peak `$" + "{:.2f}".format(ep) + "`")
        if mdd is not None:
            lines.append("  Max Drawdown: `" + "{:.1f}".format(mdd) + "%`")
            if mdd < -50:
                lines.append("  -> _Severe drawdown. Review max daily loss and position sizing._")
            elif mdd < -20:
                lines.append("  -> _Significant drawdown. Monitor closely._")
            else:
                lines.append("  -> _Drawdown within acceptable range._")

    return "\n".join(lines)


def _kb_analytics(label: str, section: str, period: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(chr(128202) + " Performance",    callback_data="analytics:perf:" + period + ":" + label),
            InlineKeyboardButton(chr(127919) + " Market Quality", callback_data="analytics:qual:" + period + ":" + label),
        ],
        [
            InlineKeyboardButton("Today",    callback_data="analytics:" + section + ":today:" + label),
            InlineKeyboardButton("7 Days",   callback_data="analytics:" + section + ":7d:" + label),
            InlineKeyboardButton("All Time", callback_data="analytics:" + section + ":alltime:" + label),
        ],
    ])
