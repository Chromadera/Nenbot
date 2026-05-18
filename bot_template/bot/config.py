"""Bot configuration — loaded from .env file."""
import os
from dataclasses import dataclass, field
from typing import List, Dict, Tuple

@dataclass
class BotConfig:
    agent_key: str
    markets: List[str]
    order_sizes: Dict[str, float] = field(default_factory=dict)
    max_inventories: Dict[str, float] = field(default_factory=dict)
    spreads: Dict[str, float] = field(default_factory=dict)         # per-market open spread
    close_spreads: Dict[str, float] = field(default_factory=dict)   # per-market close spread
    bracket_close_spreads: Dict[str, float] = field(default_factory=dict)  # per-market bracket profit target
    stop_loss_margins: Dict[str, float] = field(default_factory=dict)
    take_profit_margins: Dict[str, float] = field(default_factory=dict)
    trail_factors: Dict[str, float] = field(default_factory=dict)
    profit_lock_tiers: Dict[str, List[Tuple[float, float]]] = field(default_factory=dict)
    base_spread: float = 0.002
    inventory_skew: float = 0.0005
    requote_cooldown: float = 0.5
    price_move_threshold: float = 0.0001
    order_ttl_ms: int = 60_000
    testnet: bool = False
    default_order_size_usd: float = 100.0
    default_max_inventory_usd: float = 300.0
    allow_flips: bool = True


def load_config() -> BotConfig:
    markets_str = os.environ.get("HOTSTUFF_MARKETS", "BTC-PERP")
    markets = [m.strip() for m in markets_str.split(",") if m.strip()]

    order_sizes       = {}
    max_inventories   = {}
    spreads           = {}
    close_spreads          = {}
    bracket_close_spreads  = {}
    stop_loss_margins    = {}
    take_profit_margins  = {}
    trail_factors        = {}
    profit_lock_tiers    = {}

    base_spread       = float(os.environ.get("HOTSTUFF_BASE_SPREAD", 0.002))
    base_close_spread = float(os.environ.get("HOTSTUFF_CLOSE_SPREAD", 0.0005))

    # Global SL/TP defaults
    global_sl     = float(os.environ.get("HOTSTUFF_STOP_LOSS_MARGIN",   0.03))
    global_tp     = float(os.environ.get("HOTSTUFF_TAKE_PROFIT_MARGIN", 0.04))
    global_trail  = float(os.environ.get("HOTSTUFF_TRAIL_FACTOR",       0.80))
    global_lock1_thresh = float(os.environ.get("HOTSTUFF_PROFIT_LOCK_1_THRESHOLD", 0.0125))
    global_lock1_floor  = float(os.environ.get("HOTSTUFF_PROFIT_LOCK_1_FLOOR",     0.00))
    global_lock2_thresh = float(os.environ.get("HOTSTUFF_PROFIT_LOCK_2_THRESHOLD", 0.025))
    global_lock2_floor  = float(os.environ.get("HOTSTUFF_PROFIT_LOCK_2_FLOOR",     0.0125))

    for m in markets:
        prefix = m.split("-")[0].upper()

        order_sizes[m]     = float(os.environ.get(f"{prefix}_ORDER_SIZE_USD",    100))
        max_inventories[m] = float(os.environ.get(f"{prefix}_MAX_INVENTORY_USD", 300))
        spreads[m]         = float(os.environ.get(f"{prefix}_SPREAD",       base_spread))
        close_spreads[m]          = float(os.environ.get(f"{prefix}_CLOSE_SPREAD", base_close_spread))
        # bracket close spread — falls back to open spread if not set
        bracket_close_spreads[m]  = float(os.environ.get(f"{prefix}_BRACKET_CLOSE_SPREAD",
                                          os.environ.get(f"{prefix}_SPREAD", base_spread)))

        # Per-market SL/TP — falls back to global if not set
        stop_loss_margins[m]   = float(os.environ.get(f"{prefix}_STOP_LOSS_MARGIN",   global_sl))
        take_profit_margins[m] = float(os.environ.get(f"{prefix}_TAKE_PROFIT_MARGIN", global_tp))
        trail_factors[m]       = float(os.environ.get(f"{prefix}_TRAIL_FACTOR",       global_trail))

        # Per-market profit lock tiers
        lock1_thresh = float(os.environ.get(f"{prefix}_PROFIT_LOCK_1_THRESHOLD", global_lock1_thresh))
        lock1_floor  = float(os.environ.get(f"{prefix}_PROFIT_LOCK_1_FLOOR",     global_lock1_floor))
        lock2_thresh = float(os.environ.get(f"{prefix}_PROFIT_LOCK_2_THRESHOLD", global_lock2_thresh))
        lock2_floor  = float(os.environ.get(f"{prefix}_PROFIT_LOCK_2_FLOOR",     global_lock2_floor))
        profit_lock_tiers[m] = [(lock1_thresh, lock1_floor), (lock2_thresh, lock2_floor)]

    return BotConfig(
        agent_key=os.environ["HOTSTUFF_AGENT_PRIVATE_KEY"],
        markets=markets,
        order_sizes=order_sizes,
        max_inventories=max_inventories,
        spreads=spreads,
        close_spreads=close_spreads,
        bracket_close_spreads=bracket_close_spreads,
        stop_loss_margins=stop_loss_margins,
        take_profit_margins=take_profit_margins,
        trail_factors=trail_factors,
        profit_lock_tiers=profit_lock_tiers,
        base_spread=base_spread,
        inventory_skew=float(os.environ.get("HOTSTUFF_INVENTORY_SKEW",          0.0005)),
        requote_cooldown=float(os.environ.get("HOTSTUFF_REQUOTE_COOLDOWN",       0.5)),
        price_move_threshold=float(os.environ.get("HOTSTUFF_PRICE_MOVE_THRESHOLD", 0.0001)),
        order_ttl_ms=int(os.environ.get("HOTSTUFF_ORDER_TTL_MS",                 60_000)),
        testnet=os.environ.get("HOTSTUFF_TESTNET", "false").lower() == "true",
        allow_flips=os.environ.get("HOTSTUFF_ALLOW_FLIPS", "true").lower() == "true",
    )
