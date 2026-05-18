"""
db_admin.py — Central admin database for NenMMBot.

Lives at /root/saas/admin.db (separate from users.db).
Stores admin-level audit data that spans all users:
  - suspension_log: every suspend/resume event with reason and trigger

Usage:
    from db_admin import log_suspension, get_suspensions, init_admin_db
"""

import sqlite3
import os
from datetime import datetime, timezone

ADMIN_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(ADMIN_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_admin_db():
    """Create admin DB tables if they don't exist. Safe to call on every startup."""
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS suspension_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tid           INTEGER NOT NULL,
                username      TEXT,
                label         TEXT NOT NULL,
                market        TEXT,
                action        TEXT NOT NULL CHECK(action IN ('suspended', 'resumed')),
                reason        TEXT NOT NULL,
                triggered_by  TEXT NOT NULL CHECK(triggered_by IN ('admin', 'user', 'system')),
                note          TEXT,
                timestamp     REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_susp_tid ON suspension_log(tid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_susp_ts  ON suspension_log(timestamp)")
        conn.commit()


def log_suspension(
    tid: int,
    label: str,
    action: str,          # 'suspended' | 'resumed'
    reason: str,          # 'manual' | 'crash' | 'market_change' | 'wallet_removal'
                          # | 'key_expired' | 'suspendall' | 'unsuspendall' | 'user_close'
    triggered_by: str,    # 'admin' | 'user' | 'system'
    username: str = None,
    market: str = None,
    note: str = None,
):
    """
    Write a suspension event to the central admin DB.
    Thread-safe — uses a fresh connection per call.
    Never raises — logs errors silently so callers are never broken by audit failures.
    """
    try:
        with _get_conn() as conn:
            conn.execute(
                """
                INSERT INTO suspension_log
                    (tid, username, label, market, action, reason, triggered_by, note, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tid,
                    username,
                    label,
                    market,
                    action,
                    reason,
                    triggered_by,
                    note,
                    datetime.now(timezone.utc).timestamp(),
                )
            )
            conn.commit()
    except Exception as e:
        import logging
        logging.getLogger("db_admin").error(f"log_suspension failed {tid}/{label}: {e}")


def get_suspensions(
    tid: int = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """
    Fetch suspension log entries, newest first.
    Optionally filter by tid.
    """
    with _get_conn() as conn:
        if tid is not None:
            rows = conn.execute(
                """
                SELECT * FROM suspension_log
                WHERE tid = ?
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (tid, limit, offset)
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM suspension_log
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset)
            ).fetchall()
    return [dict(r) for r in rows]


def count_suspensions(tid: int = None) -> int:
    with _get_conn() as conn:
        if tid is not None:
            return conn.execute(
                "SELECT COUNT(*) FROM suspension_log WHERE tid = ?", (tid,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM suspension_log").fetchone()[0]
