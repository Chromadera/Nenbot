"""
db.py — SQLite database for NenMMBot user management.

Tables:
    users       — registered wallets, keyed by (telegram_id, label)
    key_expiry  — agent key expiry tracking and alerts per wallet

Agent key lifespan: 180 days max.
Alert schedule:
    Day 160 — first warning
    Day 170 — second warning
    Day 180 — bot auto-pauses

Multi-wallet: each user can have up to 2 wallets.
Labels: user-defined, case-insensitive, max 10 chars, alphanumeric + underscore.
Markets: one per wallet — BTC-PERP, ETH-PERP, SOL-PERP, HYPE-PERP, XRP-PERP, ZEC-PERP, BNB-PERP, GOLD-PERP, SILVER-PERP, BRENTOIL-PERP, WTIOIL-PERP, NATGAS-PERP, X-PERP
"""
from __future__ import annotations

import sqlite3
import re
import os
import time
from typing import Optional, List

DB_PATH = os.path.join(os.path.dirname(__file__), "users.db")

KEY_EXPIRY_DAYS  = 180
KEY_ALERT_1_DAYS = 160
KEY_ALERT_2_DAYS = 170
MAX_WALLETS      = 2

SUPPORTED_MARKETS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "XRP-PERP", "ZEC-PERP", "BNB-PERP", "GOLD-PERP", "SILVER-PERP", "BRENTOIL-PERP", "WTIOIL-PERP", "NATGAS-PERP", "X-PERP", "EURUSD-PERP", "USDJPY-PERP", "USA500-PERP", "USA100-PERP"]

LABEL_RE = re.compile(r'^[a-z0-9_]{1,10}$')


def validate_label(label: str) -> str:
    label = label.strip().lower()
    if not LABEL_RE.match(label):
        raise ValueError("Label must be 1-10 chars: letters, numbers, underscores only. No spaces.")
    return label


def validate_market(market: str) -> str:
    market = market.strip().upper()
    if "-" not in market:
        market = f"{market}-PERP"
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"Unsupported market: {market}. Choose from: {', '.join(SUPPORTED_MARKETS)}")
    return market


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id           INTEGER NOT NULL,
                label                 TEXT    NOT NULL,
                telegram_username     TEXT,
                wallet_address        TEXT    NOT NULL,
                encrypted_agent_key   TEXT    NOT NULL,
                market                TEXT    NOT NULL DEFAULT 'BTC-PERP',
                profile               TEXT    NOT NULL DEFAULT 'balanced',
                state                 TEXT    NOT NULL DEFAULT 'pending',
                key_created_at        REAL    NOT NULL,
                created_at            REAL    NOT NULL,
                updated_at            REAL    NOT NULL,
                order_size_usd        REAL,
                max_inventory_usd     REAL,
                max_daily_loss_usd    REAL,
                leverage              INTEGER,
                spread_bps            REAL,
                close_spread_bps      REAL,
                allow_flips           INTEGER,
                stop_loss_pct         REAL,
                fill_cooldown_open_s  REAL,
                fill_cooldown_close_s REAL,
                tod_08_multiplier     REAL,
                tod_13_multiplier     REAL,
                tod_14_multiplier     REAL,
                adx_threshold         REAL,
                price_move_threshold  REAL,
                order_ttl_ms          REAL,
                requote_cooldown      REAL,
                profit_target_bps     REAL,
                adverse_selection_bps REAL,
                signal_conflict_bps   REAL,
                grid_overshoot_mult   REAL,
                grid_max_loss_pct     REAL,
                last_resume_at        REAL,
                PRIMARY KEY (telegram_id, label)
            );

            CREATE TABLE IF NOT EXISTS key_expiry (
                telegram_id      INTEGER NOT NULL,
                label            TEXT    NOT NULL,
                key_created_at   REAL    NOT NULL,
                alert_160_sent   INTEGER NOT NULL DEFAULT 0,
                alert_170_sent   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (telegram_id, label),
                FOREIGN KEY (telegram_id, label) REFERENCES users(telegram_id, label)
            );

            CREATE INDEX IF NOT EXISTS idx_users_wallet   ON users(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_users_state    ON users(state);
            CREATE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_users_market   ON users(market);
        """)
    print(f"DB initialised at {DB_PATH}")


def add_wallet(
    telegram_id:         int,
    label:               str,
    telegram_username:   str,
    wallet_address:      str,
    encrypted_agent_key: str,
    market:              str = "BTC-PERP",
    profile:             str = "balanced",
) -> bool:
    label  = validate_label(label)
    market = validate_market(market)
    now    = time.time()
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()[0]
        if count >= MAX_WALLETS:
            return False
        try:
            conn.execute("""
                INSERT INTO users
                    (telegram_id, label, telegram_username, wallet_address,
                     encrypted_agent_key, market, profile, state,
                     key_created_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """, (telegram_id, label, telegram_username, wallet_address,
                  encrypted_agent_key, market, profile, now, now, now))
            conn.execute("""
                INSERT INTO key_expiry (telegram_id, label, key_created_at)
                VALUES (?, ?, ?)
            """, (telegram_id, label, now))
            return True
        except sqlite3.IntegrityError:
            return False


def get_wallet(telegram_id: int, label: str) -> Optional[sqlite3.Row]:
    label = validate_label(label)
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ? AND label = ?",
            (telegram_id, label)
        ).fetchone()


def get_user_wallets(telegram_id: int) -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ? ORDER BY created_at",
            (telegram_id,)
        ).fetchall()


def get_all_active_wallets() -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE state = 'active'"
        ).fetchall()


def set_wallet_state(telegram_id: int, label: str, state: str):
    label = validate_label(label)
    with get_conn() as conn:
        conn.execute("""
            UPDATE users SET state = ?, updated_at = ?
            WHERE telegram_id = ? AND label = ?
        """, (state, time.time(), telegram_id, label))


def set_wallet_market(telegram_id: int, label: str, market: str) -> bool:
    """Update wallet market. Returns False if market invalid."""
    try:
        market = validate_market(market)
    except ValueError:
        return False
    label = validate_label(label)
    with get_conn() as conn:
        conn.execute("""
            UPDATE users SET market = ?, updated_at = ?
            WHERE telegram_id = ? AND label = ?
        """, (market, time.time(), telegram_id, label))
    return True


def set_wallet_profile(telegram_id: int, label: str, profile: str):
    """Switch profile and clear all expert mode overrides."""
    label = validate_label(label)
    with get_conn() as conn:
        conn.execute("""
            UPDATE users
            SET profile = ?,
                order_size_usd = NULL, max_inventory_usd = NULL,
                max_daily_loss_usd = NULL, leverage = NULL,
                spread_bps = NULL, close_spread_bps = NULL,
                allow_flips = NULL, stop_loss_pct = NULL,
                fill_cooldown_open_s = NULL, fill_cooldown_close_s = NULL,
                tod_08_multiplier = NULL, tod_13_multiplier = NULL,
                tod_14_multiplier = NULL, adx_threshold = NULL,
                updated_at = ?
            WHERE telegram_id = ? AND label = ?
        """, (profile, time.time(), telegram_id, label))


def update_agent_key(telegram_id: int, label: str, encrypted_agent_key: str):
    label = validate_label(label)
    now   = time.time()
    with get_conn() as conn:
        conn.execute("""
            UPDATE users
            SET encrypted_agent_key = ?, key_created_at = ?, updated_at = ?
            WHERE telegram_id = ? AND label = ?
        """, (encrypted_agent_key, now, now, telegram_id, label))
        conn.execute("""
            UPDATE key_expiry
            SET key_created_at = ?, alert_160_sent = 0, alert_170_sent = 0
            WHERE telegram_id = ? AND label = ?
        """, (now, telegram_id, label))


def set_expert_overrides(telegram_id: int, label: str, **kwargs):
    """
    Set one or more expert mode overrides.
    Pass only the fields you want to update as kwargs.
    Valid fields: order_size_usd, max_inventory_usd, max_daily_loss_usd,
                  leverage, spread_bps, close_spread_bps, allow_flips,
                  stop_loss_pct, fill_cooldown_open_s, fill_cooldown_close_s,
                  tod_08_multiplier, tod_13_multiplier, tod_14_multiplier,
                  adx_threshold
    """
    label = validate_label(label)
    valid_fields = {
        "order_size_usd", "max_inventory_usd", "max_daily_loss_usd",
        "leverage", "spread_bps", "close_spread_bps", "allow_flips",
        "stop_loss_pct", "fill_cooldown_open_s", "fill_cooldown_close_s",
        "tod_08_multiplier", "tod_13_multiplier", "tod_14_multiplier",
        "adx_threshold",
        "price_move_threshold",
        "order_ttl_ms",
        "requote_cooldown",
        "profit_target_bps",
        "adverse_selection_bps",
        "signal_conflict_bps",
        "grid_spacing_mult",
        "grid_levels",
        "grid_vshape_alpha",
        "grid_min_spacing_pct",
        "grid_overshoot_mult",
        "grid_max_loss_pct",
    }
    updates = {k: v for k, v in kwargs.items() if k in valid_fields}
    if not updates:
        return
    updates["updated_at"] = time.time()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values     = list(updates.values()) + [telegram_id, label]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE users SET {set_clause} WHERE telegram_id = ? AND label = ?",
            values
        )


def reset_expert_overrides(telegram_id: int, label: str):
    """Clear all expert overrides — reverts to profile defaults."""
    label = validate_label(label)
    with get_conn() as conn:
        conn.execute("""
            UPDATE users
            SET order_size_usd = NULL, max_inventory_usd = NULL,
                max_daily_loss_usd = NULL, leverage = NULL,
                spread_bps = NULL, close_spread_bps = NULL,
                allow_flips = NULL, stop_loss_pct = NULL,
                fill_cooldown_open_s = NULL, fill_cooldown_close_s = NULL,
                tod_08_multiplier = NULL, tod_13_multiplier = NULL,
                tod_14_multiplier = NULL, adx_threshold = NULL,
                price_move_threshold = NULL, order_ttl_ms = NULL,
                requote_cooldown = NULL, profit_target_bps = NULL,
                adverse_selection_bps = NULL,
                signal_conflict_bps = NULL,
                grid_overshoot_mult = NULL, grid_max_loss_pct = NULL,
                updated_at = ?
            WHERE telegram_id = ? AND label = ?
        """, (time.time(), telegram_id, label))


def record_resume(telegram_id: int, label: str):
    label = validate_label(label)
    with get_conn() as conn:
        conn.execute("""
            UPDATE users SET last_resume_at = ?, updated_at = ?
            WHERE telegram_id = ? AND label = ?
        """, (time.time(), time.time(), telegram_id, label))


def remove_wallet(telegram_id: int, label: str) -> bool:
    label = validate_label(label)
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM users WHERE telegram_id = ? AND label = ?",
            (telegram_id, label)
        )
        conn.execute(
            "DELETE FROM key_expiry WHERE telegram_id = ? AND label = ?",
            (telegram_id, label)
        )
        return cur.rowcount > 0


def get_expiring_keys(warning_days: int = KEY_ALERT_1_DAYS) -> List[sqlite3.Row]:
    cutoff    = time.time() - (warning_days * 86400)
    alert_col = "alert_160_sent" if warning_days == KEY_ALERT_1_DAYS else "alert_170_sent"
    with get_conn() as conn:
        return conn.execute(f"""
            SELECT u.*
            FROM users u
            JOIN key_expiry e ON u.telegram_id = e.telegram_id AND u.label = e.label
            WHERE e.key_created_at <= ? AND e.{alert_col} = 0 AND u.state = 'active'
        """, (cutoff,)).fetchall()


def mark_expiry_alert_sent(telegram_id: int, label: str, warning_days: int):
    label     = validate_label(label)
    alert_col = "alert_160_sent" if warning_days == KEY_ALERT_1_DAYS else "alert_170_sent"
    with get_conn() as conn:
        conn.execute(f"""
            UPDATE key_expiry SET {alert_col} = 1
            WHERE telegram_id = ? AND label = ?
        """, (telegram_id, label))


def get_expired_wallets() -> List[sqlite3.Row]:
    cutoff = time.time() - (KEY_EXPIRY_DAYS * 86400)
    with get_conn() as conn:
        return conn.execute("""
            SELECT u.* FROM users u
            JOIN key_expiry e ON u.telegram_id = e.telegram_id AND u.label = e.label
            WHERE e.key_created_at <= ? AND u.state = 'active'
        """, (cutoff,)).fetchall()


if __name__ == "__main__":
    init_db()
    with get_conn() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        print(f"Tables: {[t['name'] for t in tables]}")
