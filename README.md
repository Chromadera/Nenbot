# NenMMBot — Architecture & Operations Guide

**Multi-user market-making SaaS on the Hotstuff perpetuals DEX**
*Last updated: May 2026*

---

## Overview

NenMMBot is a production market-making platform that runs automated trading bots for multiple users on the Hotstuff perpetual futures DEX. Each user gets an isolated bot process that quotes orders, manages inventory, and earns trading points — all controlled through a Telegram interface.

The platform runs on a single VPS instance , serving  active user bots across 15 markets including BTC-PERP, GOLD-PERP, SILVER-PERP, USDJPY-PERP, and commodity perps.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Telegram Users                        │
│              /start /setup /dash /config                 │
└──────────────────────┬──────────────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │    telegram_bot.py       │  Telegram Bot (async, python-telegram-bot)
          │    2,125 lines           │  Commands, inline keyboards, dashboards
          │    admin_commands.py     │  26+ admin toolkit commands
          └────────────┬────────────┘
                       │
          ┌────────────▼────────────┐
          │      manager.py          │  BotManager — process lifecycle
          │      1,295 lines         │  Provision, start, stop, restart, monitor
          │                          │  Crash detection, key expiry, notifications
          └────────────┬────────────┘
                       │ subprocess.Popen per user
                       │
     ┌─────────────────▼──────────────────┐
     │         Per-User Bot Process        │
     │    /root/bots/user_{tid}_{label}/   │
     │                                     │
     │  ┌──────────────────────────────┐   │
     │  │  main.py (929 lines)         │   │  Event loop, quote thread,
     │  │  — Quote thread              │   │  mode switching, position mgmt,
     │  │  — Grid watchdog             │   │  grid SL system, trailing TP
     │  │  — Transition engine         │   │
     │  │  — Fill handler              │   │
     │  └──────────┬───────────────────┘   │
     │             │                       │
     │  ┌──────────▼───────────────────┐   │
     │  │  exchange.py (447 lines)     │   │  HotstuffClient — WS + REST,
     │  │  — FeedReader (Unix socket)  │   │  BBO subscription, fills sub,
     │  │  — WS direct fallback        │   │  position tracking, watchdog
     │  │  — Position cache            │   │
     │  └──────────────────────────────┘   │
     │                                     │
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │ orders.py   │ │ quoting.py   │   │  OrderManager — bracket orders,
     │  │ 573 lines   │ │ 277 lines    │   │  grid placement, cancel/requote
     │  └─────────────┘ └──────────────┘   │  Quoter — fair price, spread,
     │                                     │  inventory skew, adverse selection
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │ grid.py     │ │ regime.py    │   │  GridManager — adaptive grid levels
     │  │ 228 lines   │ │ 238 lines    │   │  RegimeDetector — ADX hysteresis
     │  └─────────────┘ └──────────────┘   │
     │                                     │
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │ signals.py  │ │ indicators.py│   │  SignalPipeline — RSI, VWAP, OFI
     │  │ 232 lines   │ │ 229 lines    │   │  IndicatorEngine — ATR, RSI, VWAP
     │  └─────────────┘ └──────────────┘   │
     │                                     │
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │ markout.py  │ │ drawdown.py  │   │  MarkoutTracker — fill quality
     │  │ 328 lines   │ │ 147 lines    │   │  DrawdownMonitor — daily loss cap
     │  └─────────────┘ └──────────────┘   │
     │                                     │
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │ db.py       │ │ ofi.py       │   │  SQLite — fills, snapshots, orders
     │  │ 386 lines   │ │ 220 lines    │   │  OFI sidecar — order flow imbalance
     │  └─────────────┘ └──────────────┘   │
     │                                     │
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │ tracker.py  │ │ analytics.py │   │  PnL tracker, CSV export
     │  │ 362 lines   │ │ 266 lines    │   │  Analytics queries
     │  └─────────────┘ └──────────────┘   │
     │                                     │
     │  ┌─────────────┐ ┌──────────────┐   │
     │  │dashboard.py │ │ chart.py     │   │  FastAPI dashboard (port 3000)
     │  │ 960 lines   │ │ 479 lines    │   │  Chart generation
     │  └─────────────┘ └──────────────┘   │
     │                                     │
     └─────────────────────────────────────┘
                       │
          ┌────────────▼────────────┐
          │      feed.py             │  Shared market data feed
          │      356 lines           │  One WS per market → Unix socket broadcast
          │      /tmp/nenfeed_*.sock │  All user bots connect as clients
          └────────────┬────────────┘
                       │
          ┌────────────▼────────────┐
          │   Hotstuff DEX           │  Perpetual futures exchange
          │   REST API + WebSocket   │  Orders, positions, fills, BBO
          └─────────────────────────┘
```

---

## Core Concepts

### Two Trading Modes

The bot dynamically switches between two modes based on market regime:

**Directional Mode** (trending markets, ADX > 25): Places bracket orders — a close order and a stop loss — around the current position. Uses trailing take-profit with peak tracking, regime-aware profit targets (tight in ranging, wide in trending), and tiered profit locks.

**Grid Mode** (ranging markets, ADX < 22): Places N symmetric limit orders above and below mid-price, spaced by ATR. Levels are weighted by signal pipeline output. Grid rebalances when price moves beyond a threshold from the grid centre. Includes overshoot detection, passive SL with TTL escalation to market close, and loss-based emergency close.

The **Transition Engine** manages mode switches when the bot has an open position — it places a close order, waits for flat, then switches modes. Timeout triggers a market reduce.

### Signal Pipeline

All quoting decisions flow through a 5-stage signal pipeline that produces `SignalWeights` — multipliers in [0.0, 2.0] for each side:

1. **RSI extreme filter** — hard blocks at overbought/oversold
2. **VWAP distance** — reduces unfavourable side when far from VWAP
3. **Regime tilt** — amplifies with-trend, dampens against-trend
4. **OFI confirmation** — order flow imbalance fine-tunes entry timing
5. **Post-stop cooldown** — blocks same-direction re-entry after a stop

### Regime Detection

Uses ADX with hysteresis to prevent rapid flipping:
- Enter TRENDING when ADX crosses above 25
- Exit TRENDING when ADX drops below 22
- Direction from +DI/-DI comparison
- Confidence score from ADX magnitude drives size scaling

### Quoting Engine

The Quoter computes fair price and spread from:
- Binance feed price (primary reference) blended with Hotstuff BBO
- Inventory-aware skew — three tiers (normal, skew, one-sided) based on position size relative to max inventory
- Adverse selection floor — dynamically adjusted by markout feedback
- OFI shift — up to 8bps mid-price adjustment based on order flow imbalance
- Trend blocking — hard blocks when inventory is against strong trend

### Risk Management

**DrawdownMonitor** — tracks daily PnL with three stress levels:
- Level 0 (< 50% of max loss): normal operation
- Level 1 (50–80%): reduces position sizes proportionally
- Level 2 (> 80%): halts all new orders; requires manual `/override` to resume

**MarkoutTracker** — measures fill quality at 10s/30s/60s/5m intervals. Feeds back into the adverse selection floor — if fills are getting picked off, the spread widens automatically. Uses per-market, per-direction ATR-normalised floors.

**Grid SL System** — three-phase inventory protection in grid mode:
- Phase 1: Overshoot detected → passive reduce order
- Phase 2: TTL expires → replace or escalate
- Phase 3: Loss exceeds threshold → immediate market close

---

## Process Architecture

### Service Management

Everything runs under a single `systemd` unit: `nenmmbot.service`. This starts `manager.py`, which:

1. Initialises the shared feed (`feed.py`) for all active markets
2. Starts the Telegram bot (`telegram_bot.py`)
3. Spawns one `subprocess.Popen` per active user bot
4. Runs a monitor loop: crash detection (auto-restart), key expiry checks, Telegram notifications

### Per-User Isolation

Each user bot runs as an independent Python process with:
- Own directory: `/root/bots/user_{telegram_id}_{label}/`
- Own `.env` file with encrypted agent key, market config, risk parameters
- Own `hotstuff.db` SQLite database for fills, snapshots, placed orders
- Own log file: `bot.log`

The bot template lives at `/root/saas/bot_template/bot/`. Changes are deployed by copying files to each user directory and restarting.

### Shared Feed

`feed.py` runs one WebSocket connection per market to Hotstuff and broadcasts BBO updates via Unix domain sockets (`/tmp/nenfeed_{MARKET}.sock`). All user bots connect as clients via `FeedReader` in `exchange.py`. This eliminates redundant WebSocket connections — instead of "n" bots × 15 markets = 15n connections, the platform uses 15 feed connections + n fills subscriptions.

Each bot's `exchange.py` has fallback logic: if the feed socket is unavailable for a market, it opens a direct WebSocket to Hotstuff for that market's BBO.

### Threading Model (Per Bot)

Each bot process runs these threads:

| Thread | Purpose | Loop |
|---|---|---|
| **Main** | Startup, backfill, initial subscriptions | Exits after setup |
| **Quote** | Core trading loop — BBO → quote → orders | 50ms poll, BBO-driven |
| **Feed readers** | One per market, reads Unix socket | Blocking read |
| **Fills WS** | Hotstuff account subscription | SDK WebSocket |
| **Watchdog** | BBO staleness + fills WS health | Periodic check |
| **Snapshot** | Account equity snapshots to DB | Every 5 minutes |
| **Indicators** | ATR, RSI, VWAP computation | Candle-driven |
| **Regime** | ADX computation + regime classification | Candle-driven |
| **OFI** | Order flow imbalance tracking | BBO-driven |
| **Markout** | Fill quality sampling at intervals | Timer-based |

The quote thread is the hot path: `on_bbo()` → `_bbo_queue` → `_quote_thread()` → `run_quote_cycle()`. It processes the latest BBO per market (deduplicating stale ticks), runs mode selection, position management, and grid/directional quoting.

---

## Data Flow

### Order Lifecycle

```
BBO update (feed/WS)
  → on_bbo() populates _bbo_queue
  → _quote_thread() pops market, calls run_quote_cycle()
  → SignalPipeline.evaluate() → SignalWeights
  → RegimeDetector → mode selection (grid/directional/transition)
  → Grid: GridManager.generate() → OrderManager places grid levels
  → Directional: Quoter.compute() → OrderManager.update() places bid/ask
  → Bracket: OrderManager.place_bracket() → close + stop orders
```

### Fill Processing

```
Hotstuff WS fills subscription
  → exchange._on_fill() logs and dispatches
  → main.on_fill():
      → OrderManager.on_fill() — clears tracked orders
      → client.refresh_positions() — updates position cache
      → MarkoutTracker.on_fill() — starts markout sampling
      → db.save_fills() — persists to SQLite
      → DrawdownMonitor.record_pnl() — updates daily loss tracking
      → GridManager.mark_filled() — tracks grid level fills
```

### Startup Sequence

```
manager.py spawns bot process
  → main.py loads config from .env
  → exchange.py connects to feed sockets + Hotstuff REST
  → Loads instrument specs (tick size, lot size, min notional)
  → cancel_all_on_startup() — clears stale orders
  → _backfill_fills() — pulls missing fills from exchange API
  → subscribe_account(on_fill) — WS fills subscription
  → Starts snapshot, indicator, regime, OFI threads
  → Waits for initial BBO data
  → Starts quote thread — begins trading
```

---

## Telegram Interface

### User Commands

| Command | Function |
|---|---|
| `/start` | Onboarding, referral link |
| `/setup` | Add first wallet (agent key + label) |
| `/addwallet` | Add additional wallet |
| `/status` | All wallets with inline action buttons |
| `/dash [label]` | Full stats dashboard — PnL, volume, win rate, drawdown |
| `/config [label]` | Profile switcher (Conservative/Balanced/Aggressive) + expert mode |
| `/stop` / `/resume` | Bot lifecycle control |
| `/override` | Resume after drawdown halt |
| `/volume` / `/pnl` / `/points` / `/balance` | Quick stat lookups |

### Admin Toolkit (26+ commands)

The admin (Telegram ID `yourtgID`) has access to platform-wide commands including: suspend/resume all bots, broadcast messages, view all user stats, force restart individual bots, modify user configs, run diagnostics, and manage referrals.

### Profile System

Three preset profiles control risk parameters:
- **Conservative** — tight spreads, small sizes, low inventory caps
- **Balanced** — moderate parameters
- **Aggressive** — wider spreads, larger sizes, higher caps

Users can also enter **Expert Mode** to override individual parameters: grid spacing, grid levels, grid V-shape, grid min spacing, overshoot multiplier, max loss %, and more.

---

## Security

### Encryption at rest

- User agent keys are encrypted with **Fernet symmetric encryption** (`vault.py`) before storage in `users.db`.
- The **master decryption key** lives at `/etc/nenmmbot/secrets` (mode `600`, owned by the service user). It is never committed to the repository or included in `.env` files.
- Agent keys are **never written to disk** in plaintext. The manager decrypts keys at startup and injects them directly into each bot’s process environment at spawn time via `subprocess.Popen(env=...)`. The per-bot `.env` files contain market config and risk parameters only — no credentials.

### Isolation

- Each user bot runs as an **independent process** under a dedicated directory (`/root/bots/user_{tid}_{label}/`). One bot cannot read another’s keys, database, or log files.
- The bot template (`bot_template/bot/`) is a **read-only reference**. User bots receive copies at provision time and operate independently thereafter.
- Per-bot `.env` files and databases are `chmod 600`.

### Host hardening

- `fail2ban` is active on SSH.
- The `/root` tree is mode `700` by default — unprivileged users cannot traverse into bot directories.

### Known limitations

Root-equivalent access on the VPS (container breakout, supply-chain compromise of the bot code, or any process running as the service user) can:

1. Read `/etc/nenmmbot/secrets` and `users.db`, then decrypt every wallet from the vault.
2. Read `/proc/<pid>/environ` for any running bot process and extract the plaintext agent key directly, bypassing the vault entirely.

The platform does not currently use a hardware security module, a cloud KMS, or per-decryption audit logging. Defence in depth is limited — a single compromised process with the right privileges can access all user keys.

### Future hardening

For multi-host deployments or higher-value accounts, a **cloud KMS** (AWS Secrets Manager, GCP Secret Manager, or HashiCorp Vault) would provide:

- No master key file on disk.
- Built-in audit logging of every secret access.
- IAM-based fine-grained access — bot process A cannot read bot B’s key.
- Automatic key rotation.

The current design prioritises deployment simplicity and portability.

---

## File Layout

```
/root/saas/                          # SaaS platform layer
├── manager.py                       # BotManager — process lifecycle
├── telegram_bot.py                  # Telegram interface (2,125 lines)
├── admin_commands.py                # Admin toolkit (1,553 lines)
├── feed.py                          # Shared market data feed
├── db.py                            # Platform DB (users, wallets, referrals)
├── vault.py                         # Fernet key encryption
├── analytics_helpers.py             # Shared analytics utilities
├── add_market.py                    # Reusable market addition script
├── .env                             # Platform config + Telegram token
├── bot_template/bot/                # Bot source template
│   ├── main.py          (929 L)     # Event loop, quote thread, mode switching
│   ├── exchange.py      (447 L)     # HotstuffClient, FeedReader, WS/REST
│   ├── orders.py        (573 L)     # OrderManager, brackets, grid placement
│   ├── quoting.py       (277 L)     # Quoter — fair price, spread, skew
│   ├── grid.py          (228 L)     # GridManager — adaptive grid levels
│   ├── regime.py        (238 L)     # RegimeDetector — ADX hysteresis
│   ├── signals.py       (232 L)     # SignalPipeline — 5-stage filter
│   ├── indicators.py    (229 L)     # IndicatorEngine — ATR, RSI, VWAP
│   ├── markout.py       (328 L)     # MarkoutTracker + MarkoutAdjuster
│   ├── drawdown.py      (147 L)     # DrawdownMonitor — daily loss cap
│   ├── ofi.py           (220 L)     # Order flow imbalance sidecar
│   ├── db.py            (386 L)     # Per-bot SQLite (fills, snapshots)
│   ├── dashboard.py     (960 L)     # FastAPI dashboard
│   ├── chart.py         (479 L)     # Chart generation
│   ├── analytics.py     (266 L)     # Analytics queries
│   ├── tracker.py       (362 L)     # PnL tracker, CSV export
│   ├── config.py         (98 L)     # BotConfig from .env
│   ├── pricing.py        (83 L)     # Price utilities
│   ├── monitor.py       (279 L)     # Process monitoring
│   └── logger.py         (14 L)     # Logging setup

/root/bots/                          # User bot instances
├── user_{tid}_{label}/              # One per user wallet
│   ├── bot/                         # Copied from bot_template
│   │   ├── main.py, exchange.py ... # All bot modules
│   │   └── hotstuff.db              # Per-user SQLite
│   ├── .env                         # User-specific config
│   └── bot.log                      # Bot log

/root/python-sdk/                    # Hotstuff Python SDK
├── hotstuff/
│   ├── apis/info.py                 # InfoClient — REST queries
│   ├── apis/exchange.py             # ExchangeClient — order submission
│   ├── apis/subscription.py         # SubscriptionClient — WS subscriptions
│   └── transports/
│       ├── http.py                  # HTTP transport (3s timeout)
│       └── websocket.py             # WS transport (5 reconnect attempts)

/tmp/nenfeed_*.sock                  # Shared feed Unix sockets
```

---

## Deployment

### Adding a Market

```bash
python3 /root/saas/add_market.py NEWMARKET-PERP
```

This updates the feed config, provisions the market in all active user bots, and restarts.

### Deploying Code Changes

```bash
# 1. Edit the template
vim /root/saas/bot_template/bot/main.py

# 2. Syntax check
python3 -m py_compile /root/saas/bot_template/bot/main.py

# 3. Copy to all user bots
for d in /root/bots/user_*/bot/; do
    cp /root/saas/bot_template/bot/main.py "$d/main.py"
done

# 4. Restart
systemctl restart nenmmbot
```

### Monitoring

```bash
# Service status
systemctl status nenmmbot

# Bot logs (user-specific)
tail -f /root/bots/user_{tid}_{label}/bot.log

# Feed status
ls -la /tmp/nenfeed_*.sock

# All bot processes
ps aux | grep 'main.py'
```

---

## Known Patterns & Bugs 

**Fills WS can die silently.** The SDK's WebSocket transport gives up after 5 reconnect attempts. The exchange.py watchdog now checks `is_connected()` every cycle and resubscribes independently of BBO health. Without this, fills stop flowing and the dashboard shows stale volume until the next restart's backfill.

**Grid mode `continue` traps.** The grid SL block in `run_quote_cycle()` has multiple `continue` statements across its branches. Any new branch added must be carefully checked to ensure it doesn't skip `run_grid_cycle()` at the bottom of the block. The "No SL condition" path must fall through, not continue (i spent 2 days debugging this issue because i did not want to read through my code line by line).

**Backfill is a safety net, not a primary source.** `_backfill_fills()` runs on startup and catches fills the live WS missed. It paginates through the exchange API and uses `INSERT OR IGNORE` to avoid duplicates. It should insert 0 fills on a healthy restart — if it's consistently backfilling, the fills WS subscription is broken( i used a lazy man's approach to filter nenbot fills from user manual fills).

**Feed readers reconnect independently.** Each `FeedReader` manages its own Unix socket reconnection. If `nenfeed.service` restarts, all bots reconnect automatically within seconds.

**Position staleness guard.** Before any position-dependent decision, the bot checks `positions_are_stale(threshold_secs=60)`. If stale, it forces a `refresh_positions()` REST call. If still stale after refresh, it skips the cycle.

**Unicode in sed.** Comments in `main.py` use em-dashes (UTF-8 `e2 80 94`). Shell `sed` commands that try to match these will fail silently. Use Python for patching when comments contain non-ASCII characters.

*For the love of God, use html parsing for your telegram bot, escaping in Markdown is a terribly stressful thing to do.Dont be retarded like me. 
---


### Areas for Improvement

**Threading model.** A single quote thread processing all markets sequentially means one slow cycle delays every other market. At 15 markets with grid mode active on several, tail latency on the last market in the queue matters. An asyncio event loop or per-market thread pool would provide better isolation. This hasn't been a problem yet but will become one as the platform scales.

**Asymmetric data path hardening.** The BBO feed has custom reconnect logic in `FeedReader`. The HTTP transport has 3-second timeouts. But the fills WebSocket — the most critical data stream for correctness — was dependent on the SDK's 5-attempt reconnect limit and a watchdog check gated behind an unrelated BBO staleness condition. Each critical data dependency should have an independent health check with explicit recovery, sized to that path's importance.

**Backfill masking reliability issues.** The startup backfill is a good safety net, but it masked a broken fills subscription for weeks. Hundreds of fills were being backfilled per restart and the system appeared healthy — dashboards showed data, volume accumulated — but it was always one restart behind. A health metric like "fills inserted by backfill vs live callback" would surface WS death immediately. The general principle: safety nets should raise alerts when they catch something, not silently compensate.

**`main.py` complexity.** At 929 lines with the grid SL block, transition engine, directional trailing TP, and mode switching all inline in `run_quote_cycle()`, the control flow is hard to audit visually. The grid stall bug hid in plain sight because the relationship between the outer `if` guard and `run_grid_cycle()` wasn't obvious across 70+ lines of branching. Extracting position management, grid SL logic, and directional TP into separate modules would make each code path independently reviewable.

**Observability gaps.** The bot logs operational events well, but lacks structured metrics for things like: fills received live vs backfilled, WS reconnection frequency, quote-to-fill latency distribution, and watchdog trigger rates.

**Feed broadcast model is single-threaded and unbounded.** `feed.py` broadcasts BBO to all connected clients sequentially inside `_broadcast()`, calling `sendall()` on each socket one by one. There is no per-client send timeout — one slow consumer with a full receive buffer can stall the entire market's broadcast until the buffer drains. Every other bot on that market stops receiving updates. Client count is uncapped (the `MAX_CLIENTS = 200` constant only sets the listen backlog, not an active-connection limit). A single misbehaving bot process or a mass restart after deploy could degrade every market it's subscribed to. At scale, options worth considering: per-client send timeouts with non-blocking send + client drop, dedicated writer threads per client, or switching to UDP multicast to decouple publisher throughput from consumer health.

---

