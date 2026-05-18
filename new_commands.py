
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

