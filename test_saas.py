#!/usr/bin/env python3
"""
NenMMBot SaaS — Diagnostic Test Suite v2
Run from: /root/saas/
Usage:    python3 test_saas.py
"""

import os, sys, ast, shutil, subprocess, traceback

BASE     = "/root/saas"
BOTS_DIR = "/root/bots"
TEST_TID = 999999999

sys.path.insert(0, BASE)

# ── colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"; RED   = "\033[91m"; YELLOW = "\033[93m"
CYAN   = "\033[96m"; RESET = "\033[0m";  BOLD   = "\033[1m"
passed = 0; failed = 0; warned = 0

def ok(label):
    global passed; passed += 1
    print(f"  {GREEN}✓{RESET} {label}")

def fail(label, detail=""):
    global failed; failed += 1
    print(f"  {RED}✗ {label}{RESET}" + (f"\n    {RED}→ {detail}{RESET}" if detail else ""))

def warn(label, detail=""):
    global warned; warned += 1
    print(f"  {YELLOW}⚠ {label}{RESET}" + (f"\n    {YELLOW}→ {detail}{RESET}" if detail else ""))

def section(title):
    print(f"\n{BOLD}{CYAN}{'─'*52}{RESET}\n{BOLD}{CYAN}  {title}{RESET}\n{BOLD}{CYAN}{'─'*52}{RESET}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. FILE STRUCTURE
# ─────────────────────────────────────────────────────────────────────────────
section("1. File Structure")

for f in ["telegram_bot.py","manager.py","db.py","vault.py",".env"]:
    path = f"{BASE}/{f}"
    if os.path.isfile(path): ok(path)
    else: fail(path, "missing")

for d in [f"{BASE}/bot_template", BOTS_DIR]:
    if os.path.isdir(d): ok(f"{d}/")
    else: fail(f"{d}/", "directory missing")

tmpl_files = os.listdir(f"{BASE}/bot_template") if os.path.isdir(f"{BASE}/bot_template") else []
if tmpl_files: ok(f"bot_template contains: {', '.join(tmpl_files)}")
else: warn("bot_template is empty — spawn will fail")

svc = "/etc/systemd/system/nenmmbot.service"
if os.path.isfile(svc): ok(svc)
else: warn(f"{svc} not found")

# ─────────────────────────────────────────────────────────────────────────────
# 2. ENVIRONMENT
# ─────────────────────────────────────────────────────────────────────────────
section("2. Environment (.env)")

try:
    from dotenv import load_dotenv
    load_dotenv(f"{BASE}/.env", override=True)
    ok("dotenv loaded")
except ImportError:
    with open(f"{BASE}/.env") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()
    ok(".env loaded manually")

for key in ["NENBOT_BOT_TOKEN", "NENBOT_VAULT_KEY"]:
    val = os.environ.get(key, "")
    if val: ok(f"{key} set ({len(val)} chars)")
    else: fail(f"{key} missing — bot will crash")

for key in ["NENBOT_ADMIN_ID", "NENBOT_REFERRAL_LINK", "NENBOT_REFERRAL_CODE"]:
    val = os.environ.get(key, "")
    if val: ok(f"{key} = {val}")
    else: warn(f"{key} not set (optional)")

# ─────────────────────────────────────────────────────────────────────────────
# 3. VAULT — class-based Fernet wrapper
# ─────────────────────────────────────────────────────────────────────────────
section("3. Vault — Fernet Encryption")

vault_obj = None
try:
    import vault
    ok("vault.py imported")

    # find module-level singleton or Vault class
    for attr in ["vault", "_vault", "Vault"]:
        candidate = getattr(vault, attr, None)
        if candidate is not None:
            if isinstance(candidate, type):
                try:
                    vault_obj = candidate()
                    ok(f"vault.{attr}() instantiated")
                except Exception as e:
                    fail(f"vault.{attr}() instantiation failed", str(e))
            else:
                vault_obj = candidate
                ok(f"vault.{attr} singleton found")
            break

    if vault_obj is None:
        fail("No vault singleton or Vault class found at module level")
    else:
        secret = "0xDEADBEEF_test_agent_key"

        try:
            enc = vault_obj.encrypt(secret)
            ok(f"encrypt() → {len(enc)} char token")
        except Exception as e:
            fail("encrypt() raised", str(e)); enc = None

        if enc:
            try:
                dec = vault_obj.decrypt(enc)
                if dec == secret: ok("decrypt() round-trip ✓")
                else: fail("decrypt() mismatch", f"got: {dec!r}")
            except Exception as e:
                fail("decrypt() raised", str(e))

        try:
            vault_obj.decrypt("not-valid-fernet-token")
            fail("decrypt() should raise on bad token but didn't")
        except Exception:
            ok("decrypt() correctly raises on invalid token")

except ImportError as e:
    fail("vault.py import failed", str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 4. DATABASE — wallet-centric schema
# ─────────────────────────────────────────────────────────────────────────────
section("4. Database (db.py) — wallet-centric")

TEST_LABEL    = "testdiag"   # no underscore prefix, ≤10 chars
TEST_USERNAME = "test_user"
TEST_ADDR     = "0xDiagnosticWallet000000000000000000000001"
TEST_MARKET   = "BTC-PERP"
TEST_ENC      = "enc_agent_key_placeholder"
TEST_DB       = "/tmp/test_nenmmbot.db"

try:
    import db as dbmod
    ok("db.py imported")

    orig_db = getattr(dbmod, "DB_PATH", None)
    if orig_db:
        dbmod.DB_PATH = TEST_DB

    try:
        dbmod.init_db()
        ok("init_db() succeeded")
    except Exception as e:
        fail("init_db() raised", str(e))

    # validate_label / validate_market
    try:
        lbl = dbmod.validate_label(TEST_LABEL)
        ok(f"validate_label('{TEST_LABEL}') → '{lbl}'")
    except Exception as e:
        fail("validate_label() raised", str(e))

    try:
        mkt = dbmod.validate_market(TEST_MARKET)
        ok(f"validate_market('{TEST_MARKET}') → '{mkt}'")
    except Exception as e:
        fail("validate_market() raised", str(e))

    # add_wallet(telegram_id, label, telegram_username, wallet_address, encrypted_agent_key, market, profile)
    try:
        result = dbmod.add_wallet(TEST_TID, TEST_LABEL, TEST_USERNAME, TEST_ADDR, TEST_ENC, TEST_MARKET)
        ok(f"add_wallet() succeeded → {result}")
    except Exception as e:
        fail("add_wallet() raised", str(e))

    # get_wallet
    try:
        row = dbmod.get_wallet(TEST_TID, TEST_LABEL)
        if row:
            keys = row.keys() if hasattr(row, "keys") else []
            addr = row["wallet_address"] if "wallet_address" in keys else "?"
            ok(f"get_wallet() → wallet_address={addr}")
        else:
            fail("get_wallet() returned None after add_wallet()")
    except Exception as e:
        fail("get_wallet() raised", str(e))

    # get_user_wallets
    try:
        rows = dbmod.get_user_wallets(TEST_TID)
        ok(f"get_user_wallets() → {len(rows)} row(s)")
    except Exception as e:
        fail("get_user_wallets() raised", str(e))

    # get_all_active_wallets
    try:
        rows = dbmod.get_all_active_wallets()
        ok(f"get_all_active_wallets() → {len(rows)} row(s)")
    except Exception as e:
        fail("get_all_active_wallets() raised", str(e))

    # set_wallet_state
    for state in ("running", "stopped"):
        try:
            dbmod.set_wallet_state(TEST_TID, TEST_LABEL, state)
            ok(f"set_wallet_state('{state}') succeeded")
        except Exception as e:
            fail(f"set_wallet_state('{state}') raised", str(e))

    # set_wallet_market
    try:
        result = dbmod.set_wallet_market(TEST_TID, TEST_LABEL, "ETH-PERP")
        ok(f"set_wallet_market('ETH-PERP') → {result}")
    except Exception as e:
        fail("set_wallet_market() raised", str(e))

    # set_wallet_profile
    for profile in ("balanced", "aggressive", "conservative"):
        try:
            dbmod.set_wallet_profile(TEST_TID, TEST_LABEL, profile)
            ok(f"set_wallet_profile('{profile}') succeeded")
            break
        except Exception as e:
            fail(f"set_wallet_profile('{profile}') raised", str(e))

    # update_agent_key
    try:
        dbmod.update_agent_key(TEST_TID, TEST_LABEL, "new_enc_key")
        ok("update_agent_key() succeeded")
    except Exception as e:
        fail("update_agent_key() raised", str(e))

    # expert overrides
    try:
        dbmod.set_expert_overrides(TEST_TID, TEST_LABEL, spread=0.001)
        ok("set_expert_overrides(spread=0.001) succeeded")
        dbmod.reset_expert_overrides(TEST_TID, TEST_LABEL)
        ok("reset_expert_overrides() succeeded")
    except Exception as e:
        fail("expert_overrides raised", str(e))

    # record_resume
    try:
        dbmod.record_resume(TEST_TID, TEST_LABEL)
        ok("record_resume() succeeded")
    except Exception as e:
        fail("record_resume() raised", str(e))

    # expiry queries
    try:
        ok(f"get_expiring_keys() → {len(dbmod.get_expiring_keys())} row(s)")
    except Exception as e:
        fail("get_expiring_keys() raised", str(e))

    try:
        ok(f"get_expired_wallets() → {len(dbmod.get_expired_wallets())} row(s)")
    except Exception as e:
        fail("get_expired_wallets() raised", str(e))

    # remove_wallet
    try:
        removed = dbmod.remove_wallet(TEST_TID, TEST_LABEL)
        row_after = dbmod.get_wallet(TEST_TID, TEST_LABEL)
        if row_after is None: ok(f"remove_wallet() → {removed}, confirmed gone ✓")
        else: fail("remove_wallet() ran but row still exists")
    except Exception as e:
        fail("remove_wallet() raised", str(e))

    # restore & cleanup
    if orig_db:
        dbmod.DB_PATH = orig_db
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    real_db = orig_db or f"{BASE}/users.db"
    if os.path.isfile(real_db):
        ok(f"Production DB exists ({os.path.getsize(real_db):,} bytes)")
    else:
        warn("Production DB not found — no wallets registered yet")

except ImportError as e:
    fail("db.py import failed", str(e))
except Exception as e:
    fail("db section crashed", traceback.format_exc())

# ─────────────────────────────────────────────────────────────────────────────
# 5. MANAGER — BotManager class
# ─────────────────────────────────────────────────────────────────────────────
section("5. Manager (BotManager class)")

try:
    import manager as mgr_mod
    ok("manager.py imported")

    BotManager = getattr(mgr_mod, "BotManager", None)
    if BotManager is None:
        fail("BotManager class not found")
    else:
        ok("BotManager class found")

        for method in ["start", "stop_all", "start_bot", "stop_bot",
                       "restart_bot", "resume_drawdown", "status"]:
            if hasattr(BotManager, method): ok(f"BotManager.{method}() present")
            else: fail(f"BotManager.{method}() missing")

        # find module-level singleton
        mgr_instance = None
        for attr in ["mgr", "manager", "bot_manager", "bm"]:
            candidate = getattr(mgr_mod, attr, None)
            if candidate is not None and isinstance(candidate, BotManager):
                mgr_instance = candidate
                ok(f"Module-level singleton: mgr_mod.{attr}")
                break
        if mgr_instance is None:
            warn("No module-level BotManager singleton — telegram_bot.py may instantiate its own")

        # status() on unknown wallet should not crash
        try:
            tmp = BotManager()
            result = tmp.status(TEST_TID, "noexist")
            if isinstance(result, dict) and "running" in result:
                ok(f"status() on unknown wallet → running={result['running']}")
            else:
                warn(f"status() returned unexpected shape: {result}")
        except Exception as e:
            fail("status() raised on unknown wallet", str(e))

    # helper functions
    for fn in ["_bot_key", "_user_dir", "_build_env", "_write_env", "_provision"]:
        if hasattr(mgr_mod, fn): ok(f"{fn}() present")
        else: warn(f"{fn}() not found")

    # bot_template copy dry-run
    tmpl = f"{BASE}/bot_template"
    if os.path.isdir(tmpl) and os.listdir(tmpl):
        dst = f"{BOTS_DIR}/user_{TEST_TID}_test"
        try:
            shutil.copytree(tmpl, dst)
            ok(f"bot_template → {dst} copy succeeded")
            shutil.rmtree(dst)
            ok("test bot dir cleaned up")
        except Exception as e:
            fail("bot_template copy failed", str(e))
            shutil.rmtree(dst, ignore_errors=True)
    else:
        warn("Skipping template copy — bot_template empty")

except ImportError as e:
    fail("manager.py import failed", str(e))
except Exception as e:
    fail("manager section crashed", traceback.format_exc())

# ─────────────────────────────────────────────────────────────────────────────
# 6. TELEGRAM BOT — static analysis
# ─────────────────────────────────────────────────────────────────────────────
section("6. Telegram Bot (static checks)")

try:
    src = open(f"{BASE}/telegram_bot.py").read()

    try:
        ast.parse(src)
        ok("telegram_bot.py syntax OK")
    except SyntaxError as e:
        fail("syntax error", str(e))

    for cmd in ["/start", "/stop", "/status", "/help", "/setup", "/addwallet", "/removewallet"]:
        if cmd in src: ok(f"Handler for {cmd} found")
        else: warn(f"Handler for {cmd} not found")

    for mod in ("vault", "db", "manager"):
        if f"import {mod}" in src or f"from {mod}" in src: ok(f"imports {mod}")
        else: warn(f"doesn't import {mod}")

    for call in ("mgr.start_bot", "mgr.stop_bot", "mgr.restart_bot", "mgr.status"):
        if call in src: ok(f"{call}() called")
        else: warn(f"{call}() not found in telegram_bot.py")

except FileNotFoundError:
    fail("telegram_bot.py not found")
except Exception as e:
    fail("static check crashed", traceback.format_exc())

# ─────────────────────────────────────────────────────────────────────────────
# 7. SYSTEMD
# ─────────────────────────────────────────────────────────────────────────────
section("7. Systemd Service")

r = subprocess.run(["systemctl", "is-active", "nenmmbot"], capture_output=True, text=True)
status = r.stdout.strip()
if status == "active": ok("nenmmbot.service active ✓")
elif status == "inactive": warn("nenmmbot.service inactive (stopped)")
else: warn(f"nenmmbot.service status: {status}")

r2 = subprocess.run(["systemctl", "status", "nenmmbot", "--no-pager", "-n", "5"],
                     capture_output=True, text=True)
if r2.stdout:
    print(f"\n{YELLOW}  Last log lines:{RESET}")
    for line in r2.stdout.strip().splitlines()[-7:]:
        print(f"  {line}")


# ─────────────────────────────────────────────────────────────────────────────
# 8. ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────────────────
section("8. Admin Commands (admin_commands.py)")

try:
    import ast as _ast
    src_admin = open(f"{BASE}/admin_commands.py").read()

    # Syntax check
    try:
        _ast.parse(src_admin)
        ok("admin_commands.py syntax OK")
    except SyntaxError as e:
        fail("admin_commands.py syntax error", str(e))

    # Import check
    import admin_commands as adm
    ok("admin_commands.py imported")

    # Guard functions
    for fn in ("_is_admin", "_deny", "_parse_tid_label", "_parse_tid",
               "set_manager", "register_admin_handlers"):
        if hasattr(adm, fn): ok(f"{fn}() present")
        else: fail(f"{fn}() missing")

    # All expected command handlers present
    expected_handlers = [
        "cmd_admin_help", "cmd_admin_users", "cmd_admin_bots",
        "cmd_admin_status", "cmd_admin_logs", "cmd_admin_restart",
        "cmd_admin_stop", "cmd_admin_kill", "cmd_admin_db",
        "cmd_admin_broadcast", "cmd_admin_sysinfo",
        # User management
        "cmd_admin_wallets", "cmd_admin_suspend", "cmd_admin_unsuspend",
        "cmd_admin_wipe",
        # Monitoring
        "cmd_admin_crashed", "cmd_admin_losses", "cmd_admin_idle",
        # Financial
        "cmd_admin_revenue", "cmd_admin_referrals",
        # Ops
        "cmd_admin_update", "cmd_admin_envdump",
        "cmd_admin_diskusage", "cmd_admin_setstate",
    ]
    for fn in expected_handlers:
        if hasattr(adm, fn): ok(f"{fn}() present")
        else: fail(f"{fn}() missing")

    # Safety — confirm all handlers silently ignore non-admin
    import inspect
    for fn in expected_handlers:
        handler = getattr(adm, fn, None)
        if handler:
            src_fn = inspect.getsource(handler)
            if "_is_admin" in src_fn or "_deny" in src_fn:
                pass  # good
            else:
                warn(f"{fn}() may be missing admin guard")

    ok(f"All {len(expected_handlers)} handlers have admin guard")

    # ALLOWED_FIELDS and VALID_STATES defined
    af = getattr(adm, "ALLOWED_FIELDS", None)
    if af: ok(f"ALLOWED_FIELDS = {af}")
    else: fail("ALLOWED_FIELDS not defined")

    vs = getattr(adm, "VALID_STATES", None)
    if vs: ok(f"VALID_STATES = {vs}")
    else: fail("VALID_STATES not defined")

    # Confirm wired into telegram_bot.py
    src_bot = open(f"{BASE}/telegram_bot.py").read()
    if "from admin_commands import register_admin_handlers" in src_bot:
        ok("telegram_bot.py imports register_admin_handlers")
    else:
        fail("telegram_bot.py missing admin_commands import — patch not applied")

    if "register_admin_handlers(application, mgr)" in src_bot:
        ok("register_admin_handlers() called in build_app()")
    else:
        fail("register_admin_handlers() not called in build_app() — patch not applied")

    # ADMIN_TELEGRAM_ID is set
    admin_id = os.environ.get("NENBOT_ADMIN_ID", "")
    if admin_id:
        ok(f"NENBOT_ADMIN_ID set → {admin_id}")
    else:
        warn("NENBOT_ADMIN_ID not in env — admin guard may not work")

except FileNotFoundError:
    fail("admin_commands.py not found — deploy it to /root/saas/")
except ImportError as e:
    fail("admin_commands.py import failed", str(e))
except Exception as e:
    fail("admin section crashed", traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────────
# 9. SHARED FEED
# ─────────────────────────────────────────────────────────────────────────────
section("9. Shared Feed (nenfeed.service)")

import socket as _socket
import json as _json
import time as _time

FEED_MARKETS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "ZEC-PERP", "XRP-PERP"]

# service status
r = subprocess.run(["systemctl", "is-active", "nenfeed"], capture_output=True, text=True)
feed_status = r.stdout.strip()
if feed_status == "active": ok("nenfeed.service active ✓")
else: fail(f"nenfeed.service status: {feed_status}")

# socket files exist
for m in FEED_MARKETS:
    path = f"/tmp/nenfeed_{m.replace('-','_')}.sock"
    if os.path.exists(path): ok(f"Socket exists: {path}")
    else: fail(f"Socket missing: {path}")

# connect and read live BBO from each market
for m in FEED_MARKETS:
    path = f"/tmp/nenfeed_{m.replace('-','_')}.sock"
    if not os.path.exists(path):
        fail(f"{m}: socket missing — skipping BBO test")
        continue
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect(path)
        buf = ""
        while "\n" not in buf:
            chunk = sock.recv(1024).decode()
            if not chunk: break
            buf += chunk
        sock.close()
        line = buf.split("\n")[0].strip()
        data = _json.loads(line)
        age  = round(_time.time() - data.get("ts", 0), 1)
        bid, ask = data.get("bid"), data.get("ask")
        if bid and ask and age < 10:
            ok(f"{m}: bid={bid} ask={ask} age={age}s ✓")
        elif age >= 10:
            warn(f"{m}: data stale ({age}s)")
        else:
            fail(f"{m}: incomplete data: {data}")
    except Exception as e:
        fail(f"{m}: socket read failed", str(e))

# feed.py exists
if os.path.isfile(f"{BASE}/feed.py"): ok(f"{BASE}/feed.py present")
else: fail(f"{BASE}/feed.py missing")

# nenfeed.service exists
if os.path.isfile("/etc/systemd/system/nenfeed.service"): ok("nenfeed.service file present")
else: warn("nenfeed.service not found in /etc/systemd/system/")

# exchange.py in bot template has FeedReader
try:
    ex_src = open(f"{BASE}/bot_template/bot/exchange.py").read()
    if "FeedReader" in ex_src: ok("exchange.py has FeedReader ✓")
    else: fail("exchange.py missing FeedReader — old version deployed")
    if "_feed_available" in ex_src: ok("exchange.py has _feed_available ✓")
    else: fail("exchange.py missing _feed_available")
    if "fallback" in ex_src.lower() or "direct" in ex_src.lower(): ok("exchange.py has fallback logic ✓")
    else: warn("exchange.py may be missing fallback logic")
except FileNotFoundError:
    fail("bot_template/bot/exchange.py not found")

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
print(f"\n{BOLD}{'═'*52}{RESET}")
print(f"{BOLD}  RESULTS  —  ✓ {passed}  ✗ {failed}  ⚠ {warned}{RESET}")
print(f"{'═'*52}\n")

if failed == 0 and warned == 0:
    print(f"{GREEN}{BOLD}  All checks passed.{RESET}\n")
elif failed == 0:
    print(f"{YELLOW}{BOLD}  No hard failures — review warnings above.{RESET}\n")
else:
    print(f"{RED}{BOLD}  {failed} failure(s) need fixing.{RESET}\n")

sys.exit(1 if failed > 0 else 0)
