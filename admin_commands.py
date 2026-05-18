"""
admin_commands.py — Admin-only Telegram commands for NenMMBot.
All commands are silently ignored if the caller is not ADMIN_TELEGRAM_ID.

── Basic ────────────────────────────────────────────────────────────────────
  /admin_help                          — list all admin commands
  /admin_users                         — all wallets + live state
  /admin_bots                          — running bot PIDs
  /admin_status   <tid> <label>        — full diagnostic for one bot
  /admin_logs     <tid> <label> [N]    — last N lines of bot stdout
  /admin_restart  <tid> <label>        — force restart
  /admin_stop     <tid> <label>        — force stop
  /admin_kill     <tid> <label>        — SIGKILL stuck process
  /admin_db       <tid> <label>        — dump DB row (keys redacted)
  /admin_broadcast <message>           — message all active users
  /admin_sysinfo                       — VPS RAM/CPU/disk snapshot

── User management ──────────────────────────────────────────────────────────
  /admin_wallets  <tid>                — all wallets for a specific user
  /admin_suspend  <tid>                — stop all bots + mark suspended
  /admin_unsuspend <tid>               — reinstate suspended user
  /admin_wipe     <tid> <label>        — full removal: stop + delete dir + DB

── Monitoring ───────────────────────────────────────────────────────────────
  /admin_crashed                       — bots in DB state=active but not running
  /admin_losses   <threshold_usd>      — bots exceeding daily drawdown threshold
  /admin_idle     [hours]              — bots running but no fills in N hours

── Financial ────────────────────────────────────────────────────────────────
  /admin_revenue                       — estimated fees generated across all users
  /admin_referrals                     — referral usage stats

── Ops ──────────────────────────────────────────────────────────────────────
  /admin_update   <tid> <label> <field> <value>  — patch a DB field directly
  /admin_envdump  <tid> <label>        — show user bot .env (keys redacted)
  /admin_diskusage                     — per-user bot dir sizes
  /admin_setstate <tid> <label> <state> — manually fix DB state
"""

import os
import re
import signal
import shutil
import sqlite3
import logging
log = logging.getLogger("admin_commands")
import asyncio
import subprocess
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler

from manager import BotManager, ADMIN_TELEGRAM_ID, MARKET_PROFILES
from db import (
    get_all_active_wallets, get_wallet, get_user_wallets,
    set_wallet_state, get_conn, DB_PATH
)
from db_admin import log_suspension, get_suspensions, count_suspensions, init_admin_db
init_admin_db()  # ensure admin DB schema exists

BOTS_DIR = "/root/bots"

# ── module-level mgr reference ────────────────────────────────────────────────
_mgr: BotManager = None

def set_manager(mgr: BotManager):
    global _mgr
    _mgr = mgr

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_TELEGRAM_ID

async def _deny(update: Update):
    pass  # silent — don't reveal admin commands exist

def _parse_tid_label(context):
    args = context.args
    if not args or len(args) < 2:
        return None, None
    try:
        return int(args[0]), args[1]
    except ValueError:
        return None, None

def _parse_tid(context):
    args = context.args
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None

def _run(cmd: str) -> str:
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=5).strip()
    except Exception:
        return "—"

def _bot_dir(tid: int, label: str) -> str:
    return os.path.join(BOTS_DIR, f"user_{tid}_{label}")

async def _send_chunked(update: Update, text: str, parse_mode="Markdown"):
    pm = parse_mode if parse_mode != "None" else None
    if len(text) <= 4096:
        await update.message.reply_text(text, parse_mode=pm)
    else:
        for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i+4000], parse_mode=pm)

def _all_wallets_for_user(tid: int):
    """Return all wallet rows for a telegram_id regardless of state."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (tid,)
        ).fetchall()

# ─────────────────────────────────────────────────────────────────────────────
# /admin_help
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    text = (
        "🔧 *Admin Commands*\n\n"
        "*Basic*\n"
        "`/admin_users` — all wallets + state\n"
        "`/admin_bots` — running PIDs\n"
        "`/admin_status <tid> <lbl>` — bot diagnostic\n"
        "`/admin_logs <tid> <lbl> [N]` — stdout log\n"
        "`/admin_restart <tid> <lbl>` — force restart\n"
        "`/admin_stop <tid> <lbl>` — force stop\n"
        "`/admin_kill <tid> <lbl>` — SIGKILL\n"
        "`/admin_db <tid> <lbl>` — DB row dump\n"
        "`/admin_broadcast <msg>` — message all users\n"
        "`/admin_sysinfo` — VPS health\n\n"
        "*User Management*\n"
        "`/admin_wallets <tid>` — all wallets for user\n"
        "`/admin_suspend <tid>` — suspend all user bots\n"
        "`/admin_unsuspend <tid>` — reinstate user\n"
        "`/admin_wipe <tid> <lbl>` — full removal\n\n"
        "*Monitoring*\n"
        "`/admin_crashed` — active but not running\n"
        "`/admin_losses <usd>` — bots over drawdown\n"
        "`/admin_idle [hours]` — bots with no fills\n\n"
        "*Financial*\n"
        "`/admin_revenue` — estimated fee revenue\n"
        "`/admin_platform` — platform-wide analytics dashboard\n"
        "`/admin_analytics <tid> <lbl>` — per-user analytics\n"
        "`/admin_referrals` — referral stats\n\n"
        "*Ops*\n"
        "`/admin_update <tid> <lbl> <field> <val>` — patch DB\n"
        "`/admin_envdump <tid> <lbl>` — show .env\n"
        "`/admin_diskusage` — per-user dir sizes\n"
        "`/admin_setstate <tid> <lbl> <state>` — fix DB state\n"
        "`/admin_feed` — shared feed health\n"
        "`/admin_botstate <tid> <lbl>` — live bot internals\n"
        "`/admin_poll <question> | <opt1> | <opt2>` — send poll to all users\n"
        "`/admin_suspendall` — maintenance mode suspend all\n"
        "`/admin_unsuspendall` — resume all suspended bots\n"
        "`/admin_suspensions [tid]` — audit suspension log\n"
        "`/admin_orphans` — bot dirs with no DB entry\n"
        "`/admin_cleandir <tid> <lbl>` — archive + remove orphan dir\n"
    )
    await _send_chunked(update, text)

# ─────────────────────────────────────────────────────────────────────────────
# /admin_users
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        wallets = _conn.execute("SELECT * FROM users ORDER BY telegram_id, created_at").fetchall()
    if not wallets:
        return await update.message.reply_text("No wallets registered.")
    # Batch process check — read procs once instead of per-wallet DB queries
    with _mgr._lock:
        running_procs = {k: p for k, p in _mgr._procs.items() if p.poll() is None}

    lines = ["👥 *All Wallets* (" + str(len(wallets)) + ")\n"]
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        key     = (tid, label)
        proc    = running_procs.get(key)
        running = proc is not None
        pid     = proc.pid if running else None
        icon    = "🟢" if running else ("🔴" if w["state"] == "active" else "⚫")
        line    = icon + " @" + str(w["telegram_username"] or tid) + " " + str(tid) + " | " + label + "\n"
        line   += "   " + w["market"] + " · " + w["profile"] + " · " + w["state"]
        if running:
            line += " · PID=" + str(pid)
        lines.append(line)
    await _send_chunked(update, "\n".join(lines), parse_mode=None)

# ─────────────────────────────────────────────────────────────────────────────
# /admin_bots
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    if not _mgr:
        return await update.message.reply_text("Manager not initialised.")
    with _mgr._lock:
        procs = dict(_mgr._procs)
    if not procs:
        return await update.message.reply_text("No bots currently running.")
    lines = [f"⚙️ *Running Bots* ({len(procs)})\n"]
    for (tid, label), proc in procs.items():
        alive = proc.poll() is None
        lines.append(f"{'🟢' if alive else '💀'} `{tid}` › `{label}` PID={proc.pid}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /admin_status <tid> <label>
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_status <tid> <label>`", parse_mode="Markdown")
    wallet = get_wallet(tid, label)
    if not wallet:
        return await update.message.reply_text(f"No wallet found for `{tid}` › `{label}`", parse_mode="Markdown")
    status  = _mgr.status(tid, label) if _mgr else {}
    running = status.get("running", False)
    pid     = status.get("pid", "—")
    bdir    = _bot_dir(tid, label)
    db_path = os.path.join(bdir, "bot", "hotstuff.db")
    db_size = f"{os.path.getsize(db_path):,} bytes" if os.path.exists(db_path) else "missing"
    env_ok  = "✅" if os.path.exists(os.path.join(bdir, ".env")) else "❌"
    dir_ok  = "✅" if os.path.isdir(bdir) else "❌"
    text = (
        f"🔍 *@{wallet['telegram_username']} › `{label}`*\n\n"
        f"TID: `{tid}`\n"
        f"Market: `{wallet['market']}`\n"
        f"Profile: `{wallet['profile']}`\n"
        f"DB state: `{wallet['state']}`\n"
        f"Running: {'🟢 Yes' if running else '🔴 No'}\n"
        f"PID: `{pid}`\n"
        f"Bot dir: {dir_ok} `{bdir}`\n"
        f".env: {env_ok}\n"
        f"Trade DB: `{db_size}`\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ─────────────────────────────────────────────────────────────────────────────
# /admin_logs <tid> <label> [N]
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    args = context.args
    if not args or len(args) < 2:
        return await update.message.reply_text("Usage: `/admin_logs <tid> <label> [lines]`", parse_mode="Markdown")
    try:
        tid, label = int(args[0]), args[1]
        n = min(int(args[2]) if len(args) > 2 else 30, 100)
    except ValueError:
        return await update.message.reply_text("Invalid args.")
    # Read from persistent log file
    bot_dir  = os.path.join("/root/bots", f"user_{tid}_{label}")
    log_path = os.path.join(bot_dir, "bot.log")

    if not os.path.exists(log_path):
        return await update.message.reply_text(
            f"No log file for `{tid}` / `{label}` — bot may predate persistent logging.",
            parse_mode="Markdown")
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
        tail_lines = all_lines[-n:]
        tail = "".join(tail_lines).strip()
        if not tail:
            await update.message.reply_text(f"Log file empty for `{label}`.", parse_mode="Markdown")
        else:
            header = "Logs [" + label + "] -- last " + str(len(tail_lines)) + " lines:\n\n"

            msg    = header + tail
            if len(msg) > 4000:
                msg = header + "".join(tail_lines)[-3800:]
            await _send_chunked(update, msg, parse_mode=None)
    except Exception as e:
        await update.message.reply_text(f"Log read error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# /admin_restart  /admin_stop  /admin_kill
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_restart <tid> <label>`", parse_mode="Markdown")
    await update.message.reply_text(f"🔄 Restarting `{tid}` › `{label}`...", parse_mode="Markdown")
    try:
        ok = _mgr.restart_bot(tid, label)
        if ok:
            pid = _mgr.status(tid, label).get("pid", "?")
            await update.message.reply_text(f"✅ Restarted. PID={pid}", parse_mode="Markdown")
        else:
            await update.message.reply_text("❌ Restart failed — check wallet exists and keys are valid.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_admin_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_stop <tid> <label>`", parse_mode="Markdown")
    try:
        ok = _mgr.stop_bot(tid, label)
        await update.message.reply_text(
            f"⛔ Stopped `{tid}` › `{label}`." if ok else "Bot was not running.",
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def cmd_admin_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_kill <tid> <label>`", parse_mode="Markdown")
    with _mgr._lock:
        proc = _mgr._procs.get((tid, label))
    if not proc:
        return await update.message.reply_text("No running process found.")
    try:
        os.kill(proc.pid, signal.SIGKILL)
        await update.message.reply_text(
            f"💀 SIGKILL → PID {proc.pid} (`{tid}` › `{label}`).\n"
            f"Auto-restart in ≤60s if state=active.", parse_mode="Markdown")
    except ProcessLookupError:
        await update.message.reply_text("Process already dead.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

# ─────────────────────────────────────────────────────────────────────────────
# /admin_db <tid> <label>
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_db <tid> <label>`", parse_mode="Markdown")
    wallet = get_wallet(tid, label)
    if not wallet:
        return await update.message.reply_text("No wallet found.")
    rows = []
    for key in wallet.keys():
        val = "[redacted]" if ("encrypted" in key or "key" in key.lower()) else wallet[key]
        rows.append(f"  {key}: {val}")
    await _send_chunked(update, f"🗄 *DB: `{tid}` › `{label}`*\n\n```\n" + "\n".join(rows) + "\n```")

# ─────────────────────────────────────────────────────────────────────────────
# /admin_broadcast <message>
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    if not context.args:
        return await update.message.reply_text("Usage: `/admin_broadcast <message>`", parse_mode="Markdown")
    msg  = update.message.text.split(" ", 1)[1] if " " in update.message.text else ""
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        _all = _conn.execute("SELECT DISTINCT telegram_id FROM users").fetchall()
    tids = [r["telegram_id"] for r in _all]
    if not tids:
        return await update.message.reply_text("No registered users.")
    sent = failed = 0
    for tid in tids:
        try:
            await context.bot.send_message(tid, f"📢 *NenMMBot*\n\n{msg}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📢 Broadcast — ✅ {sent} sent, ❌ {failed} failed.")

# ─────────────────────────────────────────────────────────────────────────────
# /admin_sysinfo
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_sysinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    text = (
        f"🖥 *VPS System Info*\n\n"
        f"Uptime: `{_run('uptime -p')}`\n"
        f"Load: `{_run('cat /proc/loadavg')[:14]}`\n"
        f"CPU: `{_run('grep -c processor /proc/cpuinfo')} vCPU`\n"
        f"RAM: `{_run('free -h | grep Mem | tr -s " " | cut -d" " -f3')}/{_run('free -h | grep Mem | tr -s " " | cut -d" " -f2')}`\n"
        f"Disk: `{_run('df -h / | tail -1 | tr -s " " | cut -d" " -f3')}/{_run('df -h / | tail -1 | tr -s " " | cut -d" " -f2')} ({_run('df -h / | tail -1 | tr -s " " | cut -d" " -f5')} used)`\n"
        f"Bot procs: `{_run('pgrep -af bot.main | grep -v pgrep | wc -l')}`\n"
        f"Time: `{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# ═════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

# /admin_wallets <tid>
async def cmd_admin_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid = _parse_tid(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_wallets <tid>`", parse_mode="Markdown")
    wallets = _all_wallets_for_user(tid)
    if not wallets:
        return await update.message.reply_text(f"No wallets found for `{tid}`.", parse_mode="Markdown")
    lines = [f"👤 *Wallets for `{tid}`* ({len(wallets)})\n"]
    for w in wallets:
        status  = _mgr.status(tid, w["label"]) if _mgr else {}
        running = status.get("running", False)
        icon    = "🟢" if running else ("🔴" if w["state"] == "active" else "⚫")
        lines.append(f"{icon} `{w['label']}` — {w['market']} · {w['profile']} · {w['state']}")
    await _send_chunked(update, "\n".join(lines))

# /admin_suspend <tid>
async def cmd_admin_suspend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid = _parse_tid(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_suspend <tid>`", parse_mode="Markdown")
    wallets = _all_wallets_for_user(tid)
    if not wallets:
        return await update.message.reply_text(f"No wallets for `{tid}`.", parse_mode="Markdown")
    stopped = 0
    for w in wallets:
        label = w["label"]
        set_wallet_state(tid, label, "suspended")  # set BEFORE stop so _check_crashes won't restart
        try:
            _mgr.stop_bot(tid, label)
        except Exception:
            pass
        log_suspension(
            tid=tid, label=label, action="suspended", reason="manual",
            triggered_by="admin",
            username=str(w["telegram_username"] or tid),
            market=w["market"],
        )
        stopped += 1
    await update.message.reply_text(
        f"⛔ Suspended `{tid}` — {stopped} wallet(s) stopped and marked suspended.\n"
        f"Use `/admin_unsuspend {tid}` to reinstate.", parse_mode="Markdown")

# /admin_unsuspend <tid>
async def cmd_admin_unsuspend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid = _parse_tid(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_unsuspend <tid>`", parse_mode="Markdown")
    wallets = _all_wallets_for_user(tid)
    if not wallets:
        return await update.message.reply_text(f"No wallets for `{tid}`.", parse_mode="Markdown")
    reinstated = 0
    for w in wallets:
        if w["state"] == "suspended":
            set_wallet_state(tid, w["label"], "stopped")
            log_suspension(
                tid=tid, label=w["label"], action="resumed", reason="manual",
                triggered_by="admin",
                username=str(w["telegram_username"] or tid),
                market=w["market"],
            )
            reinstated += 1
    await update.message.reply_text(
        f"✅ `{tid}` unsuspended — {reinstated} wallet(s) set to stopped.\n"
        f"User can now `/resume` their bots.", parse_mode="Markdown")

# /admin_wipe <tid> <label>
async def cmd_admin_wipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_wipe <tid> <label>`", parse_mode="Markdown")
    steps = []
    # Stop
    try:
        _mgr.stop_bot(tid, label)
        steps.append("✅ Process stopped")
    except Exception as e:
        steps.append(f"⚠️ Stop: {e}")
    # Delete bot dir
    bdir = _bot_dir(tid, label)
    if os.path.isdir(bdir):
        try:
            shutil.rmtree(bdir)
            steps.append(f"✅ Dir removed: `{bdir}`")
        except Exception as e:
            steps.append(f"❌ Dir removal failed: {e}")
    else:
        steps.append("ℹ️ Bot dir not found (already gone)")
    # Remove from DB
    try:
        with get_conn() as conn:
            conn.execute("DELETE FROM users WHERE telegram_id=? AND label=?", (tid, label))
        steps.append("✅ DB row deleted")
    except Exception as e:
        steps.append(f"❌ DB delete failed: {e}")
    await _send_chunked(update,
        f"🗑 *Wipe: `{tid}` › `{label}`*\n\n" + "\n".join(steps))

# ═════════════════════════════════════════════════════════════════════════════
# MONITORING
# ═════════════════════════════════════════════════════════════════════════════

# /admin_crashed
async def cmd_admin_crashed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    wallets = get_all_active_wallets()
    crashed = []
    for w in wallets:
        if w["state"] != "active":
            continue
        status = _mgr.status(w["telegram_id"], w["label"]) if _mgr else {}
        if not status.get("running", False):
            crashed.append(w)
    if not crashed:
        return await update.message.reply_text("✅ No crashed bots — all active wallets are running.")
    lines = [f"💀 *Crashed Bots* ({len(crashed)} — state=active but not running)\n"]
    for w in crashed:
        lines.append(
            f"• @{w['telegram_username'] or w['telegram_id']} `{w['telegram_id']}` › `{w['label']}`\n"
            f"  {w['market']} · {w['profile']}"
        )
    lines.append(f"\nUse `/admin_restart <tid> <label>` to recover.")
    await _send_chunked(update, "\n".join(lines))

# /admin_losses <threshold_usd>
async def cmd_admin_losses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    try:
        threshold = float(context.args[0]) if context.args else 10.0
    except ValueError:
        threshold = 10.0
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        wallets = _conn.execute("SELECT * FROM users").fetchall()
    over_limit = []
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        db_path = os.path.join(_bot_dir(tid, label), "bot", "hotstuff.db")
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            # Try common drawdown table names
            for table in ("drawdown", "daily_pnl", "session"):
                try:
                    row = conn.execute(
                        f"SELECT MIN(pnl) as worst FROM {table}"
                    ).fetchone()
                    if row and row[0] is not None and row[0] < -threshold:
                        over_limit.append((w, row[0]))
                    break
                except sqlite3.OperationalError:
                    continue
            conn.close()
        except Exception:
            continue
    if not over_limit:
        await update.message.reply_text(
            f"✅ No bots exceeding ${threshold:.2f} drawdown threshold.")
    else:
        lines = [f"🔴 *Bots over ${threshold:.2f} loss*\n"]
        for w, worst in over_limit:
            lines.append(
                f"• @{w['telegram_username']} `{w['telegram_id']}` › `{w['label']}` "
                f"worst PnL: `${worst:.2f}`"
            )
        await _send_chunked(update, "\n".join(lines))

# /admin_idle [hours]
async def cmd_admin_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    try:
        hours = int(context.args[0]) if context.args else 2
    except ValueError:
        hours = 2
    cutoff  = datetime.now(timezone.utc).timestamp() - hours * 3600
    wallets = get_all_active_wallets()
    idle    = []
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        status = _mgr.status(tid, label) if _mgr else {}
        if not status.get("running", False):
            continue  # already caught by /admin_crashed
        db_path = os.path.join(_bot_dir(tid, label), "bot", "hotstuff.db")
        if not os.path.exists(db_path):
            idle.append((w, "no DB"))
            continue
        try:
            conn = sqlite3.connect(db_path)
            last_fill = None
            for table in ("fills", "trades", "markout"):
                try:
                    row = conn.execute(
                        f"SELECT MAX(timestamp) FROM {table}"
                    ).fetchone()
                    if row and row[0]:
                        last_fill = float(row[0])
                    break
                except sqlite3.OperationalError:
                    continue
            conn.close()
            if last_fill is None or last_fill < cutoff:
                since = "never" if last_fill is None else f"{(datetime.now(timezone.utc).timestamp()-last_fill)/3600:.1f}h ago"
                idle.append((w, since))
        except Exception:
            idle.append((w, "DB error"))
    if not idle:
        await update.message.reply_text(f"✅ No idle bots (all had fills in last {hours}h).")
    else:
        lines = [f"😴 *Idle Bots* (running, no fills in {hours}h)\n"]
        for w, since in idle:
            lines.append(
                f"• @{w['telegram_username']} `{w['telegram_id']}` › `{w['label']}` — last fill: {since}"
            )
        await _send_chunked(update, "\n".join(lines))

# ═════════════════════════════════════════════════════════════════════════════
# FINANCIAL
# ═════════════════════════════════════════════════════════════════════════════

# /admin_revenue
async def cmd_admin_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        wallets = _conn.execute("SELECT * FROM users").fetchall()
    total_vol      = 0.0
    total_taker    = 0.0
    total_rebates  = 0.0
    bot_count      = 0
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        db_path = os.path.join(_bot_dir(tid, label), "bot", "hotstuff.db")
        if not os.path.exists(db_path):
            continue
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT SUM(notional) vol, "
                "SUM(CASE WHEN fee > 0 THEN fee ELSE 0 END) taker, "
                "SUM(CASE WHEN fee < 0 THEN ABS(fee) ELSE 0 END) rebates "
                "FROM fills WHERE address=?",
                (w["wallet_address"],)
            ).fetchone()
            conn.close()
            if row and (row["vol"] or row["taker"]):
                total_vol     += row["vol"] or 0
                total_taker   += row["taker"] or 0
                total_rebates += row["rebates"] or 0
                bot_count     += 1
        except Exception:
            continue
    net_fees = total_taker - total_rebates
    text = (
        f"\U0001F4B0 *Revenue — Actual Fees*\n\n"
        f"Bots with data: `{bot_count}`\n"
        f"Total volume: `${total_vol:,.2f}`\n"
        f"Taker fees paid: `${total_taker:,.4f}`\n"
        f"Maker rebates: `${total_rebates:,.4f}`\n"
        f"Net fees: `${net_fees:,.4f}`\n"
        f"Avg taker rate: `{total_taker / total_vol * 10000:.2f}bps`" if total_vol > 0 else ""
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# /admin_referrals
REFERRER_ADDRESS = "0xYOUR_REFERRER_WALLET_ADDRESS"

# ── Points program constants ──────────────────────────────────────────────
RETRO_POINTS       = 1_400_000          # retroactive airdrop
TOTAL_WEEKS        = 30                 # program length
COMPLETED_WEEKS    = 6                  # weeks fully distributed  ← bump weekly
COMPLETED_RATE     = 500_000            # rate for all completed weeks
TOTAL_WEEKS_LEFT   = TOTAL_WEEKS - COMPLETED_WEEKS
DISTRIBUTED        = RETRO_POINTS + (COMPLETED_WEEKS * COMPLETED_RATE)  # 4.4M

# Weekly pool tier rates
TIER_RATES = [500_000, 1_000_000, 1_250_000, 1_500_000]
# Blended projections: average of adjacent tiers for remaining weeks
#   Low:      avg(500K, 500K)  = 500K/wk   → smallest pool (best case for holders)
#   Low-Mid:  avg(500K, 1M)    = 750K/wk
#   Mid-High: avg(1M, 1.25M)   = 1.125M/wk
#   High:     avg(1.25M, 1.5M) = 1.375M/wk → largest pool (most diluted)
BLEND_RATES = [
    (TIER_RATES[0] + TIER_RATES[0]) / 2,   # 500K
    (TIER_RATES[0] + TIER_RATES[1]) / 2,   # 750K
    (TIER_RATES[1] + TIER_RATES[2]) / 2,   # 1.125M
    (TIER_RATES[2] + TIER_RATES[3]) / 2,   # 1.375M
]
POOL_LOW  = DISTRIBUTED + int(TOTAL_WEEKS_LEFT * BLEND_RATES[0])   # 16.4M
POOL_MID  = DISTRIBUTED + int(TOTAL_WEEKS_LEFT * BLEND_RATES[1])   # 22.4M
POOL_HIGH = DISTRIBUTED + int(TOTAL_WEEKS_LEFT * BLEND_RATES[-1])  # 37.4M

FDV_SCENARIOS       = [10_000_000, 50_000_000, 100_000_000, 200_000_000]
SUPPLY_PCTS         = [0.10, 0.20, 0.30]


def _fmt_num(n):
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(int(n))


def _fmt_usd(n):
    if abs(n) >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n/1_000:.1f}K"
    if abs(n) >= 1:
        return f"${n:,.2f}"
    if abs(n) >= 0.01:
        return f"${n:,.2f}"
    return f"${n:,.4f}"


async def cmd_admin_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    await update.message.reply_text("Fetching referral data from Hotstuff...")

    try:
        import requests as _req
        from eth_utils import to_checksum_address as _cs
        resp = _req.post(
            "https://api.hotstuff.trade/info",
            json={"method": "referralSummary", "params": {"user": _cs(REFERRER_ADDRESS)}},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        data = resp.json()
    except Exception as e:
        await update.message.reply_text("Referral API error: " + str(e))
        return

    if not data or "error" in data:
        await update.message.reply_text("Referral API returned error: " + str(data))
        return

    # ── Parse referral API response ───────────────────────────────────────
    total_vol   = float(data.get("total_referred_volume") or 0)
    rolling_vol = float(data.get("rolling_referred_volume") or 0)
    referred    = data.get("referred_users") or {}
    tier        = data.get("referral_tier") or {}
    commission  = float(tier.get("referrer_commission") or 0)
    to_claim    = float(data.get("to_claim_perp_rewards") or 0)
    claimed     = float(data.get("claimed_perp_rewards") or 0)

    # ── Build wallet→username map from NenBot DB ──────────────────────────
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        wallets = _conn.execute("SELECT * FROM users").fetchall()
    addr_to_user = {}
    for w in wallets:
        addr = (w["wallet_address"] or "").lower()
        if addr:
            uname = w["telegram_username"] or w["label"] or str(w["telegram_id"])
            addr_to_user[addr] = uname

    # ── Collect actual fees per user from per-user DBs ────────────────────
    user_fee_data = {}
    for w in wallets:
        tid_w, label_w = w["telegram_id"], w["label"]
        addr = (w["wallet_address"] or "").lower()
        if not addr:
            continue
        db_path = os.path.join(_bot_dir(tid_w, label_w), "bot", "hotstuff.db")
        if not os.path.exists(db_path):
            continue
        try:
            _uconn = sqlite3.connect(db_path)
            row = _uconn.execute(
                "SELECT SUM(CASE WHEN fee > 0 THEN fee ELSE 0 END) taker_fees, "
                "SUM(CASE WHEN fee < 0 THEN ABS(fee) ELSE 0 END) maker_rebates, "
                "SUM(notional) vol "
                "FROM fills WHERE address=?",
                (w["wallet_address"],)
            ).fetchone()
            _uconn.close()
            if row and (row[0] or row[1]):
                taker = row["taker_fees"] or 0
                rebates = row["maker_rebates"] or 0
                if addr in user_fee_data:
                    user_fee_data[addr]["taker_fees"]    += taker
                    user_fee_data[addr]["maker_rebates"] += rebates
                    user_fee_data[addr]["vol"]           += abs(row["vol"]) if row["vol"] else 0
                else:
                    user_fee_data[addr] = {
                        "taker_fees": taker,
                        "maker_rebates": rebates,
                        "vol": abs(row["vol"]) if row["vol"] else 0,
                        "label": label_w,
                        "uname": w["telegram_username"] or label_w,
                    }
        except Exception:
            continue

    total_users   = len({w["telegram_id"] for w in wallets})
    total_wallets = len(wallets)
    platform_total_taker   = sum(d["taker_fees"] for d in user_fee_data.values())
    platform_total_rebates = sum(d["maker_rebates"] for d in user_fee_data.values())

    # ══════════════════════════════════════════════════════════════════════
    # Build output
    # ══════════════════════════════════════════════════════════════════════

    lines = ["\U0001F517 *Referral Summary*\n"]
    lines.append(f"Total referred volume: `${total_vol:,.2f}`")
    lines.append(f"Rolling volume:        `${rolling_vol:,.2f}`")
    lines.append(f"Commission rate:        `{commission*100:.1f}%`")
    lines.append(f"Unclaimed rewards:     `${to_claim:.4f}`")
    lines.append(f"Claimed rewards:       `${claimed:.4f}`")
    lines.append(f"Referred wallets:       `{len(referred)}`")

    # ── Per-user breakdown with actual fees + referral rewards ──────────────
    if referred:
        lines.append("")
        lines.append("\U0001F465 *Referred Users*")
        sorted_users = sorted(
            referred.items(),
            key=lambda x: float(x[1].get("referred_volume") or 0) if isinstance(x[1], dict) else 0,
            reverse=True
        )
        total_ref_rewards = 0.0
        for addr, u in sorted_users:
            if not isinstance(u, dict):
                continue
            vol        = float(u.get("referred_volume") or 0)
            ref_reward = float(u.get("referred_perp_rewards") or 0) + float(u.get("referred_spot_rewards") or 0)
            joined     = int(u.get("joined_at") or 0)
            total_ref_rewards += ref_reward
            from datetime import datetime as _dt
            joined_str = _dt.utcfromtimestamp(joined/1000).strftime("%Y-%m-%d") if joined else "?"
            addr_lower = addr.lower()
            display = addr_to_user.get(addr_lower, addr[:8] + "\u2026" + addr[-4:])
            fee_info = user_fee_data.get(addr_lower)
            if fee_info:
                tk = fee_info["taker_fees"]
                mk = fee_info["maker_rebates"]
                fee_str = f" | taker `${tk:,.2f}` rebates `${mk:,.2f}`"
            else:
                fee_str = ""
            lines.append(
                f"  `{display}` | {joined_str} | "
                f"vol `${vol:,.0f}`{fee_str} | "
                f"earned `${ref_reward:,.4f}`"
            )

    # ── Fee totals ─────────────────────────────────────────────────────────
    lines.append("")
    lines.append("\U0001F4B0 *Fee Summary*")
    lines.append(f"  Taker fees paid (all users): `${platform_total_taker:,.2f}`")
    lines.append(f"  Maker rebates earned:        `${platform_total_rebates:,.2f}`")
    lines.append(f"  Net fees:                    `${platform_total_taker - platform_total_rebates:,.2f}`")
    lines.append(f"  Total ref rewards:           `${total_ref_rewards:,.4f}`")
    lines.append(f"  Claimed so far:              `${claimed:.4f}`")
    lines.append(f"  Pending claim:               `${to_claim:.4f}`")

    # ── NenBot user count ──────────────────────────────────────────────────
    lines.append(f"\nNenBot users:   `{total_users}`")
    lines.append(f"NenBot wallets: `{total_wallets}`")

    # ── Points Value Calculator (blended range) ───────────────────────────
    lines.append("")
    lines.append("\U0001F3AF *Points Value Calculator*")
    lines.append(f"  Distributed: `{_fmt_num(DISTRIBUTED)}` pts (wk {COMPLETED_WEEKS}/{TOTAL_WEEKS})")
    lines.append(f"  Pool: `{_fmt_num(POOL_LOW)}` / `{_fmt_num(POOL_MID)}` / `{_fmt_num(POOL_HIGH)}` pts")
    lines.append(f"  _{TOTAL_WEEKS_LEFT} wks left \u00b7 blended tier projections_")

    for fdv in FDV_SCENARIOS:
        lines.append("")
        lines.append(f"\U0001F4B0 *{_fmt_num(fdv)} FDV* (per 1M pts)")
        for pct in SUPPLY_PCTS:
            vals = []
            for pool in [POOL_LOW, POOL_MID, POOL_HIGH]:
                val = (fdv * pct) / pool * 1_000_000
                vals.append(_fmt_usd(val))
            lines.append(f"  {int(pct*100)}% \u2192 {vals[0]} / {vals[1]} / {vals[2]}")

    lines.append(f"\n_Values shown as Low / Mid / High pool estimates._")
    lines.append(f"\nhttps://app.hotstuff.trade/join/YOUR_REFERRAL_CODE")

    await _send_chunked(update, "\n".join(lines), parse_mode="Markdown")

# ═════════════════════════════════════════════════════════════════════════════
# OPS
# ═════════════════════════════════════════════════════════════════════════════

# /admin_update <tid> <label> <field> <value>
ALLOWED_FIELDS = {"market", "profile", "state", "telegram_username"}

async def cmd_admin_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    args = context.args
    if not args or len(args) < 4:
        return await update.message.reply_text(
            "Usage: `/admin_update <tid> <label> <field> <value>`\n"
            f"Allowed fields: `{', '.join(sorted(ALLOWED_FIELDS))}`",
            parse_mode="Markdown")
    try:
        tid   = int(args[0])
        label = args[1]
        field = args[2]
        value = " ".join(args[3:])
    except ValueError:
        return await update.message.reply_text("Invalid tid.")
    if field not in ALLOWED_FIELDS:
        return await update.message.reply_text(
            f"Field `{field}` not allowed.\nAllowed: `{', '.join(sorted(ALLOWED_FIELDS))}`",
            parse_mode="Markdown")
    wallet = get_wallet(tid, label)
    if not wallet:
        return await update.message.reply_text("Wallet not found.")
    try:
        with get_conn() as conn:
            conn.execute(
                f"UPDATE users SET {field}=? WHERE telegram_id=? AND label=?",
                (value, tid, label)
            )
        await update.message.reply_text(
            f"✅ Updated `{field}` → `{value}` for `{tid}` › `{label}`.\n"
            f"Restart the bot for changes to take effect.",
            parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ DB update failed: {e}")

# /admin_envdump <tid> <label>
async def cmd_admin_envdump(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: `/admin_envdump <tid> <label>`", parse_mode="Markdown")
    env_path = os.path.join(_bot_dir(tid, label), ".env")
    if not os.path.exists(env_path):
        return await update.message.reply_text(
            f"No .env found at `{env_path}`\nBot dir may not have been provisioned.",
            parse_mode="Markdown")
    lines = []
    with open(env_path) as f:
        for line in f:
            line = line.rstrip()
            if not line or line.startswith("#"):
                lines.append(line)
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                if any(s in key.upper() for s in ("KEY", "TOKEN", "SECRET", "PRIVATE")):
                    lines.append(f"{key}=[redacted]")
                else:
                    lines.append(line)
            else:
                lines.append(line)
    text = f"📄 *.env: `{tid}` › `{label}`*\n\n```\n" + "\n".join(lines) + "\n```"
    await _send_chunked(update, text)

# /admin_diskusage
async def cmd_admin_diskusage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    if not os.path.isdir(BOTS_DIR):
        return await update.message.reply_text(f"`{BOTS_DIR}` not found.", parse_mode="Markdown")
    entries = []
    total   = 0
    for name in sorted(os.listdir(BOTS_DIR)):
        full = os.path.join(BOTS_DIR, name)
        if not os.path.isdir(full):
            continue
        try:
            result = subprocess.check_output(
                ["du", "-sb", full], text=True, timeout=5
            )
            size = int(result.split()[0])
            total += size
            entries.append((size, name))
        except Exception:
            entries.append((0, name))
    entries.sort(reverse=True)

    def fmt(b):
        if b >= 1_048_576: return f"{b/1_048_576:.1f}MB"
        if b >= 1024:      return f"{b/1024:.1f}KB"
        return f"{b}B"

    lines = [f"💾 *Disk Usage: `{BOTS_DIR}`*\n\nTotal: `{fmt(total)}`\n"]
    for size, name in entries[:30]:  # cap at 30
        lines.append(f"  `{fmt(size):>8}` {name}")
    if len(entries) > 30:
        lines.append(f"  ... and {len(entries)-30} more")
    await _send_chunked(update, "\n".join(lines))

# /admin_setstate <tid> <label> <state>
VALID_STATES = {"active", "stopped", "suspended", "error"}

async def cmd_admin_setstate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    args = context.args
    if not args or len(args) < 3:
        return await update.message.reply_text(
            f"Usage: `/admin_setstate <tid> <label> <state>`\n"
            f"Valid states: `{', '.join(sorted(VALID_STATES))}`",
            parse_mode="Markdown")
    try:
        tid, label, state = int(args[0]), args[1], args[2]
    except ValueError:
        return await update.message.reply_text("Invalid tid.")
    if state not in VALID_STATES:
        return await update.message.reply_text(
            f"Invalid state `{state}`.\nValid: `{', '.join(sorted(VALID_STATES))}`",
            parse_mode="Markdown")
    wallet = get_wallet(tid, label)
    if not wallet:
        return await update.message.reply_text("Wallet not found.")
    try:
        set_wallet_state(tid, label, state)
        await update.message.reply_text(
            f"✅ `{tid}` › `{label}` state → `{state}`.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ─────────────────────────────────────────────────────────────────────────────
# /admin_feed — shared feed status
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)

    import socket as _socket
    import json as _json
    import time as _time

    MARKETS = ["BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP", "ZEC-PERP", "XRP-PERP", "BNB-PERP", "GOLD-PERP", "SILVER-PERP", "USA500-PERP", "USA100-PERP"]
    FEED_DIR = "/tmp"

    def _sock_path(m):
        return f"{FEED_DIR}/nenfeed_{m.replace('-','_')}.sock"

    def _check_market(m):
        path = _sock_path(m)
        if not os.path.exists(path):
            return {"status": "❌ no socket", "bbo": None}
        try:
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(path)
            sock.settimeout(3.0)
            buf = ""
            while "\n" not in buf:
                chunk = sock.recv(1024).decode()
                if not chunk:
                    break
                buf += chunk
            sock.close()
            line = buf.split("\n")[0].strip()
            data = _json.loads(line)
            age  = round(_time.time() - data.get("ts", 0), 1)
            return {
                "status": "🟢 live",
                "bid": data.get("bid"),
                "ask": data.get("ask"),
                "age": age,
            }
        except Exception as e:
            return {"status": f"⚠️ error: {e}", "bbo": None}

    lines = ["📡 *Shared Feed Status*\n"]
    for m in MARKETS:
        info = _check_market(m)
        if info.get("bid"):
            lines.append(
                f"  {info['status']} `{m}` — bid={info['bid']} ask={info['ask']} age={info['age']}s"
            )
        else:
            lines.append(f"  {info['status']} `{m}`")

    # Check nenfeed service
    import subprocess as _sp
    r = _sp.run(["systemctl", "is-active", "nenfeed"], capture_output=True, text=True)
    svc = r.stdout.strip()
    lines.append(f"\nnenfeed.service: `{svc}`")

    await _send_chunked(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /admin_botstate <tid> <label>
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# /admin_botstate <tid> <label>
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_botstate(update, context):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text("Usage: /admin_botstate <tid> <label>")
    import json as _json, sqlite3 as _sqlite3, time as _time
    wallet = get_wallet(tid, label)
    if not wallet:
        return await update.message.reply_text("Wallet not found.")
    status  = _mgr.status(tid, label) if _mgr else {}
    running = status.get("running", False)
    pid     = status.get("pid", "---")
    bot_dir = os.path.join("/root/bots", "user_" + str(tid) + "_" + label, "bot")
    uname   = wallet["telegram_username"] or str(tid)
    out = []
    out.append("*Bot State: @" + uname + " | " + label + "*\n")
    out.append("PID: " + str(pid) + " | " + ("Running" if running else "Stopped"))
    out.append("Market: " + wallet["market"] + " | Profile: " + wallet["profile"] + "\n")
    regime_path = os.path.join(bot_dir, "regime_state.json")
    if os.path.exists(regime_path):
        try:
            rdata = _json.load(open(regime_path))
            for mkt, r in rdata.items():
                age = round(_time.time() - r.get("updated_at", 0))
                out.append("Regime " + mkt + ": " + str(r.get("regime","?"))
                    + " | ADX " + str(round(r.get("adx",0),1))
                    + " | +DI " + str(round(r.get("plus_di",0),1))
                    + " | -DI " + str(round(r.get("minus_di",0),1))
                    + " | BidMult " + str(r.get("bid_mult",1)) + "x"
                    + " | AskMult " + str(r.get("ask_mult",1)) + "x"
                    + " | age " + str(age) + "s")
        except Exception as e:
            out.append("Regime: error " + str(e))
    else:
        out.append("Regime: no state file")
    ofi_path = os.path.join(bot_dir, "ofi_state.json")
    if os.path.exists(ofi_path):
        try:
            odata = _json.load(open(ofi_path))
            for mkt, o in odata.items():
                age = round(_time.time() - o.get("updated_at", 0))
                out.append("OFI " + mkt + ": " + str(o.get("label","?"))
                    + " | val " + str(round(o.get("ofi",0),3))
                    + " | smooth " + str(round(o.get("ofi_smooth",0),3))
                    + " | BidVol $" + str(round(o.get("bid_volume",0)))
                    + " | AskVol $" + str(round(o.get("ask_volume",0)))
                    + " | age " + str(age) + "s")
        except Exception as e:
            out.append("OFI: error " + str(e))
    else:
        out.append("OFI: no state file")
    db_path = os.path.join(bot_dir, "hotstuff.db")
    if os.path.exists(db_path):
        try:
            conn = _sqlite3.connect(db_path)
            conn.row_factory = _sqlite3.Row
            orders = conn.execute("SELECT side, price, placed_at FROM placed_orders ORDER BY placed_at DESC LIMIT 4").fetchall()
            out.append("\nRecent Orders:")
            for o in orders:
                age = round(_time.time() - o["placed_at"])
                out.append("  " + o["side"] + " @ " + str(o["price"]) + " | " + str(age) + "s ago")
            if not orders:
                out.append("  none")
            fill = conn.execute("SELECT side, notional, closed_pnl, timestamp FROM fills ORDER BY rowid DESC LIMIT 1").fetchone()
            if fill:
                ts  = float(fill["timestamp"]) / 1000
                age = round(_time.time() - ts)
                out.append("\nLast Fill: " + fill["side"]
                    + " $" + str(round(fill["notional"],2))
                    + " | PnL $" + str(round(fill["closed_pnl"],4))
                    + " | " + str(age) + "s ago")
            else:
                out.append("\nLast Fill: none")
            snap = conn.execute("SELECT equity, upnl FROM account_snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            if snap:
                out.append("\nAccount: Equity $" + str(round(snap["equity"],2))
                    + " | uPnL $" + str(round(snap["upnl"],4)))
            conn.close()
        except Exception as e:
            out.append("\nDB error: " + str(e))
    else:
        out.append("\nNo trade DB found")
    await _send_chunked(update, "\n".join(out))


# ─────────────────────────────────────────────────────────────────────────────
# /admin_poll <question> | <opt1> | <opt2> ...
# ─────────────────────────────────────────────────────────────────────────────
_active_poll: dict = {}  # stores current poll info

async def cmd_admin_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    if not context.args:
        return await update.message.reply_text(
            "Usage: /admin_poll <question> | <option1> | <option2> [| <option3>]"
        )
    text  = " ".join(context.args)
    parts = [p.strip() for p in text.split("|")]
    if len(parts) < 3:
        return await update.message.reply_text(
            "Need at least a question and 2 options separated by |"
        )
    question = parts[0]
    options  = parts[1:]
    if len(options) > 10:
        return await update.message.reply_text("Max 10 options.")

    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        _all = _conn.execute("SELECT DISTINCT telegram_id FROM users").fetchall()
    tids = [r["telegram_id"] for r in _all]
    if not tids:
        return await update.message.reply_text("No registered users.")

    sent = failed = 0
    poll_ids = []
    for tid in tids:
        try:
            msg = await context.bot.send_poll(
                chat_id=tid,
                question=question,
                options=options,
                is_anonymous=False,
            )
            poll_ids.append(msg.poll.id)
            sent += 1
        except Exception as e:
            failed += 1

    # Store poll info for answer tracking
    _active_poll.clear()
    _active_poll["question"] = question
    _active_poll["options"]  = options
    _active_poll["poll_ids"] = poll_ids

    await update.message.reply_text(
        "Poll sent to " + str(sent) + " user(s). Failed: " + str(failed) + "\n"
        "Answers will be forwarded to you as they come in."
    )


async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer   = update.poll_answer
    user_id  = answer.user.id
    username = answer.user.username or str(user_id)
    selected = [_active_poll["options"][i] for i in answer.option_ids] if _active_poll.get("options") else answer.option_ids
    question = _active_poll.get("question", "Unknown poll")
    try:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text="Poll answer from @" + username + " (" + str(user_id) + "):\n"
                 "Q: " + question + "\n"
                 "A: " + ", ".join(str(s) for s in selected)
        )
    except Exception as e:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# /admin_suspendall — suspend all active bots (maintenance mode)
# /admin_unsuspendall — resume all suspended bots
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_suspendall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        wallets = _conn.execute(
            "SELECT * FROM users WHERE state NOT IN ('suspended')"
        ).fetchall()
    if not wallets:
        return await update.message.reply_text("No wallets to suspend.")

    # Pre-mark all as suspended BEFORE any signals so _check_crashes won't restart them
    for w in wallets:
        set_wallet_state(w["telegram_id"], w["label"], "suspended")

    await update.message.reply_text(
        "⛔ Sending close signal to all bots — cancelling orders and closing positions..."
    )

    import signal as _signal, asyncio as _asyncio

    # Step 1 — SIGUSR2 all running bots
    with _mgr._lock:
        procs = dict(_mgr._procs)

    for (tid, label), proc in procs.items():
        if proc.poll() is None:
            try:
                os.kill(proc.pid, _signal.SIGUSR2)
            except Exception as e:
                log.warning("suspendall SIGUSR2 failed " + str(tid) + "/" + label + ": " + str(e))

    # Step 2 — wait up to 15s for all to self-terminate
    for _ in range(15):
        await _asyncio.sleep(1)
        with _mgr._lock:
            still_running = [k for k, p in _mgr._procs.items() if p.poll() is None]
        if not still_running:
            break

    # Step 2b — exchange-level cancel for any wallets with remaining open orders
    import sys as _sys, time as _time
    _sys.path.insert(0, "/root/python-sdk")
    cancel_ok = cancel_fail = 0
    for w in wallets:
        try:
            wallet_row = get_wallet(w["telegram_id"], w["label"])
            if not wallet_row:
                continue
            agent_key = _mgr._vault.decrypt(wallet_row["encrypted_agent_key"])
            from eth_account import Account as _Acct
            from hotstuff.apis.exchange import ExchangeClient as _EC
            from hotstuff.methods.exchange.trading import CancelAllParams as _CAP
            wallet_account = _Acct.from_key(agent_key)
            ec = _EC(wallet=wallet_account, is_testnet=False)
            expires = int(_time.time() * 1000) + 30000
            ec.cancel_all(_CAP(expiresAfter=expires))
            cancel_ok += 1
            log.info(f"suspendall: cancelled orders for {w['telegram_id']}/{w['label']}")
        except Exception as e:
            cancel_fail += 1
            log.warning(f"suspendall: exchange cancel failed for {w['telegram_id']}/{w['label']}: {e}")
    if cancel_ok or cancel_fail:
        log.info(f"suspendall exchange cancel: {cancel_ok} ok, {cancel_fail} failed")

    # Step 3 — force stop and suspend all
    stopped = 0
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        set_wallet_state(tid, label, "suspended")  # set BEFORE stop so _check_crashes won't restart
        if _mgr:
            _mgr.stop_bot(tid, label)
        log_suspension(
            tid=tid, label=label, action="suspended", reason="suspendall",
            triggered_by="admin",
            username=str(w["telegram_username"] or tid),
            market=w["market"],
        )
        stopped += 1

    await update.message.reply_text(
        "⛔ Maintenance mode — " + str(stopped) + " bot(s) suspended.\n"
        "Use /admin_unsuspendall to resume all."
    )

    for w in wallets:
        try:
            await context.bot.send_message(
                w["telegram_id"],
                "⛔ NenBot has paused your bot due to scheduled exchange maintenance.\n\n"
                "Orders cancelled and positions closed. Your bot will resume once maintenance is complete."
            )
        except Exception:
            pass

async def cmd_admin_unsuspendall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    with get_conn() as _conn:
        _conn.row_factory = sqlite3.Row
        wallets = _conn.execute("SELECT * FROM users WHERE state = ?", ("suspended",)).fetchall()
    if not wallets:
        return await update.message.reply_text("No suspended wallets.")
    resumed = 0
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        set_wallet_state(tid, label, "active")
        if _mgr:
            _mgr.start_bot(tid, label)
        log_suspension(
            tid=tid, label=label, action="resumed", reason="unsuspendall",
            triggered_by="admin",
            username=str(w["telegram_username"] or tid),
            market=w["market"],
        )
        resumed += 1
    await update.message.reply_text(
        "✅ Maintenance over — " + str(resumed) + " bot(s) resumed."
    )
    for w in wallets:
        try:
            await context.bot.send_message(
                w["telegram_id"],
                "✅ Exchange maintenance is complete. Your NenBot is back online and trading."
            )
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# /admin_suspensions [tid] — audit suspension log
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_suspensions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    from datetime import datetime, timezone as _tz
    tid  = _parse_tid(context) if context.args else None
    PAGE = 20
    rows  = get_suspensions(tid=tid, limit=PAGE)
    total = count_suspensions(tid=tid)
    if not rows:
        msg = "No suspension records" + (f" for `{tid}`" if tid else "") + "."
        return await update.message.reply_text(msg, parse_mode="Markdown")
    header = "📋 *Suspension Log*" + (f" — `{tid}`" if tid else "") + f" (latest {len(rows)} of {total})\n"
    lines  = [header]
    for r in rows:
        ts    = datetime.fromtimestamp(r["timestamp"], tz=_tz.utc).strftime("%m-%d %H:%M")
        icon  = "⛔" if r["action"] == "suspended" else "✅"
        uname = r["username"] or str(r["tid"])
        mkt   = (" " + r["market"]) if r["market"] else ""
        note  = ("\n   _" + r["note"] + "_") if r["note"] else ""
        lines.append(icon + f" `{ts}` @{uname} › `{r['label']}` [{r['reason']}·{r['triggered_by']}]" + mkt + note)
    await _send_chunked(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /admin_orphans — bot dirs on disk with no matching DB entry
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_orphans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    if not os.path.isdir(BOTS_DIR):
        return await update.message.reply_text(f"`{BOTS_DIR}` not found.", parse_mode="Markdown")
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        known = {
            (str(r["telegram_id"]), r["label"])
            for r in conn.execute("SELECT telegram_id, label FROM users").fetchall()
        }
    orphans = []
    for name in sorted(os.listdir(BOTS_DIR)):
        full = os.path.join(BOTS_DIR, name)
        if not os.path.isdir(full) or not name.startswith("user_"):
            continue
        parts = name.split("_", 2)  # ["user", "<tid>", "<label>"]
        if len(parts) != 3:
            orphans.append((name, "malformed dir name"))
            continue
        _, tid_str, label = parts
        if (tid_str, label) not in known:
            has_db = os.path.exists(os.path.join(full, "bot", "hotstuff.db"))
            try:
                size = subprocess.check_output(["du", "-sh", full], text=True, timeout=5).split()[0]
            except Exception:
                size = "?"
            orphans.append((name, f"size={size} db={'yes' if has_db else 'no'}"))
    if not orphans:
        return await update.message.reply_text("✅ No orphaned bot directories found.")
    lines = [f"👻 *Orphaned Bot Dirs* ({len(orphans)})\n"]
    for name, info in orphans:
        lines.append(f"  `{name}` — {info}")
    lines.append("\nUse `/admin_cleandir <tid> <label>` to remove.")
    await _send_chunked(update, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /admin_cleandir <tid> <label> — archive SQLite + delete orphan dir
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_admin_cleandir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    tid, label = _parse_tid_label(context)
    if tid is None:
        return await update.message.reply_text(
            "Usage: `/admin_cleandir <tid> <label>`", parse_mode="Markdown")
    bdir = _bot_dir(tid, label)
    if not os.path.isdir(bdir):
        return await update.message.reply_text(
            f"Directory not found: `{bdir}`", parse_mode="Markdown")
    wallet = get_wallet(tid, label)
    if wallet:
        return await update.message.reply_text(
            f"⛔ `{tid}` › `{label}` still exists in DB — use `/admin_wipe` instead.",
            parse_mode="Markdown")
    steps = []
    db_src = os.path.join(bdir, "bot", "hotstuff.db")
    if os.path.exists(db_src):
        archive_dir = os.path.join(BOTS_DIR, "archive")
        os.makedirs(archive_dir, exist_ok=True)
        from datetime import datetime as _dt
        stamp  = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
        db_dst = os.path.join(archive_dir, f"{tid}_{label}_{stamp}.db")
        try:
            shutil.copy2(db_src, db_dst)
            steps.append(f"✅ DB archived → `{db_dst}`")
        except Exception as e:
            steps.append(f"⚠️ Archive failed: {e} (continuing anyway)")
    else:
        steps.append("ℹ️ No trade DB found — nothing to archive")
    try:
        shutil.rmtree(bdir)
        steps.append(f"✅ Removed `{bdir}`")
    except Exception as e:
        steps.append(f"❌ Remove failed: {e}")
    await _send_chunked(update, f"🗑 *Cleandir: `{tid}` › `{label}`*\n\n" + "\n".join(steps))


# ═════════════════════════════════════════════════════════════════════════════
# Registration
# ═════════════════════════════════════════════════════════════════════════════


# /admin_platform
async def cmd_admin_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    from datetime import datetime, timezone
    import sqlite3 as _sq
    import sys as _sys
    _sys.path.insert(0, "/root/saas")
    from telegram_bot import _analytics_since
    _period = context.args[0].lower() if context.args else "7d"
    _period_labels = {"today": "Today", "7d": "Last 7 Days", "alltime": "All Time"}
    _since = _analytics_since(_period)
    now        = datetime.now(timezone.utc)
    day_start  = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_start = now.timestamp() - 7 * 86400
    def _tsf():
        return ("CASE WHEN timestamp LIKE '%T%' "
                "THEN CAST(strftime('%s', timestamp) AS REAL) >= ? "
                "ELSE CAST(timestamp AS REAL) / 1000 >= ? END")
    with get_conn() as _conn:
        _conn.row_factory = _sq.Row
        wallets = _conn.execute("SELECT * FROM users").fetchall()
    platform = {
        "today":   {"vol": 0, "gross_pnl": 0, "net_pnl": 0, "fees": 0, "taker_fees": 0, "maker_rebates": 0, "trades": 0},
        "week":    {"vol": 0, "gross_pnl": 0, "net_pnl": 0, "fees": 0, "taker_fees": 0, "maker_rebates": 0, "trades": 0},
        "alltime": {"vol": 0, "gross_pnl": 0, "net_pnl": 0, "fees": 0, "taker_fees": 0, "maker_rebates": 0, "trades": 0},
    }
    user_stats   = []
    latency_vals = []
    adv_sel_vals = []
    for w in wallets:
        tid, label = w["telegram_id"], w["label"]
        db_path = os.path.join(_bot_dir(tid, label), "bot", "hotstuff.db")
        if not os.path.exists(db_path):
            continue
        try:
            conn = _sq.connect(db_path)
            conn.row_factory = _sq.Row
            addr = w["wallet_address"]
            def _q(since, _conn=conn, _addr=addr):
                try:
                    r = _conn.execute(
                        "SELECT COUNT(*) trades, SUM(notional) vol, "
                        "SUM(closed_pnl) net_pnl, SUM(closed_pnl + fee) gross_pnl, "
                        "SUM(fee) fees, "
                        "SUM(CASE WHEN fee > 0 THEN fee ELSE 0 END) taker_fees, "
                        "SUM(CASE WHEN fee < 0 THEN ABS(fee) ELSE 0 END) maker_rebates "
                        "FROM fills WHERE address=? AND " + _tsf(),
                        (_addr, since, since)
                    ).fetchone()
                    return {"trades": r["trades"] or 0, "vol": r["vol"] or 0,
                            "net_pnl": r["net_pnl"] or 0, "gross_pnl": r["gross_pnl"] or 0,
                            "fees": r["fees"] or 0, "taker_fees": r["taker_fees"] or 0,
                            "maker_rebates": r["maker_rebates"] or 0}
                except Exception:
                    return {"trades": 0, "vol": 0, "net_pnl": 0, "gross_pnl": 0, "fees": 0, "taker_fees": 0, "maker_rebates": 0}
            td = _q(day_start)
            wd = _q(week_start)
            ad = _q(0)
            for key, d in [("today", td), ("week", wd), ("alltime", ad)]:
                for k in ("vol", "gross_pnl", "net_pnl", "fees", "taker_fees", "maker_rebates", "trades"):
                    platform[key][k] += d[k]
            user_stats.append({
                "label": label, "tid": tid,
                "uname": w["telegram_username"] or str(tid),
                "market": w["market"],
                "vol": wd["vol"], "net": wd["net_pnl"], "trades": wd["trades"],
            })
            try:
                lrows = conn.execute(
                    "SELECT placement_ms FROM placed_orders "
                    "WHERE address=? AND placement_ms IS NOT NULL", (addr,)
                ).fetchall()
                latency_vals.extend(r["placement_ms"] for r in lrows)
            except Exception:
                pass
            try:
                m = conn.execute(
                    "SELECT AVG((mid_60s - fill_price) / fill_price * 100 "
                    "* CASE WHEN side='b' THEN 1 ELSE -1 END) m60, COUNT(*) cnt "
                    "FROM markouts WHERE address=? AND mid_60s IS NOT NULL", (addr,)
                ).fetchone()
                if m and m["cnt"] > 5 and m["m60"] is not None:
                    adv_sel_vals.append({"label": label, "uname": w["telegram_username"] or str(tid), "m60": m["m60"], "cnt": m["cnt"]})
            except Exception:
                pass
            conn.close()
        except Exception:
            continue
    lines = ["📊 *Platform Analytics*"]
    lines.append("")
    lines.append("💰 *Volume & PnL*")
    lines.append("  Today:    `${:,.0f}` vol · gross `${:+.2f}` · net `${:+.2f}` · {:,} trades".format(
        platform["today"]["vol"], platform["today"]["gross_pnl"], platform["today"]["net_pnl"], platform["today"]["trades"]))
    lines.append("  7 Days:   `${:,.0f}` vol · gross `${:+.2f}` · net `${:+.2f}` · {:,} trades".format(
        platform["week"]["vol"], platform["week"]["gross_pnl"], platform["week"]["net_pnl"], platform["week"]["trades"]))
    lines.append("  All Time: `${:,.0f}` vol · gross `${:+.2f}` · net `${:+.2f}` · {:,} trades".format(
        platform["alltime"]["vol"], platform["alltime"]["gross_pnl"], platform["alltime"]["net_pnl"], platform["alltime"]["trades"]))

    # ── Fees breakdown ────────────────────────────────────────────────────
    lines.append("")
    lines.append("🧾 *Fees*")
    for label_p, key in [("Today", "today"), ("7 Days", "week"), ("All Time", "alltime")]:
        tk = platform[key]["taker_fees"]
        mk = platform[key]["maker_rebates"]
        lines.append("  {:>8s}: taker `${:,.2f}` · rebates `${:,.2f}` · net `${:,.2f}`".format(
            label_p, tk, mk, tk - mk))
    sorted_users = sorted([u for u in user_stats if u["trades"] > 0], key=lambda x: x["net"], reverse=True)
    if sorted_users:
        lines.append("")
        lines.append("📈 *7D PnL by User*")
        for u in sorted_users:
            emoji = "🟢" if u["net"] >= 0 else "🔴"
            lines.append("  {} `{}` ({}) `${:+.2f}` \u00b7 `${:,.0f}` vol \u00b7 {:,} trades".format(
                emoji, u["label"], u["market"], u["net"], u["vol"], u["trades"]))
    active = [u for u in user_stats if u["trades"] > 0]
    if active:
        most  = max(active, key=lambda x: x["trades"])
        least = min(active, key=lambda x: x["trades"])
        lines.append("")
        lines.append("🏆 *Activity (7d)*")
        lines.append("  Most active:  `{}` \u2014 {:,} trades".format(most["label"], most["trades"]))
        lines.append("  Least active: `{}` \u2014 {:,} trades".format(least["label"], least["trades"]))
    if adv_sel_vals:
        sorted_adv = sorted(adv_sel_vals, key=lambda x: x["m60"])
        lines.append("")
        lines.append("📡 *Adverse Selection (60s markout)*")
        for a in sorted_adv:
            emoji = "✅" if a["m60"] >= 0 else "⚠️"
            lines.append("  {} `{}` `{:+.3f}bps` ({} fills)".format(emoji, a["label"], a["m60"], a["cnt"]))
    if latency_vals:
        avg_ms = sum(latency_vals) / len(latency_vals)
        min_ms = min(latency_vals)
        max_ms = max(latency_vals)
        lines.append("")
        lines.append("⚡ *Platform Order Latency*")
        lines.append("  Avg `{:.0f}ms` \u00b7 Min `{:.0f}ms` \u00b7 Max `{:.0f}ms` \u00b7 {:,} orders".format(
            avg_ms, min_ms, max_ms, len(latency_vals)))
        if avg_ms < 200:
            lines.append("  \u2192 _At exchange baseline. Healthy._")
        elif avg_ms < 400:
            lines.append("  \u2192 _Slightly elevated. Monitor VPS._")
        else:
            lines.append("  \u2192 _Degraded. Investigate network._")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



# /admin_analytics <tid> <label>
async def cmd_admin_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await _deny(update)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /admin_analytics <tid> <label>")
        return
    try:
        tid   = int(args[0])
        label = args[1].lower()
    except ValueError:
        await update.message.reply_text("Invalid tid.")
        return
    period = args[2].lower() if len(args) > 2 else "7d"
    period_labels = {"today": "Today", "7d": "Last 7 Days", "alltime": "All Time"}
    period_label  = period_labels.get(period, "Last 7 Days")
    wallet = get_wallet(tid, label)
    if not wallet:
        await update.message.reply_text("Wallet not found.")
        return
    import sys as _sys
    _sys.path.insert(0, "/root/saas")
    from telegram_bot import _analytics_db, _analytics_since, _build_performance_text, _build_quality_text, _fetch_points
    since = _analytics_since(period)
    d     = _analytics_db(_bot_dir(tid, label), wallet["wallet_address"], since)
    pts   = _fetch_points(wallet["wallet_address"])
    perf  = _build_performance_text(label, wallet["market"], period_label, d, pts)
    qual  = _build_quality_text(label, wallet["market"], period_label, d)
    await update.message.reply_text(perf, parse_mode="Markdown")
    await update.message.reply_text(qual, parse_mode="Markdown")


def register_admin_handlers(application, mgr: BotManager):
    set_manager(mgr)
    handlers = [
        ("admin_help",       cmd_admin_help),
        ("admin_users",      cmd_admin_users),
        ("admin_bots",       cmd_admin_bots),
        ("admin_status",     cmd_admin_status),
        ("admin_logs",       cmd_admin_logs),
        ("admin_restart",    cmd_admin_restart),
        ("admin_stop",       cmd_admin_stop),
        ("admin_kill",       cmd_admin_kill),
        ("admin_db",         cmd_admin_db),
        ("admin_broadcast",  cmd_admin_broadcast),
        ("admin_sysinfo",    cmd_admin_sysinfo),
        # User management
        ("admin_wallets",    cmd_admin_wallets),
        ("admin_suspend",    cmd_admin_suspend),
        ("admin_unsuspend",  cmd_admin_unsuspend),
        ("admin_wipe",       cmd_admin_wipe),
        # Monitoring
        ("admin_crashed",    cmd_admin_crashed),
        ("admin_losses",     cmd_admin_losses),
        ("admin_idle",       cmd_admin_idle),
        # Financial
        ("admin_revenue",    cmd_admin_revenue),
        ("admin_referrals",  cmd_admin_referrals),
        # Ops
        ("admin_update",     cmd_admin_update),
        ("admin_envdump",    cmd_admin_envdump),
        ("admin_diskusage",  cmd_admin_diskusage),
        ("admin_setstate",   cmd_admin_setstate),
        ("admin_feed",       cmd_admin_feed),
        ("admin_botstate",   cmd_admin_botstate),
        ("admin_poll",       cmd_admin_poll),
        ("admin_suspendall",   cmd_admin_suspendall),
        ("admin_unsuspendall", cmd_admin_unsuspendall),
        ("admin_suspensions",  cmd_admin_suspensions),
        ("admin_orphans",      cmd_admin_orphans),
        ("admin_cleandir",     cmd_admin_cleandir),
        ("admin_platform",     cmd_admin_platform),
        ("admin_analytics",    cmd_admin_analytics),
    ]
    for name, handler in handlers:
        application.add_handler(CommandHandler(name, handler))
