"""
SQLite persistence layer for fills, account snapshots, and leaderboard.
DB file: ~/Downloads/python-sdk-main/bot/hotstuff.db
"""
import sqlite3
import os
import threading
from datetime import datetime
from typing import List, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "hotstuff.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialise DB, auto-recovering from corruption by deleting and recreating."""
    import shutil
    if os.path.exists(DB_PATH):
        try:
            test_conn = sqlite3.connect(DB_PATH)
            test_conn.execute("PRAGMA integrity_check")
            test_conn.close()
        except sqlite3.DatabaseError:
            backup = DB_PATH + ".corrupted"
            shutil.move(DB_PATH, backup)
            import logging
            logging.getLogger("db").warning(
                f"DB was corrupted — moved to {backup} and created fresh DB"
            )

    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS fills (
            trade_id     TEXT PRIMARY KEY,
            address      TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            market       TEXT NOT NULL,
            side         TEXT NOT NULL,
            direction    TEXT NOT NULL,
            price        REAL NOT NULL,
            size         REAL NOT NULL,
            notional     REAL NOT NULL,
            closed_pnl   REAL NOT NULL,
            fee          REAL NOT NULL,
            tx_hash      TEXT,
            cloid        TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            address         TEXT NOT NULL,
            timestamp       TEXT NOT NULL,
            equity          REAL,
            balance         REAL,
            upnl            REAL,
            total_pnl       REAL,
            available       REAL,
            im_utilization  REAL,
            volume          REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            address     TEXT NOT NULL,
            volume      REAL NOT NULL,
            trades      INTEGER NOT NULL,
            markets     TEXT NOT NULL
        )
    """)

    # Indexes
    c.execute("""
        CREATE TABLE IF NOT EXISTS markouts (
            cloid       TEXT PRIMARY KEY,
            address     TEXT NOT NULL,
            market      TEXT NOT NULL,
            side        TEXT NOT NULL,
            direction   TEXT NOT NULL,
            fill_price  REAL NOT NULL,
            fill_time   REAL NOT NULL,
            mid_10s     REAL,
            mid_30s     REAL,
            mid_60s     REAL,
            mid_5m      REAL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS placed_orders (
            cloid        TEXT PRIMARY KEY,
            address      TEXT NOT NULL,
            market       TEXT NOT NULL,
            side         TEXT NOT NULL,
            price        REAL NOT NULL,
            placed_at    REAL NOT NULL,
            placement_ms REAL
        )
    """)

    c.execute("CREATE INDEX IF NOT EXISTS idx_fills_address ON fills(address)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fills_market  ON fills(market)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_fills_ts      ON fills(timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_addr ON account_snapshots(address)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_lb_ts          ON leaderboard_snapshots(timestamp)")

    # Prune placed_orders older than 8 days (full epoch + 1 day buffer)
    import time as _time
    cutoff = _time.time() - 8 * 86400
    c.execute("DELETE FROM placed_orders WHERE placed_at < ?", (cutoff,))
    conn.commit()
    conn.close()


_lock = threading.Lock()


def save_fills(fills: List[dict]):
    """Upsert fills — skips duplicates by trade_id."""
    if not fills:
        return
    with _lock:
        conn = get_conn()
        c = conn.cursor()
        inserted = 0
        for f in fills:
            trade_id = str(f.get("trade_id", ""))
            if not trade_id:
                continue
            try:
                c.execute("""
                    INSERT OR IGNORE INTO fills
                    (trade_id, address, timestamp, market, side, direction,
                     price, size, notional, closed_pnl, fee, tx_hash, cloid)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    trade_id,
                    f.get("account", ""),
                    str(f.get("block_timestamp", "")),
                    f.get("instrument", ""),
                    f.get("side", ""),
                    f.get("direction", ""),
                    float(f.get("price", 0)),
                    float(f.get("size", 0)),
                    float(f.get("notional_value") or 0) or abs(float(f.get("size", 0)) * float(f.get("price", 0))),
                    float(f.get("closed_pnl", 0)),
                    float(f.get("fee", 0)),
                    f.get("tx_hash", ""),
                    f.get("cloid", ""),
                ))
                inserted += c.rowcount
            except Exception:
                pass
        conn.commit()
        conn.close()
        return inserted


def save_snapshot(address: str, summary):
    """Save an account equity snapshot."""
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO account_snapshots
            (address, timestamp, equity, balance, upnl, total_pnl, available, im_utilization, volume)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            address,
            datetime.utcnow().isoformat(),
            getattr(summary, "total_account_equity", 0) or 0,
            getattr(summary, "margin_balance", 0) or 0,
            getattr(summary, "upnl", 0) or 0,
            getattr(summary, "total_pnl", 0) or 0,
            getattr(summary, "available_balance", 0) or 0,
            (getattr(summary, "initial_margin_utilization", 0) or 0) * 100,
            getattr(summary, "total_volume", 0) or 0,
        ))
        conn.commit()
        conn.close()


def save_leaderboard(traders: dict):
    """Snapshot current leaderboard rankings."""
    if not traders:
        return
    ts = datetime.utcnow().isoformat()
    with _lock:
        conn = get_conn()
        for addr, data in traders.items():
            conn.execute("""
                INSERT INTO leaderboard_snapshots (timestamp, address, volume, trades, markets)
                VALUES (?,?,?,?,?)
            """, (ts, addr, data["volume"], data["trades"], ",".join(sorted(data["markets"]))))
        conn.commit()
        conn.close()


def save_markout(cloid: str, address: str, market: str, side: str, direction: str,
                 fill_price: float, fill_time: float,
                 mid_10s: float = None, mid_30s: float = None,
                 mid_60s: float = None, mid_5m: float = None):
    """Save fill price and subsequent mid prices for markout analysis."""
    with _lock:
        conn = get_conn()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO markouts
                (cloid, address, market, side, direction, fill_price, fill_time,
                 mid_10s, mid_30s, mid_60s, mid_5m)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (cloid, address, market, side, direction, fill_price, fill_time,
                    mid_10s, mid_30s, mid_60s, mid_5m))
            conn.commit()
        except Exception:
            pass
        conn.close()


def get_markouts(address: str, market: str = None, limit: int = 200) -> List[dict]:
    conn = get_conn()
    query = "SELECT * FROM markouts WHERE address = ?"
    params = [address]
    if market:
        query += " AND market = ?"
        params.append(market)
    query += " ORDER BY fill_time DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def markout_stats(address: str) -> List[dict]:
    """Average markout by market and side."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT market, side, direction,
               COUNT(*) as fills,
               AVG((mid_10s - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) as markout_10s_bps,
               AVG((mid_30s - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) as markout_30s_bps,
               AVG((mid_60s - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) as markout_60s_bps,
               AVG((mid_5m  - fill_price) / fill_price * 100 * CASE WHEN side='b' THEN 1 ELSE -1 END) as markout_5m_bps
        FROM markouts
        WHERE address = ? AND mid_10s IS NOT NULL
        GROUP BY market, side, direction
        ORDER BY market, side
    """, (address,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_placed_order(cloid: str, address: str, market: str, side: str, price: float, placed_at: float, placement_ms: float = None):
    with _lock:
        conn = get_conn()
        try:
            conn.execute("""
                INSERT OR IGNORE INTO placed_orders (cloid, address, market, side, price, placed_at, placement_ms)
                VALUES (?,?,?,?,?,?,?)
            """, (cloid, address, market, side, price, placed_at, placement_ms))
            conn.commit()
        except Exception:
            pass
        conn.close()


def get_fill_latency(address: str, limit: int = 100) -> List[dict]:
    """Join fills with placed_orders on cloid to compute order-to-fill latency."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT f.market, f.side, f.direction,
               f.closed_pnl, f.price,
               (CAST(SUBSTR(f.timestamp, 1, 10) AS REAL) - p.placed_at) AS latency_sec,
               f.timestamp
        FROM fills f
        JOIN placed_orders p ON f.cloid = p.cloid
        WHERE f.address = ?
        ORDER BY f.timestamp DESC
        LIMIT ?
    """, (address, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latency_stats_by_market(address: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT f.market,
               COUNT(*)                                                       AS fills,
               AVG((CAST(SUBSTR(f.timestamp, 1, 10) AS REAL) - p.placed_at)) AS avg_latency_sec,
               MIN((CAST(SUBSTR(f.timestamp, 1, 10) AS REAL) - p.placed_at)) AS min_latency_sec,
               MAX((CAST(SUBSTR(f.timestamp, 1, 10) AS REAL) - p.placed_at)) AS max_latency_sec
        FROM fills f
        JOIN placed_orders p ON f.cloid = p.cloid
        WHERE f.address = ?
        GROUP BY f.market
    """, (address,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Analytics queries ─────────────────────────────────────────────────────────

def pnl_by_market(address: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT market,
               SUM(closed_pnl)  AS total_pnl,
               SUM(fee)         AS total_fee,
               SUM(closed_pnl + fee) AS net_pnl,
               COUNT(*)         AS trades,
               SUM(notional)    AS volume
        FROM fills WHERE address = ?
        GROUP BY market ORDER BY net_pnl DESC
    """, (address,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def pnl_by_direction(address: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT direction,
               SUM(closed_pnl)  AS total_pnl,
               SUM(fee)         AS total_fee,
               SUM(closed_pnl + fee) AS net_pnl,
               COUNT(*)         AS trades
        FROM fills WHERE address = ?
        GROUP BY direction ORDER BY net_pnl DESC
    """, (address,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def pnl_over_time(address: str, bucket: str = "hour") -> List[dict]:
    """bucket: 'hour' or 'day'"""
    fmt = "%Y-%m-%dT%H" if bucket == "hour" else "%Y-%m-%d"
    conn = get_conn()
    rows = conn.execute(f"""
        SELECT SUBSTR(timestamp, 1, {'13' if bucket == 'hour' else '10'}) AS period,
               SUM(closed_pnl)  AS total_pnl,
               SUM(fee)         AS total_fee,
               SUM(closed_pnl + fee) AS net_pnl,
               COUNT(*)         AS trades
        FROM fills WHERE address = ?
        GROUP BY period ORDER BY period ASC
    """, (address,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fee_analysis(address: str) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT market,
               SUM(fee)                         AS total_fee,
               SUM(closed_pnl)                  AS gross_pnl,
               SUM(closed_pnl + fee)            AS net_pnl,
               AVG(ABS(fee) / notional * 100)   AS avg_fee_pct,
               COUNT(*)                         AS trades
        FROM fills WHERE address = ? AND notional > 0
        GROUP BY market ORDER BY total_fee ASC
    """, (address,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def equity_history(address: str, limit: int = 100) -> List[dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT timestamp, equity, upnl, total_pnl, im_utilization
        FROM account_snapshots WHERE address = ?
        ORDER BY timestamp DESC LIMIT ?
    """, (address, limit)).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]
