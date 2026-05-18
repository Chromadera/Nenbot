#!/usr/bin/env python3
"""
add_market.py — Add a new perpetual market to NenMMBot SaaS.

Usage:
    python3 add_market.py <MARKET-PERP> <symbol_id> [options]

Examples:
    python3 add_market.py LINK-PERP 14
    python3 add_market.py DOGE-PERP 15 --leverage 20 --tier crypto
    python3 add_market.py COPPER-PERP 16 --leverage 10 --tier commodity

Tiers (sets spread/size defaults):
    crypto      — like XRP/HYPE  (spread 0.0015/0.0010/0.0007, size 300/500/1000, lev 20)
    crypto_major— like BTC/ETH   (spread 0.0010/0.0006/0.0004, size 500/1000/2000, lev 50)
    commodity   — like GOLD/OIL  (spread 0.0020/0.0015/0.0010, size 200/300/500,  lev 10)
    natgas      — like NATGAS    (spread 0.0025/0.0020/0.0015, size 200/300/500,  lev 10)

Override any default with --leverage, --lev-conservative etc.
--dry-run to preview without writing.
"""
import argparse
import shutil
import sys
import time
import os

SAAS_DIR = "/root/saas"
FILES = {
    "indicators":     f"{SAAS_DIR}/bot_template/bot/indicators.py",
    "manager":        f"{SAAS_DIR}/manager.py",
    "db":             f"{SAAS_DIR}/db.py",
    "admin_commands": f"{SAAS_DIR}/admin_commands.py",
}

# ── Tier presets ──────────────────────────────────────────────────────────────
TIERS = {
    "crypto": {
        "leverage": 20,
        "conservative": {"spread": "0.0015", "close_spread": "0.0002", "size": "300",  "inv": "600",  "loss": "15.0", "cooldown_open": "30"},
        "balanced":     {"spread": "0.0010", "close_spread": "0.0001", "size": "500",  "inv": "1000", "loss": "30.0", "cooldown_open": "20"},
        "aggressive":   {"spread": "0.0007", "close_spread": "0.0001", "size": "1000", "inv": "2000", "loss": "60.0", "cooldown_open": "10"},
    },
    "crypto_major": {
        "leverage": 50,
        "conservative": {"spread": "0.0010", "close_spread": "0.0001", "size": "500",  "inv": "1000", "loss": "20.0", "cooldown_open": "30"},
        "balanced":     {"spread": "0.0006", "close_spread": "0.0001", "size": "1000", "inv": "2000", "loss": "50.0", "cooldown_open": "20"},
        "aggressive":   {"spread": "0.0004", "close_spread": "0.0001", "size": "2000", "inv": "4000", "loss": "100.0","cooldown_open": "10"},
    },
    "commodity": {
        "leverage": 10,
        "conservative": {"spread": "0.0020", "close_spread": "0.0003", "size": "200", "inv": "400",  "loss": "10.0", "cooldown_open": "30"},
        "balanced":     {"spread": "0.0015", "close_spread": "0.0002", "size": "300", "inv": "600",  "loss": "20.0", "cooldown_open": "20"},
        "aggressive":   {"spread": "0.0010", "close_spread": "0.0001", "size": "500", "inv": "1000", "loss": "40.0", "cooldown_open": "10"},
    },
    "natgas": {
        "leverage": 10,
        "conservative": {"spread": "0.0025", "close_spread": "0.0004", "size": "200", "inv": "400",  "loss": "10.0", "cooldown_open": "30"},
        "balanced":     {"spread": "0.0020", "close_spread": "0.0003", "size": "300", "inv": "600",  "loss": "20.0", "cooldown_open": "20"},
        "aggressive":   {"spread": "0.0015", "close_spread": "0.0002", "size": "500", "inv": "1000", "loss": "40.0", "cooldown_open": "10"},
    },
}

TOD = {
    "conservative": {"08": "2.5", "13": "2.0", "14": "2.0"},
    "balanced":     {"08": "2.5", "13": "2.0", "14": "2.0"},
    "aggressive":   {"08": "1.5", "13": "1.2", "14": "1.2"},
}


def build_profile_block(market: str, tier: dict, leverage: int) -> str:
    prefix = market.replace("-PERP", "")
    lines = [f'    "{market}": {{']
    for profile in ("conservative", "balanced", "aggressive"):
        p = tier[profile]
        tod = TOD[profile]
        lines.append(f'        "{profile}": {{')
        lines.append(f'            "{prefix}_ORDER_SIZE_USD":{"":>10}"{p["size"]}",')
        lines.append(f'            "{prefix}_MAX_INVENTORY_USD":{"":>6}"{p["inv"]}",')
        lines.append(f'            "{prefix}_SPREAD":{"":>15}"{p["spread"]}",')
        lines.append(f'            "{prefix}_CLOSE_SPREAD":{"":>10}"{p["close_spread"]}",')
        lines.append(f'            "HOTSTUFF_MAX_DAILY_LOSS":{"":>5}"{p["loss"]}",')
        lines.append(f'            "HOTSTUFF_LEVERAGE":{"":>11}"{leverage}",')
        lines.append(f'            "TOD_08_MULTIPLIER":{"":>11}"{tod["08"]}",')
        lines.append(f'            "TOD_13_MULTIPLIER":{"":>11}"{tod["13"]}",')
        lines.append(f'            "TOD_14_MULTIPLIER":{"":>11}"{tod["14"]}",')
        lines.append(f'            "FILL_COOLDOWN_OPEN_S":{"":>8}"{p["cooldown_open"]}",')
        lines.append(f'            "FILL_COOLDOWN_CLOSE_S":{"":>7}"2",')
        lines.append(f'        }},')
    lines.append(f'    }},')
    return "\n".join(lines)


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def write(path: str, content: str, dry_run: bool):
    if dry_run:
        print(f"  [DRY RUN] would write {path}")
        return
    shutil.copy2(path, f"{path}.bak.{int(time.time())}")
    with open(path, "w") as f:
        f.write(content)
    print(f"  written → {path}")


def verify(content: str, label: str) -> bool:
    try:
        compile(content, "<string>", "exec")
        return True
    except SyntaxError as e:
        print(f"  SYNTAX ERROR in {label}: {e}")
        return False


def patch_indicators(market: str, symbol_id: str, dry_run: bool) -> bool:
    print("\n[1/4] indicators.py")
    src = read(FILES["indicators"])

    if f'"{market}"' in src:
        print(f"  {market} already present — skipping")
        return True

    # Find closing brace of SYMBOL_IDS
    old = '}\n\n\n@dataclass'
    new = f'    "{market}": "{symbol_id}",\n' + '}\n\n\n@dataclass'
    if old not in src:
        # try alternate spacing
        old = '}\n\n@dataclass'
        new = f'    "{market}": "{symbol_id}",\n' + '}\n\n@dataclass'

    if old not in src:
        print("  ERROR: SYMBOL_IDS closing anchor not found")
        return False

    result = src.replace(old, new, 1)
    if not verify(result, "indicators.py"):
        return False
    write(FILES["indicators"], result, dry_run)
    print(f"  SYMBOL_IDS: added {market} = {symbol_id}")
    return True


def patch_manager(market: str, profile_block: str, leverage: int, dry_run: bool) -> bool:
    print("\n[2/4] manager.py")
    src = read(FILES["manager"])
    errors = []

    # ── MARKET_PROFILES ───────────────────────────────────────────────────────
    # Anchor: last market entry closes with `    },}` then blank line then # Expose
    profiles_anchor = '    },}\n\n# Expose flat PROFILES for telegram_bot profile summary display'
    profiles_section = src.split('MARKET_PROFILES')[1].split('MARKET_MAX_LEVERAGE')[0] if 'MARKET_PROFILES' in src and 'MARKET_MAX_LEVERAGE' in src else ''
    if f'"{market}"' in profiles_section:
        print(f"  {market} already present in MARKET_PROFILES — skipping")
    elif profiles_anchor not in src:
        print("  ERROR: MARKET_PROFILES closing anchor not found")
        errors.append("profiles anchor")
    else:
        # profiles_anchor starts with `    },}` — the `}` closes MARKET_PROFILES
        # We want: previous last entry closes with `    },` then new market block then `}` closes dict
        new_profiles = '    },\n\n' + profile_block + '\n}\n\n# Expose flat PROFILES for telegram_bot profile summary display'
        src = src.replace(profiles_anchor, new_profiles, 1)
        print(f"  MARKET_PROFILES: added {market}")

    # ── MARKET_MAX_LEVERAGE ───────────────────────────────────────────────────
    # Anchor: the closing brace of MARKET_MAX_LEVERAGE is followed by \n\n\ndef _bot_key
    lev_anchor = '}\n\n\ndef _bot_key'
    if lev_anchor not in src:
        print("  ERROR: MARKET_MAX_LEVERAGE closing anchor not found")
        errors.append("leverage anchor")
    else:
        # Check if already present in leverage block specifically
        lev_block_start = src.find('MARKET_MAX_LEVERAGE')
        lev_block_end   = src.find(lev_anchor) + 1
        lev_block       = src[lev_block_start:lev_block_end]
        if f'"{market}"' in lev_block:
            print(f"  {market} already present in MARKET_MAX_LEVERAGE — skipping")
        else:
            new_lev = f'    "{market}": {leverage},\n' + lev_anchor
            src = src.replace(lev_anchor, new_lev, 1)
            print(f"  MARKET_MAX_LEVERAGE: added {market} = {leverage}")

    if errors:
        return False
    if not verify(src, "manager.py"):
        return False
    write(FILES["manager"], src, dry_run)
    return True


def patch_db(market: str, dry_run: bool) -> bool:
    print("\n[3/4] db.py")
    src = read(FILES["db"])

    if f'"{market}"' in src:
        print(f"  {market} already present — skipping")
        return True

    old_marker = 'SUPPORTED_MARKETS = ['
    idx = src.find(old_marker)
    if idx == -1:
        print("  ERROR: SUPPORTED_MARKETS not found")
        return False

    end_idx = src.find(']', idx)
    old_line = src[idx:end_idx+1]
    new_line = old_line[:-1] + f', "{market}"]'
    result = src.replace(old_line, new_line, 1)

    if not verify(result, "db.py"):
        return False
    write(FILES["db"], result, dry_run)
    print(f"  SUPPORTED_MARKETS: added {market}")
    return True


def patch_admin_commands(market: str, dry_run: bool) -> bool:
    print("\n[4/4] admin_commands.py")
    src = read(FILES["admin_commands"])

    if f'"{market}"' in src.split('cmd_admin_feed')[1] if 'cmd_admin_feed' in src else False:
        print(f"  {market} already present — skipping")
        return True

    # Find the MARKETS list inside cmd_admin_feed
    idx = src.find('cmd_admin_feed')
    if idx == -1:
        print("  ERROR: cmd_admin_feed not found")
        return False

    snippet = src[idx:idx+500]
    markets_start = snippet.find('MARKETS = [')
    if markets_start == -1:
        print("  ERROR: MARKETS list not found in cmd_admin_feed")
        return False

    abs_start = idx + markets_start
    end_idx = src.find(']', abs_start)
    old_line = src[abs_start:end_idx+1]
    new_line = old_line[:-1] + f', "{market}"]'
    result = src.replace(old_line, new_line, 1)

    if not verify(result, "admin_commands.py"):
        return False
    write(FILES["admin_commands"], result, dry_run)
    print(f"  admin_feed MARKETS: added {market}")
    return True


def propagate_indicators(dry_run: bool):
    if dry_run:
        print("\n  [DRY RUN] would propagate indicators.py to all user bot dirs")
        return
    count = 0
    for d in os.scandir("/root/bots"):
        target = f"{d.path}/bot/indicators.py"
        if os.path.isfile(target):
            shutil.copy2(FILES["indicators"], target)
            count += 1
    print(f"\n  indicators.py propagated to {count} user bot dir(s)")


def main():
    parser = argparse.ArgumentParser(description="Add a new market to NenMMBot SaaS")
    parser.add_argument("market",    help="Market name e.g. LINK-PERP")
    parser.add_argument("symbol_id", help="Numeric symbol ID from exchange instruments API")
    parser.add_argument("--tier",     default="crypto",
                        choices=list(TIERS.keys()),
                        help="Parameter tier preset (default: crypto)")
    parser.add_argument("--leverage", type=int, default=None,
                        help="Override leverage cap (default: from tier)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Preview changes without writing files")
    args = parser.parse_args()

    # Normalise market name
    market = args.market.upper()
    if not market.endswith("-PERP"):
        market = f"{market}-PERP"

    tier     = TIERS[args.tier]
    leverage = args.leverage if args.leverage is not None else tier["leverage"]

    print(f"\nAdding market: {market}")
    print(f"  symbol_id : {args.symbol_id}")
    print(f"  tier      : {args.tier}")
    print(f"  leverage  : {leverage}")
    if args.dry_run:
        print("  mode      : DRY RUN")

    profile_block = build_profile_block(market, tier, leverage)

    errors = []
    if not patch_indicators(market, args.symbol_id, args.dry_run):
        errors.append("indicators")
    if not patch_manager(market, profile_block, leverage, args.dry_run):
        errors.append("manager")
    if not patch_db(market, args.dry_run):
        errors.append("db")
    if not patch_admin_commands(market, args.dry_run):
        errors.append("admin_commands")

    print("\n" + "="*60)
    if errors:
        print(f"COMPLETED WITH ERRORS: {errors}")
        sys.exit(1)
    else:
        if not args.dry_run:
            propagate_indicators(args.dry_run)
        print("ALL PATCHES APPLIED SUCCESSFULLY")
        print(f"\nNext step:")
        print(f"  systemctl restart nenmmbot.service")
        print(f"\nTo add {market} to the feed:")
        print(f"  Edit /root/saas/.env — append {market} to NENFEED_MARKETS")
        print(f"  systemctl restart nenfeed.service")


if __name__ == "__main__":
    main()
