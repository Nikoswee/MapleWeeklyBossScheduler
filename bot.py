"""
MapleStory Guild Boss Scheduler — Telegram Bot
Full inline-button flow, PostgreSQL backend, Railway-ready.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import calendar

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", None)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────

(
    SELECT_BOSS,
    SELECT_DIFF,
    SELECT_MEMBERS,
    SELECT_DATE,
    SELECT_HOUR,
    SELECT_MINUTE,
    SELECT_REMINDER,
    CONFIRM_RUN,
) = range(8)

# Reminder options
REMINDER_OPTIONS = {
    "r60": (60, "1 hour before"),
    "r30": (30, "30 mins before"),
    "r15": (15, "15 mins before"),
    "r0":  (0,  "No reminder"),
}

# ── Helpers ───────────────────────────────────────────────────────────────────

DIFF_EMOJI = {"easy": "🟢", "normal": "🔵", "hard": "🟠", "chaos": "🔴", "extreme": "⚫"}

def diff_icon(diff):
    return DIFF_EMOJI.get(diff.lower(), "⚪")

def esc(text):
    """Escape Markdown special characters in user-provided text."""
    if not text:
        return ""
    for ch in ["_", "*", "[", "]", "`"]:
        text = str(text).replace(ch, f"\\{ch}")
    return text

def get_run_dt(run):
    run_dt = run["run_at"]
    if isinstance(run_dt, str):
        run_dt = datetime.fromisoformat(run_dt)
    if run_dt.tzinfo is None:
        run_dt = run_dt.replace(tzinfo=timezone.utc)
    return run_dt

def fmt_run(run, members=None):
    icon     = diff_icon(run["difficulty"])
    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    lines = [
        f"⚔️ *Run #{run['id']}* — {icon} {esc(run['boss_name'])} {esc(run['difficulty'])}",
        f"📅 {time_str}",
        f"👑 Leader: @{esc(run['leader_username'])}",
        f"📋 Status: *{run['status'].upper()}*",
    ]
    if members:
        lines.append("\n👥 *Party:*")
        for m in members:
            status = {1: "✅", -1: "❌", 0: "⏳"}[m["accepted"]]
            line   = f"  {status} *{esc(m['ign'])}*"
            if m["class"]:    line += f" — {esc(m['class'])}"
            if m["level"]:    line += f" Lv.{m['level']}"
            if m["username"]: line += f" (@{esc(m['username'])})"
            lines.append(line)
    return "\n".join(lines)

def get_reminder_str(reminder_mins):
    _, s = REMINDER_OPTIONS.get(
        next((k for k, (v, _) in REMINDER_OPTIONS.items() if v == reminder_mins), "r0"),
        (0, "No reminder")
    )
    return s

# ── Creator ownership check ───────────────────────────────────────────────────

async def _check_creator(query, ctx) -> bool:
    if not ctx.user_data.get("creator_id"):
        await query.answer(
            "⚠️ Session expired. Please use /createrun to start again.",
            show_alert=True
        )
        return False
    if query.from_user.id != ctx.user_data.get("creator_id"):
        await query.answer(
            "⚠️ Only the run creator can use these buttons.",
            show_alert=True
        )
        return False
    return True

# ── Keyboards ─────────────────────────────────────────────────────────────────

def build_calendar(year, month):
    now      = datetime.now(timezone(timedelta(hours=8)))
    keyboard = []
    keyboard.append([
        InlineKeyboardButton("◀", callback_data=f"cal_prev_{year}_{month}"),
        InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="cal_noop"),
        InlineKeyboardButton("▶", callback_data=f"cal_next_{year}_{month}"),
    ])
    keyboard.append([
        InlineKeyboardButton(d, callback_data="cal_noop")
        for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    ])
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_noop"))
            else:
                dt   = datetime(year, month, day, tzinfo=timezone(timedelta(hours=8)))
                past = dt.date() < now.date()
                label = f"{day}" if past else f"✦{day}"
                cb    = "cal_noop" if past else f"cal_day_{year}_{month}_{day}"
                row.append(InlineKeyboardButton(label, callback_data=cb))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    return InlineKeyboardMarkup(keyboard)

def build_hour_picker(selected=None):
    rows = []
    for i in range(0, 24, 6):
        row = []
        for h in range(i, i + 6):
            label = f"[{h:02d}]" if h == selected else f"{h:02d}"
            row.append(InlineKeyboardButton(label, callback_data=f"hr_{h}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    return InlineKeyboardMarkup(rows)

def build_minute_picker(cur=0):
    quick = [0, 15, 30, 45]
    row1  = [
        InlineKeyboardButton(f"[:{m:02d}]" if m == cur else f":{m:02d}", callback_data=f"mn_{m}")
        for m in quick
    ]
    row2 = [
        InlineKeyboardButton("−5",            callback_data=f"mn_{(cur - 5) % 60}"),
        InlineKeyboardButton(f"  :{cur:02d}  ", callback_data="mn_noop"),
        InlineKeyboardButton("+5",            callback_data=f"mn_{(cur + 5) % 60}"),
    ]
    return InlineKeyboardMarkup([
        row1, row2,
        [
            InlineKeyboardButton("✔️ Confirm time", callback_data=f"mn_done_{cur}"),
            InlineKeyboardButton("❌ Cancel",        callback_data="cx"),
        ]
    ])

def build_reminder_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏰ 1 hour before",  callback_data="r60")],
        [InlineKeyboardButton("⏰ 30 mins before", callback_data="r30")],
        [InlineKeyboardButton("⏰ 15 mins before", callback_data="r15")],
        [InlineKeyboardButton("🚫 No reminder",    callback_data="r0")],
        [InlineKeyboardButton("❌ Cancel",          callback_data="cx")],
    ])

# ── /start & /help ────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    log.info(f"/start from {update.effective_user.id} @{update.effective_user.username}")
    await update.message.reply_text(
        "🍄 *MapleStory Boss Scheduler*\n\n"
        "Schedule boss runs with your guild — all buttons, no typing!\n\n"
        "*Getting started:*\n"
        "`/register Ayumilove Bowmaster 275`\n"
        "`/bosses` — see all bosses\n"
        "`/allchars` — see guild characters\n\n"
        "*Party leaders:*\n"
        "`/createrun` — guided run creator\n\n"
        "*Members:*\n"
        "`/myruns` — your invitations\n\n"
        "Type `/help` for all commands.",
        parse_mode="Markdown"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *All Commands*\n\n"
        "*Characters*\n"
        "`/register <IGN> [Class] [Level]`\n"
        "`/chars` — your characters\n"
        "`/removechar <IGN>`\n"
        "`/allchars` — all guild characters\n\n"
        "*Bosses*\n"
        "`/bosses`\n\n"
        "*Scheduling*\n"
        "`/createrun` — create a run (all buttons)\n"
        "`/cancelrun <run_id>`\n"
        "`/myruns` — your invitations\n"
        "`/runs` — all upcoming runs\n\n"
        "📅 All times SGT (UTC+8)",
        parse_mode="Markdown"
    )

# ── Character & boss commands ─────────────────────────────────────────────────

async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text(
            "Usage: `/register <IGN> [Class] [Level]`\n"
            "Example: `/register Ayumilove Bowmaster 275`",
            parse_mode="Markdown"
        )
        return
    ign   = ctx.args[0]
    cls   = ctx.args[1] if len(ctx.args) > 1 else None
    level = int(ctx.args[2]) if len(ctx.args) > 2 and ctx.args[2].isdigit() else None
    ok    = db.add_character(update.effective_user.id, ign, cls, level)
    if ok:
        parts = [f"✅ Registered *{esc(ign)}*"]
        if cls:   parts.append(f"Class: {esc(cls)}")
        if level: parts.append(f"Level: {level}")
        await update.message.reply_text(" | ".join(parts), parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ IGN *{esc(ign)}* is already registered.", parse_mode="Markdown")

async def cmd_chars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chars = db.get_characters(update.effective_user.id)
    if not chars:
        await update.message.reply_text("No characters yet. Use `/register`.", parse_mode="Markdown")
        return
    lines = ["👤 *Your Characters*\n"]
    for ch in chars:
        line = f"• *{esc(ch['ign'])}*"
        if ch["class"]: line += f" — {esc(ch['class'])}"
        if ch["level"]: line += f" Lv.{ch['level']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_allchars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chars = db.get_all_characters()
    if not chars:
        await update.message.reply_text("No characters registered yet.")
        return
    lines = ["🌍 *All Guild Characters*\n"]
    for ch in chars:
        line = f"• *{esc(ch['ign'])}*"
        if ch["class"]:    line += f" — {esc(ch['class'])}"
        if ch["level"]:    line += f" Lv.{ch['level']}"
        if ch["username"]: line += f" (@{esc(ch['username'])})"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_removechar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/removechar <IGN>`", parse_mode="Markdown")
        return
    ok  = db.remove_character(update.effective_user.id, ctx.args[0])
    msg = f"🗑️ Removed *{esc(ctx.args[0])}*." if ok else f"⚠️ No character *{esc(ctx.args[0])}* on your account."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_bosses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    lines = ["⚔️ *Available Bosses*\n"]
    for name, diffs in grouped.items():
        icons = "  ".join(f"{diff_icon(d)} {d}" for d in diffs)
        lines.append(f"*{esc(name)}*\n  {icons}\n")
    lines.append("🟢Easy 🔵Normal 🟠Hard 🔴Chaos ⚫Extreme")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── /createrun — Step 1: Boss ─────────────────────────────────────────────────

async def createrun_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    ctx.user_data.clear()
    ctx.user_data["creator_id"] = update.effective_user.id

    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    ctx.user_data["boss_map"]  = grouped
    ctx.user_data["boss_list"] = list(grouped.keys())

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"boss_{i}")]
        for i, name in enumerate(grouped)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    await update.message.reply_text(
        "⚔️ *Create a Boss Run*\n\n*Step 1 of 6* — Which boss?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SELECT_BOSS

# ── Step 2: Difficulty ────────────────────────────────────────────────────────

async def step_select_boss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_BOSS
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    idx       = int(query.data.split("_")[1])
    boss_name = ctx.user_data["boss_list"][idx]
    ctx.user_data["boss_name"] = boss_name
    diffs     = ctx.user_data["boss_map"][boss_name]
    ctx.user_data["diff_list"] = diffs
    keyboard  = [
        [InlineKeyboardButton(f"{diff_icon(d)} {d}", callback_data=f"diff_{i}")]
        for i, d in enumerate(diffs)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    await query.edit_message_text(
        f"⚔️ *Create a Boss Run*\n\nBoss: *{esc(boss_name)}*\n\n*Step 2 of 6* — Difficulty?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SELECT_DIFF

# ── Step 3: Members ───────────────────────────────────────────────────────────

async def step_select_diff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_DIFF
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    idx = int(query.data.split("_")[1])
    ctx.user_data["difficulty"]     = ctx.user_data["diff_list"][idx]
    ctx.user_data["selected_chars"] = []
    return await _render_member_picker(query, ctx)

async def _render_member_picker(query, ctx):
    all_chars  = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected   = ctx.user_data.get("selected_chars", [])
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]

    buttons = []
    for i, ch in enumerate(all_chars):
        tick  = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]: label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"tog_{i}"))

    keyboard = []
    for i in range(0, len(buttons), 2):
        keyboard.append(buttons[i:i+2])

    keyboard.append([
        InlineKeyboardButton(f"✔️ Done ({len(selected)})", callback_data="members_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])
    await query.edit_message_text(
        f"⚔️ *Create a Boss Run*\n\n"
        f"Boss: *{esc(boss_name)} {esc(difficulty)}*\n\n"
        f"*Step 3 of 6* — Select party members:\n_(tap to toggle)_",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SELECT_MEMBERS

async def step_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_MEMBERS
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    if query.data == "members_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member first!", show_alert=True)
            return SELECT_MEMBERS
        await query.answer()
        return await _render_calendar(query, ctx)
    await query.answer()
    idx      = int(query.data.split("_")[1])
    char_id  = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected:
        selected.remove(char_id)
    else:
        selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_member_picker(query, ctx)

# ── Step 4: Date ──────────────────────────────────────────────────────────────

async def _render_calendar(query, ctx):
    now  = datetime.now(timezone(timedelta(hours=8)))
    year = ctx.user_data.get("cal_year",  now.year)
    mon  = ctx.user_data.get("cal_month", now.month)
    ctx.user_data["cal_year"]  = year
    ctx.user_data["cal_month"] = mon
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    selected   = ctx.user_data["selected_chars"]
    await query.edit_message_text(
        f"⚔️ *Create a Boss Run*\n\n"
        f"Boss: *{esc(boss_name)} {esc(difficulty)}* | Members: *{len(selected)}*\n\n"
        f"*Step 4 of 6* — Pick a date (SGT):",
        reply_markup=build_calendar(year, mon),
        parse_mode="Markdown"
    )
    return SELECT_DATE

async def step_select_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_DATE
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    if query.data == "cal_noop":
        return SELECT_DATE
    parts = query.data.split("_")
    if parts[1] in ("prev", "next"):
        year  = int(parts[2])
        month = int(parts[3])
        if parts[1] == "prev":
            month -= 1
            if month < 1:  month = 12; year -= 1
        else:
            month += 1
            if month > 12: month = 1;  year += 1
        ctx.user_data["cal_year"]  = year
        ctx.user_data["cal_month"] = month
        return await _render_calendar(query, ctx)
    if parts[1] == "day":
        ctx.user_data["run_year"]  = int(parts[2])
        ctx.user_data["run_month"] = int(parts[3])
        ctx.user_data["run_day"]   = int(parts[4])
        return await _render_hour_picker(query, ctx)
    return SELECT_DATE

# ── Step 5a: Hour ─────────────────────────────────────────────────────────────

async def _render_hour_picker(query, ctx):
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    y, mo, d   = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    await query.edit_message_text(
        f"⚔️ *Create a Boss Run*\n\n"
        f"Boss: *{esc(boss_name)} {esc(difficulty)}*\n"
        f"Date: *{d:02d}/{mo:02d}/{y}*\n\n"
        f"*Step 5 of 6* — Pick an hour (SGT, 24h):",
        reply_markup=build_hour_picker(ctx.user_data.get("run_hour")),
        parse_mode="Markdown"
    )
    return SELECT_HOUR

async def step_select_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_HOUR
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    ctx.user_data["run_hour"]   = int(query.data.split("_")[1])
    ctx.user_data["run_minute"] = ctx.user_data.get("run_minute", 0)
    return await _render_minute_picker(query, ctx)

# ── Step 5b: Minute ───────────────────────────────────────────────────────────

async def _render_minute_picker(query, ctx):
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    y, mo, d   = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour       = ctx.user_data["run_hour"]
    minute     = ctx.user_data.get("run_minute", 0)
    await query.edit_message_text(
        f"⚔️ *Create a Boss Run*\n\n"
        f"Boss: *{esc(boss_name)} {esc(difficulty)}*\n"
        f"Date: *{d:02d}/{mo:02d}/{y}* at *{hour:02d}:{minute:02d} SGT*\n\n"
        f"*Step 5 of 6* — Pick minutes:",
        reply_markup=build_minute_picker(minute),
        parse_mode="Markdown"
    )
    return SELECT_MINUTE

async def step_select_minute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_MINUTE
    if query.data == "mn_noop":
        await query.answer()
        return SELECT_MINUTE
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    parts = query.data.split("_")
    if parts[1] == "done":
        ctx.user_data["run_minute"] = int(parts[2])
        return await _render_reminder_picker(query, ctx)
    ctx.user_data["run_minute"] = int(parts[1])
    return await _render_minute_picker(query, ctx)

# ── Step 6: Reminder ──────────────────────────────────────────────────────────

async def _render_reminder_picker(query, ctx):
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    d, mo, y   = ctx.user_data["run_day"], ctx.user_data["run_month"], ctx.user_data["run_year"]
    h, m       = ctx.user_data["run_hour"], ctx.user_data["run_minute"]
    await query.edit_message_text(
        f"⚔️ *Create a Boss Run*\n\n"
        f"Boss: *{esc(boss_name)} {esc(difficulty)}*\n"
        f"Date: *{d:02d}/{mo:02d}/{y} {h:02d}:{m:02d} SGT*\n\n"
        f"*Step 6 of 6* — Set a reminder for all participants?",
        reply_markup=build_reminder_keyboard(),
        parse_mode="Markdown"
    )
    return SELECT_REMINDER

async def step_select_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_REMINDER
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END
    if query.data not in REMINDER_OPTIONS:
        return await _render_reminder_picker(query, ctx)
    minutes, _ = REMINDER_OPTIONS[query.data]
    ctx.user_data["reminder_minutes"] = minutes
    return await _render_confirmation(query, ctx)

# ── Confirmation ──────────────────────────────────────────────────────────────

async def _render_confirmation(query, ctx):
    boss_name     = ctx.user_data["boss_name"]
    difficulty    = ctx.user_data["difficulty"]
    selected      = ctx.user_data["selected_chars"]
    y, mo, d      = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour, minute  = ctx.user_data["run_hour"], ctx.user_data["run_minute"]
    reminder_mins = ctx.user_data.get("reminder_minutes", 0)

    sgt_dt = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    if sgt_dt <= datetime.now(timezone(timedelta(hours=8))):
        await query.edit_message_text(
            "⚠️ That date/time is in the past. Use `/createrun` to start over.",
            parse_mode="Markdown"
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    ctx.user_data["run_at_iso"] = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    chars        = [db.get_character_by_id(cid) for cid in selected]
    member_names = ", ".join(esc(ch["ign"]) for ch in chars if ch)
    reminder_str = get_reminder_str(reminder_mins)

    await query.edit_message_text(
        f"📋 *Run Summary — Please confirm:*\n\n"
        f"⚔️ {diff_icon(difficulty)} *{esc(boss_name)} {esc(difficulty)}*\n"
        f"📅 {d:02d}/{mo:02d}/{y} {hour:02d}:{minute:02d} SGT\n"
        f"⏰ Reminder: *{reminder_str}*\n\n"
        f"👥 *Party ({len(chars)}):* {member_names}\n\n"
        f"Tap *Confirm* to create and notify all members.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm & Notify", callback_data="run_confirm"),
            InlineKeyboardButton("❌ Cancel",            callback_data="cx"),
        ]]),
        parse_mode="Markdown"
    )
    return CONFIRM_RUN

async def step_confirm_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return CONFIRM_RUN
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    boss_name     = ctx.user_data["boss_name"]
    difficulty    = ctx.user_data["difficulty"]
    run_at_iso    = ctx.user_data["run_at_iso"]
    selected      = ctx.user_data["selected_chars"]
    reminder_mins = ctx.user_data.get("reminder_minutes", 0)
    y, mo, d      = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour, minute  = ctx.user_data["run_hour"], ctx.user_data["run_minute"]

    boss   = db.find_boss(boss_name, difficulty)
    run_id = db.create_run(boss["id"], update.effective_user.id, run_at_iso)
    for char_id in selected:
        db.add_run_member(run_id, char_id)

    if reminder_mins > 0:
        sgt_dt    = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(hours=8)))
        remind_dt = sgt_dt - timedelta(minutes=reminder_mins)
        if remind_dt > datetime.now(timezone.utc):
            db.set_run_reminder(
                run_id,
                remind_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )

    members      = db.get_run_members(run_id)
    leader       = update.effective_user.username or str(update.effective_user.id)
    time_str     = f"{d:02d}/{mo:02d}/{y} {hour:02d}:{minute:02d} SGT"
    reminder_str = get_reminder_str(reminder_mins)

    invite_text = (
        f"📨 *You've been invited to a boss run!*\n\n"
        f"⚔️ {diff_icon(difficulty)} *{esc(boss_name)} {esc(difficulty)}*\n"
        f"📅 {time_str}\n"
        f"⏰ Reminder: {reminder_str}\n"
        f"👑 Leader: @{esc(leader)}\n\n"
        f"👥 *Party:*\n"
        + "\n".join(
            f"  ⏳ *{esc(m['ign'])}*" + (f" (@{esc(m['username'])})" if m["username"] else "")
            for m in members
        )
        + "\n\nTap below to respond:"
    )
    invite_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept",  callback_data=f"rsvp_accept_{run_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"rsvp_decline_{run_id}"),
    ]])

    await query.edit_message_text(
        f"🎉 *Run #{run_id} created!* Notifying members...",
        parse_mode="Markdown"
    )

    notified, failed = [], []
    for m in members:
        try:
            log.info(f"DM attempt → {m['ign']} | telegram_id: {m['telegram_id']} | username: @{m['username']}")
            await ctx.bot.send_message(
                chat_id=m["telegram_id"],
                text=invite_text,
                reply_markup=invite_kb,
                parse_mode="Markdown"
            )
            notified.append(m["ign"])
            log.info(f"DM success → {m['ign']}")
        except Exception as e:
            log.warning(f"DM failed → {m['ign']} (id:{m['telegram_id']}): {e}")
            failed.append(m["ign"])

    if GROUP_CHAT_ID:
        tags = " ".join(
            f"@{esc(m['username'])}" if m["username"] else esc(m["ign"]) for m in members
        )
        try:
            await ctx.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=(
                    f"📢 *New Boss Run Created!*\n\n"
                    f"⚔️ {diff_icon(difficulty)} *{esc(boss_name)} {esc(difficulty)}*\n"
                    f"📅 {time_str}\n"
                    f"⏰ Reminder: {reminder_str}\n"
                    f"👑 Leader: @{esc(leader)}\n\n"
                    f"Invited: {tags}\n"
                    f"Check your DMs to accept/decline!"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Group post failed: {e}")

    summary = f"✅ Run #{run_id} created! {len(notified)} member(s) notified via DM."
    if failed:
        summary += f"\n⚠️ Couldn't DM: {', '.join(failed)} — they need to send /start to the bot first."
    await ctx.bot.send_message(
        chat_id=update.effective_chat.id,
        text=summary,
        parse_mode="Markdown"
    )
    ctx.user_data.clear()
    return ConversationHandler.END

async def createrun_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("❌ Run creation cancelled.")
    return ConversationHandler.END

# ── RSVP callbacks ────────────────────────────────────────────────────────────

async def rsvp_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    parts    = query.data.split("_")
    action   = parts[1]
    run_id   = int(parts[2])
    accepted = 1 if action == "accept" else -1

    run = db.get_run(run_id)
    if not run:
        await query.edit_message_text(f"⚠️ Run #{run_id} not found.")
        return
    if run["status"] == "cancelled":
        await query.edit_message_text(f"⚠️ Run #{run_id} has been cancelled.")
        return

    user_chars = db.get_characters(update.effective_user.id)
    matched    = None
    for ch in user_chars:
        rm = db.get_run_member_by_char(run_id, ch["id"])
        if rm:
            matched = (ch, rm)
            break

    if not matched:
        await query.answer("⚠️ You're not invited to this run.", show_alert=True)
        return

    ch, rm   = matched
    db.set_member_response(run_id, ch["id"], accepted)
    members  = db.get_run_members(run_id)
    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")

    party_lines = "\n".join(
        f"  {['❌','⏳','✅'][m['accepted']+1]} *{esc(m['ign'])}*"
        + (f" (@{esc(m['username'])})" if m["username"] else "")
        for m in members
    )

    if accepted == 1:
        all_confirmed = db.check_and_confirm_run(run_id)
        if all_confirmed:
            run = db.get_run(run_id)
            confirm_msg = (
                f"🎉 *Run #{run_id} is CONFIRMED!* All members accepted.\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} *{esc(run['boss_name'])} {esc(run['difficulty'])}*\n"
                f"📅 {time_str}\n\n"
                f"👥 *Party:*\n{party_lines}"
            )
            await query.edit_message_text(confirm_msg, parse_mode="Markdown")
            for m in members:
                if m["telegram_id"] != update.effective_user.id:
                    try:
                        await ctx.bot.send_message(
                            chat_id=m["telegram_id"], text=confirm_msg, parse_mode="Markdown"
                        )
                    except Exception as e:
                        log.warning(f"Confirm notify failed {m['ign']}: {e}")
            try:
                await ctx.bot.send_message(
                    chat_id=run["leader_id"], text=confirm_msg, parse_mode="Markdown"
                )
            except Exception as e:
                log.warning(f"Leader confirm notify failed: {e}")
            if GROUP_CHAT_ID:
                try:
                    await ctx.bot.send_message(
                        chat_id=GROUP_CHAT_ID, text=confirm_msg, parse_mode="Markdown"
                    )
                except Exception as e:
                    log.warning(f"Group confirm failed: {e}")
        else:
            pending = [m for m in members if m["accepted"] == 0]
            await query.edit_message_text(
                f"✅ *{esc(ch['ign'])}* accepted Run #{run_id}!\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} *{esc(run['boss_name'])} {esc(run['difficulty'])}*\n"
                f"📅 {time_str}\n\n"
                f"👥 *Party:*\n{party_lines}\n\n"
                f"⏳ Still waiting on: {', '.join(esc(m['ign']) for m in pending)}",
                parse_mode="Markdown"
            )
            try:
                await ctx.bot.send_message(
                    chat_id=run["leader_id"],
                    text=(
                        f"ℹ️ *{esc(ch['ign'])}* accepted Run #{run_id}.\n"
                        f"Still waiting on: {', '.join(esc(m['ign']) for m in pending)}"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                log.warning(f"Leader notify failed: {e}")
    else:
        await query.edit_message_text(
            f"❌ *{esc(ch['ign'])}* declined Run #{run_id}.\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} *{esc(run['boss_name'])} {esc(run['difficulty'])}*\n"
            f"📅 {time_str}",
            parse_mode="Markdown"
        )
        try:
            await ctx.bot.send_message(
                chat_id=run["leader_id"],
                text=(
                    f"❌ *{esc(ch['ign'])}* (@{esc(update.effective_user.username or '')}) declined Run #{run_id}.\n"
                    f"Boss: {esc(run['boss_name'])} {esc(run['difficulty'])}\n"
                    f"Use `/cancelrun {run_id}` or create a new run."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Leader decline notify failed: {e}")

# ── /cancelrun ────────────────────────────────────────────────────────────────

async def cmd_cancelrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: `/cancelrun <run_id>`", parse_mode="Markdown")
        return
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number.", parse_mode="Markdown")
        return
    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found.", parse_mode="Markdown")
        return
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run leader can cancel.", parse_mode="Markdown")
        return
    if run["status"] == "cancelled":
        await update.message.reply_text("ℹ️ Already cancelled.", parse_mode="Markdown")
        return
    db.cancel_run(run_id)
    members = db.get_run_members(run_id)
    for m in members:
        try:
            await ctx.bot.send_message(
                chat_id=m["telegram_id"],
                text=(
                    f"❌ *Run #{run_id} cancelled.*\n"
                    f"⚔️ {diff_icon(run['difficulty'])} {esc(run['boss_name'])} {esc(run['difficulty'])}\n"
                    f"Cancelled by @{esc(update.effective_user.username or '')}."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Cancel notify failed {m['ign']}: {e}")
    await update.message.reply_text(f"🗑️ Run #{run_id} cancelled and members notified.", parse_mode="Markdown")

# ── /myruns & /runs ───────────────────────────────────────────────────────────

async def cmd_myruns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runs = db.get_user_runs(update.effective_user.id)
    if not runs:
        await update.message.reply_text("You have no upcoming run invitations.")
        return
    lines = ["📅 *Your Upcoming Runs*\n"]
    for run in runs:
        lines.append(fmt_run(run, db.get_run_members(run["id"])))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def cmd_runs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runs = db.get_active_runs()
    if not runs:
        await update.message.reply_text("No upcoming runs scheduled.")
        return
    lines = ["📅 *All Upcoming Runs*\n"]
    for run in runs:
        lines.append(fmt_run(run, db.get_run_members(run["id"])))
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── /chatid & /version ────────────────────────────────────────────────────────

async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: {chat.id}\nType: {chat.type}\nTitle: {getattr(chat, 'title', 'N/A')}"
    )

async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import subprocess
    try:
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %ci"], text=True
        ).strip()
        await update.message.reply_text(f"🤖 Version: `{commit}`", parse_mode="Markdown")
    except Exception:
        build_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await update.message.reply_text(f"🤖 Started: `{build_time}`", parse_mode="Markdown")

# ── Scheduler jobs ────────────────────────────────────────────────────────────

async def send_reminders(app: Application):
    runs = db.get_runs_due_for_reminder()
    for run in runs:
        members  = db.get_run_members(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
        msg = (
            f"⏰ *Boss Run Reminder!*\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} *{esc(run['boss_name'])} {esc(run['difficulty'])}*\n"
            f"📅 Starting at *{time_str}*\n\n"
            f"👥 Party:\n"
            + "\n".join(
                f"  • *{esc(m['ign'])}*" + (f" (@{esc(m['username'])})" if m["username"] else "")
                for m in members
            )
        )
        for m in members:
            try:
                await app.bot.send_message(chat_id=m["telegram_id"], text=msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Reminder failed {m['ign']}: {e}")
        if GROUP_CHAT_ID:
            try:
                await app.bot.send_message(chat_id=GROUP_CHAT_ID, text=msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Group reminder failed: {e}")

async def auto_cancel_pending_runs(app: Application):
    expired = db.get_expired_pending_runs(hours=12)
    for run in expired:
        db.cancel_run(run["id"])
        members  = db.get_run_members(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
        pending  = [m for m in members if m["accepted"] == 0]
        msg = (
            f"⏰ *Run #{run['id']} auto-cancelled.*\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} *{esc(run['boss_name'])} {esc(run['difficulty'])}*\n"
            f"📅 {time_str}\n\n"
            f"Not everyone responded within 12 hours."
        )
        for m in members:
            try:
                await app.bot.send_message(chat_id=m["telegram_id"], text=msg, parse_mode="Markdown")
            except Exception as e:
                log.warning(f"Auto-cancel notify failed {m['ign']}: {e}")
        try:
            await app.bot.send_message(
                chat_id=run["leader_id"],
                text=(
                    f"⏰ *Run #{run['id']} auto-cancelled* — no response within 12 hours.\n"
                    f"Boss: {esc(run['boss_name'])} {esc(run['difficulty'])}\n"
                    f"No response from: {', '.join(esc(m['ign']) for m in pending)}\n\n"
                    f"Use `/createrun` to reschedule."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            log.warning(f"Leader auto-cancel notify failed: {e}")
        log.info(f"Auto-cancelled run #{run['id']} (pending >12h)")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()
    log.info("Database initialised.")

    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("❌ Set BOT_TOKEN as an environment variable.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    createrun_conv = ConversationHandler(
        entry_points=[CommandHandler("createrun", createrun_start)],
        states={
            SELECT_BOSS:    [CallbackQueryHandler(step_select_boss,     pattern=r"^boss_\d+$|^cx$")],
            SELECT_DIFF:    [CallbackQueryHandler(step_select_diff,     pattern=r"^diff_\d+$|^cx$")],
            SELECT_MEMBERS: [CallbackQueryHandler(step_toggle_member,   pattern=r"^tog_\d+$|^members_done$|^cx$")],
            SELECT_DATE:    [CallbackQueryHandler(step_select_date,     pattern=r"^cal_")],
            SELECT_HOUR:    [CallbackQueryHandler(step_select_hour,     pattern=r"^hr_\d+$|^cx$")],
            SELECT_MINUTE:  [CallbackQueryHandler(step_select_minute,   pattern=r"^mn_|^cx$")],
            SELECT_REMINDER:[CallbackQueryHandler(step_select_reminder, pattern=r"^r\d+$|^cx$")],
            CONFIRM_RUN:    [CallbackQueryHandler(step_confirm_run,     pattern=r"^run_confirm$|^cx$")],
        },
        fallbacks=[
            CommandHandler("cancel", createrun_cancel),
            CallbackQueryHandler(createrun_cancel, pattern=r"^cx$"),
        ],
        per_message=False,
        per_chat=False,
        per_user=True,
    )

    app.add_handler(createrun_conv)
    app.add_handler(CallbackQueryHandler(rsvp_callback, pattern=r"^rsvp_"))
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("register",   cmd_register))
    app.add_handler(CommandHandler("chars",      cmd_chars))
    app.add_handler(CommandHandler("allchars",   cmd_allchars))
    app.add_handler(CommandHandler("removechar", cmd_removechar))
    app.add_handler(CommandHandler("bosses",     cmd_bosses))
    app.add_handler(CommandHandler("cancelrun",  cmd_cancelrun))
    app.add_handler(CommandHandler("myruns",     cmd_myruns))
    app.add_handler(CommandHandler("runs",       cmd_runs))
    app.add_handler(CommandHandler("chatid",     cmd_chatid))
    app.add_handler(CommandHandler("version",    cmd_version))

    async def on_startup(app: Application):
        scheduler = AsyncIOScheduler(timezone="UTC")
        scheduler.add_job(
            lambda: asyncio.ensure_future(send_reminders(app)),
            CronTrigger(minute="0,15,30,45")
        )
        scheduler.add_job(
            lambda: asyncio.ensure_future(auto_cancel_pending_runs(app)),
            CronTrigger(minute=0)
        )
        scheduler.start()
        log.info("Scheduler started.")

    app.post_init = on_startup
    log.info("🍄 Bot is running.")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
