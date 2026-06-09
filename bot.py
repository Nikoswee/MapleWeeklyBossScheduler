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
    ConversationHandler, MessageHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN     = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GROUP_CHAT_ID   = os.environ.get("GROUP_CHAT_ID", None)
GROUP_THREAD_ID = int(os.environ.get("GROUP_THREAD_ID", 0)) or None

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────

(
    SELECT_BOSS,
    SELECT_DIFF,
    SELECT_METHOD,
    SELECT_TEAM,
    SELECT_MEMBERS,
    SELECT_DATE,
    SELECT_HOUR,
    SELECT_MINUTE,
    SELECT_REMINDER,
    CONFIRM_RUN,
) = range(10)

(
    EDIT_CHOOSE,
    EDIT_DATE,
    EDIT_HOUR,
    EDIT_MINUTE,
    EDIT_MEMBERS,
) = range(10, 15)

(
    TEAM_NAME,
    TEAM_MEMBERS,
    TEAM_CONFIRM,
    ETEAM_CHOOSE,
    ETEAM_NAME,
    ETEAM_MEMBERS,
) = range(15, 21)

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
        f"⚔️ #{run['id']} · {run['boss_name']} {run['difficulty']} {icon}",
        f"📅 {time_str}",
        f"👑 @{run['leader_username']}",
    ]
    if members:
        total    = len(members)
        accepted = sum(1 for m in members if m["accepted"] == 1)
        waiting  = [m for m in members if m["accepted"] != 1]
        party    = f"👥 {accepted}/{total} accepted"
        if waiting:
            names = ", ".join(
                m["ign"] + (f" (@{m['username']})" if m["username"] else "")
                for m in waiting
            )
            label = "Pending/Declined" if any(m["accepted"] == -1 for m in waiting) else "Pending"
            party += f" · {label}: {names}"
        lines.append(party)
    return "\n".join(lines)

def fmt_runs_grouped(runs):
    RUN_DIVIDER     = "- - - - - - - - - - - - - -"
    SECTION_DIVIDER = "──────────────"
    pending   = [r for r in runs if r["status"] == "pending"]
    confirmed = [r for r in runs if r["status"] == "confirmed"]
    lines     = ["📅 UPCOMING RUNS"]
    if confirmed:
        lines.append("")
        lines.append("✅ CONFIRMED")
        lines.append(SECTION_DIVIDER)
        for i, run in enumerate(confirmed):
            lines.append(fmt_run(run, db.get_run_members(run["id"])))
            if i < len(confirmed) - 1:
                lines.append(RUN_DIVIDER)
    if pending:
        lines.append("")
        lines.append("⏳ PENDING")
        lines.append(SECTION_DIVIDER)
        for i, run in enumerate(pending):
            lines.append(fmt_run(run, db.get_run_members(run["id"])))
            if i < len(pending) - 1:
                lines.append(RUN_DIVIDER)
    return "\n".join(lines)

def get_reminder_str(reminder_mins):
    _, s = REMINDER_OPTIONS.get(
        next((k for k, (v, _) in REMINDER_OPTIONS.items() if v == reminder_mins), "r0"),
        (0, "No reminder")
    )
    return s

def fmt_party_lines(members):
    return "\n".join(
        f"  {['❌','⏳','✅'][m['accepted']+1]} {m['ign']}"
        + (f" (@{m['username']})" if m["username"] else "")
        for m in members
    )

# ── Creator / editor checks ───────────────────────────────────────────────────

async def _check_creator(query, ctx) -> bool:
    if not ctx.user_data.get("creator_id"):
        await query.answer("⚠️ Session expired. Use /createrun to start again.", show_alert=True)
        return False
    if query.from_user.id != ctx.user_data.get("creator_id"):
        await query.answer("⚠️ Only the run creator can use these buttons.", show_alert=True)
        return False
    return True

async def _check_editor(query, ctx) -> bool:
    if not ctx.user_data.get("editor_id"):
        await query.answer("⚠️ Session expired. Use /editrun to start again.", show_alert=True)
        return False
    if query.from_user.id != ctx.user_data.get("editor_id"):
        await query.answer("⚠️ Only the run creator can edit this run.", show_alert=True)
        return False
    return True

# ── Keyboards ─────────────────────────────────────────────────────────────────

def build_calendar(year, month):
    now      = datetime.now(timezone(timedelta(hours=8)))
    keyboard = []

    # Only show ◀ if there are future months to go back to (not before current month)
    can_prev = (year, month) > (now.year, now.month)
    keyboard.append([
        InlineKeyboardButton("◀" if can_prev else " ", callback_data=f"cal_prev_{year}_{month}" if can_prev else "cal_noop"),
        InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="cal_noop"),
        InlineKeyboardButton("▶", callback_data=f"cal_next_{year}_{month}"),
    ])
    keyboard.append([
        InlineKeyboardButton(d, callback_data="cal_noop")
        for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    ])
    for week in calendar.monthcalendar(year, month):
        row = []
        has_valid = False
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_noop"))
            else:
                dt      = datetime(year, month, day, tzinfo=timezone(timedelta(hours=8)))
                past    = dt.date() < now.date()
                too_far = dt.date() > (now + timedelta(weeks=4)).date()
                if past or too_far:
                    row.append(InlineKeyboardButton(" ", callback_data="cal_noop"))
                else:
                    has_valid = True
                    row.append(InlineKeyboardButton(str(day), callback_data=f"cal_day_{year}_{month}_{day}"))
        if has_valid or any(d != 0 for d in week):
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    return InlineKeyboardMarkup(keyboard)

COMMON_HOURS = [20, 21, 22, 23]  # 8pm, 9pm, 10pm, 11pm SGT

def build_hour_picker(selected=None):
    # Quick presets row first
    presets = []
    for h in COMMON_HOURS:
        label = f"★{h:02d}:00" if h == selected else f"{h:02d}:00"
        presets.append(InlineKeyboardButton(label, callback_data=f"hr_{h}"))
    rows = [presets]
    # Divider label
    rows.append([InlineKeyboardButton("── Other times ──", callback_data="cal_noop")])
    # Full grid
    for i in range(0, 24, 6):
        row = []
        for h in range(i, i + 6):
            if h in COMMON_HOURS: continue  # already shown above
            label = f"[{h:02d}]" if h == selected else f"{h:02d}"
            row.append(InlineKeyboardButton(label, callback_data=f"hr_{h}"))
        if row: rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    return InlineKeyboardMarkup(rows)

def build_minute_picker(cur=0):
    quick = [0, 15, 30, 45]
    row1  = [
        InlineKeyboardButton(f"[:{m:02d}]" if m == cur else f":{m:02d}", callback_data=f"mn_{m}")
        for m in quick
    ]
    row2 = [
        InlineKeyboardButton("−5",             callback_data=f"mn_{(cur - 5) % 60}"),
        InlineKeyboardButton(f"  :{cur:02d}  ", callback_data="mn_noop"),
        InlineKeyboardButton("+5",             callback_data=f"mn_{(cur + 5) % 60}"),
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
        "🍄 MapleStory Boss Scheduler\n\n"
        "Schedule boss runs with your guild — all buttons!\n\n"
        "Getting started:\n"
        "/register <IGN> [Class] [Level]\n"
        "/bosses — see all bosses\n"
        "/allchars — see guild characters\n\n"
        "Party leaders:\n"
        "/createrun — guided run creator\n"
        "/editrun <run_id> — edit a run\n"
        "/resendrun <run_id> — resend invites\n\n"
        "Members:\n"
        "/myruns — your invitations\n\n"
        "Type /help for all commands."
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 All Commands\n\n"
        "Characters:\n"
        "/register <IGN> [Class] [Level]\n"
        "/chars — your characters\n"
        "/removechar <IGN>\n"
        "/allchars — all guild characters\n\n"
        "Bosses:\n"
        "/bosses\n\n"
        "Preset Teams:\n"
        "/createteam <name> — create a preset team\n"
        "/teams — list all preset teams\n"
        "/editteam <name> — rename or change members\n"
        "/deleteteam <name> — delete a team\n\n"
        "Scheduling:\n"
        "/createrun — create a run (tap Load from team to use preset)\n"
        "/editrun <run_id> — edit date/time or members\n"
        "/cancelrun <run_id>\n"
        "/resendrun <run_id> — resend invites to pending\n"
        "/myruns — your invitations\n"
        "/runs — all upcoming runs\n\n"
        "Discord:\n"
        "/linkdiscord — generate a code to link Discord account\n"
        "/linkstatus — check link status\n\n"
        "All times SGT (UTC+8)"
    )

# ── Character & boss commands ─────────────────────────────────────────────────

async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /register <IGN> [Class] [Level]\nExample: /register Ayumilove Bowmaster 275")
        return
    ign   = ctx.args[0]
    cls   = ctx.args[1] if len(ctx.args) > 1 else None
    level = int(ctx.args[2]) if len(ctx.args) > 2 and ctx.args[2].isdigit() else None
    ok    = db.add_character(update.effective_user.id, ign, cls, level)
    if ok:
        parts = [f"✅ Registered {ign}"]
        if cls:   parts.append(f"Class: {cls}")
        if level: parts.append(f"Level: {level}")
        await update.message.reply_text(" | ".join(parts))
    else:
        await update.message.reply_text(f"⚠️ IGN {ign} is already registered.")

async def cmd_chars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chars = db.get_characters(update.effective_user.id)
    if not chars:
        await update.message.reply_text("No characters yet. Use /register.")
        return
    lines = ["👤 Your Characters\n"]
    for ch in chars:
        line = f"• {ch['ign']}"
        if ch["class"]: line += f" — {ch['class']}"
        if ch["level"]: line += f" Lv.{ch['level']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))

async def cmd_allchars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chars = db.get_all_characters()
    if not chars:
        await update.message.reply_text("No characters registered yet.")
        return
    lines = ["🌍 All Guild Characters\n"]
    for ch in chars:
        line = f"• {ch['ign']}"
        if ch["class"]:    line += f" — {ch['class']}"
        if ch["level"]:    line += f" Lv.{ch['level']}"
        if ch["username"]: line += f" (@{ch['username']})"
        lines.append(line)
    await update.message.reply_text("\n".join(lines))

async def cmd_removechar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /removechar <IGN>")
        return
    ok  = db.remove_character(update.effective_user.id, ctx.args[0])
    msg = f"🗑️ Removed {ctx.args[0]}." if ok else f"⚠️ No character {ctx.args[0]} on your account."
    await update.message.reply_text(msg)

async def cmd_bosses(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    lines = ["⚔️ Available Bosses\n"]
    for name, diffs in grouped.items():
        icons = "  ".join(f"{diff_icon(d)} {d}" for d in diffs)
        lines.append(f"{name}\n  {icons}\n")
    lines.append("🟢Easy 🔵Normal 🟠Hard 🔴Chaos ⚫Extreme")
    await update.message.reply_text("\n".join(lines))

# ── /createrun ────────────────────────────────────────────────────────────────

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
    # Show recent bosses at top as quick picks
    recent   = db.get_recent_bosses(3)
    keyboard = []
    if recent:
        keyboard.append([InlineKeyboardButton("⭐ Recent", callback_data="cal_noop")])
        for r in recent:
            if r["name"] in grouped and r["difficulty"] in grouped[r["name"]]:
                b_idx = ctx.user_data["boss_list"].index(r["name"])
                d_idx = grouped[r["name"]].index(r["difficulty"])
                keyboard.append([InlineKeyboardButton(
                    f"⭐ {r['name']} {r['difficulty']}",
                    callback_data=f"boss_diff_{b_idx}_{d_idx}"
                )])
        keyboard.append([InlineKeyboardButton("── All Bosses ──", callback_data="cal_noop")])
    for i, name in enumerate(grouped):
        keyboard.append([InlineKeyboardButton(name, callback_data=f"boss_{i}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    await update.message.reply_text(
        "⚔️ Create a Boss Run\n\nStep 1 — Which boss?\n⭐ = recently scheduled",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_BOSS

async def step_select_boss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_BOSS
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    # Handle quick-pick boss+difficulty shortcut
    if query.data.startswith("boss_diff_"):
        parts     = query.data.split("_")
        boss_idx  = int(parts[2])
        diff_idx  = int(parts[3])
        boss_name = ctx.user_data["boss_list"][boss_idx]
        ctx.user_data["boss_name"]      = boss_name
        ctx.user_data["difficulty"]     = ctx.user_data["boss_map"][boss_name][diff_idx]
        ctx.user_data["selected_chars"] = []
        return await _render_method_picker(query, ctx)

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
        f"⚔️ Create a Boss Run\n\nBoss: {boss_name}\n\nStep 2 of 6 — Difficulty?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_DIFF

async def step_select_diff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_DIFF
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    idx = int(query.data.split("_")[1])
    ctx.user_data["difficulty"]     = ctx.user_data["diff_list"][idx]
    ctx.user_data["selected_chars"] = []
    return await _render_method_picker(query, ctx)

async def _render_method_picker(query, ctx):
    """Step 3a — choose between team or individual."""
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    teams      = db.get_all_teams()
    keyboard   = []
    if teams:
        keyboard.append([InlineKeyboardButton("👥 Load from Team", callback_data="method_team")])
    keyboard.append([InlineKeyboardButton("👤 Select Individually", callback_data="method_individual")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    await query.edit_message_text(
        f"⚔️ Create a Boss Run\n\nBoss: {boss_name} {difficulty}\n\n"
        f"Step 3 of 6 — How would you like to add members?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_METHOD

async def _render_team_picker_run(query, ctx):
    """Step 3b-A — pick a preset team."""
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    teams      = db.get_all_teams()

    # Build message with team details
    lines = [f"⚔️ Create a Boss Run\n\nBoss: {boss_name} {difficulty}\n\nPick a preset team:\n"]
    for t in teams:
        members = db.get_team_members(t["id"])
        names   = " · ".join(m["ign"] for m in members)
        lines.append(f"📋 {t['name']} ({len(members)} members)")
        lines.append(f"    {names}\n")

    # Team buttons
    team_btns = [InlineKeyboardButton(t["name"], callback_data=f"pickteam_{t['id']}") for t in teams]
    keyboard  = [team_btns[i:i+2] for i in range(0, len(team_btns), 2)]
    keyboard.append([
        InlineKeyboardButton("◀ Back", callback_data="method_back"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_TEAM

async def _render_member_picker(query, ctx):
    """Step 3b-B — individual character selection."""
    all_chars  = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected   = ctx.user_data.get("selected_chars", [])
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    buttons    = []
    for i, ch in enumerate(all_chars):
        tick  = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]: label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"tog_{i}"))
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    done_label = f"✔️ Done ({len(selected)})" if selected else "⚠️ Select at least 1"
    done_cb    = "members_done" if selected else "cal_noop"
    keyboard.append([
        InlineKeyboardButton("◀ Back",    callback_data="members_back"),
        InlineKeyboardButton(done_label,  callback_data=done_cb),
        InlineKeyboardButton("❌ Cancel",  callback_data="cx"),
    ])
    await query.edit_message_text(
        f"⚔️ Create a Boss Run\n\nBoss: {boss_name} {difficulty}\n\n"
        f"Step 3 of 6 — Select members (tap to toggle):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_MEMBERS

async def step_select_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3a — handle method choice."""
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_METHOD
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "method_team":
        return await _render_team_picker_run(query, ctx)
    if query.data == "method_individual":
        ctx.user_data["selected_chars"] = []
        return await _render_member_picker(query, ctx)
    return SELECT_METHOD

async def step_select_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3b-A — handle team selection."""
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_TEAM
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "method_back":
        return await _render_method_picker(query, ctx)
    if query.data.startswith("pickteam_"):
        team_id = int(query.data.split("_")[1])
        members = db.get_team_members(team_id)
        ctx.user_data["selected_chars"] = [m["id"] for m in members]
        return await _render_calendar(query, ctx)
    return SELECT_TEAM

async def step_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3b-B — individual member toggle."""
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_MEMBERS
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "members_back":
        await query.answer()
        return await _render_method_picker(query, ctx)
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
    if char_id in selected: selected.remove(char_id)
    else: selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_member_picker(query, ctx)

async def _render_calendar(query, ctx, edit=False):
    now  = datetime.now(timezone(timedelta(hours=8)))
    year = ctx.user_data.get("cal_year",  now.year)
    mon  = ctx.user_data.get("cal_month", now.month)
    ctx.user_data["cal_year"]  = year
    ctx.user_data["cal_month"] = mon
    boss_name  = ctx.user_data.get("boss_name", "")
    difficulty = ctx.user_data.get("difficulty", "")
    label      = "Edit Run" if edit else f"Step 4 of 6"
    await query.edit_message_text(
        f"⚔️ {label}\n\nBoss: {boss_name} {difficulty}\n\n📅 Pick a date (SGT):",
        reply_markup=build_calendar(year, mon)
    )
    return EDIT_DATE if edit else SELECT_DATE

async def step_select_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_DATE
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "cal_noop": return SELECT_DATE
    parts = query.data.split("_")
    if parts[1] in ("prev", "next"):
        year, month = int(parts[2]), int(parts[3])
        if parts[1] == "prev":
            month -= 1
            if month < 1: month = 12; year -= 1
        else:
            month += 1
            if month > 12: month = 1; year += 1
        ctx.user_data["cal_year"]  = year
        ctx.user_data["cal_month"] = month
        return await _render_calendar(query, ctx)
    if parts[1] == "day":
        ctx.user_data["run_year"]  = int(parts[2])
        ctx.user_data["run_month"] = int(parts[3])
        ctx.user_data["run_day"]   = int(parts[4])
        return await _render_hour_picker(query, ctx)
    return SELECT_DATE

async def _render_hour_picker(query, ctx, edit=False):
    boss_name  = ctx.user_data.get("boss_name", "")
    difficulty = ctx.user_data.get("difficulty", "")
    y, mo, d   = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    label      = "Edit Run" if edit else "Step 5 of 6"
    await query.edit_message_text(
        f"⚔️ {label}\n\nBoss: {boss_name} {difficulty}\nDate: {d:02d}/{mo:02d}/{y}\n\n🕐 Pick an hour (SGT, 24h):",
        reply_markup=build_hour_picker(ctx.user_data.get("run_hour"))
    )
    return EDIT_HOUR if edit else SELECT_HOUR

async def step_select_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_HOUR
    await query.answer()
    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    ctx.user_data["run_hour"]   = int(query.data.split("_")[1])
    ctx.user_data["run_minute"] = 0  # Auto :00
    # If it's a common hour preset, auto-confirm at :00 and skip to reminder
    if ctx.user_data["run_hour"] in COMMON_HOURS:
        return await _render_confirmation(query, ctx)
    # Otherwise show minute picker for custom times
    return await _render_minute_picker(query, ctx)

async def _render_minute_picker(query, ctx, edit=False):
    boss_name  = ctx.user_data.get("boss_name", "")
    difficulty = ctx.user_data.get("difficulty", "")
    y, mo, d   = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour       = ctx.user_data["run_hour"]
    minute     = ctx.user_data.get("run_minute", 0)
    label      = "Edit Run" if edit else "Step 5 of 6"
    await query.edit_message_text(
        f"⚔️ {label}\n\nBoss: {boss_name} {difficulty}\nDate: {d:02d}/{mo:02d}/{y} at {hour:02d}:{minute:02d} SGT\n\n⏱ Pick minutes:",
        reply_markup=build_minute_picker(minute)
    )
    return EDIT_MINUTE if edit else SELECT_MINUTE

async def step_select_minute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_MINUTE
    if query.data == "mn_noop":
        await query.answer(); return SELECT_MINUTE
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    parts = query.data.split("_")
    if parts[1] == "done":
        ctx.user_data["run_minute"] = int(parts[2])
        return await _render_reminder_picker(query, ctx)
    ctx.user_data["run_minute"] = int(parts[1])
    return await _render_minute_picker(query, ctx)

async def _render_reminder_picker(query, ctx):
    boss_name  = ctx.user_data.get("boss_name", "")
    difficulty = ctx.user_data.get("difficulty", "")
    d, mo, y   = ctx.user_data["run_day"], ctx.user_data["run_month"], ctx.user_data["run_year"]
    h, m       = ctx.user_data["run_hour"], ctx.user_data["run_minute"]
    await query.edit_message_text(
        f"⚔️ Create a Boss Run\n\nBoss: {boss_name} {difficulty}\n"
        f"Date: {d:02d}/{mo:02d}/{y} {h:02d}:{m:02d} SGT\n\n"
        f"Step 6 of 6 — Set a reminder for all participants?",
        reply_markup=build_reminder_keyboard()
    )
    return SELECT_REMINDER

async def step_select_reminder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return SELECT_REMINDER
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data not in REMINDER_OPTIONS:
        return await _render_reminder_picker(query, ctx)
    minutes, _ = REMINDER_OPTIONS[query.data]
    ctx.user_data["reminder_minutes"] = minutes
    return await _render_confirmation(query, ctx)

async def _render_confirmation(query, ctx):
    boss_name    = ctx.user_data["boss_name"]
    difficulty   = ctx.user_data["difficulty"]
    selected     = ctx.user_data["selected_chars"]
    y, mo, d     = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour, minute = ctx.user_data["run_hour"], ctx.user_data["run_minute"]

    sgt_dt = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    if sgt_dt <= datetime.now(timezone(timedelta(hours=8))):
        await query.edit_message_text("⚠️ That date/time is in the past. Use /createrun to start over.")
        ctx.user_data.clear(); return ConversationHandler.END

    ctx.user_data["run_at_iso"] = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Reminder at 8am SGT on the day of the run, unless run is today
    sgt_tz  = timezone(timedelta(hours=8))
    now_sgt = datetime.now(sgt_tz)
    run_8am = sgt_dt.replace(hour=8, minute=0, second=0, microsecond=0)
    if run_8am.date() > now_sgt.date():
        ctx.user_data["reminder_8am_iso"] = run_8am.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        ctx.user_data["reminder_label"]   = "8:00 AM SGT on the day of the run"
    else:
        ctx.user_data["reminder_8am_iso"] = None
        ctx.user_data["reminder_label"]   = "None (same-day run)"

    chars        = [db.get_character_by_id(cid) for cid in selected]
    platform_map = db.get_character_platform_info(selected)
    member_lines = []
    for ch in chars:
        if ch:
            plat = platform_map.get(ch["id"], "⚠️")
            member_lines.append(f"• {ch['ign']} [{plat}]")
    member_list = "\n".join(member_lines)

    await query.edit_message_text(
        f"📋 Run Summary — Please confirm:\n\n"
        f"⚔️ {diff_icon(difficulty)} {boss_name} {difficulty}\n"
        f"📅 {d:02d}/{mo:02d}/{y} {hour:02d}:{minute:02d} SGT\n"
        f"⏰ Reminder: {ctx.user_data['reminder_label']}\n\n"
        f"👥 Party ({len(chars)}):\n{member_list}\n\n"
        f"Tap Confirm to create and notify all members.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm & Notify", callback_data="run_confirm"),
            InlineKeyboardButton("❌ Cancel",            callback_data="cx"),
        ]])
    )
    return CONFIRM_RUN

async def step_confirm_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx): return CONFIRM_RUN
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
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
    reminder_8am = ctx.user_data.get("reminder_8am_iso")
    if reminder_8am:
        db.set_run_reminder(run_id, reminder_8am)
    await query.edit_message_text(f"🎉 Run #{run_id} created! Notifying members...")
    await _notify_run(ctx, run_id, boss_name, difficulty, y, mo, d, hour, minute,
                      reminder_mins, update.effective_user.username or str(update.effective_user.id),
                      update.effective_user.id, is_edit=False)
    ctx.user_data.clear()
    return ConversationHandler.END

async def createrun_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Run creation cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Run creation cancelled.")
    return ConversationHandler.END

# ── Shared notification helper ────────────────────────────────────────────────

async def _notify_run(ctx, run_id, boss_name, difficulty, y, mo, d, hour, minute,
                       reminder_mins, leader, chat_id, is_edit=False):
    members      = db.get_run_members(run_id)
    time_str     = f"{d:02d}/{mo:02d}/{y} {hour:02d}:{minute:02d} SGT"
    reminder_str = get_reminder_str(reminder_mins)
    verb         = "updated" if is_edit else "invited to a"

    invite_text = (
        f"📨 You've been {verb} boss run!\n\n"
        f"⚔️ {diff_icon(difficulty)} {boss_name} {difficulty}\n"
        f"📅 {time_str}\n"
        f"⏰ Reminder: {reminder_str}\n"
        f"👑 Leader: @{leader}\n\n"
        f"👥 Party:\n"
        + "\n".join(
            f"  ⏳ {m['ign']}" + (f" (@{m['username']})" if m["username"] else "")
            for m in members
        )
        + f"\n\nRun #{run_id} — tap below to respond:"
    )
    invite_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept",  callback_data=f"rsvp_accept_{run_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"rsvp_decline_{run_id}"),
    ]])

    notified, failed = [], []
    for m in members:
        try:
            log.info(f"DM → {m['ign']} | id:{m['telegram_id']}")
            await ctx.bot.send_message(
                chat_id=m["telegram_id"], text=invite_text, reply_markup=invite_kb
            )
            notified.append(m["ign"])
        except Exception as e:
            log.warning(f"DM failed → {m['ign']} (id:{m['telegram_id']}): {e}")
            failed.append(m["ign"])

    if GROUP_CHAT_ID:
        tags = " ".join(f"@{m['username']}" if m["username"] else m["ign"] for m in members)
        verb_g = "Updated" if is_edit else "New"
        try:
            await ctx.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                message_thread_id=GROUP_THREAD_ID,
                is_topic_message=bool(GROUP_THREAD_ID),
                text=(
                    f"📢 {verb_g} Boss Run\n\n"
                    f"⚔️ {diff_icon(difficulty)} {boss_name} {difficulty}\n"
                    f"📅 {time_str}\n"
                    f"⏰ Reminder: {reminder_str}\n"
                    f"👑 Leader: @{leader}\n\n"
                    f"Invited: {tags}\n"
                    f"Check your DMs to accept/decline!"
                )
            )
        except Exception as e:
            log.warning(f"Group post failed: {e}")

    summary = f"✅ Run #{run_id} {'updated' if is_edit else 'created'}! {len(notified)} member(s) notified via DM."
    if failed:
        summary += f"\n⚠️ Couldn't DM: {', '.join(failed)} — they need to send /start to the bot first."
    if not is_edit:
        summary += f"\n\n📝 To edit: /editrun {run_id}\n🗑️ To cancel: /cancelrun {run_id}"
    await ctx.bot.send_message(chat_id=chat_id, text=summary)
    # Notify Discord channel
    try:
        run_obj         = db.get_run(run_id)
        discord_members = db.get_run_members_discord(run_id)
        embed           = _build_discord_embed(run_obj, discord_members)
        sgt_dt          = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(hours=8)))
        time_str_dc     = sgt_dt.strftime("%d/%m/%Y %H:%M SGT")
        verb            = "Updated" if is_edit else "New"
        mentions        = " ".join(f"<@{m['discord_id']}>" for m in discord_members if m.get("discord_id"))
        reminder_str    = f"⏰ Reminder: {get_reminder_str(reminder_mins)}"
        dc_msg = (
            f"📢 **{verb} Boss Run!** {mentions}\n"
            f"⚔️ {diff_icon(difficulty)} **{boss_name} {difficulty}**\n"
            f"📅 {time_str_dc}\n{reminder_str}\n"
            f"Accept or decline in this channel:"
        )
        if is_edit:
            # Update existing Discord post if it exists
            await _update_discord_run_message(run_obj, embed)
            await _notify_discord_channel(run_obj, discord_members,
                f"✏️ **Run #{run_id} has been updated!**\n"
                f"⚔️ {diff_icon(difficulty)} **{boss_name} {difficulty}**\n"
                f"📅 {time_str_dc}\nAll responses reset — please re-accept."
            )
        else:
            # New run — post to Discord channel with RSVP buttons via Discord bot API
            await _notify_discord_channel(run_obj, discord_members, dc_msg)
    except Exception as e:
        log.warning(f"Discord notify_run failed: {e}")


# ── Teams helpers ─────────────────────────────────────────────────────────────

async def _render_team_picker(target, ctx, is_edit=False):
    """Render member picker for team create/edit. target can be query or update."""
    all_chars  = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected   = ctx.user_data.get("selected_chars", [])
    name       = ctx.user_data.get("team_name") or ctx.user_data.get("eteam_name", "")
    step       = "Edit Team" if is_edit else "Step 2 of 3"

    buttons = []
    for i, ch in enumerate(all_chars):
        tick  = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]: label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"ttog_{i}"))

    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard.append([
        InlineKeyboardButton(f"✔️ Done ({len(selected)})", callback_data="tmembers_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])
    text = (
        f"👥 {step}\n\n"
        f"Team: {name}\n\n"
        f"Select members (tap to toggle):"
    )
    markup = InlineKeyboardMarkup(keyboard)
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=markup)
    else:
        await target.message.reply_text(text, reply_markup=markup)
    return ETEAM_MEMBERS if is_edit else TEAM_MEMBERS

def _check_team_creator(query, ctx):
    return query.from_user.id == ctx.user_data.get("team_creator_id")

# ── /createteam ───────────────────────────────────────────────────────────────

async def createteam_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /createteam <team name>\n"
            "Example: /createteam Lotus Party\n"
            "Example: /createteam CKALOS 3MAN RUN"
        )
        return ConversationHandler.END
    name = " ".join(ctx.args).strip()
    if len(name) > 50:
        await update.message.reply_text("⚠️ Team name too long (max 50 chars). Try again.")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["team_creator_id"] = update.effective_user.id
    ctx.user_data["team_name"]       = name
    ctx.user_data["selected_chars"]  = []
    return await _render_team_picker(update, ctx, is_edit=False)



async def team_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can use these buttons.", show_alert=True)
        return TEAM_MEMBERS
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Team creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "tmembers_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member!", show_alert=True)
            return TEAM_MEMBERS
        await query.answer()
        # Show confirmation
        name     = ctx.user_data["team_name"]
        selected = ctx.user_data["selected_chars"]
        chars    = [db.get_character_by_id(cid) for cid in selected]
        members  = ", ".join(ch["ign"] for ch in chars if ch)
        await query.edit_message_text(
            f"📋 Team Summary\n\nTeam: {name}\nMembers ({len(chars)}): {members}\n\nConfirm?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm", callback_data="tconfirm"),
                InlineKeyboardButton("❌ Cancel",  callback_data="cx"),
            ]])
        )
        return TEAM_CONFIRM
    await query.answer()
    idx      = int(query.data.split("_")[1])
    char_id  = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected: selected.remove(char_id)
    else: selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_team_picker(query, ctx, is_edit=False)

async def team_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can confirm.", show_alert=True)
        return TEAM_CONFIRM
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Team creation cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    name     = ctx.user_data["team_name"]
    selected = ctx.user_data["selected_chars"]
    team_id, err = db.create_team(name, update.effective_user.id, selected)
    if err:
        await query.edit_message_text(f"⚠️ {err}\nUse /createteam to try again.")
    else:
        chars   = [db.get_character_by_id(cid) for cid in selected]
        members = ", ".join(ch["ign"] for ch in chars if ch)
        await query.edit_message_text(
            f"✅ Team saved!\n\nName: {name}\nMembers ({len(chars)}): {members}\n\n"
            f"Use /teams to see all teams.\n"
            f"When creating a run, tap Load from team to pre-select this team."
        )
    ctx.user_data.clear()
    return ConversationHandler.END

async def createteam_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Team creation cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Team creation cancelled.")
    return ConversationHandler.END

# ── /teams ────────────────────────────────────────────────────────────────────

async def cmd_teams(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    teams = db.get_all_teams()
    if not teams:
        await update.message.reply_text(
            "No preset teams yet. Use /createteam to create one."
        ); return
    lines = ["👥 Preset Teams\n"]
    for t in teams:
        members = db.get_team_members(t["id"])
        names   = ", ".join(m["ign"] for m in members)
        lines.append(f"• {t['name']} ({len(members)} members)")
        lines.append(f"  {names}")
        lines.append("")
    lines.append("Commands: /editteam <name> · /deleteteam <name>")
    await update.message.reply_text("\n".join(lines))

# ── /editteam ─────────────────────────────────────────────────────────────────

async def editteam_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /editteam <team name>\nExample: /editteam Lotus Party")
        return ConversationHandler.END
    name = " ".join(ctx.args)
    team = db.get_team_by_name(name)
    if not team:
        await update.message.reply_text(f"⚠️ Team not found. Use /teams to see all teams.")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["team_creator_id"] = update.effective_user.id
    ctx.user_data["edit_team_id"]    = team["id"]
    ctx.user_data["eteam_name"]      = team["name"]
    current = db.get_team_members(team["id"])
    ctx.user_data["selected_chars"]  = [m["id"] for m in current]
    await update.message.reply_text(
        f"✏️ Edit Team: {team['name']}\n\nWhat would you like to edit?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Rename",      callback_data="eteam_rename")],
            [InlineKeyboardButton("👥 Edit Members", callback_data="eteam_members")],
            [InlineKeyboardButton("❌ Cancel",       callback_data="cx")],
        ])
    )
    return ETEAM_CHOOSE

async def eteam_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can edit.", show_alert=True)
        return ETEAM_CHOOSE
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "eteam_rename":
        # Show rename instruction — name comes via /editteam <newname> so tell user to re-run
        await query.edit_message_text(
            f"✏️ To rename the team, use:\n"
            f"/editteam <new name>\n\n"
            f"This will open the edit menu for the new name.\n"
            f"Then use Edit Members to restore the members.\n\n"
            f"Or delete and recreate:\n"
            f"/deleteteam {ctx.user_data['eteam_name']}\n"
            f"/createteam <new name>"
        )
        ctx.user_data.clear()
        return ConversationHandler.END
    if query.data == "eteam_members":
        return await _render_team_picker(query, ctx, is_edit=True)
    return ETEAM_CHOOSE

async def eteam_get_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ctx.user_data.get("team_creator_id"):
        return ETEAM_NAME
    name = update.message.text.strip()
    if not name or len(name) > 50:
        await update.message.reply_text("⚠️ Invalid name. Try again (max 50 chars):")
        return ETEAM_NAME
    team_id = ctx.user_data["edit_team_id"]
    current = db.get_team_members(team_id)
    ok, err = db.update_team(team_id, name, [m["id"] for m in current])
    if ok:
        await update.message.reply_text(f"✅ Team renamed to {name}!")
    else:
        await update.message.reply_text(f"⚠️ {err}")
    ctx.user_data.clear()
    return ConversationHandler.END

async def eteam_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can edit.", show_alert=True)
        return ETEAM_MEMBERS
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "tmembers_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member!", show_alert=True)
            return ETEAM_MEMBERS
        await query.answer()
        team_id  = ctx.user_data["edit_team_id"]
        name     = ctx.user_data["eteam_name"]
        selected = ctx.user_data["selected_chars"]
        ok, err  = db.update_team(team_id, name, selected)
        if ok:
            chars   = [db.get_character_by_id(cid) for cid in selected]
            members = ", ".join(ch["ign"] for ch in chars if ch)
            await query.edit_message_text(
                f"✅ Team updated!\nName: {name}\nMembers ({len(chars)}): {members}"
            )
        else:
            await query.edit_message_text(f"⚠️ {err}")
        ctx.user_data.clear()
        return ConversationHandler.END
    await query.answer()
    idx      = int(query.data.split("_")[1])
    char_id  = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected: selected.remove(char_id)
    else: selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_team_picker(query, ctx, is_edit=True)

async def editteam_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Edit cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Edit cancelled.")
    return ConversationHandler.END

# ── /deleteteam ───────────────────────────────────────────────────────────────

async def cmd_deleteteam(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /deleteteam <team name>\nExample: /deleteteam Lotus Party")
        return
    name = " ".join(ctx.args)
    team = db.get_team_by_name(name)
    if not team:
        await update.message.reply_text(f"⚠️ Team not found. Use /teams to see all teams.")
        return
    db.delete_team(team["id"])
    await update.message.reply_text(f"🗑️ Team deleted.")

# ── /editrun ──────────────────────────────────────────────────────────────────

async def editrun_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /editrun <run_id>"); return ConversationHandler.END
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number."); return ConversationHandler.END
    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found."); return ConversationHandler.END
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run creator can edit this run."); return ConversationHandler.END
    if run["status"] == "cancelled":
        await update.message.reply_text("⚠️ This run has been cancelled."); return ConversationHandler.END
    if get_run_dt(run) <= datetime.now(timezone.utc):
        await update.message.reply_text("⚠️ This run has already passed."); return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["editor_id"]   = update.effective_user.id
    ctx.user_data["edit_run_id"] = run_id
    ctx.user_data["boss_name"]   = run["boss_name"]
    ctx.user_data["difficulty"]  = run["difficulty"]

    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")

    await update.message.reply_text(
        f"✏️ Edit Run #{run_id}\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {time_str}\n\nWhat would you like to edit?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Date & Time", callback_data="edit_datetime")],
            [InlineKeyboardButton("👥 Party Members", callback_data="edit_members")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cx")],
        ])
    )
    return EDIT_CHOOSE

async def edit_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx): return EDIT_CHOOSE
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "edit_datetime":
        now = datetime.now(timezone(timedelta(hours=8)))
        ctx.user_data["cal_year"]  = now.year
        ctx.user_data["cal_month"] = now.month
        return await _render_calendar(query, ctx, edit=True)
    if query.data == "edit_members":
        run_id  = ctx.user_data["edit_run_id"]
        current = db.get_run_members(run_id)
        ctx.user_data["selected_chars"] = [m["character_id"] for m in current]
        return await _render_edit_member_picker(query, ctx)
    return EDIT_CHOOSE

async def edit_select_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx): return EDIT_DATE
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "cal_noop": return EDIT_DATE
    parts = query.data.split("_")
    if parts[1] in ("prev", "next"):
        year, month = int(parts[2]), int(parts[3])
        if parts[1] == "prev":
            month -= 1
            if month < 1: month = 12; year -= 1
        else:
            month += 1
            if month > 12: month = 1; year += 1
        ctx.user_data["cal_year"]  = year
        ctx.user_data["cal_month"] = month
        return await _render_calendar(query, ctx, edit=True)
    if parts[1] == "day":
        ctx.user_data["run_year"]  = int(parts[2])
        ctx.user_data["run_month"] = int(parts[3])
        ctx.user_data["run_day"]   = int(parts[4])
        return await _render_hour_picker(query, ctx, edit=True)
    return EDIT_DATE

async def edit_select_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx): return EDIT_HOUR
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    ctx.user_data["run_hour"]   = int(query.data.split("_")[1])
    ctx.user_data["run_minute"] = ctx.user_data.get("run_minute", 0)
    return await _render_minute_picker(query, ctx, edit=True)

async def edit_select_minute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx): return EDIT_MINUTE
    if query.data == "mn_noop":
        await query.answer(); return EDIT_MINUTE
    await query.answer()
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    parts = query.data.split("_")
    if parts[1] == "done":
        ctx.user_data["run_minute"] = int(parts[2])
        return await _apply_datetime_edit(query, ctx)
    ctx.user_data["run_minute"] = int(parts[1])
    return await _render_minute_picker(query, ctx, edit=True)

async def _apply_datetime_edit(query, ctx):
    run_id       = ctx.user_data["edit_run_id"]
    y, mo, d     = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour, minute = ctx.user_data["run_hour"], ctx.user_data["run_minute"]
    sgt_dt = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    if sgt_dt <= datetime.now(timezone(timedelta(hours=8))):
        await query.edit_message_text("⚠️ That date/time is in the past. Use /editrun to try again.")
        ctx.user_data.clear(); return ConversationHandler.END
    run_at_iso = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.update_run_time(run_id, run_at_iso)
    run    = db.get_run(run_id)
    leader = query.from_user.username or str(query.from_user.id)
    await query.edit_message_text(
        f"✅ Run #{run_id} updated to {d:02d}/{mo:02d}/{y} {hour:02d}:{minute:02d} SGT.\n"
        f"Run reset to PENDING — members need to re-accept."
    )
    await _notify_run(ctx, run_id, run["boss_name"], run["difficulty"],
                      y, mo, d, hour, minute, 0, leader, query.from_user.id, is_edit=True)
    ctx.user_data.clear()
    return ConversationHandler.END

async def _render_edit_member_picker(query, ctx):
    all_chars  = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected   = ctx.user_data.get("selected_chars", [])
    run_id     = ctx.user_data["edit_run_id"]
    boss_name  = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    buttons    = []
    for i, ch in enumerate(all_chars):
        tick  = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]: label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"etog_{i}"))
    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard.append([
        InlineKeyboardButton(f"✔️ Done ({len(selected)})", callback_data="emembers_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])
    await query.edit_message_text(
        f"✏️ Edit Run #{run_id} — Update party members:\n"
        f"Boss: {boss_name} {difficulty}\n\n(tap to toggle)",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return EDIT_MEMBERS

async def edit_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx): return EDIT_MEMBERS
    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear(); return ConversationHandler.END
    if query.data == "emembers_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member first!", show_alert=True)
            return EDIT_MEMBERS
        await query.answer()
        return await _apply_members_edit(query, ctx)
    await query.answer()
    idx      = int(query.data.split("_")[1])
    char_id  = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected: selected.remove(char_id)
    else: selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_edit_member_picker(query, ctx)

async def _apply_members_edit(query, ctx):
    run_id   = ctx.user_data["edit_run_id"]
    selected = ctx.user_data["selected_chars"]
    run      = db.get_run(run_id)
    db.reset_run_members(run_id, selected)
    run_dt   = get_run_dt(run)
    sgt      = run_dt + timedelta(hours=8)
    leader   = query.from_user.username or str(query.from_user.id)
    await query.edit_message_text(
        f"✅ Run #{run_id} party updated ({len(selected)} members).\n"
        f"Run reset to PENDING — members need to re-accept."
    )
    await _notify_run(ctx, run_id, run["boss_name"], run["difficulty"],
                      sgt.year, sgt.month, sgt.day, sgt.hour, sgt.minute,
                      0, leader, query.from_user.id, is_edit=True)
    ctx.user_data.clear()
    return ConversationHandler.END

async def editrun_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("❌ Edit cancelled.")
    elif update.message:
        await update.message.reply_text("❌ Edit cancelled.")
    return ConversationHandler.END

# ── /resendrun ────────────────────────────────────────────────────────────────

async def cmd_resendrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /resendrun <run_id>"); return
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number."); return
    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found."); return
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run leader can resend invites."); return
    if run["status"] == "cancelled":
        await update.message.reply_text("⚠️ This run has been cancelled."); return

    members = db.get_run_members(run_id)
    pending = [m for m in members if m["accepted"] == 0]
    if not pending:
        await update.message.reply_text(f"ℹ️ No pending members for Run #{run_id} — everyone has already responded.")
        return

    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    leader   = update.effective_user.username or str(update.effective_user.id)

    invite_text = (
        f"📨 Reminder: You haven't responded to this boss run yet!\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {time_str}\n"
        f"👑 Leader: @{leader}\n\n"
        f"Please respond:"
    )
    invite_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept",  callback_data=f"rsvp_accept_{run_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"rsvp_decline_{run_id}"),
    ]])

    notified, failed = [], []
    for m in pending:
        try:
            await ctx.bot.send_message(chat_id=m["telegram_id"], text=invite_text, reply_markup=invite_kb)
            notified.append(m["ign"])
        except Exception as e:
            log.warning(f"Resend failed → {m['ign']} (id:{m['telegram_id']}): {e}")
            failed.append(m["ign"])

    summary = f"✅ Resent invite to {len(notified)} pending member(s): {', '.join(notified)}"
    if failed:
        summary += f"\n⚠️ Still couldn't DM: {', '.join(failed)}"
    await update.message.reply_text(summary)

# ── RSVP callbacks ────────────────────────────────────────────────────────────

async def rsvp_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    parts    = query.data.split("_")
    action   = parts[1]
    run_id   = int(parts[2])
    accepted = 1 if action == "accept" else -1

    log.info(f"RSVP: {update.effective_user.username} | action:{action} | run_id:{run_id}")

    run = db.get_run(run_id)
    if not run:
        await query.edit_message_text(f"⚠️ Run #{run_id} not found."); return
    if run["status"] == "cancelled":
        await query.edit_message_text(f"⚠️ Run #{run_id} has been cancelled."); return

    user_chars = db.get_characters(update.effective_user.id)
    log.info(f"RSVP: user has {len(user_chars)} characters")

    matched = None
    for ch in user_chars:
        rm = db.get_run_member_by_char(run_id, ch["id"])
        log.info(f"RSVP: checking {ch['ign']} for run {run_id} → {rm}")
        if rm:
            matched = (ch, rm); break

    if not matched:
        await query.answer("⚠️ You're not invited to this run.", show_alert=True)
        log.warning(f"RSVP: no match for {update.effective_user.username} in run {run_id}")
        return

    ch, rm = matched
    log.info(f"RSVP: matched {ch['ign']} | current accepted={rm['accepted']}")

    # Already responded — show status and remove buttons
    if rm["accepted"] != 0:
        status_map = {1: "✅ accepted", -1: "❌ declined"}
        status = status_map.get(rm["accepted"], "responded")
        await query.edit_message_text(
            f"ℹ️ You already {status} Run #{run_id}.\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n\n"
            f"If you need to change your response, contact the run leader."
        )
        return

    db.set_member_response(run_id, ch["id"], accepted)
    members  = db.get_run_members(run_id)
    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    party    = fmt_party_lines(members)

    if accepted == 1:
        all_confirmed = db.check_and_confirm_run(run_id)
        if all_confirmed:
            run = db.get_run(run_id)
            confirm_msg = (
                f"🎉 Run #{run_id} is CONFIRMED! All members accepted.\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                f"📅 {time_str}\n\n"
                f"👥 Party:\n{party}"
            )
            await query.edit_message_text(confirm_msg)
            for m in members:
                if m["telegram_id"] != update.effective_user.id:
                    try:
                        await ctx.bot.send_message(chat_id=m["telegram_id"], text=confirm_msg)
                    except Exception as e:
                        log.warning(f"Confirm notify failed {m['ign']}: {e}")
            try:
                await ctx.bot.send_message(chat_id=run["leader_id"], text=confirm_msg)
            except Exception as e:
                log.warning(f"Leader confirm notify failed: {e}")
            if GROUP_CHAT_ID:
                try:
                    await ctx.bot.send_message(chat_id=GROUP_CHAT_ID, message_thread_id=GROUP_THREAD_ID, is_topic_message=bool(GROUP_THREAD_ID), text=confirm_msg)
                except Exception as e:
                    log.warning(f"Group confirm failed: {e}")
            # Update Discord post
            try:
                embed = _build_discord_embed(run, members)
                embed["title"] = f"🎉 Run #{run_id} CONFIRMED! — {run['boss_name']} {run['difficulty']}"
                embed["footer"] = {"text": "✅ All members confirmed"}
                await _update_discord_run_message(run, embed)
                await _notify_discord_channel(run, members, f"🎉 **Run #{run_id} is CONFIRMED!** All members accepted.")
            except Exception as e:
                log.warning(f"Discord confirm update failed: {e}")
        else:
            pending = [m for m in members if m["accepted"] == 0]
            total   = len(members)
            done    = sum(1 for m in members if m["accepted"] == 1)
            await query.edit_message_text(
                f"✅ {ch['ign']} accepted Run #{run_id}! ({done}/{total})\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                f"📅 {time_str}\n\n"
                f"👥 Party:\n{party}\n\n"
                f"Still waiting on: {', '.join(m['ign'] for m in pending)}"
            )
            try:
                await ctx.bot.send_message(
                    chat_id=run["leader_id"],
                    text=(
                        f"ℹ️ {ch['ign']} accepted Run #{run_id}. ({done}/{total})\n"
                        f"Still waiting on: {', '.join(m['ign'] for m in pending)}"
                    )
                )
            except Exception as e:
                log.warning(f"Leader notify failed: {e}")
            # Update Discord post with new acceptance status
            try:
                await _update_discord_run_message(run, _build_discord_embed(run, members))
            except Exception as e:
                log.warning(f"Discord partial accept update failed: {e}")
    else:
        # Auto-cancel the run when anyone declines
        db.cancel_run(run_id)
        all_members = db.get_run_members(run_id)
        cancel_msg = (
            f"❌ Run #{run_id} has been cancelled.\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {time_str}\n\n"
            f"{ch['ign']} (@{update.effective_user.username or ''}) declined the invite."
        )
        await query.edit_message_text(cancel_msg)
        # Notify all other members
        for m in all_members:
            if m["telegram_id"] != update.effective_user.id:
                try:
                    await ctx.bot.send_message(chat_id=m["telegram_id"], text=cancel_msg)
                except Exception as e:
                    log.warning(f"Decline cancel notify failed {m['ign']}: {e}")
        # Notify leader
        try:
            await ctx.bot.send_message(chat_id=run["leader_id"], text=cancel_msg)
        except Exception as e:
            log.warning(f"Leader decline cancel notify failed: {e}")
        # Notify group
        if GROUP_CHAT_ID:
            try:
                await ctx.bot.send_message(chat_id=GROUP_CHAT_ID, message_thread_id=GROUP_THREAD_ID, is_topic_message=bool(GROUP_THREAD_ID), text=cancel_msg)
            except Exception as e:
                log.warning(f"Group decline cancel notify failed: {e}")
        log.info(f"Run #{run_id} auto-cancelled due to decline by {ch['ign']}")
        # Update Discord post
        try:
            run_data = db.get_run(run_id)
            discord_members = db.get_run_members_discord(run_id)
            embed = _build_discord_embed(run_data, discord_members)
            embed["footer"] = {"text": f"❌ Cancelled — {ch['ign']} declined"}
            await _update_discord_run_message(run_data, embed)
            sgt = get_run_dt(run_data) + timedelta(hours=8)
            cancel_notice = (
                f"❌ **Run #{run_id} has been cancelled.**\n"
                f"⚔️ {diff_icon(run_data['difficulty'])} {run_data['boss_name']} {run_data['difficulty']}\n"
                f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n"
                f"{ch['ign']} (@{update.effective_user.username or ''}) declined on Telegram."
            )
            await _notify_discord_channel(run_data, discord_members, cancel_notice)
        except Exception as e:
            log.warning(f"Discord decline update failed: {e}")


async def cmd_linkdiscord(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    code = db.create_link_code(update.effective_user.id)
    await update.message.reply_text(
        f"🔗 Link your Discord account\n\n"
        f"Your one-time code: {code}\n\n"
        f"On Discord, type:\n"
        f"/linkaccount {code}\n\n"
        f"⚠️ This code expires in 10 minutes.\n"
        f"Once linked, your characters will be accessible on both platforms."
    )

async def cmd_linkstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    linked = db.get_link_status(update.effective_user.id)
    if linked:
        await update.message.reply_text(
            f"✅ Your Telegram is linked to Discord account @{linked['username']} (ID: {linked['discord_id']})"
        )
    else:
        await update.message.reply_text(
            "❌ No Discord account linked yet.\nUse /linkdiscord to generate a linking code."
        )


# ── Cross-platform Discord notification helpers ───────────────────────────────

import httpx as _httpx

DISCORD_TOKEN_ENV   = os.environ.get("DISCORD_TOKEN")
RUNS_CHANNEL_ID_ENV = os.environ.get("RUNS_CHANNEL_ID")

async def _discord_api(method, endpoint, **kwargs):
    """Make a Discord API call from the Telegram bot."""
    if not DISCORD_TOKEN_ENV:
        return None
    try:
        async with _httpx.AsyncClient() as c:
            resp = await getattr(c, method)(
                f"https://discord.com/api/v10{endpoint}",
                headers={"Authorization": f"Bot {DISCORD_TOKEN_ENV}", "Content-Type": "application/json"},
                **kwargs
            )
            if resp.status_code in (200, 204):
                return resp.json() if resp.content else True
            log.warning(f"Discord API {endpoint}: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.warning(f"Discord API error {endpoint}: {e}")
    return None

async def _notify_discord_channel(run, members, message):
    """Post a message to the Discord runs channel."""
    if not RUNS_CHANNEL_ID_ENV:
        return
    mentions = " ".join(
        f"<@{m['discord_id']}>" for m in members if m.get("discord_id")
    )
    text = f"{message}\n{mentions}" if mentions else message
    await _discord_api("post", f"/channels/{RUNS_CHANNEL_ID_ENV}/messages",
                       json={"content": text})

async def _update_discord_run_message(run, embed_dict):
    """Edit the Discord run post embed."""
    msg_id = run.get("discord_message_id")
    ch_id  = run.get("discord_channel_id")
    if not msg_id or not ch_id:
        return
    await _discord_api("patch", f"/channels/{ch_id}/messages/{msg_id}",
                       json={"embeds": [embed_dict], "components": []})

def _build_discord_embed(run, members):
    """Build a Discord embed dict for a run."""
    from datetime import datetime, timezone, timedelta
    run_dt = run["run_at"]
    if isinstance(run_dt, str):
        run_dt = datetime.fromisoformat(run_dt)
    if run_dt.tzinfo is None:
        run_dt = run_dt.replace(tzinfo=timezone.utc)
    sgt      = run_dt + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    color_map = {"confirmed": 0x57F287, "pending": 0xFEE75C, "cancelled": 0xED4245}
    color = color_map.get(run.get("status", "pending"), 0x5865F2)
    total    = len(members) if members else 0
    accepted = sum(1 for m in members if m.get("accepted") == 1) if members else 0
    party_lines = []
    if members:
        for m in members:
            icon = {1: "✅", -1: "❌", 0: "⏳"}.get(m.get("accepted", 0), "⏳")
            line = f"{icon} **{m['ign']}**"
            if m.get("discord_id"): line += f" (<@{m['discord_id']}>)"
            party_lines.append(line)
    status_map = {"confirmed": "✅ CONFIRMED", "pending": "⏳ PENDING", "cancelled": "❌ CANCELLED"}
    diff_emoji = {"easy":"🟢","normal":"🔵","hard":"🟠","chaos":"🔴","extreme":"⚫"}
    icon = diff_emoji.get(run.get("difficulty","").lower(), "⚪")
    return {
        "title": f"⚔️ Run #{run['id']} — {icon} {run.get('boss_name','')} {run.get('difficulty','')}",
        "color": color,
        "fields": [
            {"name": "📅 Date & Time", "value": time_str, "inline": True},
            {"name": "👑 Leader",      "value": f"@{run.get('leader_username','')}", "inline": True},
            {"name": "📋 Status",      "value": status_map.get(run.get("status",""), "UNKNOWN"), "inline": True},
            {"name": f"👥 Party ({accepted}/{total})", "value": "\n".join(party_lines) or "None", "inline": False},
        ]
    }

# ── /cancelrun ────────────────────────────────────────────────────────────────

async def cmd_cancelrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /cancelrun <run_id>"); return
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number."); return
    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found."); return
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run leader can cancel."); return
    if run["status"] == "cancelled":
        await update.message.reply_text("ℹ️ Already cancelled."); return
    db.cancel_run(run_id)
    run     = db.get_run(run_id)
    members = db.get_run_members(run_id)
    for m in members:
        try:
            await ctx.bot.send_message(
                chat_id=m["telegram_id"],
                text=(
                    f"❌ Run #{run_id} cancelled.\n"
                    f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                    f"Cancelled by @{update.effective_user.username or ''}."
                )
            )
        except Exception as e:
            log.warning(f"Cancel notify failed {m['ign']}: {e}")
    # Update Discord post
    try:
        discord_members = db.get_run_members_discord(run_id)
        embed = _build_discord_embed(run, discord_members)
        embed["footer"] = {"text": f"❌ Cancelled by @{update.effective_user.username or ''}"}
        await _update_discord_run_message(run, embed)
        sgt = get_run_dt(run) + timedelta(hours=8)
        await _notify_discord_channel(
            run, discord_members,
            f"❌ **Run #{run_id} has been cancelled.**\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n"
            f"Cancelled by @{update.effective_user.username or ''}."
        )
    except Exception as e:
        log.warning(f"Discord cancel update failed: {e}")
    await update.message.reply_text(f"🗑️ Run #{run_id} cancelled and members notified.")

# ── /myruns & /runs ───────────────────────────────────────────────────────────

async def cmd_myruns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runs = db.get_user_runs(update.effective_user.id)
    if not runs:
        await update.message.reply_text("You have no upcoming run invitations."); return
    await update.message.reply_text(fmt_runs_grouped(runs))

async def cmd_runs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runs = db.get_active_runs()
    if not runs:
        await update.message.reply_text("No upcoming runs scheduled."); return
    await update.message.reply_text(fmt_runs_grouped(runs))

# ── /chatid & /version ────────────────────────────────────────────────────────

async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat      = update.effective_chat
    thread_id = update.message.message_thread_id
    await update.message.reply_text(
        f"Chat ID: {chat.id}\n"
        f"Thread ID: {thread_id}\n"
        f"Type: {chat.type}\n"
        f"Title: {getattr(chat, 'title', 'N/A')}"
    )

async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import subprocess
    sgt = datetime.now(timezone(timedelta(hours=8)))
    try:
        commit = subprocess.check_output(["git", "log", "-1", "--format=%h %ci"], text=True).strip()
        await update.message.reply_text(f"🤖 Version: {commit}\nChecked: {sgt.strftime('%Y-%m-%d %H:%M SGT')}")
    except Exception:
        await update.message.reply_text(f"🤖 Started: {sgt.strftime('%Y-%m-%d %H:%M SGT')}")

# ── Scheduler ─────────────────────────────────────────────────────────────────

async def send_reminders(app: Application):
    runs = db.get_runs_due_for_reminder()
    for run in runs:
        members  = db.get_run_members(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
        msg = (
            f"⏰ Boss Run Reminder!\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 Starting at {time_str}\n\n"
            f"👥 Party:\n"
            + "\n".join(
                f"  • {m['ign']}" + (f" (@{m['username']})" if m["username"] else "")
                for m in members
            )
        )
        for m in members:
            try:
                await app.bot.send_message(chat_id=m["telegram_id"], text=msg)
            except Exception as e:
                log.warning(f"Reminder failed {m['ign']}: {e}")
        if GROUP_CHAT_ID:
            try:
                await app.bot.send_message(chat_id=GROUP_CHAT_ID, message_thread_id=GROUP_THREAD_ID, is_topic_message=bool(GROUP_THREAD_ID), text=msg)
            except Exception as e:
                log.warning(f"Group reminder failed: {e}")
        # Also notify Discord channel
        try:
            discord_members = db.get_run_members_discord(run["id"])
            reminder_notice = (
                f"⏰ **Boss Run Reminder!**\n"
                f"⚔️ {diff_icon(run['difficulty'])} **{run['boss_name']} {run['difficulty']}**\n"
                f"📅 Starting at **{time_str}**"
            )
            await _notify_discord_channel(run, discord_members, reminder_notice)
        except Exception as e:
            log.warning(f"Discord reminder failed: {e}")

async def auto_cancel_pending_runs(app: Application):
    expired = db.get_expired_pending_runs(hours=12)
    for run in expired:
        db.cancel_run(run["id"])
        members  = db.get_run_members(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
        pending  = [m for m in members if m["accepted"] == 0]
        msg = (
            f"⏰ Run #{run['id']} auto-cancelled.\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {time_str}\n\n"
            f"Not everyone responded within 12 hours."
        )
        for m in members:
            try:
                await app.bot.send_message(chat_id=m["telegram_id"], text=msg)
            except Exception as e:
                log.warning(f"Auto-cancel notify failed {m['ign']}: {e}")
        try:
            await app.bot.send_message(
                chat_id=run["leader_id"],
                text=(
                    f"⏰ Run #{run['id']} auto-cancelled — no response within 12 hours.\n"
                    f"No response from: {', '.join(m['ign'] for m in pending)}\n\n"
                    f"Use /createrun to reschedule."
                )
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
            SELECT_BOSS:    [CallbackQueryHandler(step_select_boss,     pattern=r"^boss_\d+$|^boss_diff_\d+_\d+$|^cx$")],
            SELECT_DIFF:    [CallbackQueryHandler(step_select_diff,     pattern=r"^diff_\d+$|^cx$")],
            SELECT_METHOD:  [CallbackQueryHandler(step_select_method,   pattern=r"^method_|^cx$")],
            SELECT_TEAM:    [CallbackQueryHandler(step_select_team,     pattern=r"^pickteam_\d+$|^method_back$|^cx$")],
            SELECT_MEMBERS: [CallbackQueryHandler(step_toggle_member,   pattern=r"^tog_\d+$|^members_done$|^members_back$|^cx$")],
            SELECT_DATE:    [CallbackQueryHandler(step_select_date,     pattern=r"^cal_|^cx$")],
            SELECT_HOUR:    [CallbackQueryHandler(step_select_hour,     pattern=r"^hr_\d+$|^cx$")],
            SELECT_MINUTE:  [CallbackQueryHandler(step_select_minute,   pattern=r"^mn_|^cx$")],
            CONFIRM_RUN:    [CallbackQueryHandler(step_confirm_run,     pattern=r"^run_confirm$|^cx$")],
        },
        fallbacks=[
            CommandHandler("cancel", createrun_cancel),
            CallbackQueryHandler(createrun_cancel, pattern=r"^cx$"),
        ],
        per_message=False, per_chat=False, per_user=True,
    )

    editrun_conv = ConversationHandler(
        entry_points=[CommandHandler("editrun", editrun_start)],
        states={
            EDIT_CHOOSE:  [CallbackQueryHandler(edit_choose,        pattern=r"^edit_|^cx$")],
            EDIT_DATE:    [CallbackQueryHandler(edit_select_date,   pattern=r"^cal_|^cx$")],
            EDIT_HOUR:    [CallbackQueryHandler(edit_select_hour,   pattern=r"^hr_\d+$|^cx$")],
            EDIT_MINUTE:  [CallbackQueryHandler(edit_select_minute, pattern=r"^mn_|^cx$")],
            EDIT_MEMBERS: [CallbackQueryHandler(edit_toggle_member, pattern=r"^etog_\d+$|^emembers_done$|^cx$")],
        },
        fallbacks=[
            CommandHandler("cancel", editrun_cancel),
            CallbackQueryHandler(editrun_cancel, pattern=r"^cx$"),
        ],
        per_message=False, per_chat=False, per_user=True,
    )

    createteam_conv = ConversationHandler(
        entry_points=[CommandHandler("createteam", createteam_start)],
        states={
            TEAM_MEMBERS: [CallbackQueryHandler(team_toggle_member, pattern=r"^ttog_\d+$|^tmembers_done$|^cx$")],
            TEAM_CONFIRM: [CallbackQueryHandler(team_confirm,       pattern=r"^tconfirm$|^cx$")],
        },
        fallbacks=[
            CommandHandler("cancel", createteam_cancel),
            CallbackQueryHandler(createteam_cancel, pattern=r"^cx$"),
        ],
        per_message=False, per_chat=False, per_user=True,
    )

    editteam_conv = ConversationHandler(
        entry_points=[CommandHandler("editteam", editteam_start)],
        states={
            ETEAM_CHOOSE:  [CallbackQueryHandler(eteam_choose,        pattern=r"^eteam_|^cx$")],
            ETEAM_MEMBERS: [CallbackQueryHandler(eteam_toggle_member, pattern=r"^ttog_\d+$|^tmembers_done$|^cx$")],
        },
        fallbacks=[
            CommandHandler("cancel", editteam_cancel),
            CallbackQueryHandler(editteam_cancel, pattern=r"^cx$"),
        ],
        per_message=False, per_chat=False, per_user=True,
    )

    app.add_handler(createrun_conv)
    app.add_handler(editrun_conv)
    app.add_handler(createteam_conv)
    app.add_handler(editteam_conv)
    app.add_handler(CallbackQueryHandler(rsvp_callback, pattern=r"^rsvp_"))
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("register",   cmd_register))
    app.add_handler(CommandHandler("chars",      cmd_chars))
    app.add_handler(CommandHandler("allchars",   cmd_allchars))
    app.add_handler(CommandHandler("removechar", cmd_removechar))
    app.add_handler(CommandHandler("bosses",     cmd_bosses))
    app.add_handler(CommandHandler("cancelrun",  cmd_cancelrun))
    app.add_handler(CommandHandler("resendrun",  cmd_resendrun))
    app.add_handler(CommandHandler("myruns",     cmd_myruns))
    app.add_handler(CommandHandler("runs",       cmd_runs))
    app.add_handler(CommandHandler("teams",      cmd_teams))
    app.add_handler(CommandHandler("deleteteam", cmd_deleteteam))
    app.add_handler(CommandHandler("linkdiscord", cmd_linkdiscord))
    app.add_handler(CommandHandler("linkstatus",  cmd_linkstatus))
    app.add_handler(CommandHandler("chatid",      cmd_chatid))
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
