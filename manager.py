"""
manager.py — NenMMBot process manager.

Spawns, monitors, and stops individual bot instances per (telegram_id, label).
Each wallet gets its own directory: /root/bots/user_<telegram_id>_<label>/
"""
from __future__ import annotations

import os
import time
import shutil
import signal
import subprocess
import threading
import logging
from typing import Dict, Optional, Tuple

from db import (
    get_wallet, get_all_active_wallets, set_wallet_state,
    KEY_EXPIRY_DAYS, KEY_ALERT_1_DAYS, KEY_ALERT_2_DAYS,
    get_expiring_keys, get_expired_wallets,
    mark_expiry_alert_sent, record_resume, validate_label,
)
from vault import Vault
from db_admin import log_suspension, init_admin_db
init_admin_db()  # ensure admin DB schema exists on manager startup

log = logging.getLogger("manager")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)

SAAS_DIR     = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(SAAS_DIR, "bot_template")

def _feed_available(market: str) -> bool:
    """Check if the shared feed Unix socket exists and is connectable."""
    import socket as _sock
    path = f"/tmp/nenfeed_{market.replace('-','_')}.sock"
    if not os.path.exists(path):
        return False
    try:
        s = _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect(path)
        s.close()
        return True
    except Exception:
        return False

BOTS_DIR     = "/root/bots"
PYTHON       = "/usr/bin/python3"

ADMIN_TELEGRAM_ID = int(os.environ.get("NENBOT_ADMIN_ID", "0"))

BotKey = Tuple[int, str]

# ── Per-market profile defaults ───────────────────────────────────────────────
# Structure: MARKET_PROFILES[market][profile] = {env_key: value}
MARKET_PROFILES = {
    "BTC-PERP": {
        "conservative": {
            "BTC_ORDER_SIZE_USD":      "500",
            "BTC_MAX_INVENTORY_USD":   "1000",
            "BTC_SPREAD":              "0.0010",
            "BTC_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "20.0",
            "HOTSTUFF_LEVERAGE":       "50",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "BTC_ORDER_SIZE_USD":      "1000",
            "BTC_MAX_INVENTORY_USD":   "2000",
            "BTC_SPREAD":              "0.0006",
            "BTC_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "50.0",
            "HOTSTUFF_LEVERAGE":       "50",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "BTC_ORDER_SIZE_USD":      "2000",
            "BTC_MAX_INVENTORY_USD":   "4000",
            "BTC_SPREAD":              "0.0004",
            "BTC_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "100.0",
            "HOTSTUFF_LEVERAGE":       "50",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },
    "ETH-PERP": {
        "conservative": {
            "ETH_ORDER_SIZE_USD":      "500",
            "ETH_MAX_INVENTORY_USD":   "1000",
            "ETH_SPREAD":              "0.0010",
            "ETH_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "20.0",
            "HOTSTUFF_LEVERAGE":       "50",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "ETH_ORDER_SIZE_USD":      "1000",
            "ETH_MAX_INVENTORY_USD":   "2000",
            "ETH_SPREAD":              "0.0006",
            "ETH_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "50.0",
            "HOTSTUFF_LEVERAGE":       "50",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "ETH_ORDER_SIZE_USD":      "2000",
            "ETH_MAX_INVENTORY_USD":   "4000",
            "ETH_SPREAD":              "0.0004",
            "ETH_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "100.0",
            "HOTSTUFF_LEVERAGE":       "50",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },
    "SOL-PERP": {
        "conservative": {
            "SOL_ORDER_SIZE_USD":      "300",
            "SOL_MAX_INVENTORY_USD":   "600",
            "SOL_SPREAD":              "0.0015",
            "SOL_CLOSE_SPREAD":        "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS": "15.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "SOL_ORDER_SIZE_USD":      "500",
            "SOL_MAX_INVENTORY_USD":   "1000",
            "SOL_SPREAD":              "0.0010",
            "SOL_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "30.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "SOL_ORDER_SIZE_USD":      "1000",
            "SOL_MAX_INVENTORY_USD":   "2000",
            "SOL_SPREAD":              "0.0007",
            "SOL_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "60.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },
    "HYPE-PERP": {
        "conservative": {
            "HYPE_ORDER_SIZE_USD":     "300",
            "HYPE_MAX_INVENTORY_USD":  "600",
            "HYPE_SPREAD":             "0.0015",
            "HYPE_CLOSE_SPREAD":       "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS": "15.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "HYPE_ORDER_SIZE_USD":     "500",
            "HYPE_MAX_INVENTORY_USD":  "1000",
            "HYPE_SPREAD":             "0.0010",
            "HYPE_CLOSE_SPREAD":       "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "30.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "HYPE_ORDER_SIZE_USD":     "1000",
            "HYPE_MAX_INVENTORY_USD":  "2000",
            "HYPE_SPREAD":             "0.0007",
            "HYPE_CLOSE_SPREAD":       "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "60.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },
    "XRP-PERP": {
        "conservative": {
            "XRP_ORDER_SIZE_USD":      "300",
            "XRP_MAX_INVENTORY_USD":   "600",
            "XRP_SPREAD":              "0.0015",
            "XRP_CLOSE_SPREAD":        "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS": "15.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "XRP_ORDER_SIZE_USD":      "500",
            "XRP_MAX_INVENTORY_USD":   "1000",
            "XRP_SPREAD":              "0.0010",
            "XRP_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "30.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "XRP_ORDER_SIZE_USD":      "1000",
            "XRP_MAX_INVENTORY_USD":   "2000",
            "XRP_SPREAD":              "0.0007",
            "XRP_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "60.0",
            "HOTSTUFF_LEVERAGE":       "20",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },
    "ZEC-PERP": {
        "conservative": {
            "ZEC_ORDER_SIZE_USD":      "200",
            "ZEC_MAX_INVENTORY_USD":   "400",
            "ZEC_SPREAD":              "0.0020",
            "ZEC_CLOSE_SPREAD":        "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS": "10.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "ZEC_ORDER_SIZE_USD":      "300",
            "ZEC_MAX_INVENTORY_USD":   "600",
            "ZEC_SPREAD":              "0.0015",
            "ZEC_CLOSE_SPREAD":        "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS": "20.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "ZEC_ORDER_SIZE_USD":      "500",
            "ZEC_MAX_INVENTORY_USD":   "1000",
            "ZEC_SPREAD":              "0.0010",
            "ZEC_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "40.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },

    "BNB-PERP": {
        "conservative": {
            "BNB_ORDER_SIZE_USD":      "300",
            "BNB_MAX_INVENTORY_USD":   "600",
        },
        "balanced": {
            "BNB_ORDER_SIZE_USD":      "500",
            "BNB_MAX_INVENTORY_USD":   "1000",
        },
        "aggressive": {
            "BNB_ORDER_SIZE_USD":      "1000",
            "BNB_MAX_INVENTORY_USD":   "2000",
        },
    },

    "GOLD-PERP": {
        "conservative": {
            "GOLD_ORDER_SIZE_USD":     "200",
            "GOLD_MAX_INVENTORY_USD":  "400",
            "GOLD_SPREAD":             "0.0020",
            "GOLD_CLOSE_SPREAD":       "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS": "10.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "GOLD_ORDER_SIZE_USD":     "300",
            "GOLD_MAX_INVENTORY_USD":  "600",
            "GOLD_SPREAD":             "0.0015",
            "GOLD_CLOSE_SPREAD":       "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS": "20.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "GOLD_ORDER_SIZE_USD":     "500",
            "GOLD_MAX_INVENTORY_USD":  "1000",
            "GOLD_SPREAD":             "0.0010",
            "GOLD_CLOSE_SPREAD":       "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "40.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },

    "SILVER-PERP": {
        "conservative": {
            "SILVER_ORDER_SIZE_USD":   "200",
            "SILVER_MAX_INVENTORY_USD":"400",
            "SILVER_SPREAD":           "0.0020",
            "SILVER_CLOSE_SPREAD":     "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS": "10.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "30",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "balanced": {
            "SILVER_ORDER_SIZE_USD":   "300",
            "SILVER_MAX_INVENTORY_USD":"600",
            "SILVER_SPREAD":           "0.0015",
            "SILVER_CLOSE_SPREAD":     "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS": "20.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "2.5",
            "TOD_13_MULTIPLIER":       "2.0",
            "TOD_14_MULTIPLIER":       "2.0",
            "FILL_COOLDOWN_OPEN_S":    "20",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
        "aggressive": {
            "SILVER_ORDER_SIZE_USD":   "500",
            "SILVER_MAX_INVENTORY_USD":"1000",
            "SILVER_SPREAD":           "0.0010",
            "SILVER_CLOSE_SPREAD":     "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS": "40.0",
            "HOTSTUFF_LEVERAGE":       "10",
            "TOD_08_MULTIPLIER":       "1.5",
            "TOD_13_MULTIPLIER":       "1.2",
            "TOD_14_MULTIPLIER":       "1.2",
            "FILL_COOLDOWN_OPEN_S":    "10",
            "FILL_COOLDOWN_CLOSE_S":   "2",
        },
    },

    "BRENTOIL-PERP": {
        "conservative": {
            "BRENTOIL_ORDER_SIZE_USD":     "200",
            "BRENTOIL_MAX_INVENTORY_USD":  "400",
            "BRENTOIL_SPREAD":             "0.0020",
            "BRENTOIL_CLOSE_SPREAD":       "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":     "10.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "BRENTOIL_ORDER_SIZE_USD":     "300",
            "BRENTOIL_MAX_INVENTORY_USD":  "600",
            "BRENTOIL_SPREAD":             "0.0015",
            "BRENTOIL_CLOSE_SPREAD":       "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "BRENTOIL_ORDER_SIZE_USD":     "500",
            "BRENTOIL_MAX_INVENTORY_USD":  "1000",
            "BRENTOIL_SPREAD":             "0.0010",
            "BRENTOIL_CLOSE_SPREAD":       "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "40.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "WTIOIL-PERP": {
        "conservative": {
            "WTIOIL_ORDER_SIZE_USD":       "200",
            "WTIOIL_MAX_INVENTORY_USD":    "400",
            "WTIOIL_SPREAD":               "0.0020",
            "WTIOIL_CLOSE_SPREAD":         "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":     "10.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "WTIOIL_ORDER_SIZE_USD":       "300",
            "WTIOIL_MAX_INVENTORY_USD":    "600",
            "WTIOIL_SPREAD":               "0.0015",
            "WTIOIL_CLOSE_SPREAD":         "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "WTIOIL_ORDER_SIZE_USD":       "500",
            "WTIOIL_MAX_INVENTORY_USD":    "1000",
            "WTIOIL_SPREAD":               "0.0010",
            "WTIOIL_CLOSE_SPREAD":         "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "40.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "NATGAS-PERP": {
        "conservative": {
            "NATGAS_ORDER_SIZE_USD":       "200",
            "NATGAS_MAX_INVENTORY_USD":    "400",
            "NATGAS_SPREAD":               "0.0025",
            "NATGAS_CLOSE_SPREAD":         "0.0004",
            "HOTSTUFF_MAX_DAILY_LOSS":     "10.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "NATGAS_ORDER_SIZE_USD":       "300",
            "NATGAS_MAX_INVENTORY_USD":    "600",
            "NATGAS_SPREAD":               "0.0020",
            "NATGAS_CLOSE_SPREAD":         "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "NATGAS_ORDER_SIZE_USD":       "500",
            "NATGAS_MAX_INVENTORY_USD":    "1000",
            "NATGAS_SPREAD":               "0.0015",
            "NATGAS_CLOSE_SPREAD":         "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "40.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "X-PERP": {
        "conservative": {
            "X_ORDER_SIZE_USD":            "300",
            "X_MAX_INVENTORY_USD":         "600",
            "X_SPREAD":                    "0.0015",
            "X_CLOSE_SPREAD":              "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "15.0",
            "HOTSTUFF_LEVERAGE":           "20",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "X_ORDER_SIZE_USD":            "500",
            "X_MAX_INVENTORY_USD":         "1000",
            "X_SPREAD":                    "0.0010",
            "X_CLOSE_SPREAD":              "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "30.0",
            "HOTSTUFF_LEVERAGE":           "20",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "X_ORDER_SIZE_USD":            "1000",
            "X_MAX_INVENTORY_USD":         "2000",
            "X_SPREAD":                    "0.0007",
            "X_CLOSE_SPREAD":              "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "60.0",
            "HOTSTUFF_LEVERAGE":           "20",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "BRENTOIL-PERP": {
        "conservative": {
            "BRENTOIL_ORDER_SIZE_USD":     "200",
            "BRENTOIL_MAX_INVENTORY_USD":  "400",
            "BRENTOIL_SPREAD":             "0.0020",
            "BRENTOIL_CLOSE_SPREAD":       "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":     "10.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "BRENTOIL_ORDER_SIZE_USD":     "300",
            "BRENTOIL_MAX_INVENTORY_USD":  "600",
            "BRENTOIL_SPREAD":             "0.0015",
            "BRENTOIL_CLOSE_SPREAD":       "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "BRENTOIL_ORDER_SIZE_USD":     "500",
            "BRENTOIL_MAX_INVENTORY_USD":  "1000",
            "BRENTOIL_SPREAD":             "0.0010",
            "BRENTOIL_CLOSE_SPREAD":       "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "40.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "WTIOIL-PERP": {
        "conservative": {
            "WTIOIL_ORDER_SIZE_USD":       "200",
            "WTIOIL_MAX_INVENTORY_USD":    "400",
            "WTIOIL_SPREAD":               "0.0020",
            "WTIOIL_CLOSE_SPREAD":         "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":     "10.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "WTIOIL_ORDER_SIZE_USD":       "300",
            "WTIOIL_MAX_INVENTORY_USD":    "600",
            "WTIOIL_SPREAD":               "0.0015",
            "WTIOIL_CLOSE_SPREAD":         "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "WTIOIL_ORDER_SIZE_USD":       "500",
            "WTIOIL_MAX_INVENTORY_USD":    "1000",
            "WTIOIL_SPREAD":               "0.0010",
            "WTIOIL_CLOSE_SPREAD":         "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "40.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "NATGAS-PERP": {
        "conservative": {
            "NATGAS_ORDER_SIZE_USD":       "200",
            "NATGAS_MAX_INVENTORY_USD":    "400",
            "NATGAS_SPREAD":               "0.0025",
            "NATGAS_CLOSE_SPREAD":         "0.0004",
            "HOTSTUFF_MAX_DAILY_LOSS":     "10.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "NATGAS_ORDER_SIZE_USD":       "300",
            "NATGAS_MAX_INVENTORY_USD":    "600",
            "NATGAS_SPREAD":               "0.0020",
            "NATGAS_CLOSE_SPREAD":         "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "NATGAS_ORDER_SIZE_USD":       "500",
            "NATGAS_MAX_INVENTORY_USD":    "1000",
            "NATGAS_SPREAD":               "0.0015",
            "NATGAS_CLOSE_SPREAD":         "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "40.0",
            "HOTSTUFF_LEVERAGE":           "10",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "X-PERP": {
        "conservative": {
            "X_ORDER_SIZE_USD":            "300",
            "X_MAX_INVENTORY_USD":         "600",
            "X_SPREAD":                    "0.0015",
            "X_CLOSE_SPREAD":              "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":     "15.0",
            "HOTSTUFF_LEVERAGE":           "20",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "X_ORDER_SIZE_USD":            "500",
            "X_MAX_INVENTORY_USD":         "1000",
            "X_SPREAD":                    "0.0010",
            "X_CLOSE_SPREAD":              "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "30.0",
            "HOTSTUFF_LEVERAGE":           "20",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "X_ORDER_SIZE_USD":            "1000",
            "X_MAX_INVENTORY_USD":         "2000",
            "X_SPREAD":                    "0.0007",
            "X_CLOSE_SPREAD":              "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "60.0",
            "HOTSTUFF_LEVERAGE":           "20",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "EURUSD-PERP": {
        "conservative": {
            "EURUSD_ORDER_SIZE_USD":          "500",
            "EURUSD_MAX_INVENTORY_USD":      "1000",
            "EURUSD_SPREAD":               "0.0010",
            "EURUSD_CLOSE_SPREAD":          "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "20.0",
            "HOTSTUFF_LEVERAGE":           "50",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "30",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "balanced": {
            "EURUSD_ORDER_SIZE_USD":          "1000",
            "EURUSD_MAX_INVENTORY_USD":      "2000",
            "EURUSD_SPREAD":               "0.0006",
            "EURUSD_CLOSE_SPREAD":          "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "50.0",
            "HOTSTUFF_LEVERAGE":           "50",
            "TOD_08_MULTIPLIER":           "2.5",
            "TOD_13_MULTIPLIER":           "2.0",
            "TOD_14_MULTIPLIER":           "2.0",
            "FILL_COOLDOWN_OPEN_S":        "20",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
        "aggressive": {
            "EURUSD_ORDER_SIZE_USD":          "2000",
            "EURUSD_MAX_INVENTORY_USD":      "4000",
            "EURUSD_SPREAD":               "0.0004",
            "EURUSD_CLOSE_SPREAD":          "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":     "100.0",
            "HOTSTUFF_LEVERAGE":           "50",
            "TOD_08_MULTIPLIER":           "1.5",
            "TOD_13_MULTIPLIER":           "1.2",
            "TOD_14_MULTIPLIER":           "1.2",
            "FILL_COOLDOWN_OPEN_S":        "10",
            "FILL_COOLDOWN_CLOSE_S":       "2",
        },
    },

    "USA100-PERP": {
        "conservative": {
            "USA100_ORDER_SIZE_USD":      "200",
            "USA100_MAX_INVENTORY_USD":   "400",
            "USA100_SPREAD":              "0.0020",
            "USA100_CLOSE_SPREAD":        "0.0003",
            "HOTSTUFF_MAX_DAILY_LOSS":    "10.0",
            "HOTSTUFF_LEVERAGE":          "50",
            "TOD_08_MULTIPLIER":          "2.5",
            "TOD_13_MULTIPLIER":          "2.0",
            "TOD_14_MULTIPLIER":          "2.0",
            "FILL_COOLDOWN_OPEN_S":       "30",
            "FILL_COOLDOWN_CLOSE_S":      "2",
        },
        "balanced": {
            "USA100_ORDER_SIZE_USD":      "300",
            "USA100_MAX_INVENTORY_USD":   "600",
            "USA100_SPREAD":              "0.0015",
            "USA100_CLOSE_SPREAD":        "0.0002",
            "HOTSTUFF_MAX_DAILY_LOSS":    "20.0",
            "HOTSTUFF_LEVERAGE":          "50",
            "TOD_08_MULTIPLIER":          "2.5",
            "TOD_13_MULTIPLIER":          "2.0",
            "TOD_14_MULTIPLIER":          "2.0",
            "FILL_COOLDOWN_OPEN_S":       "20",
            "FILL_COOLDOWN_CLOSE_S":      "2",
        },
        "aggressive": {
            "USA100_ORDER_SIZE_USD":      "500",
            "USA100_MAX_INVENTORY_USD":   "1000",
            "USA100_SPREAD":              "0.0010",
            "USA100_CLOSE_SPREAD":        "0.0001",
            "HOTSTUFF_MAX_DAILY_LOSS":    "40.0",
            "HOTSTUFF_LEVERAGE":          "50",
            "TOD_08_MULTIPLIER":          "1.5",
            "TOD_13_MULTIPLIER":          "1.2",
            "TOD_14_MULTIPLIER":          "1.2",
            "FILL_COOLDOWN_OPEN_S":       "10",
            "FILL_COOLDOWN_CLOSE_S":      "2",
        },
    },
}

# Expose flat PROFILES for telegram_bot profile summary display
PROFILES = {
    profile: MARKET_PROFILES["BTC-PERP"][profile]
    for profile in ("conservative", "balanced", "aggressive")
}

# ── Static env shared across all users ───────────────────────────────────────
STATIC_ENV = {
    "HOTSTUFF_TESTNET":            "false",
    "ALLOW_FLIPS":                 "false",
    "PRICE_MOVE_THRESHOLD":        "0.0003",
    "ORDER_TTL_MS":                "30000",
    "REQUOTE_COOLDOWN":            "0.3",
    "HOTSTUFF_STOP_LOSS_MARGIN":   "0.015",
    "HOTSTUFF_ADX_TREND_THRESHOLD":"22.0",
}

# Market leverage caps
MARKET_MAX_LEVERAGE = {
    "BTC-PERP":      50,
    "ETH-PERP":      50,
    "SOL-PERP":      25,
    "HYPE-PERP":     25,
    "XRP-PERP":      20,
    "ZEC-PERP":      10,
    "BNB-PERP":      25,
    "GOLD-PERP":     25,
    "SILVER-PERP":   25,
    "BRENTOIL-PERP": 10,
    "WTIOIL-PERP":   10,
    "NATGAS-PERP":   10,
    "X-PERP":        20,
    "EURUSD-PERP": 50,
}


def _bot_key(telegram_id: int, label: str) -> BotKey:
    return (telegram_id, validate_label(label))


def _user_dir(telegram_id: int, label: str) -> str:
    return os.path.join(BOTS_DIR, f"user_{telegram_id}_{label}")


def _build_env(wallet_row, agent_key_plain: str) -> dict:
    market  = wallet_row["market"]
    profile = wallet_row["profile"]
    prefix  = market.split("-")[0]

    # Start with static env
    env = {}
    env.update(STATIC_ENV)

    # Set market
    env["HOTSTUFF_MARKETS"] = market

    # Apply profile defaults for this market
    market_profile = MARKET_PROFILES.get(market, MARKET_PROFILES["BTC-PERP"])
    env.update(market_profile.get(profile, market_profile["balanced"]))

    # Grid defaults — always written so bot template can read them
    env["HOTSTUFF_GRID_LEVELS"]           = "5"
    env["HOTSTUFF_GRID_SPACING_ATR_MULT"]  = "0.5"
    env["HOTSTUFF_GRID_VSHAPE_ALPHA"]      = "0.4"
    env["HOTSTUFF_GRID_MIN_SPACING_PCT"]   = "0.0005"
    env["HOTSTUFF_GRID_OVERSHOOT_MULT"]    = "1.1"
    env["HOTSTUFF_GRID_MAX_LOSS_PCT"]      = "0.02"
    # Apply expert overrides — only non-NULL fields
    if wallet_row["order_size_usd"] is not None:
        env[f"{prefix}_ORDER_SIZE_USD"] = str(wallet_row["order_size_usd"])
    if wallet_row["max_inventory_usd"] is not None:
        env[f"{prefix}_MAX_INVENTORY_USD"] = str(wallet_row["max_inventory_usd"])
    if wallet_row["max_daily_loss_usd"] is not None:
        env["HOTSTUFF_MAX_DAILY_LOSS"] = str(wallet_row["max_daily_loss_usd"])
    if wallet_row["leverage"] is not None:
        env["HOTSTUFF_LEVERAGE"] = str(wallet_row["leverage"])
    if wallet_row["spread_bps"] is not None:
        env[f"{prefix}_SPREAD"] = str(wallet_row["spread_bps"] / 10000)
    if wallet_row["close_spread_bps"] is not None:
        env[f"{prefix}_CLOSE_SPREAD"] = str(wallet_row["close_spread_bps"] / 10000)
    if wallet_row["allow_flips"] is not None:
        env["ALLOW_FLIPS"] = "true" if wallet_row["allow_flips"] else "false"
    if wallet_row["stop_loss_pct"] is not None:
        env["HOTSTUFF_STOP_LOSS_MARGIN"] = str(wallet_row["stop_loss_pct"] / 100)
    if wallet_row["fill_cooldown_open_s"] is not None:
        env["FILL_COOLDOWN_OPEN_S"] = str(wallet_row["fill_cooldown_open_s"])
    if wallet_row["fill_cooldown_close_s"] is not None:
        env["FILL_COOLDOWN_CLOSE_S"] = str(wallet_row["fill_cooldown_close_s"])
    if wallet_row["tod_08_multiplier"] is not None:
        env["TOD_08_MULTIPLIER"] = str(wallet_row["tod_08_multiplier"])
    if wallet_row["tod_13_multiplier"] is not None:
        env["TOD_13_MULTIPLIER"] = str(wallet_row["tod_13_multiplier"])
    if wallet_row["tod_14_multiplier"] is not None:
        env["TOD_14_MULTIPLIER"] = str(wallet_row["tod_14_multiplier"])
    if wallet_row["adx_threshold"] is not None:
        env["HOTSTUFF_ADX_TREND_THRESHOLD"] = str(wallet_row["adx_threshold"])
    if wallet_row["price_move_threshold"] is not None:
        env["PRICE_MOVE_THRESHOLD"] = str(wallet_row["price_move_threshold"] / 10000)
    if wallet_row["order_ttl_ms"] is not None:
        env["ORDER_TTL_MS"] = str(int(wallet_row["order_ttl_ms"]))
    if wallet_row["requote_cooldown"] is not None:
        env["REQUOTE_COOLDOWN"] = str(wallet_row["requote_cooldown"])
    if wallet_row["profit_target_bps"] is not None:
        env["HOTSTUFF_PROFIT_TARGET_BPS"] = str(wallet_row["profit_target_bps"])
    if wallet_row["adverse_selection_bps"] is not None:
        env["HOTSTUFF_ADVERSE_SELECTION_BPS"] = str(wallet_row["adverse_selection_bps"])
    if wallet_row["signal_conflict_bps"] is not None:
        env["HOTSTUFF_SIGNAL_CONFLICT_BPS"] = str(wallet_row["signal_conflict_bps"])
    if wallet_row["grid_spacing_mult"] is not None:
        env["HOTSTUFF_GRID_SPACING_ATR_MULT"] = str(wallet_row["grid_spacing_mult"])
    if wallet_row["grid_levels"] is not None:
        env["HOTSTUFF_GRID_LEVELS"] = str(int(wallet_row["grid_levels"]))
    if wallet_row["grid_vshape_alpha"] is not None:
        env["HOTSTUFF_GRID_VSHAPE_ALPHA"] = str(wallet_row["grid_vshape_alpha"])
    if wallet_row["grid_min_spacing_pct"] is not None:
        env["HOTSTUFF_GRID_MIN_SPACING_PCT"] = str(wallet_row["grid_min_spacing_pct"])
    if wallet_row["grid_overshoot_mult"] is not None:
        env["HOTSTUFF_GRID_OVERSHOOT_MULT"] = str(wallet_row["grid_overshoot_mult"])
    if wallet_row["grid_max_loss_pct"] is not None:
        env["HOTSTUFF_GRID_MAX_LOSS_PCT"] = str(wallet_row["grid_max_loss_pct"])

    # Credentials (agent key passed via process env, not written to disk)
    env["HOTSTUFF_WALLET_ADDRESS"]    = wallet_row["wallet_address"]

    return env


def _write_env(path: str, env: dict):
    with open(path, "w") as f:
        for k, v in env.items():
            f.write(f"{k}={v}\n")
    os.chmod(path, 0o600)



def _migrate_fills(telegram_id: int, new_label: str, wallet_address: str, new_user_dir: str):
    """Copy fill history from any existing bot dir for the same wallet address."""
    import sqlite3 as _sq
    new_db = os.path.join(new_user_dir, "bot", "hotstuff.db")
    migrated = 0
    for entry in os.listdir(BOTS_DIR):
        if not entry.startswith(f"user_{telegram_id}_"):
            continue
        old_label = entry.split(f"user_{telegram_id}_", 1)[-1]
        if old_label == new_label:
            continue
        old_db = os.path.join(BOTS_DIR, entry, "bot", "hotstuff.db")
        if not os.path.exists(old_db):
            continue
        try:
            old_conn = _sq.connect(old_db)
            old_conn.row_factory = _sq.Row
            fills = old_conn.execute(
                "SELECT * FROM fills WHERE address=?", (wallet_address,)
            ).fetchall()
            old_conn.close()
            if not fills:
                continue
            new_conn = _sq.connect(new_db)
            cols = fills[0].keys()
            for f in fills:
                try:
                    placeholders = ",".join(["?"] * len(cols))
                    new_conn.execute(
                        f"INSERT OR IGNORE INTO fills ({','.join(cols)}) VALUES ({placeholders})",
                        tuple(f[c] for c in cols)
                    )
                    migrated += 1
                except Exception:
                    pass
            new_conn.commit()
            new_conn.close()
            log.info(f"Migrated {migrated} fills from {entry} to user_{telegram_id}_{new_label}")
        except Exception as e:
            log.warning(f"Fill migration failed from {entry}: {e}")

def _provision(telegram_id: int, label: str, env: dict) -> str:
    user_dir = _user_dir(telegram_id, label)
    if os.path.exists(user_dir):
        # Dir exists — only update .env, preserve DB and state
        _write_env(os.path.join(user_dir, ".env"), env)
        log.info(f"Re-provisioned env only for {user_dir}")
        return user_dir
    # Fresh provision — copy template
    shutil.copytree(TEMPLATE_DIR, user_dir)
    for stale in ["hotstuff.db", "hotstuff.db.corrupted",
                  "ofi_state.json", "regime_state.json"]:
        p = os.path.join(user_dir, "bot", stale)
        if os.path.exists(p):
            os.remove(p)
    _write_env(os.path.join(user_dir, ".env"), env)

    # Migrate fill history from any existing bot dir for the same wallet address
    wallet_address = env.get("HOTSTUFF_WALLET_ADDRESS", "")
    if wallet_address:
        _migrate_fills(telegram_id, label, wallet_address, user_dir)

    log.info(f"Provisioned fresh {user_dir}")
    return user_dir


class BotManager:
    def __init__(self):
        self._vault   = Vault()
        self._procs:  Dict[BotKey, subprocess.Popen] = {}
        self._lock    = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self.notify_fn = None
        self._event_loop = None

    def start(self):
        import asyncio
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = asyncio.get_event_loop()
        os.makedirs(BOTS_DIR, exist_ok=True)
        self._running = True
        active = get_all_active_wallets()
        log.info(f"Resuming {len(active)} active wallet(s)")
        for w in active:
            try:
                self.start_bot(w["telegram_id"], w["label"])
            except Exception as e:
                log.error(f"Failed to resume {w['telegram_id']}/{w['label']}: {e}")
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        log.info("BotManager started")

    def stop_all(self):
        self._running = False
        with self._lock:
            for key, proc in list(self._procs.items()):
                self._kill_proc(key, proc)

    def start_bot(self, telegram_id: int, label: str) -> bool:
        key    = _bot_key(telegram_id, label)
        # Kill any existing process for this wallet before starting fresh
        with self._lock:
            existing = self._procs.get(key)
        if existing and existing.poll() is None:
            log.info(f"Killing existing process for {telegram_id}/{label} (PID {existing.pid})")
            self._kill_proc(key, existing)
        wallet = get_wallet(telegram_id, label)
        if not wallet:
            log.error(f"start_bot: not found {telegram_id}/{label}")
            return False
        try:
            agent_key = self._vault.decrypt(wallet["encrypted_agent_key"])
        except Exception as e:
            log.error(f"start_bot: decrypt failed {telegram_id}/{label}: {e}")
            return False
        env      = _build_env(wallet, agent_key)
        user_dir = _provision(telegram_id, label, env)
        log_path = os.path.join(user_dir, "bot.log")
        log_file = open(log_path, "a", buffering=1)
        proc_env = os.environ.copy()
        proc_env["HOTSTUFF_AGENT_PRIVATE_KEY"] = agent_key
        proc     = subprocess.Popen(
            [PYTHON, "-m", "bot.main"],
            cwd=user_dir,
            env=proc_env,
            stdout=log_file,
            stderr=log_file,
        )
        with self._lock:
            self._procs[key] = proc
        set_wallet_state(telegram_id, label, "active")
        log.info(f"Started {telegram_id}/{label} (PID {proc.pid})")
        # ── Feed health check ────────────────────────────────────────────────
        _market = wallet["market"]
        if not _feed_available(_market):
            log.warning(f"start_bot: shared feed not available for {_market} — bot will use direct WS fallback")
            self._notify(ADMIN_TELEGRAM_ID,
                f"⚠️ Feed not available for `{_market}` — {telegram_id}/{label} using direct WS fallback")
        else:
            log.info(f"start_bot: {_market} feed confirmed available for {telegram_id}/{label}")
        # ────────────────────────────────────────────────────────────────────

        # Warn if wallet balance too low — skip if user has expert overrides
        try:
            _expert_fields = [
                "order_size_usd", "max_inventory_usd", "max_daily_loss_usd",
                "leverage", "spread_bps", "close_spread_bps", "allow_flips",
                "stop_loss_pct", "fill_cooldown_open_s", "fill_cooldown_close_s",
                "tod_08_multiplier", "tod_13_multiplier", "tod_14_multiplier",
                "adx_threshold", "price_move_threshold", "order_ttl_ms",
                "requote_cooldown", "profit_target_bps", "adverse_selection_bps",
                "signal_conflict_bps",
                "grid_spacing_mult", "grid_levels", "grid_vshape_alpha", "grid_min_spacing_pct",
                "grid_overshoot_mult", "grid_max_loss_pct",
            ]
            _has_expert = any(wallet[f] is not None for f in _expert_fields if f in wallet.keys())
            if not _has_expert:
                import requests
                from eth_utils import to_checksum_address
                address = to_checksum_address(wallet["wallet_address"])
                r = requests.post(
                    "https://api.hotstuff.trade/info",
                    json={"method": "accountSummary", "params": {"user": address}},
                    headers={"Content-Type": "application/json"}, timeout=5
                )
                equity = float(r.json().get("total_account_equity") or 0)
                prefix = wallet["market"].split("-")[0]
                order_size = float(
                    MARKET_PROFILES.get(wallet["market"], {})
                    .get(wallet["profile"], {})
                    .get(f"{prefix}_ORDER_SIZE_USD", 0))
                if order_size > 0 and equity < order_size:
                    self._notify(telegram_id,
                        f"⚠️ *[{label}] Low balance warning*\n\n"
                        f"Your equity (`${equity:.2f}`) is below the order size "
                        f"for *{wallet['profile'].capitalize()}* profile (`${order_size:.0f}`)\n\n"
                        f"Use /config {label} → Expert Mode → Order Size to set a smaller size."
                    )
        except Exception as e:
            log.warning(f"Balance check failed for {telegram_id}/{label}: {e}")

        return True

    def stop_bot(self, telegram_id: int, label: str) -> bool:
        key = _bot_key(telegram_id, label)
        with self._lock:
            proc = self._procs.get(key)
        if not proc:
            # Fallback: scan /proc for orphaned bot process matching this user dir
            import glob as _glob
            bot_dir = _user_dir(telegram_id, label)
            for pid_path in _glob.glob("/proc/*/cwd"):
                try:
                    import os as _os
                    if _os.readlink(pid_path) == bot_dir:
                        pid = int(pid_path.split("/")[2])
                        import signal as _sig
                        _os.kill(pid, _sig.SIGTERM)
                        log.info(f"stop_bot: killed orphaned process PID={pid} for {telegram_id}/{label}")
                        wallet = get_wallet(telegram_id, label)
                        if wallet and wallet["state"] != "suspended":
                            set_wallet_state(telegram_id, label, "stopped")
                        return True
                except Exception:
                    continue
            return False
        self._kill_proc(key, proc)
        wallet = get_wallet(telegram_id, label)
        if wallet and wallet["state"] != "suspended":
            set_wallet_state(telegram_id, label, "stopped")
        return True

    def restart_bot(self, telegram_id: int, label: str) -> bool:
        self.stop_bot(telegram_id, label)
        time.sleep(2)
        return self.start_bot(telegram_id, label)

    def resume_drawdown(self, telegram_id: int, label: str) -> bool:
        key = _bot_key(telegram_id, label)
        with self._lock:
            proc = self._procs.get(key)
        if not proc or proc.poll() is not None:
            return False
        try:
            os.kill(proc.pid, signal.SIGUSR1)
            record_resume(telegram_id, label)
            wallet   = get_wallet(telegram_id, label)
            username = wallet["telegram_username"] if wallet else str(telegram_id)
            self._notify(ADMIN_TELEGRAM_ID,
                f"⚡ Drawdown override: @{username} [{label}] resumed."
            )
            return True
        except Exception as e:
            log.error(f"resume_drawdown failed {telegram_id}/{label}: {e}")
            return False

    def status(self, telegram_id: int, label: str) -> dict:
        key    = _bot_key(telegram_id, label)
        wallet = get_wallet(telegram_id, label)
        state  = wallet["state"] if wallet else "unknown"
        with self._lock:
            proc = self._procs.get(key)
        running = proc is not None and proc.poll() is None
        return {"running": running, "pid": proc.pid if running else None, "state": state}

    def _kill_proc(self, key: BotKey, proc: subprocess.Popen):
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass
        with self._lock:
            self._procs.pop(key, None)

    def _monitor_loop(self):
        while self._running:
            time.sleep(60)
            try:
                self._check_crashes()
                self._check_key_expiry()
            except Exception as e:
                log.error(f"Monitor loop error: {e}")

    def _check_crashes(self):
        with self._lock:
            dead = [k for k, p in self._procs.items() if p.poll() is not None]
        for key in dead:
            tid, label = key
            with self._lock:
                self._procs.pop(key, None)
            # Only restart if user didn't intentionally stop it
            wallet = get_wallet(tid, label)
            if not wallet or wallet["state"] != "active":
                log.info(f"Bot {tid}/{label} exited — state={wallet['state'] if wallet else 'unknown'}, not restarting")
                continue
            try:
                self.start_bot(tid, label)
                wallet   = get_wallet(tid, label)
                username = wallet["telegram_username"] if wallet else str(tid)
                market  = wallet["market"] if wallet else "?"
                address = wallet["wallet_address"][:10] + "..." if wallet else "?"
                bot_dir = _user_dir(tid, label)
                log_suspension(
                    tid=tid, label=label, action="resumed", reason="crash",
                    triggered_by="system",
                    username=username, market=market,
                    note="auto-restarted after crash",
                )
                self._notify(ADMIN_TELEGRAM_ID,
                    f"🔄 Crash/restart\n"
                    f"User: @{username} ({tid})\n"
                    f"Wallet: {label} | {market}\n"
                    f"Address: {address}\n"
                    f"Dir: {bot_dir}"
                )
            except Exception as e:
                self._notify(ADMIN_TELEGRAM_ID,
                    f"🔴 *Restart FAILED*\n"
                    f"TID: `{tid}` · Label: `{label}`\n"
                    f"Error: `{e}`\n"
                    f"Dir: `{_user_dir(tid, label)}`"
                )

    def _check_key_expiry(self):
        for w in get_expiring_keys(KEY_ALERT_1_DAYS):
            tid, label = w["telegram_id"], w["label"]
            self._notify(tid,
                f"⚠️ [{label}] Agent key expires in {KEY_EXPIRY_DAYS - KEY_ALERT_1_DAYS} days.\n"
                f"Renew on Hotstuff and use /renewkey {label}."
            )
            mark_expiry_alert_sent(tid, label, KEY_ALERT_1_DAYS)

        for w in get_expiring_keys(KEY_ALERT_2_DAYS):
            tid, label = w["telegram_id"], w["label"]
            self._notify(tid,
                f"🚨 [{label}] Agent key expires in {KEY_EXPIRY_DAYS - KEY_ALERT_2_DAYS} days.\n"
                f"Renew now and use /renewkey {label}."
            )
            mark_expiry_alert_sent(tid, label, KEY_ALERT_2_DAYS)

        for w in get_expired_wallets():
            tid, label   = w["telegram_id"], w["label"]
            username      = w["telegram_username"]
            self.stop_bot(tid, label)
            set_wallet_state(tid, label, "suspended")
            log_suspension(
                tid=tid, label=label, action="suspended", reason="key_expired",
                triggered_by="system",
                username=username, market=w.get("market"),
                note="agent key expired",
            )
            self._notify(tid,
                f"🔴 [{label}] Agent key expired. Bot paused.\n"
                f"Generate a new key and use /renewkey {label}."
            )
            self._notify(ADMIN_TELEGRAM_ID,
                f"🔴 Key expired: @{username} [{label}] suspended."
            )

    def _notify(self, telegram_id: int, message: str):
        if self.notify_fn:
            try:
                import asyncio
                try:
                    # Check if we are inside a running event loop
                    loop = asyncio.get_running_loop()
                    asyncio.ensure_future(self.notify_fn(telegram_id, message))
                except RuntimeError:
                    # We are in a background thread — find the running loop
                    import threading
                    for thread in threading.enumerate():
                        if hasattr(thread, "_target") and thread._target:
                            pass
                    # Use stored event loop if available and running
                    if self._event_loop and not self._event_loop.is_closed() and self._event_loop.is_running():
                        asyncio.run_coroutine_threadsafe(self.notify_fn(telegram_id, message), self._event_loop)
                    else:
                        # Fallback — find any running loop
                        import asyncio as _aio
                        for obj in _aio.all_tasks.__module__ and []:
                            pass
                        try:
                            loop = _aio.get_event_loop()
                            if loop.is_running():
                                asyncio.run_coroutine_threadsafe(self.notify_fn(telegram_id, message), loop)
                        except Exception:
                            pass
            except Exception as e:
                log.error(f"Notify failed {telegram_id}: {e}")
        else:
            log.info(f"[NOTIFY] {telegram_id}: {message}")
