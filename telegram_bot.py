"""
telegram_bot.py — NenMMBot Telegram interface.

Hybrid UX: inline buttons for navigation, text input only where necessary.
Auto-updates messages in place (edit_message_text) for clean UX.

Commands:
    /start              — onboarding + referral link
    /setup              — add first wallet
    /addwallet          — add second wallet
    /removewallet       — remove a wallet
    /status             — all wallets with action buttons
    /stop    <label>    — stop a bot
    /resume  <label>    — restart a stopped bot
    /override <label>   — resume after drawdown halt
    /dash    [label]    — full stats dashboard
    /pnl     <label>    — today's PnL
    /volume  <label>    — today's volume
    /points  <label>    — points + league + rank
    /pointsvalue [amt]  — points value calculator
    /balance <label>    — wallet balance + margin
    /config  <label>    — profile switcher + expert mode
    /renewkey <label>   — update agent key
    /help               — all commands
"""
from __future__ import annotations

import os
import asyncio
import time
import sqlite3
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv("/root/saas/.env")

from eth_utils import to_checksum_address
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, ConversationHandler, filters,
)

from db import (
    init_db, add_wallet, get_wallet, get_user_wallets,
    set_wallet_state, set_wallet_profile, set_wallet_market, update_agent_key,
    set_expert_overrides, reset_expert_overrides, record_resume,
    remove_wallet, validate_label, validate_market,
    MAX_WALLETS, SUPPORTED_MARKETS,
)
from vault import Vault
from manager import BotManager, ADMIN_TELEGRAM_ID, MARKET_PROFILES, MARKET_MAX_LEVERAGE
from admin_commands import register_admin_handlers, handle_poll_answer, cmd_admin_poll, _send_chunked

log = logging.getLogger("telegram_bot")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
)

BOT_TOKEN       = os.environ["NENBOT_BOT_TOKEN"]
REFERRAL_LINK   = os.environ.get("NENBOT_REFERRAL_LINK", "https://hotstuff.trade")
MY_REFERRAL_CODE = os.environ.get("NENBOT_REFERRAL_CODE", "YOUR_REFERRAL_CODE")
HOTSTUFF_API  = "https://api.hotstuff.trade/info"
API_HEADERS   = {"Content-Type": "application/json"}

LEAGUE_MAP = {
    1: "Master", 2: "Diamond", 3: "Platinum",
    4: "Silver",  5: "Gold",    6: "Copper",
    7: "Bronze",  8: "Iron",
}

# ── Conversation states ───────────────────────────────────────────────────────
(
    SETUP_LABEL, SETUP_WALLET_ADDR, SETUP_AGENT_KEY,
    ADD_LABEL, ADD_WALLET_ADDR, ADD_AGENT_KEY,
    RENEW_KEY,
    EXPERT_FIELD_VALUE,
) = range(8)

# ── Globals ───────────────────────────────────────────────────────────────────
vault = Vault()
mgr   = BotManager()
app   = None


# ── Hotstuff API ──────────────────────────────────────────────────────────────
def _check_referral(address: str) -> bool:
    """Returns True if wallet was referred by our code."""
    try:
        data = _hs("referralSummary", {"user": address})
        return data.get("referrer_code", "").lower() == MY_REFERRAL_CODE.lower()
    except Exception:
        return False


def _hs(method: str, params: dict) -> dict:
    try:
        if "user" in params:
            try:
                params["user"] = to_checksum_address(params["user"])
            except Exception:
                pass
        r = requests.post(HOTSTUFF_API,
                          json={"method": method, "params": params},
                          headers=API_HEADERS, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.error(f"API error {method}: {e}")
    return {}


def _fetch_points(address: str) -> dict:
    return _hs("pointsHistory", {"user": address})


def _fetch_account(address: str) -> dict:
    return _hs("accountSummary", {"user": address})


# ── DB stats ──────────────────────────────────────────────────────────────────
def _db_stats(wallet_address: str, bot_dir: str) -> dict:
    db_path = os.path.join(bot_dir, "bot", "hotstuff.db")
    if not os.path.exists(db_path):
        return {}
    now        = time.time()
    day_start  = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    week_start = day_start - (6 * 86400)
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        def q(since):
            r = conn.execute("""
                SELECT COUNT(*) trades, SUM(notional) volume,
                       SUM(closed_pnl) net_pnl, SUM(fee) fees,
                       SUM(closed_pnl + fee) gross_pnl,
                       SUM(CASE WHEN fee > 0 THEN fee ELSE 0 END) taker_fees,
                       SUM(CASE WHEN fee < 0 THEN ABS(fee) ELSE 0 END) maker_rebates
                FROM fills WHERE address=?
                AND CASE WHEN timestamp LIKE '%T%'
                    THEN CAST(strftime('%s', timestamp) AS REAL) >= ?
                    ELSE CAST(timestamp AS REAL) / 1000 >= ?
                    END
            """, (wallet_address, since, since)).fetchone()
            return {"trades": r["trades"] or 0, "volume": r["volume"] or 0.0,
                    "gross_pnl": r["gross_pnl"] or 0.0, "net_pnl": r["net_pnl"] or 0.0,
                    "fees": r["fees"] or 0.0,
                    "taker_fees": r["taker_fees"] or 0.0,
                    "maker_rebates": r["maker_rebates"] or 0.0}
        result = {"today": q(day_start), "week": q(week_start), "alltime": q(0)}
        conn.close()
        return result
    except Exception as e:
        log.error(f"DB stats error: {e}")
        return {}


def _bot_dir(tid: int, label: str) -> str:
    return f"/root/bots/user_{tid}_{label}"


# ── Notify ────────────────────────────────────────────────────────────────────
async def _notify(telegram_id: int, message: str):
    try:
        await app.bot.send_message(chat_id=telegram_id, text=message)
    except Exception as e:
        log.error(f"Notify failed {telegram_id}: {e}")


async def _admin(msg: str):
    await _notify(ADMIN_TELEGRAM_ID, msg)


# ── Keyboards ─────────────────────────────────────────────────────────────────
def _kb_start():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Setup Bot", callback_data="goto_setup"),
        InlineKeyboardButton("❓ Help", callback_data="goto_help"),
    ]])


def _kb_market():
    rows = []
    row  = []
    for m in SUPPORTED_MARKETS:
        row.append(InlineKeyboardButton(m.replace("-PERP", ""), callback_data=f"market:{m}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_setup")])
    return InlineKeyboardMarkup(rows)


def _kb_profile(market: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐢 Conservative", callback_data=f"profile:conservative:{market}"),
            InlineKeyboardButton("⚖️ Balanced",     callback_data=f"profile:balanced:{market}"),
            InlineKeyboardButton("🔥 Aggressive",   callback_data=f"profile:aggressive:{market}"),
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_setup")],
    ])


def _kb_wallet_actions(label: str, running: bool):
    row1 = []
    if running:
        row1.append(InlineKeyboardButton("⏹ Stop",     callback_data=f"stop:{label}"))
    else:
        row1.append(InlineKeyboardButton("▶️ Resume",   callback_data=f"resume:{label}"))
    row1.append(InlineKeyboardButton("📊 Dash",         callback_data=f"dash:{label}"))
    row1.append(InlineKeyboardButton("⚙️ Config",       callback_data=f"config:{label}"))
    row2 = [
        InlineKeyboardButton("🔑 Renew Key",            callback_data=f"renewkey:{label}"),
        InlineKeyboardButton("🔓 Override Halt",        callback_data=f"override:{label}"),
    ]
    row3 = [
        InlineKeyboardButton("📈 Analytics",            callback_data=f"analytics:perf:7d:{label}"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3])


def _kb_config(label: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🐢 Conservative", callback_data=f"setprofile:conservative:{label}"),
            InlineKeyboardButton("⚖️ Balanced",     callback_data=f"setprofile:balanced:{label}"),
            InlineKeyboardButton("🔥 Aggressive",   callback_data=f"setprofile:aggressive:{label}"),
        ],
        [InlineKeyboardButton("🔧 Expert Mode",     callback_data=f"expert:{label}")],
        [InlineKeyboardButton("📋 View Config",     callback_data=f"viewconfig:{label}")],
        [InlineKeyboardButton("🔄 Change Market",   callback_data=f"changemarket:{label}")],
        [InlineKeyboardButton("↩️ Reset Defaults",  callback_data=f"resetdefaults:{label}")],
        [InlineKeyboardButton("❌ Cancel",           callback_data=f"cancel_config:{label}")],
    ])


def _kb_expert(label: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📏 Spread",        callback_data=f"exp:spread_bps:{label}"),
            InlineKeyboardButton("📏 Close Spread",  callback_data=f"exp:close_spread_bps:{label}"),
        ],
        [
            InlineKeyboardButton("💰 Order Size",    callback_data=f"exp:order_size_usd:{label}"),
            InlineKeyboardButton("📦 Max Inventory", callback_data=f"exp:max_inventory_usd:{label}"),
        ],
        [
            InlineKeyboardButton("📉 Max Loss",      callback_data=f"exp:max_daily_loss_usd:{label}"),
            InlineKeyboardButton("🔢 Leverage",      callback_data=f"exp:leverage:{label}"),
        ],
        [
            InlineKeyboardButton("🛑 Stop Loss %",   callback_data=f"exp:stop_loss_pct:{label}"),
            InlineKeyboardButton("🔄 Flips On/Off",  callback_data=f"exp:allow_flips:{label}"),
        ],
        [
            InlineKeyboardButton("⏱ Cooldown Open",  callback_data=f"exp:fill_cooldown_open_s:{label}"),
            InlineKeyboardButton("⏱ Cooldown Close", callback_data=f"exp:fill_cooldown_close_s:{label}"),
        ],
        [
            InlineKeyboardButton("🕗 TOD 08:00",     callback_data=f"exp:tod_08_multiplier:{label}"),
            InlineKeyboardButton("🕑 TOD 13:00",     callback_data=f"exp:tod_13_multiplier:{label}"),
            InlineKeyboardButton("🕑 TOD 14:00",     callback_data=f"exp:tod_14_multiplier:{label}"),
        ],
        [InlineKeyboardButton("📊 ADX Threshold",   callback_data=f"exp:adx_threshold:{label}")],
        [
            InlineKeyboardButton("📶 Price Move",   callback_data=f"exp:price_move_threshold:{label}"),
            InlineKeyboardButton("⏳ Order TTL",    callback_data=f"exp:order_ttl_ms:{label}"),
            InlineKeyboardButton("⚡ Requote CD",   callback_data=f"exp:requote_cooldown:{label}"),
        ],
        [
            InlineKeyboardButton("🎯 Profit Target",  callback_data=f"exp:profit_target_bps:{label}"),
            InlineKeyboardButton("🛡 Adv Selection",  callback_data=f"exp:adverse_selection_bps:{label}"),
        ],
        [
            InlineKeyboardButton("⚔️ Signal Conflict", callback_data=f"exp:signal_conflict_bps:{label}"),
        ],
        [
            InlineKeyboardButton("🔲 Grid Spacing",   callback_data=f"exp:grid_spacing_mult:{label}"),
            InlineKeyboardButton("🔢 Grid Levels",    callback_data=f"exp:grid_levels:{label}"),
        ],
        [
            InlineKeyboardButton("📐 Grid V-Shape",   callback_data=f"exp:grid_vshape_alpha:{label}"),
            InlineKeyboardButton("📏 Grid Min BPS",   callback_data=f"exp:grid_min_spacing_pct:{label}"),
        ],
        [
            InlineKeyboardButton("🔰 Grid Overshoot",  callback_data=f"exp:grid_overshoot_mult:{label}"),
            InlineKeyboardButton("🚨 Grid Max Loss",   callback_data=f"exp:grid_max_loss_pct:{label}"),
        ],
        [InlineKeyboardButton("↩️ Back to Config",  callback_data=f"config:{label}")],
    ])


def _kb_market_change(label: str):
    rows = []
    row  = []
    for m in SUPPORTED_MARKETS:
        row.append(InlineKeyboardButton(
            m.replace("-PERP", ""), callback_data=f"setmarket:{m}:{label}"
        ))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"config:{label}")])
    return InlineKeyboardMarkup(rows)


def _kb_dash_actions(label: str):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Refresh",   callback_data=f"dash:{label}"),
            InlineKeyboardButton("⚙️ Config",    callback_data=f"config:{label}"),
            InlineKeyboardButton("⏹ Stop",      callback_data=f"stop:{label}"),
        ],
        [
            InlineKeyboardButton("📈 Analytics", callback_data=f"analytics:perf:7d:{label}"),
        ],
    ])


# ── Profile summary text ──────────────────────────────────────────────────────
def _profile_text(profile: str, market: str) -> str:
    p = MARKET_PROFILES.get(market, MARKET_PROFILES["BTC-PERP"]).get(profile, {})
    prefix = market.split("-")[0]
    size   = p.get(f"{prefix}_ORDER_SIZE_USD", "—")
    inv    = p.get(f"{prefix}_MAX_INVENTORY_USD", "—")
    spread = p.get(f"{prefix}_SPREAD", "0")
    loss   = p.get("HOTSTUFF_MAX_DAILY_LOSS", "—")
    lev    = p.get("HOTSTUFF_LEVERAGE", "—")
    return (
        f"*{profile.capitalize()}*: "
        f"${size} size · ${inv} inv · "
        f"{float(spread)*10000:.0f}bps · "
        f"${loss} max loss · {lev}x lev"
    )


# ── Dash text ─────────────────────────────────────────────────────────────────
LEAGUE_EMOJI = {
    "Master": "👑", "Diamond": "💎", "Platinum": "🏅",
    "Silver": "🥈", "Gold": "🥇", "Copper": "🟤",
    "Bronze": "🟫", "Iron": "⚫", "Unranked": "—",
}

def _pnl_emoji(val: float) -> str:
    return "📈" if val >= 0 else "📉"

def _dash_text(tid: int, label: str) -> str:
    wallet  = get_wallet(tid, label)
    if not wallet:
        return f"❌ Wallet `{label}` not found."
    address = wallet["wallet_address"]
    market  = wallet["market"]
    profile = wallet["profile"].capitalize()
    status  = mgr.status(tid, label)
    state   = "🟢 Running" if status["running"] else "🔴 Stopped"
    stats   = _db_stats(address, _bot_dir(tid, label))
    pts     = _fetch_points(address)
    acc     = _fetch_account(address)

    total_pts  = pts.get("total_points", 0)
    net_rank   = pts.get("net_rank", 0)
    league     = LEAGUE_MAP.get(pts.get("net_league", 0), "Unranked")
    leag_emoji = LEAGUE_EMOJI.get(league, "—")
    equity     = float(acc.get("total_account_equity") or 0)
    im_used    = float(acc.get("initial_margin") or 0)
    avail      = float(acc.get("available_balance") or 0)
    upnl       = float(acc.get("upnl") or 0)

    lines = [
        f"⚡ *{label.upper()}* · `{market}` · {state}",
        f"🏷 *{profile}* · 👛 `{address[:6]}...{address[-4:]}`",
        "",
    ]

    if stats:
        t  = stats["today"]
        w7 = stats["week"]
        at = stats["alltime"]
        lines += [
            f"📅 *Today*",
            f"  📊 Gross `${t['gross_pnl']:+.2f}` · 💸 Taker `${t['taker_fees']:.2f}` · 💚 Rebates `${t['maker_rebates']:.2f}`",
            f"  ✅ Net `${t['net_pnl']:+.2f}` · 📦 Vol `${t['volume']:,.0f}` · 🔁 `{t['trades']}` trades",
            "",
            f"📆 *Last 7 Days*",
            f"  📊 Gross `${w7['gross_pnl']:+.2f}` · ✅ Net `${w7['net_pnl']:+.2f}` · 📦 Vol `${w7['volume']:,.0f}` · 🔁 `{w7['trades']}` trades",
            "",
            f"🗂 *All Time*",
            f"  📊 Gross `${at['gross_pnl']:+.2f}` · ✅ Net `${at['net_pnl']:+.2f}` · 📦 Vol `${at['volume']:,.0f}` · 🔁 `{at['trades']}` trades",
        ]
    else:
        lines.append("_No trade data yet — bot is warming up_")

    lines += [
        "",
        f"💳 *Account*",
        f"  💰 Equity `${equity:,.2f}` · ✅ Free `${avail:,.2f}` · 🔒 IM `${im_used:,.2f}`",
        f"  {'📈' if upnl >= 0 else '📉'} uPnL `${upnl:+.4f}`",
        "",
        f"⭐ *Points*",
        f"  {leag_emoji} *{league}* · 🏆 `{total_pts:,}` pts · 📊 Rank `#{net_rank}`",
    ]
    return "\n".join(lines)


# ── Label parser ──────────────────────────────────────────────────────────────
def _parse_label_arg(context, wallets) -> str | None:
    if context.args:
        try:
            return validate_label(context.args[0])
        except ValueError:
            return None
    if len(wallets) == 1:
        return wallets[0]["label"]
    return None


async def _require_label(update, context):
    tid     = update.effective_user.id
    wallets = get_user_wallets(tid)
    if not wallets:
        await update.message.reply_text("No wallets found. Use /setup first.")
        return None, None, None
    label = _parse_label_arg(context, wallets)
    if not label:
        labels = ", ".join(f"`{w['label']}`" for w in wallets)
        await update.message.reply_text(
            f"Specify a wallet label: {labels}", parse_mode="Markdown"
        )
        return None, None, None
    if not get_wallet(tid, label):
        await update.message.reply_text(f"❌ Wallet `{label}` not found.", parse_mode="Markdown")
        return None, None, None
    return tid, label, wallets


# ─────────────────────────────────────────────────────────────────────────────
# /start
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"👋 Welcome to *NenMM Bot*, {u.first_name}!\n\n"
        f"Automated market-making for Hotstuff perpetuals.\n"
        f"Farm volume and points quietly on your behalf.\n\n"
        f"*Get started:*\n"
        f"1️⃣ Sign up via referral: {REFERRAL_LINK}\n"
        f"2️⃣ Deposit funds on Hotstuff\n"
        f"3️⃣ Generate an agent key _(Settings → API)_\n"
        f"4️⃣ Tap Setup Bot below",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=_kb_start(),
    )
    await _admin(f"👤 New user: @{u.username} (ID: {u.id})")


# ─────────────────────────────────────────────────────────────────────────────
# /help
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_modes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explain Grid vs Trend mode, signal conflict, transitions, and how to force a mode."""
    text = (
        "\U0001f916 *NenBot Modes*\n\n"
        "\U0001f4ca *Grid Mode* \- market is ranging, no clear direction\.\n"
        "Bot quotes both sides of the spread, collecting the bid\-ask repeatedly\.\n"
        "Uses RSI, VWAP and order flow to scale quote aggression per side\.\n"
        "If inventory builds beyond the cap, the bot market\-reduces back to flat automatically\.\n"
        "_Risk: inventory builds faster than it can be reduced if price breaks out hard\._ \n\n"
        "\U0001f4c8 *Trend Mode* \- market is moving with conviction\.\n"
        "Bot trades directionally using momentum signals \(OFI, RSI, DI crossover\)\.\n"
        "Aggression scales with trend strength \- weak trend \= cautious, strong trend \= full tilt\.\n"
        "_Risk: losses if breakout reverses\._ \n\n"
        "\U0001f507 *Signal Conflict* \- in Trend Mode, if live order flow disagrees with the trend "
        "the bot pauses entirely\. Resumes automatically when signals realign\. "
        "Intentional \- protects against informed flow\.\n\n"
        "\U0001f4c9 *Markout Filter* \- after each fill, the bot measures how much price moved against it\. "
        "If recent fills are consistently marking out badly, the spread floor widens automatically "
        "until fill quality improves\. Applies to both modes\.\n\n"
        "\U0001f500 *Auto\-switch:* ADX below 19 \= Grid, above 22 \= Trend\. "
        "Between 19\-22 holds current mode\.\n\n"
        "\U0001f504 *Transitions:* Open position when mode switches\? "
        "Bot closes it first, then activates new mode\.\n\n"
        "\u2699\ufe0f *Force a mode* via `/config` \u2192 Expert Mode \u2192 ADX Threshold\n"
        "\u2022 `100` \= always Grid \(directional signals ignored\)\n"
        "\u2022 `28`\-`30` \= Trend only on strong trends\n"
        "\u2022 `22` \= auto \(recommended\)\n"
        "\u2022 `18`\-`20` \= Trend on weaker trends, more directional activity\n"
        "\u2022 `1` \= always Trend\n\n"
        "\U0001f6d1 Forcing a mode removes the bot\'s ability to adapt\."
    )
    await _send_chunked(update, text, parse_mode="MarkdownV2")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*NenMM Bot Commands*\n\n"
        "/setup — Add first wallet\n"
        "/addwallet — Add second wallet\n"
        "/removewallet — Remove a wallet\n"
        "/status — All wallets \\+ action buttons\n"
        "/stop \\<label\\> — Stop a bot\n"
        "/resume \\<label\\> — Restart stopped bot\n"
        "/override \\<label\\> — Resume after drawdown halt\n"
        "/dash \\[label\\] — Full stats dashboard\n"
        "/pnl \\<label\\> — Today's PnL\n"
        "/volume \\<label\\> — Today's volume\n"
        "/points \\<label\\> — Points \\+ league \\+ rank\n"
        "/pointsvalue \\[amount\\] — Points value calculator\n"
        "/balance \\<label\\> — Wallet balance\n"
        "/config \\<label\\> — Profile \\+ expert mode\n"
        "/renewkey \\<label\\> — Update agent key\n"
        "/help — This message"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="MarkdownV2")
    else:
        await update.message.reply_text(text, parse_mode="MarkdownV2")
        # Send user guide PDF if available
        guide_path = "/root/saas/NenMMBot_User_Guide.pdf"
        import os as _os
        if _os.path.exists(guide_path):
            with open(guide_path, "rb") as pdf:
                await update.message.reply_document(
                    document=pdf,
                    filename="NenBot_User_Guide.pdf",
                    caption="📖 Full user guide — everything you need to get started."
                )


# ─────────────────────────────────────────────────────────────────────────────
# /setup conversation
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid     = update.effective_user.id
    wallets = get_user_wallets(tid)
    if len(wallets) >= MAX_WALLETS:
        txt = f"⚠️ Max {MAX_WALLETS} wallets. Use /removewallet first."
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(txt)
        else:
            await update.message.reply_text(txt)
        return ConversationHandler.END

    txt = (
        "*Add Wallet — Step 1 of 5*\n\n"
        "Choose a label for this wallet.\n"
        "_e.g. `main`, `grind`, `w1` — max 10 chars, letters/numbers/underscore_"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(txt, parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, parse_mode="Markdown")
    context.user_data["setup_awaiting"] = "label"
    return SETUP_LABEL


async def setup_label(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        label = validate_label(update.message.text.strip())
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}\nTry again:")
        return SETUP_LABEL
    tid = update.effective_user.id
    if get_wallet(tid, label):
        await update.message.reply_text(f"❌ Label `{label}` already used. Choose another:", parse_mode="Markdown")
        return SETUP_LABEL
    context.user_data["setup_label"] = label
    await update.message.reply_text(
        f"*Step 2 of 5 — Market*\n\nLabel: `{label}`\n\nWhich market to trade?",
        parse_mode="Markdown",
        reply_markup=_kb_market(),
    )
    return ConversationHandler.END   # market selection handled by callback


async def setup_market_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    market = query.data.split(":", 1)[1]
    context.user_data["setup_market"] = market
    await query.edit_message_text(
        f"*Step 3 of 5 — Wallet Address*\n\n"
        f"Label: `{context.user_data['setup_label']}` · Market: `{market}`\n\n"
        f"Send your Hotstuff wallet address (0x...):\n"
        f"_Or tap Cancel to abort._",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_setup")]]),
    )
    context.user_data["setup_awaiting"] = "wallet_addr"


async def setup_wallet_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("setup_awaiting") != "wallet_addr":
        return
    address = update.message.text.strip()
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("❌ Invalid address. Must be 0x... 42 chars. Try again:")
        return
    # Verify referral
    await update.message.reply_text("⏳ Verifying referral...")
    if not _check_referral(address):
        await update.message.reply_text(
            f"❌ *Referral not found*\n\n"
            f"This wallet was not signed up via the NenMM referral link.\n\n"
            f"Please sign up on Hotstuff first:\n{REFERRAL_LINK}\n\n"
            f"Then come back and run /setup again.",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        context.user_data.clear()
        return

    # Block duplicate wallet addresses across all users
    from db import get_conn as _gc
    import sqlite3 as _sq
    with _gc() as _c:
        _c.row_factory = _sq.Row
        _existing = _c.execute(
            "SELECT telegram_id, label FROM users WHERE wallet_address = ?",
            (address,)
        ).fetchone()
    if _existing:
        await update.message.reply_text(
            "❌ *Wallet already registered*\n\n"
            "This wallet address is already linked to a NenBot account. "
            "Each wallet can only be used once.\n\n"
            "Use a different wallet or contact the admin for help.",
            parse_mode="Markdown",
        )
        context.user_data.clear()
        return

    context.user_data["setup_wallet"]   = address
    context.user_data["setup_awaiting"] = "agent_key"
    await update.message.reply_text(
        "*Step 4 of 5 — Agent Key*\n\n"
        "Send your Hotstuff agent private key.\n"
        "_(Settings → API → Create Agent Key)_\n\n"
        "⚠️ Trade-only key — cannot withdraw funds.\n"
        "Message will be deleted immediately.",
        parse_mode="Markdown",
    )


async def setup_agent_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("setup_awaiting") != "agent_key":
        return
    agent_key = update.message.text.strip()
    try:
        await update.message.delete()
    except Exception:
        pass
    if not agent_key.startswith("0x") or len(agent_key) < 60:
        await update.message.reply_text("❌ Invalid key format. Try again:")
        return
    try:
        context.user_data["setup_enc_key"]  = vault.encrypt(agent_key)
        context.user_data["setup_awaiting"] = "profile"
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to secure key: {e}")
        context.user_data.clear()
        return

    market = context.user_data["setup_market"]
    await update.message.reply_text(
        f"*Step 5 of 5 — Choose Profile*\n\n"
        f"{_profile_text('conservative', market)}\n"
        f"{_profile_text('balanced', market)}\n"
        f"{_profile_text('aggressive', market)}\n\n"
        f"_You can change this anytime with /config_",
        parse_mode="Markdown",
        reply_markup=_kb_profile(market),
    )


async def setup_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, profile, market = query.data.split(":", 2)

    if context.user_data.get("setup_awaiting") != "profile":
        await query.answer("Session expired. Use /setup again.", show_alert=True)
        return

    tg_user = update.effective_user
    label   = context.user_data["setup_label"]
    wallet  = context.user_data["setup_wallet"]
    enc_key = context.user_data["setup_enc_key"]
    context.user_data.clear()

    add_wallet(
        telegram_id=tg_user.id,
        label=label,
        telegram_username=tg_user.username or str(tg_user.id),
        wallet_address=wallet,
        encrypted_agent_key=enc_key,
        market=market,
        profile=profile,
    )

    await query.edit_message_text("✅ Wallet added! Starting your bot...")
    started = mgr.start_bot(tg_user.id, label)
    if started:
        await app.bot.send_message(
            chat_id=tg_user.id,
            text=(
                f"🟢 *Bot live!*\n\n"
                f"Label: `{label}`  Market: `{market}`\n"
                f"Profile: {profile.capitalize()}\n"
                f"Wallet: `{wallet[:6]}...{wallet[-4:]}`"
            ),
            parse_mode="Markdown",
            reply_markup=_kb_wallet_actions(label, True),
        )
        await _admin(f"🟢 New bot: @{tg_user.username} [{label}] {market} {profile}")
    else:
        await app.bot.send_message(chat_id=tg_user.id, text="❌ Failed to start. Contact support.")


# ─────────────────────────────────────────────────────────────────────────────
# cancel_setup — abort setup flow at any inline step
# ─────────────────────────────────────────────────────────────────────────────
async def cancel_setup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "❌ *Setup cancelled.*\n\nUse /setup or /addwallet to start again.",
        parse_mode="Markdown",
    )


# /addwallet — same flow as setup, different entry
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_addwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await cmd_setup(update, context)


# ─────────────────────────────────────────────────────────────────────────────
# /removewallet
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_removewallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid     = update.effective_user.id
    wallets = get_user_wallets(tid)
    if not wallets:
        await update.message.reply_text("No wallets found.")
        return
    buttons = [[InlineKeyboardButton(
        f"🗑 {w['label']} ({w['market']})", callback_data=f"remove:{w['label']}"
    )] for w in wallets]
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="remove:cancel")])
    await update.message.reply_text(
        "Which wallet do you want to remove?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    label = query.data.split(":", 1)[1]
    if label == "cancel":
        await query.edit_message_text("Cancelled.")
        return
    tid = update.effective_user.id
    try:
        validate_label(label)
    except ValueError:
        await query.edit_message_text("❌ Invalid label.")
        return
    # Send SIGUSR2 to cancel orders + close position before removal
    await query.edit_message_text(
        f"🔄 Closing orders and positions for `{label}`, removing wallet...",
        parse_mode="Markdown",
    )
    import signal as _signal
    with mgr._lock:
        proc = mgr._procs.get((tid, validate_label(label)))
    if proc and proc.poll() is None:
        try:
            os.kill(proc.pid, _signal.SIGUSR2)
            for _ in range(15):
                await asyncio.sleep(1)
                if proc.poll() is not None:
                    break
        except Exception as e:
            log.warning(f"SIGUSR2 failed for remove_wallet {tid}/{label}: {e}")

    mgr.stop_bot(tid, label)
    removed = remove_wallet(tid, label)
    if removed:
        await query.edit_message_text(f"✅ Wallet `{label}` removed.", parse_mode="Markdown")
        uname = update.effective_user.username or str(tid)
        await _admin(f"🗑 Wallet removed: @{uname} ({tid}) | {label}")
    else:
        await query.edit_message_text("❌ Wallet not found.")


# ─────────────────────────────────────────────────────────────────────────────
# /status
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid     = update.effective_user.id
    wallets = get_user_wallets(tid)
    if not wallets:
        await update.message.reply_text("No wallets. Use /setup to get started.")
        return
    for w in wallets:
        label  = w["label"]
        status = mgr.status(tid, label)
        state  = "🟢 Running" if status["running"] else "🔴 Stopped"
        days   = max(0, 180 - int((time.time() - w["key_created_at"]) / 86400))
        await update.message.reply_text(
            f"*{label}* · {w['market']} · {state}\n"
            f"Profile: {w['profile'].capitalize()} · Key: {days}d left\n"
            f"`{w['wallet_address'][:6]}...{w['wallet_address'][-4:]}`",
            parse_mode="Markdown",
            reply_markup=_kb_wallet_actions(label, status["running"]),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Inline button callbacks — stop/resume/override/dash/config
# ─────────────────────────────────────────────────────────────────────────────
async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    tid    = update.effective_user.id
    # Handle goto_setup and goto_help
    if query.data == "goto_help":
        await cmd_help(update, context)
        return

    if query.data == "goto_setup":
        tid     = update.effective_user.id
        wallets = get_user_wallets(tid)
        if len(wallets) >= MAX_WALLETS:
            await query.edit_message_text(f"⚠️ Max {MAX_WALLETS} wallets. Use /removewallet first.")
            return
        context.user_data["setup_awaiting"] = "label"
        await query.edit_message_text(
            "*Add Wallet — Step 1 of 5*\n\n"
            "Choose a label for this wallet.\n"
            "_e.g. `main`, `grind`, `w1` — max 10 chars, letters/numbers/underscore_",
            parse_mode="Markdown",
        )
        return

    if ":" not in query.data:
        return
    action, label = query.data.split(":", 1)
    log.info(f"action_callback fired: action={action} label={label}")

    # ── stop ──
    if action == "stop":
        stopped = mgr.stop_bot(tid, label)
        if stopped:
            await query.edit_message_reply_markup(reply_markup=_kb_wallet_actions(label, False))
            await app.bot.send_message(tid, f"⏹ `{label}` stopped.", parse_mode="Markdown")
        else:
            await query.answer("Bot is not running.", show_alert=True)

    # ── resume ──
    elif action == "resume":
        started = mgr.start_bot(tid, label)
        if started:
            await query.edit_message_reply_markup(reply_markup=_kb_wallet_actions(label, True))
            await app.bot.send_message(tid, f"▶️ `{label}` restarted.", parse_mode="Markdown")
        else:
            await query.answer("Failed to start bot.", show_alert=True)

    # ── override drawdown ──
    elif action == "override":
        resumed = mgr.resume_drawdown(tid, label)
        if resumed:
            await app.bot.send_message(
                tid,
                f"✅ `{label}` drawdown override sent. Trading resumed.",
                parse_mode="Markdown",
            )
        else:
            await query.answer("Bot not running or not halted.", show_alert=True)

    # ── dash ──
    elif action == "dash":
        text = _dash_text(tid, label)
        try:
            await query.edit_message_text(text, parse_mode="Markdown",
                                           reply_markup=_kb_dash_actions(label))
        except Exception:
            await app.bot.send_message(tid, text, parse_mode="Markdown",
                                       reply_markup=_kb_dash_actions(label))

    # ── config ──
    elif action == "config":
        wallet = get_wallet(tid, label)
        if not wallet:
            await query.answer("Wallet not found.", show_alert=True)
            return
        text = (
            f"⚙️ *Config — [{label}]* · {wallet['market']}\n\n"
            f"Profile: {wallet['profile'].capitalize()}\n"
            f"Order size: ${wallet['order_size_usd'] or 'profile default'}\n"
            f"Max inventory: ${wallet['max_inventory_usd'] or 'profile default'}\n"
            f"Max daily loss: ${wallet['max_daily_loss_usd'] or 'profile default'}\n"
            f"Leverage: {wallet['leverage'] or 'profile default'}x\n\n"
            f"_Switch profile to reset all overrides._"
        )
        try:
            await query.edit_message_text(text, parse_mode="Markdown",
                                           reply_markup=_kb_config(label))
        except Exception:
            await app.bot.send_message(tid, text, parse_mode="Markdown",
                                       reply_markup=_kb_config(label))

    # ── set profile ──
    elif action == "setprofile":
        profile, lbl = label.split(":", 1)
        set_wallet_profile(tid, lbl, profile)
        mgr.restart_bot(tid, lbl)
        await query.edit_message_text(
            f"✅ `{lbl}` → *{profile.capitalize()}* profile. Bot restarted.\n"
            f"All expert overrides cleared.",
            parse_mode="Markdown",
        )

    # ── expert mode ──
    elif action == "expert":
        wallet = get_wallet(tid, label)
        max_lev = MARKET_MAX_LEVERAGE.get(wallet["market"] if wallet else "BTC-PERP", 50)
        await query.edit_message_text(
            f"🔧 *Expert Mode — [{label}]*\n\n"
            f"Tap any parameter to edit it.\n"
            f"_Leverage max for this market: {max_lev}x_\n"
            f"_Spread values in bps e.g. 6 = 6bps_\n"
            f"_Stop loss in % e.g. 1.5 = 1.5%_\n"
            f"_TOD multipliers e.g. 2.5_",
            parse_mode="Markdown",
            reply_markup=_kb_expert(label),
        )

    # ── change market ──
    elif action == "changemarket":
        await query.edit_message_text(
            f"🔄 *Change Market — [{label}]*\n\n"
            f"Select new market. Your trade history will be preserved.",
            parse_mode="Markdown",
            reply_markup=_kb_market_change(label),
        )

    # ── market selected for change ──
    elif action == "setmarket":
        new_market, lbl = label.split(":", 1)
        wallet = get_wallet(tid, lbl)
        old_market = wallet["market"] if wallet else "?"

        await query.edit_message_text(
            f"🔄 Closing orders and positions on `{old_market}`, switching to `{new_market}`...",
            parse_mode="Markdown",
        )

        # Send SIGUSR2 — cancels all orders + closes position + stops bot
        import signal as _signal
        with mgr._lock:
            proc = mgr._procs.get((tid, validate_label(lbl)))
        if proc and proc.poll() is None:
            try:
                os.kill(proc.pid, _signal.SIGUSR2)
                for _ in range(15):
                    await asyncio.sleep(1)
                    if proc.poll() is not None:
                        break
            except Exception as e:
                log.warning(f"SIGUSR2 failed for market change {tid}/{lbl}: {e}")

        # Force stop if still running
        mgr.stop_bot(tid, lbl)

        # Verify position is closed before switching
        position_open = False
        try:
            from hotstuff import InfoClient as _IC
            from hotstuff.methods.info.account import PositionsParams as _PP
            from eth_utils import to_checksum_address as _cs
            _info    = _IC(is_testnet=False)
            _address = _cs(wallet["wallet_address"])
            _pos     = _info.positions(_PP(user=_address))
            _open    = [p for p in _pos if p["instrument"] == old_market and abs(float(p["size"])) > 0]
            if _open:
                position_open = True
        except Exception as e:
            log.warning(f"Position verify failed for market change {tid}/{lbl}: {e}")

        if position_open:
            await query.edit_message_text(
                f"⚠️ Market change aborted — position on {old_market} could not be closed in time.\n\n"
                f"Please close it manually then change market.",
                parse_mode="Markdown",
            )
            mgr.restart_bot(tid, lbl)
            return

        # Switch market and restart
        set_wallet_market(tid, lbl, new_market)
        mgr.restart_bot(tid, lbl)
        await query.edit_message_text(
            f"✅ `{lbl}` market changed from `{old_market}` to `{new_market}`. Bot restarted.",
            parse_mode="Markdown",
        )
        await _admin(f"🔄 Market change: @{update.effective_user.username} [{lbl}] {old_market} -> {new_market}")

    # ── reset defaults ──
    elif action == "viewconfig":
        wallet  = get_wallet(tid, label)
        market  = wallet["market"] if wallet else "?"
        profile = wallet["profile"] if wallet else "?"
        prefix  = market.split("-")[0]
        defaults = MARKET_PROFILES.get(market, {}).get(profile, {})
        def _ev(field, default_key=None, divisor=1, suffix=""):
            def _fmt(v):
                v = float(v) / divisor
                if v == int(v): return str(int(v))
                return f"{v:.4f}".rstrip("0").rstrip(".")
            val = wallet[field] if wallet and wallet[field] is not None else None
            if val is not None:
                return f"{_fmt(val)}{suffix} ⚙️"
            if default_key and default_key in defaults:
                return f"{_fmt(defaults[default_key])}{suffix}"
            return "default"
        lines = [
            f"📋 *Config: {label.upper()}* · `{market}` · {profile.capitalize()}\n",
            f"💰 Order Size:       `{_ev('order_size_usd', f'{prefix}_ORDER_SIZE_USD')}`",
            f"📦 Max Inventory:    `{_ev('max_inventory_usd', f'{prefix}_MAX_INVENTORY_USD')}`",
            f"📉 Max Daily Loss:   `{_ev('max_daily_loss_usd', 'HOTSTUFF_MAX_DAILY_LOSS')}`",
            f"🔢 Leverage:         `{_ev('leverage', 'HOTSTUFF_LEVERAGE')}x`",
            f"📏 Spread:           `{_ev('spread_bps', f'{prefix}_SPREAD', divisor=1)}bps`",
            f"📏 Close Spread:     `{_ev('close_spread_bps', f'{prefix}_CLOSE_SPREAD', divisor=1)}bps`",
            f"🛑 Stop Loss:        `{_ev('stop_loss_pct')}%`",
            f"🔄 Allow Flips:      `{'on' if wallet and wallet['allow_flips'] else 'off'}`",
            f"⏱ Cooldown Open:    `{_ev('fill_cooldown_open_s', 'FILL_COOLDOWN_OPEN_S')}s`",
            f"⏱ Cooldown Close:   `{_ev('fill_cooldown_close_s', 'FILL_COOLDOWN_CLOSE_S')}s`",
            f"🕗 TOD 08:00 Mult:   `{_ev('tod_08_multiplier', 'TOD_08_MULTIPLIER')}x`",
            f"🕑 TOD 13:00 Mult:   `{_ev('tod_13_multiplier', 'TOD_13_MULTIPLIER')}x`",
            f"🕑 TOD 14:00 Mult:   `{_ev('tod_14_multiplier', 'TOD_14_MULTIPLIER')}x`",
            f"⚔️ Signal Conflict:  `{_ev('signal_conflict_bps', 'HOTSTUFF_SIGNAL_CONFLICT_BPS')}`",
            f"🔲 Grid Spacing:     `{_ev('grid_spacing_mult', 'HOTSTUFF_GRID_SPACING_ATR_MULT')}`",
            f"🔢 Grid Levels:      `{_ev('grid_levels', 'HOTSTUFF_GRID_LEVELS')}`",
            f"📐 Grid V-Shape:     `{_ev('grid_vshape_alpha', 'HOTSTUFF_GRID_VSHAPE_ALPHA')}`",
            f"📏 Grid Min BPS:     `{_ev('grid_min_spacing_pct', 'HOTSTUFF_GRID_MIN_SPACING_PCT')}`",
            f"🛡 Grid Overshoot:   `{_ev('grid_overshoot_mult', 'HOTSTUFF_GRID_OVERSHOOT_MULT')}`",
            f"📉 Grid Max Loss:    `{_ev('grid_max_loss_pct', 'HOTSTUFF_GRID_MAX_LOSS_PCT')}`",
            f"📊 ADX Threshold:    `{_ev('adx_threshold', 'HOTSTUFF_ADX_TREND_THRESHOLD')}`",
            f"📶 Price Move:       `{_ev('price_move_threshold')}bps`",
            f"⏳ Order TTL:        `{_ev('order_ttl_ms')}ms`",
            f"⚡ Requote CD:       `{_ev('requote_cooldown')}s`",
            f"🎯 Profit Target:    `{_ev('profit_target_bps')}bps`",
            f"🛡 Adv Selection:    `{_ev('adverse_selection_bps')}bps`",
            f"\n_⚙️ = expert override · others = profile default_",
        ]
        cfg_text = "\n".join(lines)
        try:
            await query.edit_message_text(cfg_text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Back", callback_data=f"config:{label}")
                ]]))
        except Exception:
            await app.bot.send_message(tid, cfg_text, parse_mode="Markdown")

    elif action == "resetdefaults":
        reset_expert_overrides(tid, label)
        mgr.restart_bot(tid, label)
        await query.edit_message_text(
            f"✅ `{label}` expert overrides cleared. Bot restarted with profile defaults.",
            parse_mode="Markdown",
        )

    # ── analytics ──
    elif action == 'analytics':
        parts = query.data.split(':', 3)
        if len(parts) < 4:
            return
        _, section, period, lbl = parts
        wallet = get_wallet(tid, lbl)
        if not wallet:
            await query.answer('Wallet not found.', show_alert=True)
            return
        period_labels = {'today': 'Today', '7d': 'Last 7 Days', 'alltime': 'All Time'}
        period_label  = period_labels.get(period, 'Last 7 Days')
        since = _analytics_since(period)
        d     = _analytics_db(_bot_dir(tid, lbl), wallet['wallet_address'], since)
        if section == 'perf':
            pts  = _fetch_points(wallet['wallet_address'])
            text = _build_performance_text(lbl, wallet['market'], period_label, d, pts)
        else:
            text = _build_quality_text(lbl, wallet['market'], period_label, d)
        try:
            await query.edit_message_text(
                text, parse_mode='Markdown',
                reply_markup=_kb_analytics(lbl, section, period),
            )
        except Exception:
            await app.bot.send_message(
                tid, text, parse_mode='Markdown',
                reply_markup=_kb_analytics(lbl, section, period),
            )
        return

    # ── close position + stop ──
    elif action == "closeconfirm":
        await query.edit_message_text(f"⏳ Closing position for `{label}`...", parse_mode="Markdown")
        # Send SIGUSR2 to bot — main.py cancels all orders, market-closes positions, then shuts down
        import signal as _signal
        with mgr._lock:
            proc = mgr._procs.get((tid, validate_label(label)))
        if proc and proc.poll() is None:
            try:
                os.kill(proc.pid, _signal.SIGUSR2)
                # Wait up to 15s for bot to market-close and self-terminate
                for _ in range(15):
                    await asyncio.sleep(1)
                    if proc.poll() is not None:
                        break
            except Exception:
                pass
        # Force stop if still running after wait
        mgr.stop_bot(tid, label)

        # Verify position is actually closed
        position_closed = True
        try:
            from hotstuff import InfoClient
            from hotstuff.methods.info.account import PositionsParams
            from eth_utils import to_checksum_address
            wallet_row = get_wallet(tid, label)
            if wallet_row:
                info      = InfoClient(is_testnet=False)
                address   = to_checksum_address(wallet_row["wallet_address"])
                positions = info.positions(PositionsParams(user=address))
                open_pos  = [p for p in positions if abs(float(p["size"])) > 0]
                if open_pos:
                    position_closed = False
                    pos_lines = "\n".join(
                        f"• `{p['instrument']}` size: `{float(p['size']):+.4f}`"
                        for p in open_pos
                    )
        except Exception as e:
            log.warning(f"Position verify failed: {e}")

        if position_closed:
            await query.edit_message_text(
                f"✅ `{label}` position closed and bot stopped.",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                f"⚠️ *Bot stopped but position may still be open:*\n{pos_lines}\n\n"
                f"Please close manually on the exchange.",
                parse_mode="Markdown"
            )
        await _admin(f"🔴 Close+stop: @{update.effective_user.username} [{label}]")

    # ── cancel config ──
    elif action == "cancel_config":
        await query.edit_message_text("Cancelled.")

    # ── renewkey ──
    elif action == "renewkey":
        context.user_data["renew_label"]   = label
        context.user_data["renew_awaiting"] = True
        await query.edit_message_text(
            f"*Renew Key — [{label}]*\n\n"
            f"Send your new agent private key.\n"
            f"⚠️ Bot will restart automatically.",
            parse_mode="Markdown",
        )




# ─────────────────────────────────────────────────────────────────────────────
# Expert field edit callback
# ─────────────────────────────────────────────────────────────────────────────
EXPERT_FIELD_LABELS = {
    "spread_bps":            ("Spread (bps)", "Enter in bps. e.g. 6 = 6bps. Default: balanced profile value"),
    "close_spread_bps":      ("Close Spread (bps)", "Enter in bps. e.g. 1 = 1bps. Spread used when closing a position"),
    "order_size_usd":        ("Order Size ($)", "Max size per order in USD. e.g. 500"),
    "max_inventory_usd":     ("Max Inventory ($)", "Max total position size in USD. e.g. 1000"),
    "max_daily_loss_usd":    ("Max Daily Loss ($)", "Bot halts if daily loss exceeds this. e.g. 50"),
    "leverage":              ("Leverage (x)", "Must match your leverage setting on Hotstuff. e.g. 50"),
    "stop_loss_pct":         ("Stop Loss (%)", "Hard stop loss as % of margin. e.g. 1.5 = 1.5%"),
    "allow_flips":           ("Allow Flips", "0 = off (safer), 1 = on (bot can flip from long to short)"),
    "fill_cooldown_open_s":  ("Fill Cooldown Open (s)", "Seconds to pause quoting after an opening fill. e.g. 20"),
    "fill_cooldown_close_s": ("Fill Cooldown Close (s)", "Seconds to pause after a closing fill. e.g. 2"),
    "tod_08_multiplier":     ("TOD 08:00 Multiplier", "Spread multiplier at 08:00 UTC (London open). e.g. 2.5 = 2.5x wider. Always UTC — not your local time."),
    "tod_13_multiplier":     ("TOD 13:00 Multiplier", "Spread multiplier at 13:00 UTC (New York open). e.g. 2.0 = 2x wider. Always UTC — not your local time."),
    "tod_14_multiplier":     ("TOD 14:00 Multiplier", "Spread multiplier at 14:00 UTC (NY/London overlap). e.g. 2.0 = 2x wider. Always UTC — not your local time."),
    "adx_threshold":         ("ADX Threshold", "ADX level to detect trending market. e.g. 22. Higher = less sensitive"),
    "price_move_threshold":  ("Price Move Threshold (bps)", "Enter in bps. e.g. 0.3 = 0.3bps. Min price move before bot requotes. Lower = tighter quotes but more cancels"),
    "order_ttl_ms":          ("Order TTL (ms)", "How long an order rests before auto-cancel. e.g. 10000 = 10s, 30000 = 30s"),
    "requote_cooldown":      ("Requote Cooldown (s)", "Min seconds between requotes. e.g. 0.3. Raise to reduce cancel rate"),
    "profit_target_bps":     ("Profit Target (bps)", "Enter in bps. e.g. 5 = 5bps (default). Min profit built into spread. Set to 0 for tightest possible quotes"),
    "adverse_selection_bps": ("Adverse Selection Floor (bps)", "Enter in bps. e.g. 0.27 (default). Cost of toxic flow built into spread. Set to 0 to disable entirely"),
    "signal_conflict_bps":   ("Signal Conflict (0-1)", "Blocks trading when regime and order flow disagree. e.g. 0.3 (default). 0 = disabled, 1 = only trade when fully aligned"),
    "grid_spacing_mult":     ("Grid Spacing Multiplier", "Controls spacing between grid levels. spacing = ATR x this value. Lower = tighter grid, more fills. e.g. 0.5 (default), 0.2 = tight, 0.8 = wide."),
    "grid_levels":           ("Grid Levels (per side)", "Number of resting orders per side. Min 1, max 100. e.g. 5 (default), 10 = more coverage, 20 = very dense grid. Note: account size limits how many levels are viable."),
    "grid_vshape_alpha":     ("Grid V-Shape Alpha", "Size distribution across levels. 0.0 = uniform (all levels same size). 0.4 = default (outer levels bigger). Higher = more size at outer levels."),
    "grid_min_spacing_pct":  ("Grid Min Spacing", "Minimum level spacing as a fraction of mid price. e.g. 0.0005 = 5bps (default), 0.0002 = 2bps, 0.0001 = 1bps. Lower = tighter grid, more fills. Below 2bps on BTC causes very frequent rebalances which can overwhelm the quote cycle — recommended minimum is 2bps."),
    "grid_overshoot_mult":   ("Grid Overshoot Multiplier", "Inventory overshoot threshold. If position exceeds max inventory x this value, a passive close is placed and grid halts. e.g. 1.1 = 10 percent overshoot allowed (default), 1.05 = tighter, 1.2 = looser."),
    "grid_max_loss_pct":     ("Grid Max Loss", "Maximum unrealised loss as a fraction of max inventory before immediate market close. e.g. 0.02 = 2 percent (default), 0.05 = 5 percent, 0.01 = 1 percent. Fires directly as market close with no passive phase."),
}


async def expert_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, field, label = query.data.split(":", 2)
    fname, hint     = EXPERT_FIELD_LABELS.get(field, (field, ""))
    context.user_data["expert_field"] = field
    context.user_data["expert_label"] = label
    context.user_data["expert_awaiting"] = True
    cancel_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"expert_cancel:{label}")
    ]])
    await query.edit_message_text(
        f"🔧 *{fname}* — [{label}]\n\n_{hint}_\n\nSend the new value:",
        parse_mode="Markdown",
        reply_markup=cancel_kb,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Generic text handler — catches wallet setup steps + expert edits + renew key
# ─────────────────────────────────────────────────────────────────────────────

async def expert_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    label = query.data.split(":", 1)[1]
    context.user_data.pop("expert_awaiting", None)
    context.user_data.pop("expert_field", None)
    context.user_data.pop("expert_label", None)
    await query.edit_message_text(
        f"↩️ Cancelled.",
        reply_markup=_kb_expert(label),
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data

    # ── setup label (from button flow) ──
    if ud.get("setup_awaiting") == "label":
        try:
            label = validate_label(update.message.text.strip())
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}\nTry again:")
            return
        tid = update.effective_user.id
        if get_wallet(tid, label):
            await update.message.reply_text(f"❌ Label `{label}` already used. Choose another:", parse_mode="Markdown")
            return
        ud["setup_label"]   = label
        ud["setup_awaiting"] = None
        await update.message.reply_text(
            f"*Step 2 of 5 — Market*\n\nLabel: `{label}`\n\nWhich market to trade?",
            parse_mode="Markdown",
            reply_markup=_kb_market(),
        )
        return

    # ── setup wallet addr ──
    if ud.get("setup_awaiting") == "wallet_addr":
        await setup_wallet_addr(update, context)
        return

    # ── setup agent key ──
    if ud.get("setup_awaiting") == "agent_key":
        await setup_agent_key(update, context)
        return

    # ── renew key ──
    if ud.get("renew_awaiting"):
        label     = ud.get("renew_label")
        agent_key = update.message.text.strip()
        try:
            await update.message.delete()
        except Exception:
            pass
        if not agent_key.startswith("0x") or len(agent_key) < 60:
            await update.message.reply_text("❌ Invalid key. Try again:")
            return
        tid = update.effective_user.id
        try:
            enc = vault.encrypt(agent_key)
            update_agent_key(tid, label, enc)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed: {e}")
            ud.clear()
            return
        await update.message.reply_text(f"✅ Key updated for `{label}`. Restarting...", parse_mode="Markdown")
        mgr.restart_bot(tid, label)
        await update.message.reply_text(f"🟢 `{label}` restarted.", parse_mode="Markdown")
        await _admin(f"🔑 Key renewed: @{update.effective_user.username} [{label}]")
        ud.clear()
        return

    # ── expert field value ──
    if ud.get("expert_awaiting"):
        field = ud.get("expert_field")
        label = ud.get("expert_label")
        tid   = update.effective_user.id
        raw   = update.message.text.strip()
        try:
            if field == "allow_flips":
                val = int(raw)
                assert val in (0, 1)
            elif field == "leverage":
                wallet  = get_wallet(tid, label)
                max_lev = MARKET_MAX_LEVERAGE.get(wallet["market"] if wallet else "BTC-PERP", 50)
                val     = int(raw)
                assert 1 <= val <= max_lev, f"Max leverage for this market is {max_lev}x"
            elif field == "grid_levels":
                val = int(float(raw))
                assert 1 <= val <= 100, "Grid levels must be between 1 and 100"
            elif field == "grid_vshape_alpha":
                val = float(raw)
                assert val >= 0, "Grid V-Shape must be 0 or higher"
            elif field == "grid_overshoot_mult":
                val = float(raw)
                assert val >= 1.0, "Grid Overshoot must be 1.0 or higher (e.g. 1.1 = 10% overshoot)"
            elif field == "grid_max_loss_pct":
                val = float(raw)
                assert 0 < val < 1.0, "Grid Max Loss must be between 0 and 1 (e.g. 0.02 = 2%)"
            else:
                val = float(raw)
                assert val > 0
        except AssertionError as e:
            await update.message.reply_text(f"❌ {e}\nTry again:")
            return
        except Exception:
            await update.message.reply_text("❌ Invalid value. Try again:")
            return

        set_expert_overrides(tid, label, **{field: val})
        fname = EXPERT_FIELD_LABELS.get(field, (field,))[0]
        status = mgr.status(tid, label)
        if status.get("running"):
            mgr.restart_bot(tid, label)
            msg = f"✅ *{fname}* set to `{val}` for [{label}]. Bot restarted."
        else:
            msg = f"✅ *{fname}* set to `{val}` for [{label}]. Bot is stopped — changes will apply on next start."
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=_kb_expert(label))
        ud.pop("expert_awaiting", None)
        ud.pop("expert_field", None)
        ud.pop("expert_label", None)
        return


# ─────────────────────────────────────────────────────────────────────────────
# /stop /resume /override via commands
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    if not wallet:
        return
    status = mgr.status(tid, label)
    if not status["running"]:
        await update.message.reply_text(
            f"⚠️ `{label}` is not running. Use /resume first.",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text(
        f"⚠️ *Close Position — [{label}]*\n\n"
        f"This will:\n"
        f"1. Market-close your open position\n"
        f"2. Cancel all open orders\n"
        f"3. Stop the bot\n\n"
        f"Are you sure?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm", callback_data=f"closeconfirm:{label}"),
            InlineKeyboardButton("❌ Cancel",  callback_data=f"cancel_config:{label}"),
        ]])
    )


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    bot_dir  = f"/root/bots/user_{tid}_{label}"
    log_path = f"{bot_dir}/bot.log"
    if not os.path.exists(log_path):
        await update.message.reply_text(
            f"⚠️ No log file found for `{label}`.",
            parse_mode="Markdown"
        )
        return
    try:
        with open(log_path, "r") as f:
            all_lines = f.readlines()
        tail_lines = all_lines[-30:]
        tail = "".join(tail_lines).strip()
        if not tail:
            await update.message.reply_text(f"📋 Log file empty for `{label}`.", parse_mode="Markdown")
            return
        header = f"📋 *Logs [{label}] — last {len(tail_lines)} lines:*\n\n"
        msg    = header + tail
        if len(msg) > 4000:
            msg = header + "".join(tail_lines)[-3800:]
        from admin_commands import _send_chunked
        await _send_chunked(update, msg, parse_mode=None)
    except Exception as e:
        await update.message.reply_text(f"❌ Error reading log: {e}")


async def cmd_admin_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    import signal as _signal
    rows = list_all_wallets()
    running = [(r["telegram_id"], r["label"]) for r in rows if mgr.status(r["telegram_id"], r["label"])["running"]]
    if not running:
        await update.message.reply_text("No bots currently running.")
        return
    await update.message.reply_text(f"⏳ Sending close signal to {len(running)} bots...")
    results = []
    for tid, label in running:
        try:
            with mgr._lock:
                proc = mgr._procs.get((tid, label))
            if proc and proc.poll() is None:
                os.kill(proc.pid, _signal.SIGUSR2)
                results.append(f"✅ `{label}` — SIGUSR2 sent")
            else:
                results.append(f"⚠️ `{label}` — not running")
        except Exception as e:
            results.append(f"❌ `{label}` — {e}")
    # Wait up to 15s for bots to self-terminate
    import asyncio
    for _ in range(15):
        await asyncio.sleep(1)
        still_running = [
            (tid, label) for tid, label in running
            if mgr.status(tid, label)["running"]
        ]
        if not still_running:
            break
    # Force stop any stragglers
    for tid, label in still_running:
        mgr.stop_bot(tid, label)
        results.append(f"🔴 `{label}` — force stopped")
    summary = "\n".join(results)
    await update.message.reply_text(
        f"*Admin Close All — Done*\n\n{summary}",
        parse_mode="Markdown"
    )
    await update.message.reply_text(
        f"⚠️ Positions may still be open on exchange — verify manually or via `/admin_platform`."
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    if mgr.stop_bot(tid, label):
        await update.message.reply_text(f"⏹ `{label}` stopped.", parse_mode="Markdown",
                                         reply_markup=_kb_wallet_actions(label, False))
    else:
        await update.message.reply_text(f"⚠️ `{label}` not running.", parse_mode="Markdown")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    if wallet and wallet["state"] == "active":
        await update.message.reply_text(f"⚠️ `{label}` already running.", parse_mode="Markdown")
        return
    if mgr.start_bot(tid, label):
        await update.message.reply_text(f"▶️ `{label}` restarted.", parse_mode="Markdown",
                                         reply_markup=_kb_wallet_actions(label, True))
    else:
        await update.message.reply_text(f"❌ Failed to restart `{label}`.", parse_mode="Markdown")


async def cmd_override(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    if mgr.resume_drawdown(tid, label):
        await update.message.reply_text(
            f"✅ `{label}` drawdown override sent. Trading resumed.",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"⚠️ Could not override `{label}`.", parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# /dash /pnl /volume /points /balance
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# /analytics helpers
# ─────────────────────────────────────────────────────────────────────────────
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
            "SUM(closed_pnl) net_pnl, SUM(fee) fees, "
            "SUM(closed_pnl + fee) gross_pnl "
            "FROM fills WHERE address=? AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        best = conn.execute(
            "SELECT closed_pnl as net, timestamp FROM fills "
            "WHERE address=? AND " + ts_filter + " ORDER BY net DESC LIMIT 1",
            (wallet_address, since, since)
        ).fetchone()

        worst = conn.execute(
            "SELECT closed_pnl as net, timestamp FROM fills "
            "WHERE address=? AND " + ts_filter + " ORDER BY net ASC LIMIT 1",
            (wallet_address, since, since)
        ).fetchone()

        wins = conn.execute(
            "SELECT COUNT(*) wins FROM fills "
            "WHERE address=? AND direction IN ('closeShort','closeLong') "
            "AND closed_pnl > 0 AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        total_closes = conn.execute(
            "SELECT COUNT(*) total FROM fills "
            "WHERE address=? AND direction IN ('closeShort','closeLong') "
            "AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        recent = conn.execute(
            "SELECT closed_pnl as net FROM fills "
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
            "SELECT direction, SUM(closed_pnl) net_pnl, COUNT(*) trades "
            "FROM fills WHERE address=? AND " + ts_filter + " "
            "GROUP BY direction ORDER BY direction",
            (wallet_address, since, since)
        ).fetchall()

        fee_eff = conn.execute(
            "SELECT SUM(fee) total_fee, SUM(closed_pnl + fee) gross_pnl, "
            "SUM(CASE WHEN fee > 0 THEN fee ELSE 0 END) taker_fees, "
            "SUM(CASE WHEN fee < 0 THEN ABS(fee) ELSE 0 END) maker_rebates, "
            "AVG(CASE WHEN fee > 0 THEN fee / NULLIF(notional, 0) * 100 END) avg_taker_fee_pct "
            "FROM fills WHERE address=? AND notional > 0 AND " + ts_filter,
            (wallet_address, since, since)
        ).fetchone()

        hourly = conn.execute(
            "SELECT CAST(CASE "
            "WHEN timestamp LIKE '%T%' THEN SUBSTR(timestamp, 12, 2) "
            "ELSE CAST(CAST(timestamp AS REAL)/1000/3600%24 AS INTEGER) "
            "END AS INTEGER) AS hour, "
            "SUM(notional) vol, SUM(closed_pnl) net_pnl, COUNT(*) trades "
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

        placement = conn.execute(
            "SELECT AVG(placement_ms) avg_ms, MIN(placement_ms) min_ms, "
            "MAX(placement_ms) max_ms, COUNT(*) cnt "
            "FROM placed_orders WHERE address=? AND placement_ms IS NOT NULL",
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
            "net_pnl":        r["net_pnl"] or 0,
            "fees":           r["fees"] or 0,
            "gross_pnl":      r["gross_pnl"] or 0,
            "fee_total":      fee_eff["total_fee"] or 0 if fee_eff else 0,
            "taker_fees":     fee_eff["taker_fees"] or 0 if fee_eff else 0,
            "maker_rebates":  fee_eff["maker_rebates"] or 0 if fee_eff else 0,
            "fee_pct":        fee_eff["avg_taker_fee_pct"] or 0 if fee_eff else 0,
            "best_trade":     {"net": best["net"], "ts": best["timestamp"]} if best and best["net"] else None,
            "worst_trade":    {"net": worst["net"], "ts": worst["timestamp"]} if worst and worst["net"] else None,
            "win_rate":       round(wins["wins"] / total_closes["total"] * 100) if total_closes and total_closes["total"] > 0 else None,
            "streak":         (streak, "W" if recent and recent[0]["net"] > 0 else "L") if streak else None,
            "directions":     [dict(d) for d in directions],
            "hourly":         [dict(h) for h in hourly],
            "markout":        dict(markouts) if markouts and markouts["cnt"] > 0 else None,
            "spread_capture": spread_capture,
            "latency":        dict(latency) if latency and latency["cnt"] > 0 else None,
            "placement":      dict(placement) if placement and placement["cnt"] > 0 else None,
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
    if avg < 5.0:
        return "Fast fills. Spread may be too tight - consider widening."
    elif avg < 30.0:
        return "Healthy. Orders resting before filling naturally."
    elif avg < 120.0:
        return "Slow fills. Spread may be too wide or market is quiet."
    else:
        return "Very slow fills. Consider tightening spread or switching market."


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
    lines.append("  Gross `$" + "{:+.2f}".format(d["gross_pnl"]) + "` - Net `$" + "{:+.2f}".format(d["net_pnl"]) + "`")
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

    lines.append("\n" + chr(128184) + " *Fees*")
    lines.append("  Taker paid `$" + "{:.2f}".format(d["taker_fees"]) + "` - Maker rebates `$" + "{:.2f}".format(d["maker_rebates"]) + "` - Net `$" + "{:.2f}".format(d["fee_total"]) + "`")
    lines.append("  Avg taker fee `" + "{:.4f}".format(d["fee_pct"]) + "%`/trade")
    if d.get("gross_pnl") and d["gross_pnl"] != 0:
        fee_ratio = abs(d["taker_fees"]) / abs(d["gross_pnl"]) * 100
        lines.append("  Taker fees as pct of Gross PnL: `" + "{:.1f}".format(fee_ratio) + "%`")

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
        lines.append("\n" + chr(8987) + " *Fill Time*")
        lines.append("  Avg `" + "{:.1f}".format(avg) + "s` - Min `" + "{:.1f}".format(mn) + "s` - Max `" + "{:.1f}".format(mx) + "s`")
        lines.append("  -> _" + _latency_interpretation(avg) + "_")

    pl = d.get("placement")
    if pl and pl.get("cnt", 0) > 0:
        avg_ms = pl.get("avg_ms") or 0
        min_ms = pl.get("min_ms") or 0
        max_ms = pl.get("max_ms") or 0
        lines.append("\n" + chr(9889) + " *Order Placement Latency*")
        lines.append("  Avg `" + "{:.0f}".format(avg_ms) + "ms` - Min `" + "{:.0f}".format(min_ms) + "ms` - Max `" + "{:.0f}".format(max_ms) + "ms`")
        if avg_ms < 200:
            lines.append("  -> _At exchange baseline. Healthy._")
        elif avg_ms < 400:
            lines.append("  -> _Slightly elevated. Monitor VPS load._")
        elif avg_ms < 1000:
            lines.append("  -> _Degraded. Check network and VPS._")
        else:
            lines.append("  -> _Critical. Exchange connectivity issue._")

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


async def cmd_dash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid     = update.effective_user.id
    wallets = get_user_wallets(tid)
    if not wallets:
        await update.message.reply_text("No wallets. Use /setup first.")
        return
    targets = wallets
    if context.args:
        try:
            lbl     = validate_label(context.args[0])
            targets = [w for w in wallets if w["label"] == lbl]
            if not targets:
                await update.message.reply_text(f"❌ Wallet `{lbl}` not found.", parse_mode="Markdown")
                return
        except ValueError:
            pass
    await update.message.reply_text("⏳ Fetching stats...")
    for w in targets:
        text = _dash_text(tid, w["label"])
        await update.message.reply_text(text, parse_mode="Markdown",
                                         reply_markup=_kb_dash_actions(w["label"]))


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    stats  = _db_stats(wallet["wallet_address"], _bot_dir(tid, label))
    if not stats:
        await update.message.reply_text(f"`{label}`: No trade data yet.", parse_mode="Markdown")
        return
    t = stats["today"]
    await update.message.reply_text(
        f"💰 *{label}* Today's PnL\n\n"
        f"Gross: `${t['gross_pnl']:+.2f}` · Taker fees: `${t['taker_fees']:.2f}` · "
        f"Rebates: `${t['maker_rebates']:.2f}`\n"
        f"Net: `${t['net_pnl']:+.2f}` · Trades: `{t['trades']}`",
        parse_mode="Markdown",
    )


async def cmd_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    stats  = _db_stats(wallet["wallet_address"], _bot_dir(tid, label))
    if not stats:
        await update.message.reply_text(f"`{label}`: No trade data yet.", parse_mode="Markdown")
        return
    t = stats["today"]
    await update.message.reply_text(
        f"📈 *{label}* Today's Volume\n\n"
        f"Volume: `${t['volume']:,.0f}` · Trades: `{t['trades']}`",
        parse_mode="Markdown",
    )


async def cmd_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    data   = _fetch_points(wallet["wallet_address"])
    league = LEAGUE_MAP.get(data.get("net_league", 0), "Unranked")
    total_pts = data.get("total_points", 0)

    lines = [
        f"\U0001F3C6 *{label}* Points\n",
        f"Points: `{total_pts:,}` \u00b7 "
        f"League: `{league}` \u00b7 Rank: `#{data.get('net_rank',0)}`",
    ]

    if total_pts > 0:
        lines.append("")
        lines.append(_points_value_text(total_pts))

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_pointsvalue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone points value calculator.
    Usage: /pointsvalue           — uses your wallet's points
           /pointsvalue 50000     — manual point count
    """
    tid = update.effective_user.id
    wallets_list = get_user_wallets(tid)
    manual_pts = None

    if context.args:
        try:
            manual_pts = int(context.args[0].replace(",", "").replace("_", ""))
        except ValueError:
            await update.message.reply_text(
                "Usage: `/pointsvalue` or `/pointsvalue 50000`",
                parse_mode="Markdown",
            )
            return

    if manual_pts is not None:
        total_pts = manual_pts
    elif wallets_list:
        total_pts = 0
        for w in wallets_list:
            pts = _fetch_points(w["wallet_address"])
            total_pts += pts.get("total_points", 0)
        if total_pts == 0:
            await update.message.reply_text("No points earned yet.")
            return
    else:
        await update.message.reply_text(
            "No wallets set up. Use `/pointsvalue 50000` to estimate a custom amount.",
            parse_mode="Markdown",
        )
        return

    lines = [
        f"\U0001F3AF *Points Value Calculator*\n",
        f"Your points: `{total_pts:,}`\n",
        _points_value_text(total_pts),
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Points value helpers ─────────────────────────────────────────────────
# Program constants — update COMPLETED_WEEKS each week
_PTS_RETRO          = 1_400_000
_PTS_TOTAL_WEEKS    = 30
_PTS_COMPLETED_WKS  = 6                # ← bump weekly
_PTS_COMPLETED_RATE = 500_000           # rate for all completed weeks so far
_PTS_WEEKS_LEFT     = _PTS_TOTAL_WEEKS - _PTS_COMPLETED_WKS
_PTS_DISTRIBUTED    = _PTS_RETRO + (_PTS_COMPLETED_WKS * _PTS_COMPLETED_RATE)

# Tier rates: 500K, 1M, 1.25M, 1.5M
# Blended projections for remaining weeks (avg of adjacent tiers)
#   Low:      avg(500K, 500K) = 500K/wk
#   Low-Mid:  avg(500K, 1M)   = 750K/wk
#   High:     avg(1.25M,1.5M) = 1.375M/wk
_PTS_BLEND_LO  = 500_000               # avg(500K, 500K)
_PTS_BLEND_MID = 750_000               # avg(500K, 1M)
_PTS_BLEND_HI  = 1_375_000             # avg(1.25M, 1.5M)
_PTS_POOL_LOW  = _PTS_DISTRIBUTED + int(_PTS_WEEKS_LEFT * _PTS_BLEND_LO)   # 16.4M
_PTS_POOL_MID  = _PTS_DISTRIBUTED + int(_PTS_WEEKS_LEFT * _PTS_BLEND_MID)  # 22.4M
_PTS_POOL_HIGH = _PTS_DISTRIBUTED + int(_PTS_WEEKS_LEFT * _PTS_BLEND_HI)   # 37.4M

_PTS_FDV_SCENARIOS = [10_000_000, 50_000_000, 100_000_000, 200_000_000]
_PTS_SUPPLY_PCTS   = [0.10, 0.20, 0.30]


def _pts_fmt_num(n):
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(int(n))


def _pts_fmt_usd(n):
    if abs(n) >= 1_000_000:
        return f"${n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"${n/1_000:.1f}K"
    if abs(n) >= 1:
        return f"${n:,.2f}"
    if abs(n) >= 0.01:
        return f"${n:,.2f}"
    return f"${n:,.4f}"


def _points_value_text(user_pts: int) -> str:
    """Build value display grouped by FDV, showing low/mid/high per supply %."""
    share_best  = user_pts / _PTS_POOL_LOW
    share_worst = user_pts / _PTS_POOL_HIGH

    lines = []
    lines.append(f"\U0001F4CA *Estimated Value*")
    lines.append(f"  Pool: `{_pts_fmt_num(_PTS_POOL_LOW)}` / `{_pts_fmt_num(_PTS_POOL_MID)}` / `{_pts_fmt_num(_PTS_POOL_HIGH)}` pts")
    lines.append(f"  Your share: `{share_worst*100:.4f}%` \u2013 `{share_best*100:.4f}%`")

    for fdv in _PTS_FDV_SCENARIOS:
        lines.append("")
        lines.append(f"\U0001F4B0 *{_pts_fmt_num(fdv)} FDV*")
        for pct in _PTS_SUPPLY_PCTS:
            vals = []
            for pool in [_PTS_POOL_LOW, _PTS_POOL_MID, _PTS_POOL_HIGH]:
                val = fdv * pct * (user_pts / pool)
                vals.append(_pts_fmt_usd(val))
            lines.append(f"  {int(pct*100)}% \u2192 {vals[0]} / {vals[1]} / {vals[2]}")

    lines.append(f"\n_Values shown as Low / Mid / High pool estimates._")
    lines.append(f"_Wk {_PTS_COMPLETED_WKS}/{_PTS_TOTAL_WEEKS} \u00b7 {_PTS_WEEKS_LEFT} wks remaining._")
    return "\n".join(lines)


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet  = get_wallet(tid, label)
    data    = _fetch_account(wallet["wallet_address"])
    equity  = float(data.get("total_account_equity") or 0)
    im_used = float(data.get("initial_margin") or 0)
    avail   = float(data.get("available_balance") or 0)
    upnl_b  = float(data.get("upnl") or 0)
    await update.message.reply_text(
        f"💳 *{label}* Balance\n\n"
        f"Equity: `${equity:,.2f}` · "
        f"Available: `${avail:,.2f}` · "
        f"IM Used: `${im_used:,.2f}`\n"
        f"uPnL: `${upnl_b:+.4f}`",
        parse_mode="Markdown",
    )




# ─────────────────────────────────────────────────────────────────────────────
# /analytics
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    if not wallet:
        await update.message.reply_text('Wallet not found.')
        return
    since  = _analytics_since('7d')
    d      = _analytics_db(_bot_dir(tid, label), wallet['wallet_address'], since)
    pts    = _fetch_points(wallet['wallet_address'])
    text   = _build_performance_text(label, wallet['market'], 'Last 7 Days', d, pts)
    await update.message.reply_text(
        text, parse_mode='Markdown',
        reply_markup=_kb_analytics(label, 'perf', '7d'),
    )
# ─────────────────────────────────────────────────────────────────────────────
# /config /renewkey via commands
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    wallet = get_wallet(tid, label)
    await update.message.reply_text(
        f"⚙️ *Config — [{label}]* · {wallet['market']}\n\n"
        f"Profile: {wallet['profile'].capitalize()}\n"
        f"Order size: ${wallet['order_size_usd'] or 'profile default'}\n"
        f"Max inventory: ${wallet['max_inventory_usd'] or 'profile default'}\n"
        f"Max daily loss: ${wallet['max_daily_loss_usd'] or 'profile default'}\n"
        f"Leverage: {wallet['leverage'] or 'profile default'}x",
        parse_mode="Markdown",
        reply_markup=_kb_config(label),
    )


async def cmd_renewkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tid, label, _ = await _require_label(update, context)
    if not label:
        return
    context.user_data["renew_label"]    = label
    context.user_data["renew_awaiting"] = True
    await update.message.reply_text(
        f"*Renew Key — [{label}]*\n\nSend your new agent private key.\n"
        f"⚠️ Bot restarts automatically.",
        parse_mode="Markdown",
    )


# ─────────────────────────────────────────────────────────────────────────────
# App build
# ─────────────────────────────────────────────────────────────────────────────
def build_app() -> Application:
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Commands
    application.add_handler(CommandHandler("start",        cmd_start))
    application.add_handler(CommandHandler("setup",        cmd_setup))
    application.add_handler(CommandHandler("addwallet",    cmd_addwallet))
    application.add_handler(CommandHandler("help",         cmd_help))
    application.add_handler(CommandHandler("status",       cmd_status))
    application.add_handler(CommandHandler("stop",         cmd_stop))
    application.add_handler(CommandHandler("resume",       cmd_resume))
    application.add_handler(CommandHandler("override",     cmd_override))
    application.add_handler(CommandHandler("analytics",    cmd_analytics))
    application.add_handler(CommandHandler("dash",         cmd_dash))
    application.add_handler(CommandHandler("pnl",          cmd_pnl))
    application.add_handler(CommandHandler("volume",       cmd_volume))
    application.add_handler(CommandHandler("points",       cmd_points))
    application.add_handler(CommandHandler("pointsvalue",  cmd_pointsvalue))
    application.add_handler(CommandHandler("balance",      cmd_balance))
    application.add_handler(CommandHandler("config",       cmd_config))
    application.add_handler(CommandHandler("renewkey",     cmd_renewkey))
    application.add_handler(CommandHandler("close",        cmd_close))
    application.add_handler(CommandHandler("logs",         cmd_logs))
    application.add_handler(CommandHandler("removewallet", cmd_removewallet))
    application.add_handler(CommandHandler("admin_closeall", cmd_admin_closeall))
    application.add_handler(CommandHandler("modes",        cmd_modes))

    # Callbacks
    application.add_handler(CallbackQueryHandler(setup_market_callback,  pattern=r"^market:"))
    application.add_handler(CallbackQueryHandler(cancel_setup_callback,  pattern=r"^cancel_setup$"))
    application.add_handler(CallbackQueryHandler(setup_profile_callback, pattern=r"^profile:"))
    application.add_handler(CallbackQueryHandler(remove_callback,        pattern=r"^remove:"))
    application.add_handler(CallbackQueryHandler(expert_field_callback,  pattern=r"^exp:"))
    application.add_handler(CallbackQueryHandler(expert_cancel_callback, pattern=r"^expert_cancel:"))
    application.add_handler(CallbackQueryHandler(action_callback))

    # Generic text (setup steps, expert edits, renew key)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    from telegram.ext import PollAnswerHandler
    application.add_handler(PollAnswerHandler(handle_poll_answer))
    register_admin_handlers(application, mgr)

    return application


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
async def post_init(application: Application) -> None:
    """Register commands with Telegram so they appear in the menu."""
    mgr.start()
    mgr.start()
    await application.bot.set_my_commands([
        ("start",        "Welcome + onboarding"),
        ("setup",        "Add first wallet"),
        ("addwallet",    "Add second wallet"),
        ("analytics",    "Trading analytics dashboard"),
        ("removewallet", "Remove a wallet"),
        ("status",       "All wallets + action buttons"),
        ("dash",         "Full stats dashboard"),
        ("pnl",          "Today PnL"),
        ("volume",       "Today volume"),
        ("points",       "Points + league + rank"),
        ("pointsvalue",  "Points value calculator"),
        ("balance",      "Wallet balance"),
        ("stop",         "Stop a bot"),
        ("resume",       "Restart stopped bot"),
        ("override",     "Resume after drawdown halt"),
        ("config",       "Profile + expert mode"),
        ("renewkey",     "Update agent key"),
        ("close",        "Close position and stop bot"),
        ("logs",         "View recent bot logs"),
        ("modes",        "How Range vs Trend mode works"),
        ("help",         "All commands"),
    ])
    log.info("Commands registered with Telegram")


if __name__ == "__main__":
    init_db()
    app = build_app()
    mgr.notify_fn = _notify
    log.info("NenMMBot starting...")
    app.run_polling(drop_pending_updates=True)
