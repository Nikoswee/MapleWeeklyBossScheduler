"""
MapleStory Guild Boss Scheduler — Telegram Bot

Fixes applied:
- ConversationHandler conversation_timeout: stuck sessions auto-clear after 10 min
- GROUP_CHAT_ID: guarded against empty-string env var (was crashing send_message)
- _notify_run: per-member try/except so one blocked user can't abort the whole loop
- leader_id notify: skips negative IDs (Discord-origin runs) silently, no crash
- APScheduler jobs wrapped in top-level try/except so errors don't kill the scheduler
- rsvp_callback: negative telegram_id members skipped in notify loops
- _build_discord_embed / _update_discord_run_message / _notify_discord_channel:
  properly check for DISCORD_TOKEN and discord_channel_id before attempting HTTP calls
- cmd_resendrun: skips negative telegram_ids
- Scheduler: get_expired_pending_runs loop notifies members before cancelling
- _render_method_picker / _render_team_picker / _render_calendar / _render_hour_picker /
  _render_minute_picker: all defined (were in the missing 379 lines, now explicit)
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import calendar

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import db

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
# Guard: treat empty string same as not-set
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "").strip() or None
GROUP_THREAD_ID = int(os.environ.get("GROUP_THREAD_ID", 0)) or None
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "").strip() or None

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
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
    CONFIRM_RUN,
) = range(9)

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
    icon = diff_icon(run["difficulty"])
    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    lines = [
        f"⚔️ #{run['id']} · {run['boss_name']} {run['difficulty']} {icon}",
        f"📅 {time_str}",
        f"👑 @{run['leader_username']}",
    ]
    if members:
        total = len(members)
        accepted = sum(1 for m in members if m["accepted"] == 1)
        waiting = [m for m in members if m["accepted"] != 1]
        party = f"👥 {accepted}/{total} accepted"
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
    RUN_DIVIDER = "- - - - - - - - - - - - - -"
    SECTION_DIVIDER = "──────────────"
    pending = [r for r in runs if r["status"] == "pending"]
    confirmed = [r for r in runs if r["status"] == "confirmed"]
    lines = ["📅 UPCOMING RUNS"]
    if confirmed:
        lines += ["", "✅ CONFIRMED", SECTION_DIVIDER]
        for i, run in enumerate(confirmed):
            lines.append(fmt_run(run, db.get_run_members(run["id"])))
            if i < len(confirmed) - 1:
                lines.append(RUN_DIVIDER)
    if pending:
        lines += ["", "⏳ PENDING", SECTION_DIVIDER]
        for i, run in enumerate(pending):
            lines.append(fmt_run(run, db.get_run_members(run["id"])))
            if i < len(pending) - 1:
                lines.append(RUN_DIVIDER)
    return "\n".join(lines)


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


def _check_team_creator(query, ctx):
    return query.from_user.id == ctx.user_data.get("team_creator_id")


# ── Keyboards ─────────────────────────────────────────────────────────────────

def build_calendar(year, month):
    now = datetime.now(timezone(timedelta(hours=8)))
    keyboard = []
    can_prev = (year, month) > (now.year, now.month)
    keyboard.append([
        InlineKeyboardButton("◀" if can_prev else " ", callback_data=f"cal_prev_{year}_{month}" if can_prev else "cal_noop"),
        InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="cal_noop"),
        InlineKeyboardButton("▶", callback_data=f"cal_next_{year}_{month}"),
    ])
    keyboard.append([InlineKeyboardButton(d, callback_data="cal_noop") for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]])
    for week in calendar.monthcalendar(year, month):
        row = []
        has_valid = False
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal_noop"))
            else:
                dt = datetime(year, month, day, tzinfo=timezone(timedelta(hours=8)))
                past = dt.date() < now.date()
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


COMMON_HOURS = [20, 21, 22, 23]


def build_hour_picker(selected=None):
    presets = []
    for h in COMMON_HOURS:
        label = f"★{h:02d}:00" if h == selected else f"{h:02d}:00"
        presets.append(InlineKeyboardButton(label, callback_data=f"hr_{h}"))
    rows = [presets, [InlineKeyboardButton("── Other times ──", callback_data="cal_noop")]]
    for i in range(0, 24, 6):
        row = []
        for h in range(i, i + 6):
            if h in COMMON_HOURS:
                continue
            label = f"[{h:02d}]" if h == selected else f"{h:02d}"
            row.append(InlineKeyboardButton(label, callback_data=f"hr_{h}"))
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    return InlineKeyboardMarkup(rows)


def build_minute_picker(cur=0):
    quick = [0, 15, 30, 45]
    row1 = [InlineKeyboardButton(f"[:{m:02d}]" if m == cur else f":{m:02d}", callback_data=f"mn_{m}") for m in quick]
    row2 = [
        InlineKeyboardButton("−5", callback_data=f"mn_{(cur - 5) % 60}"),
        InlineKeyboardButton(f" :{cur:02d} ", callback_data="mn_noop"),
        InlineKeyboardButton("+5", callback_data=f"mn_{(cur + 5) % 60}"),
    ]
    return InlineKeyboardMarkup([
        row1, row2,
        [
            InlineKeyboardButton("✔️ Confirm time", callback_data=f"mn_done_{cur}"),
            InlineKeyboardButton("❌ Cancel", callback_data="cx"),
        ],
    ])


# ── Render helpers ────────────────────────────────────────────────────────────

async def _render_calendar(query_or_update, ctx, edit=False):
    year = ctx.user_data.get("cal_year", datetime.now().year)
    month = ctx.user_data.get("cal_month", datetime.now().month)
    label = "Edit Run — " if edit else ""
    text = f"⚔️ {label}Step — Pick a date (SGT):"
    kb = build_calendar(year, month)
    if hasattr(query_or_update, "edit_message_text"):
        await query_or_update.edit_message_text(text, reply_markup=kb)
    else:
        await query_or_update.message.reply_text(text, reply_markup=kb)
    return EDIT_DATE if edit else SELECT_DATE


async def _render_hour_picker(query, ctx, edit=False):
    d = ctx.user_data["run_day"]
    mo = ctx.user_data["run_month"]
    y = ctx.user_data["run_year"]
    await query.edit_message_text(
        f"📅 {d:02d}/{mo:02d}/{y}\n\nPick a start hour (SGT):",
        reply_markup=build_hour_picker(),
    )
    return EDIT_HOUR if edit else SELECT_HOUR


async def _render_minute_picker(query, ctx, edit=False):
    cur = ctx.user_data.get("run_minute", 0)
    h = ctx.user_data["run_hour"]
    await query.edit_message_text(
        f"🕐 {h:02d}:xx SGT\n\nPick minutes:",
        reply_markup=build_minute_picker(cur),
    )
    return EDIT_MINUTE if edit else SELECT_MINUTE


async def _render_method_picker(query, ctx):
    """Step 3: ask how to select members (team or individual)."""
    teams = db.get_all_teams()
    boss = ctx.user_data["boss_name"]
    diff = ctx.user_data["difficulty"]

    if not teams:
        # No teams exist, go straight to individual picker
        return await _render_member_picker(query, ctx)

    keyboard = [
        [InlineKeyboardButton("📋 Load from Team", callback_data="method_team")],
        [InlineKeyboardButton("👤 Select Individually", callback_data="method_individual")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cx")],
    ]
    await query.edit_message_text(
        f"⚔️ {boss} {diff_icon(diff)} {diff}\n\n👥 How would you like to add members?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_METHOD


async def _render_team_picker(query_or_update, ctx, is_edit=False):
    """Render checkboxes for all characters for team create/edit."""
    all_chars = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected = ctx.user_data.get("selected_chars", [])

    buttons = []
    for i, ch in enumerate(all_chars):
        tick = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]:
            label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"tmem_{i}"))

    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    action = "Update" if is_edit else "Select"
    keyboard.append([
        InlineKeyboardButton(f"✔️ Done ({len(selected)})", callback_data="tmembers_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])

    text = f"👥 {action} team members (tap to toggle):"
    if hasattr(query_or_update, "edit_message_text"):
        await query_or_update.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await query_or_update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return ETEAM_MEMBERS if is_edit else TEAM_MEMBERS


async def _render_member_picker(query, ctx):
    """Step 3b: individual member picker for /createrun."""
    all_chars = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected = ctx.user_data.get("selected_chars", [])

    buttons = []
    for i, ch in enumerate(all_chars):
        tick = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]:
            label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"mem_{i}"))

    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard.append([
        InlineKeyboardButton(f"✔️ Done ({len(selected)})", callback_data="members_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])
    boss = ctx.user_data["boss_name"]
    diff = ctx.user_data["difficulty"]
    await query.edit_message_text(
        f"⚔️ {boss} {diff_icon(diff)} {diff}\n\n👥 Select party members (tap to toggle):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_MEMBERS


# ── Discord cross-notify helpers (called from Telegram bot) ──────────────────

def _build_discord_embed(run, members):
    """Build a plain-dict Discord embed payload for HTTP API calls."""
    icon = diff_icon(run["difficulty"])
    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    color_map = {"confirmed": 0x57F287, "pending": 0xFEE75C, "cancelled": 0xED4245}
    lines = []
    for m in members:
        status_icon = {1: "✅", -1: "❌", 0: "⏳"}.get(m["accepted"], "⏳")
        line = f"{status_icon} {m['ign']}"
        if m.get("discord_id"):
            line += f" (<@{m['discord_id']}>)"
        lines.append(line)
    total = len(members)
    accepted = sum(1 for m in members if m["accepted"] == 1)
    return {
        "title": f"⚔️ Run #{run['id']} — {icon} {run['boss_name']} {run['difficulty']}",
        "color": color_map.get(run["status"], 0x5865F2),
        "fields": [
            {"name": "📅 Date & Time", "value": time_str, "inline": True},
            {"name": "👑 Leader", "value": f"@{run['leader_username']}", "inline": True},
            {"name": f"👥 Party ({accepted}/{total})", "value": "\n".join(lines) or "None", "inline": False},
        ],
    }


async def _update_discord_run_message(run, embed: dict):
    """Edit the Discord run embed in-place. Silently skips if token/IDs not available."""
    if not DISCORD_TOKEN:
        return
    message_id = run.get("discord_message_id")
    channel_id = run.get("discord_channel_id")
    if not message_id or not channel_id:
        return
    import httpx
    try:
        async with httpx.AsyncClient() as c:
            await c.patch(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
                json={"embeds": [embed]},
                timeout=10,
            )
    except Exception as e:
        log.warning(f"Discord message update failed (run #{run.get('id')}): {e}")


async def _notify_discord_channel(run, members, text: str):
    """Post a plain text notification to the run's originating Discord channel."""
    if not DISCORD_TOKEN:
        return
    channel_id = run.get("discord_channel_id")
    if not channel_id:
        return
    import httpx
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"},
                json={"content": text},
                timeout=10,
            )
    except Exception as e:
        log.warning(f"Discord channel notify failed (channel #{channel_id}): {e}")


# ── _notify_run: send DM invites after creating / editing a run ───────────────

async def _notify_run(
    ctx, run_id, boss_name, difficulty,
    year, month, day, hour, minute, remind_mins,
    leader_username, creator_telegram_id, is_edit=False,
):
    """
    Send DM invites to all Telegram members of a run.
    Each member is wrapped in its own try/except — one blocked user cannot abort the loop.
    Skips members with negative telegram_id (Discord-only placeholder accounts).
    """
    members = db.get_run_members(run_id)
    sgt_str = f"{day:02d}/{month:02d}/{year} {hour:02d}:{minute:02d} SGT"
    icon = diff_icon(difficulty)

    if is_edit:
        intro = f"✏️ Run #{run_id} has been updated!\n\nPlease re-confirm your attendance:"
    else:
        intro = f"📨 You've been invited to a boss run!"

    party_lines = fmt_party_lines(members)

    invite_text = (
        f"{intro}\n\n"
        f"⚔️ {icon} {boss_name} {difficulty}\n"
        f"📅 {sgt_str}\n"
        f"👑 Leader: @{leader_username}\n\n"
        f"👥 Party:\n{party_lines}\n\n"
        f"Please respond:"
    )

    invite_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"rsvp_accept_{run_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"rsvp_decline_{run_id}"),
    ]])

    notified, failed = [], []

    for m in members:
        tg_id = m.get("telegram_id")
        # Skip Discord-only placeholder accounts (negative IDs)
        if not tg_id or tg_id < 0:
            continue
        # Skip self-notify (creator already knows about their own run)
        if tg_id == creator_telegram_id and not is_edit:
            continue
        try:
            await ctx.bot.send_message(chat_id=tg_id, text=invite_text, reply_markup=invite_kb)
            notified.append(m["ign"])
        except Exception as e:
            log.warning(f"Invite DM failed → {m['ign']} (tg_id:{tg_id}): {e}")
            failed.append(m["ign"])

    log.info(f"Run #{run_id} notified: {notified} | failed: {failed}")
    return notified, failed


# ── /createrun ────────────────────────────────────────────────────────────────

async def createrun_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    ctx.user_data.clear()
    ctx.user_data["creator_id"] = update.effective_user.id

    bosses = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    ctx.user_data["boss_map"] = grouped
    ctx.user_data["boss_list"] = list(grouped.keys())

    recent = db.get_recent_bosses(3)
    keyboard = []
    if recent:
        keyboard.append([InlineKeyboardButton("⭐ Recent", callback_data="cal_noop")])
        for r in recent:
            if r["name"] in grouped and r["difficulty"] in grouped[r["name"]]:
                b_idx = ctx.user_data["boss_list"].index(r["name"])
                d_idx = grouped[r["name"]].index(r["difficulty"])
                keyboard.append([InlineKeyboardButton(
                    f"⭐ {r['name']} {r['difficulty']}",
                    callback_data=f"boss_diff_{b_idx}_{d_idx}",
                )])
    keyboard.append([InlineKeyboardButton("── All Bosses ──", callback_data="cal_noop")])
    for i, name in enumerate(grouped):
        keyboard.append([InlineKeyboardButton(name, callback_data=f"boss_{i}")])
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])

    await update.message.reply_text(
        "⚔️ Create a Boss Run\n\nStep 1 — Which boss?\n⭐ = recently scheduled",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_BOSS


async def step_select_boss(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_BOSS
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data.startswith("boss_diff_"):
        parts = query.data.split("_")
        boss_idx = int(parts[2])
        diff_idx = int(parts[3])
        boss_name = ctx.user_data["boss_list"][boss_idx]
        ctx.user_data["boss_name"] = boss_name
        ctx.user_data["difficulty"] = ctx.user_data["boss_map"][boss_name][diff_idx]
        ctx.user_data["selected_chars"] = []
        return await _render_method_picker(query, ctx)

    idx = int(query.data.split("_")[1])
    boss_name = ctx.user_data["boss_list"][idx]
    ctx.user_data["boss_name"] = boss_name
    diffs = ctx.user_data["boss_map"][boss_name]
    ctx.user_data["diff_list"] = diffs
    keyboard = [[InlineKeyboardButton(f"{diff_icon(d)} {d}", callback_data=f"diff_{i}")] for i, d in enumerate(diffs)]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
    await query.edit_message_text(
        f"⚔️ Create a Boss Run\n\nBoss: {boss_name}\n\nStep 2 — Difficulty?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELECT_DIFF


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
    ctx.user_data["difficulty"] = ctx.user_data["diff_list"][idx]
    ctx.user_data["selected_chars"] = []
    return await _render_method_picker(query, ctx)


async def step_select_method(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_METHOD
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "method_team":
        teams = db.get_all_teams()
        options = [
            [InlineKeyboardButton(
                f"{t['name']} ({len(db.get_team_members(t['id']))} members)",
                callback_data=f"team_{t['id']}",
            )]
            for t in teams
        ]
        options.append([InlineKeyboardButton("❌ Cancel", callback_data="cx")])
        await query.edit_message_text(
            "📋 Select a preset team:",
            reply_markup=InlineKeyboardMarkup(options),
        )
        return SELECT_TEAM

    if query.data == "method_individual":
        return await _render_member_picker(query, ctx)

    return SELECT_METHOD


async def step_select_team(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_TEAM
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    team_id = int(query.data.split("_")[1])
    members = db.get_team_members(team_id)
    ctx.user_data["selected_chars"] = [m["id"] for m in members]
    now = datetime.now(timezone(timedelta(hours=8)))
    ctx.user_data["cal_year"] = now.year
    ctx.user_data["cal_month"] = now.month
    return await _render_calendar(query, ctx)


async def step_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_MEMBERS
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "members_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member!", show_alert=True)
            return SELECT_MEMBERS
        now = datetime.now(timezone(timedelta(hours=8)))
        ctx.user_data["cal_year"] = now.year
        ctx.user_data["cal_month"] = now.month
        return await _render_calendar(query, ctx)

    idx = int(query.data.split("_")[1])
    char_id = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected:
        selected.remove(char_id)
    else:
        selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_member_picker(query, ctx)


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
        year, month = int(parts[2]), int(parts[3])
        if parts[1] == "prev":
            month -= 1
            if month < 1:
                month = 12; year -= 1
        else:
            month += 1
            if month > 12:
                month = 1; year += 1
        ctx.user_data["cal_year"] = year
        ctx.user_data["cal_month"] = month
        return await _render_calendar(query, ctx)

    if parts[1] == "day":
        ctx.user_data["run_year"] = int(parts[2])
        ctx.user_data["run_month"] = int(parts[3])
        ctx.user_data["run_day"] = int(parts[4])
        return await _render_hour_picker(query, ctx)

    return SELECT_DATE


async def step_select_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_creator(query, ctx):
        return SELECT_HOUR
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Run creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    ctx.user_data["run_hour"] = int(query.data.split("_")[1])
    ctx.user_data["run_minute"] = ctx.user_data.get("run_minute", 0)
    return await _render_minute_picker(query, ctx)


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
        return await _show_confirm(query, ctx)

    ctx.user_data["run_minute"] = int(parts[1])
    return await _render_minute_picker(query, ctx)


async def _show_confirm(query, ctx):
    y = ctx.user_data["run_year"]
    mo = ctx.user_data["run_month"]
    d = ctx.user_data["run_day"]
    h = ctx.user_data["run_hour"]
    m = ctx.user_data["run_minute"]
    boss = ctx.user_data["boss_name"]
    diff = ctx.user_data["difficulty"]
    selected = ctx.user_data.get("selected_chars", [])
    chars = [db.get_character_by_id(cid) for cid in selected]

    member_lines = "\n".join(f"  • {ch['ign']}" for ch in chars if ch)
    await query.edit_message_text(
        f"📋 Run Summary\n\n"
        f"⚔️ {diff_icon(diff)} {boss} {diff}\n"
        f"📅 {d:02d}/{mo:02d}/{y} {h:02d}:{m:02d} SGT\n\n"
        f"👥 Party ({len(selected)}):\n{member_lines}\n\n"
        f"Confirm?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm & Post", callback_data="confirm_run"),
            InlineKeyboardButton("❌ Cancel", callback_data="cx"),
        ]]),
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

    y = ctx.user_data["run_year"]
    mo = ctx.user_data["run_month"]
    d = ctx.user_data["run_day"]
    h = ctx.user_data["run_hour"]
    m = ctx.user_data["run_minute"]
    boss_name = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]
    selected = ctx.user_data["selected_chars"]

    sgt_dt = datetime(y, mo, d, h, m, tzinfo=timezone(timedelta(hours=8)))
    if sgt_dt <= datetime.now(timezone(timedelta(hours=8))):
        await query.edit_message_text("⚠️ That date/time is in the past. Use /createrun to try again.")
        ctx.user_data.clear()
        return ConversationHandler.END

    run_at_iso = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    boss = db.find_boss(boss_name, difficulty)
    telegram_id = update.effective_user.id
    run_id = db.create_run(boss["id"], telegram_id, run_at_iso)

    for char_id in selected:
        db.add_run_member(run_id, char_id)

    # Set 8am SGT reminder on run day (skip if same day)
    sgt_tz = timezone(timedelta(hours=8))
    now_sgt = datetime.now(sgt_tz)
    run_8am = sgt_dt.replace(hour=8, minute=0, second=0, microsecond=0)
    if run_8am.date() > now_sgt.date():
        db.set_run_reminder(run_id, run_8am.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

    leader = update.effective_user.username or str(telegram_id)
    await query.edit_message_text(
        f"✅ Run #{run_id} created!\n\n"
        f"⚔️ {diff_icon(difficulty)} {boss_name} {difficulty}\n"
        f"📅 {d:02d}/{mo:02d}/{y} {h:02d}:{m:02d} SGT\n\n"
        f"Sending invites..."
    )

    notified, failed = await _notify_run(
        ctx, run_id, boss_name, difficulty, y, mo, d, h, m, 0, leader, telegram_id
    )

    summary = f"✅ Run #{run_id} created! Notified: {', '.join(notified) or 'none'}"
    if failed:
        summary += f"\n⚠️ Could not DM: {', '.join(failed)}"
    await query.edit_message_text(summary)

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


# ── /cancelrun ────────────────────────────────────────────────────────────────

async def cmd_cancelrun(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /cancelrun <run_id>")
        return
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number.")
        return

    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found.")
        return
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run leader can cancel this run.")
        return
    if run["status"] == "cancelled":
        await update.message.reply_text("⚠️ This run is already cancelled.")
        return

    db.cancel_run(run_id)
    members = db.get_run_members(run_id)
    sgt = get_run_dt(run) + timedelta(hours=8)
    cancel_msg = (
        f"❌ Run #{run_id} has been cancelled.\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}"
    )

    notified = []
    for m in members:
        tg_id = m.get("telegram_id")
        if not tg_id or tg_id < 0:
            continue
        if tg_id == update.effective_user.id:
            continue
        try:
            await ctx.bot.send_message(chat_id=tg_id, text=cancel_msg)
            notified.append(m["ign"])
        except Exception as e:
            log.warning(f"Cancel notify failed → {m['ign']}: {e}")

    if GROUP_CHAT_ID:
        try:
            await ctx.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                message_thread_id=GROUP_THREAD_ID,
                is_topic_message=bool(GROUP_THREAD_ID),
                text=cancel_msg,
            )
        except Exception as e:
            log.warning(f"Group cancel notify failed: {e}")

    # Update Discord post
    try:
        run_data = db.get_run(run_id)
        discord_members = db.get_run_members_discord(run_id)
        embed = _build_discord_embed(run_data, discord_members)
        embed["footer"] = {"text": "❌ Cancelled by leader"}
        await _update_discord_run_message(run_data, embed)
        await _notify_discord_channel(run_data, discord_members, cancel_msg)
    except Exception as e:
        log.warning(f"Discord cancel update failed: {e}")

    await update.message.reply_text(f"✅ Run #{run_id} cancelled. Notified {len(notified)} member(s).")


# ── /createteam ───────────────────────────────────────────────────────────────

async def createteam_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text(
            "Usage: /createteam <team name>\nExample: /createteam Lotus Party"
        )
        return ConversationHandler.END
    name = " ".join(ctx.args).strip()
    if len(name) > 50:
        await update.message.reply_text("⚠️ Team name too long (max 50 chars).")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["team_creator_id"] = update.effective_user.id
    ctx.user_data["team_name"] = name
    ctx.user_data["selected_chars"] = []
    return await _render_team_picker(update, ctx, is_edit=False)


async def team_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can use these buttons.", show_alert=True)
        return TEAM_MEMBERS

    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Team creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "tmembers_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member!", show_alert=True)
            return TEAM_MEMBERS
        await query.answer()
        name = ctx.user_data["team_name"]
        selected = ctx.user_data["selected_chars"]
        chars = [db.get_character_by_id(cid) for cid in selected]
        members = ", ".join(ch["ign"] for ch in chars if ch)
        await query.edit_message_text(
            f"📋 Team Summary\n\nTeam: {name}\nMembers ({len(chars)}): {members}\n\nConfirm?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm", callback_data="tconfirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="cx"),
            ]]),
        )
        return TEAM_CONFIRM

    await query.answer()
    idx = int(query.data.split("_")[1])
    char_id = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected:
        selected.remove(char_id)
    else:
        selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_team_picker(query, ctx, is_edit=False)


async def team_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can confirm.", show_alert=True)
        return TEAM_CONFIRM
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Team creation cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    name = ctx.user_data["team_name"]
    selected = ctx.user_data["selected_chars"]
    team_id, err = db.create_team(name, update.effective_user.id, selected)
    if err:
        await query.edit_message_text(f"⚠️ {err}\nUse /createteam to try again.")
    else:
        chars = [db.get_character_by_id(cid) for cid in selected]
        members = ", ".join(ch["ign"] for ch in chars if ch)
        await query.edit_message_text(
            f"✅ Team saved!\n\nName: {name}\nMembers ({len(chars)}): {members}\n\n"
            f"Use /teams to see all teams."
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
        await update.message.reply_text("No preset teams yet. Use /createteam to create one.")
        return
    lines = ["👥 Preset Teams\n"]
    for t in teams:
        members = db.get_team_members(t["id"])
        names = ", ".join(m["ign"] for m in members)
        lines.append(f"• {t['name']} ({len(members)} members)")
        lines.append(f"  {names}")
        lines.append("")
    lines.append("Commands: /editteam <name> · /deleteteam <name>")
    await update.message.reply_text("\n".join(lines))


# ── /editteam ─────────────────────────────────────────────────────────────────

async def editteam_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /editteam <team name>")
        return ConversationHandler.END
    name = " ".join(ctx.args)
    team = db.get_team_by_name(name)
    if not team:
        await update.message.reply_text("⚠️ Team not found. Use /teams to see all teams.")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["team_creator_id"] = update.effective_user.id
    ctx.user_data["edit_team_id"] = team["id"]
    ctx.user_data["eteam_name"] = team["name"]
    current = db.get_team_members(team["id"])
    ctx.user_data["selected_chars"] = [m["id"] for m in current]
    await update.message.reply_text(
        f"✏️ Edit Team: {team['name']}\n\nWhat would you like to edit?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Rename", callback_data="eteam_rename")],
            [InlineKeyboardButton("👥 Edit Members", callback_data="eteam_members")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cx")],
        ]),
    )
    return ETEAM_CHOOSE


async def eteam_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can edit.", show_alert=True)
        return ETEAM_CHOOSE
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "eteam_rename":
        await query.edit_message_text(
            f"✏️ To rename, delete and recreate:\n"
            f"/deleteteam {ctx.user_data['eteam_name']}\n"
            f"/createteam <new name>"
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "eteam_members":
        return await _render_team_picker(query, ctx, is_edit=True)

    return ETEAM_CHOOSE


async def eteam_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not _check_team_creator(query, ctx):
        await query.answer("⚠️ Only the team creator can edit.", show_alert=True)
        return ETEAM_MEMBERS

    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "tmembers_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member!", show_alert=True)
            return ETEAM_MEMBERS
        await query.answer()
        team_id = ctx.user_data["edit_team_id"]
        name = ctx.user_data["eteam_name"]
        selected = ctx.user_data["selected_chars"]
        ok, err = db.update_team(team_id, name, selected)
        if ok:
            chars = [db.get_character_by_id(cid) for cid in selected]
            members = ", ".join(ch["ign"] for ch in chars if ch)
            await query.edit_message_text(f"✅ Team updated!\nName: {name}\nMembers ({len(chars)}): {members}")
        else:
            await query.edit_message_text(f"⚠️ {err}")
        ctx.user_data.clear()
        return ConversationHandler.END

    await query.answer()
    idx = int(query.data.split("_")[1])
    char_id = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected:
        selected.remove(char_id)
    else:
        selected.append(char_id)
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
        await update.message.reply_text("Usage: /deleteteam <team name>")
        return
    name = " ".join(ctx.args)
    team = db.get_team_by_name(name)
    if not team:
        await update.message.reply_text("⚠️ Team not found. Use /teams to see all teams.")
        return
    db.delete_team(team["id"])
    await update.message.reply_text("🗑️ Team deleted.")


# ── /editrun ──────────────────────────────────────────────────────────────────

async def editrun_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /editrun <run_id>")
        return ConversationHandler.END
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number.")
        return ConversationHandler.END

    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found.")
        return ConversationHandler.END
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run creator can edit this run.")
        return ConversationHandler.END
    if run["status"] == "cancelled":
        await update.message.reply_text("⚠️ This run has been cancelled.")
        return ConversationHandler.END
    if get_run_dt(run) <= datetime.now(timezone.utc):
        await update.message.reply_text("⚠️ This run has already passed.")
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["editor_id"] = update.effective_user.id
    ctx.user_data["edit_run_id"] = run_id
    ctx.user_data["boss_name"] = run["boss_name"]
    ctx.user_data["difficulty"] = run["difficulty"]

    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    await update.message.reply_text(
        f"✏️ Edit Run #{run_id}\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {time_str}\n\nWhat would you like to edit?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Date & Time", callback_data="edit_datetime")],
            [InlineKeyboardButton("👥 Party Members", callback_data="edit_members")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cx")],
        ]),
    )
    return EDIT_CHOOSE


async def edit_choose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx):
        return EDIT_CHOOSE
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "edit_datetime":
        now = datetime.now(timezone(timedelta(hours=8)))
        ctx.user_data["cal_year"] = now.year
        ctx.user_data["cal_month"] = now.month
        return await _render_calendar(query, ctx, edit=True)

    if query.data == "edit_members":
        run_id = ctx.user_data["edit_run_id"]
        current = db.get_run_members(run_id)
        ctx.user_data["selected_chars"] = [m["character_id"] for m in current]
        return await _render_edit_member_picker(query, ctx)

    return EDIT_CHOOSE


async def edit_select_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx):
        return EDIT_DATE
    await query.answer()

    if query.data in ("cx",):
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "cal_noop":
        return EDIT_DATE

    parts = query.data.split("_")
    if parts[1] in ("prev", "next"):
        year, month = int(parts[2]), int(parts[3])
        if parts[1] == "prev":
            month -= 1
            if month < 1:
                month = 12; year -= 1
        else:
            month += 1
            if month > 12:
                month = 1; year += 1
        ctx.user_data["cal_year"] = year
        ctx.user_data["cal_month"] = month
        return await _render_calendar(query, ctx, edit=True)

    if parts[1] == "day":
        ctx.user_data["run_year"] = int(parts[2])
        ctx.user_data["run_month"] = int(parts[3])
        ctx.user_data["run_day"] = int(parts[4])
        return await _render_hour_picker(query, ctx, edit=True)

    return EDIT_DATE


async def edit_select_hour(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx):
        return EDIT_HOUR
    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    ctx.user_data["run_hour"] = int(query.data.split("_")[1])
    ctx.user_data["run_minute"] = ctx.user_data.get("run_minute", 0)
    return await _render_minute_picker(query, ctx, edit=True)


async def edit_select_minute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx):
        return EDIT_MINUTE

    if query.data == "mn_noop":
        await query.answer()
        return EDIT_MINUTE

    await query.answer()

    if query.data == "cx":
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    parts = query.data.split("_")
    if parts[1] == "done":
        ctx.user_data["run_minute"] = int(parts[2])
        return await _apply_datetime_edit(query, ctx)

    ctx.user_data["run_minute"] = int(parts[1])
    return await _render_minute_picker(query, ctx, edit=True)


async def _apply_datetime_edit(query, ctx):
    run_id = ctx.user_data["edit_run_id"]
    y, mo, d = ctx.user_data["run_year"], ctx.user_data["run_month"], ctx.user_data["run_day"]
    hour, minute = ctx.user_data["run_hour"], ctx.user_data["run_minute"]
    sgt_dt = datetime(y, mo, d, hour, minute, tzinfo=timezone(timedelta(hours=8)))
    if sgt_dt <= datetime.now(timezone(timedelta(hours=8))):
        await query.edit_message_text("⚠️ That date/time is in the past. Use /editrun to try again.")
        ctx.user_data.clear()
        return ConversationHandler.END

    run_at_iso = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.update_run_time(run_id, run_at_iso)
    run = db.get_run(run_id)
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
    all_chars = db.get_all_characters()
    ctx.user_data["char_list"] = [ch["id"] for ch in all_chars]
    selected = ctx.user_data.get("selected_chars", [])
    run_id = ctx.user_data["edit_run_id"]
    boss_name = ctx.user_data["boss_name"]
    difficulty = ctx.user_data["difficulty"]

    buttons = []
    for i, ch in enumerate(all_chars):
        tick = "✅" if ch["id"] in selected else "⬜"
        label = f"{tick} {ch['ign']}"
        if ch["level"]:
            label += f" {ch['level']}"
        buttons.append(InlineKeyboardButton(label, callback_data=f"etog_{i}"))

    keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
    keyboard.append([
        InlineKeyboardButton(f"✔️ Done ({len(selected)})", callback_data="emembers_done"),
        InlineKeyboardButton("❌ Cancel", callback_data="cx"),
    ])
    await query.edit_message_text(
        f"✏️ Edit Run #{run_id} — Update party members:\n"
        f"Boss: {boss_name} {difficulty}\n\n(tap to toggle)",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return EDIT_MEMBERS


async def edit_toggle_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _check_editor(query, ctx):
        return EDIT_MEMBERS

    if query.data == "cx":
        await query.answer()
        await query.edit_message_text("❌ Edit cancelled.")
        ctx.user_data.clear()
        return ConversationHandler.END

    if query.data == "emembers_done":
        if not ctx.user_data.get("selected_chars"):
            await query.answer("⚠️ Select at least one member first!", show_alert=True)
            return EDIT_MEMBERS
        await query.answer()
        return await _apply_members_edit(query, ctx)

    await query.answer()
    idx = int(query.data.split("_")[1])
    char_id = ctx.user_data["char_list"][idx]
    selected = ctx.user_data.get("selected_chars", [])
    if char_id in selected:
        selected.remove(char_id)
    else:
        selected.append(char_id)
    ctx.user_data["selected_chars"] = selected
    return await _render_edit_member_picker(query, ctx)


async def _apply_members_edit(query, ctx):
    run_id = ctx.user_data["edit_run_id"]
    selected = ctx.user_data["selected_chars"]
    run = db.get_run(run_id)
    db.reset_run_members(run_id, selected)
    run_dt = get_run_dt(run)
    sgt = run_dt + timedelta(hours=8)
    leader = query.from_user.username or str(query.from_user.id)
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
        await update.message.reply_text("Usage: /resendrun <run_id>")
        return
    try:
        run_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("⚠️ Run ID must be a number.")
        return

    run = db.get_run(run_id)
    if not run:
        await update.message.reply_text(f"⚠️ Run #{run_id} not found.")
        return
    if run["leader_id"] != update.effective_user.id:
        await update.message.reply_text("⚠️ Only the run leader can resend invites.")
        return
    if run["status"] == "cancelled":
        await update.message.reply_text("⚠️ This run has been cancelled.")
        return

    members = db.get_run_members(run_id)
    pending = [m for m in members if m["accepted"] == 0]
    if not pending:
        await update.message.reply_text(f"ℹ️ No pending members for Run #{run_id}.")
        return

    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    leader = update.effective_user.username or str(update.effective_user.id)
    invite_text = (
        f"📨 Reminder: You haven't responded to this boss run yet!\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {time_str}\n"
        f"👑 Leader: @{leader}\n\nPlease respond:"
    )
    invite_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept", callback_data=f"rsvp_accept_{run_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"rsvp_decline_{run_id}"),
    ]])

    notified, failed = [], []
    for m in pending:
        tg_id = m.get("telegram_id")
        if not tg_id or tg_id < 0:
            continue
        try:
            await ctx.bot.send_message(chat_id=tg_id, text=invite_text, reply_markup=invite_kb)
            notified.append(m["ign"])
        except Exception as e:
            log.warning(f"Resend failed → {m['ign']} (id:{tg_id}): {e}")
            failed.append(m["ign"])

    summary = f"✅ Resent to {len(notified)} pending member(s): {', '.join(notified)}"
    if failed:
        summary += f"\n⚠️ Still couldn't DM: {', '.join(failed)}"
    await update.message.reply_text(summary)


# ── RSVP callbacks ────────────────────────────────────────────────────────────

async def rsvp_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    action = parts[1]
    run_id = int(parts[2])
    accepted = 1 if action == "accept" else -1

    log.info(f"RSVP: {update.effective_user.username} | action:{action} | run_id:{run_id}")

    run = db.get_run(run_id)
    if not run:
        await query.edit_message_text(f"⚠️ Run #{run_id} not found.")
        return

    if run["status"] == "cancelled":
        await query.edit_message_text(f"⚠️ Run #{run_id} has been cancelled.")
        return

    user_chars = db.get_characters(update.effective_user.id)
    matched = None
    for ch in user_chars:
        rm = db.get_run_member_by_char(run_id, ch["id"])
        if rm:
            matched = (ch, rm)
            break

    if not matched:
        await query.answer("⚠️ You're not invited to this run.", show_alert=True)
        log.warning(f"RSVP: no match for {update.effective_user.username} in run {run_id}")
        return

    ch, rm = matched

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
    members = db.get_run_members(run_id)
    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    party = fmt_party_lines(members)

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
                tg_id = m.get("telegram_id")
                if not tg_id or tg_id < 0:
                    continue
                if tg_id == update.effective_user.id:
                    continue
                try:
                    await ctx.bot.send_message(chat_id=tg_id, text=confirm_msg)
                except Exception as e:
                    log.warning(f"Confirm notify failed {m['ign']}: {e}")

            # Notify leader (skip if negative / Discord-only)
            leader_id = run["leader_id"]
            if leader_id and leader_id > 0 and leader_id != update.effective_user.id:
                try:
                    await ctx.bot.send_message(chat_id=leader_id, text=confirm_msg)
                except Exception as e:
                    log.warning(f"Leader confirm notify failed: {e}")

            if GROUP_CHAT_ID:
                try:
                    await ctx.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        message_thread_id=GROUP_THREAD_ID,
                        is_topic_message=bool(GROUP_THREAD_ID),
                        text=confirm_msg,
                    )
                except Exception as e:
                    log.warning(f"Group confirm failed: {e}")

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
            total = len(members)
            done = sum(1 for m in members if m["accepted"] == 1)
            await query.edit_message_text(
                f"✅ {ch['ign']} accepted Run #{run_id}! ({done}/{total})\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                f"📅 {time_str}\n\n"
                f"👥 Party:\n{party}\n\n"
                f"Still waiting on: {', '.join(m['ign'] for m in pending)}"
            )
            leader_id = run["leader_id"]
            if leader_id and leader_id > 0:
                try:
                    await ctx.bot.send_message(
                        chat_id=leader_id,
                        text=(
                            f"ℹ️ {ch['ign']} accepted Run #{run_id}. ({done}/{total})\n"
                            f"Still waiting on: {', '.join(m['ign'] for m in pending)}"
                        ),
                    )
                except Exception as e:
                    log.warning(f"Leader notify failed: {e}")

            try:
                await _update_discord_run_message(run, _build_discord_embed(run, members))
            except Exception as e:
                log.warning(f"Discord partial accept update failed: {e}")

    else:
        db.cancel_run(run_id)
        all_members = db.get_run_members(run_id)
        cancel_msg = (
            f"❌ Run #{run_id} has been cancelled.\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {time_str}\n\n"
            f"{ch['ign']} (@{update.effective_user.username or ''}) declined the invite."
        )
        await query.edit_message_text(cancel_msg)

        for m in all_members:
            tg_id = m.get("telegram_id")
            if not tg_id or tg_id < 0:
                continue
            if tg_id == update.effective_user.id:
                continue
            try:
                await ctx.bot.send_message(chat_id=tg_id, text=cancel_msg)
            except Exception as e:
                log.warning(f"Decline cancel notify failed {m['ign']}: {e}")

        leader_id = run["leader_id"]
        if leader_id and leader_id > 0 and leader_id != update.effective_user.id:
            try:
                await ctx.bot.send_message(chat_id=leader_id, text=cancel_msg)
            except Exception as e:
                log.warning(f"Leader decline cancel notify failed: {e}")

        if GROUP_CHAT_ID:
            try:
                await ctx.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    message_thread_id=GROUP_THREAD_ID,
                    is_topic_message=bool(GROUP_THREAD_ID),
                    text=cancel_msg,
                )
            except Exception as e:
                log.warning(f"Group decline cancel notify failed: {e}")

        log.info(f"Run #{run_id} auto-cancelled due to decline by {ch['ign']}")

        try:
            run_data = db.get_run(run_id)
            discord_members = db.get_run_members_discord(run_id)
            embed = _build_discord_embed(run_data, discord_members)
            embed["footer"] = {"text": f"❌ Cancelled — {ch['ign']} declined"}
            await _update_discord_run_message(run_data, embed)
            sgt2 = get_run_dt(run_data) + timedelta(hours=8)
            cancel_notice = (
                f"❌ **Run #{run_id} has been cancelled.**\n"
                f"⚔️ {diff_icon(run_data['difficulty'])} {run_data['boss_name']} {run_data['difficulty']}\n"
                f"📅 {sgt2.strftime('%d/%m/%Y %H:%M SGT')}\n"
                f"{ch['ign']} (@{update.effective_user.username or ''}) declined on Telegram."
            )
            await _notify_discord_channel(run_data, discord_members, cancel_notice)
        except Exception as e:
            log.warning(f"Discord decline update failed: {e}")


# ── Simple commands ───────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    await update.message.reply_text(
        "🍄 *MapleStory Boss Scheduler*\n\n"
        "You're registered! Send me DMs and I'll notify you about boss runs.\n\n"
        "/register <IGN> — add your character\n"
        "/createrun — create a boss run\n"
        "/runs — view upcoming runs\n"
        "/linkdiscord — link to Discord account\n"
        "/help — all commands",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *All Commands*\n\n"
        "*Characters*\n"
        "/register <IGN> [Class] [Level]\n"
        "/chars — your characters\n"
        "/allchars — all guild characters\n\n"
        "*Scheduling*\n"
        "/createrun — create a run\n"
        "/cancelrun <id> — cancel a run\n"
        "/editrun <id> — edit date/time or party\n"
        "/resendrun <id> — resend invites\n"
        "/myruns — your upcoming runs\n"
        "/runs — all upcoming runs\n\n"
        "*Teams*\n"
        "/createteam <name>\n"
        "/teams · /editteam <name> · /deleteteam <name>\n\n"
        "*Account*\n"
        "/linkdiscord — generate Discord link code\n"
        "/linkstatus — check link status\n\n"
        "📅 All times SGT (UTC+8)",
        parse_mode="Markdown",
    )


async def cmd_version(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import time
    await update.message.reply_text(f"🍄 Bot online. Started: {ctx.bot_data.get('start_time', 'unknown')}")


async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    if not ctx.args:
        await update.message.reply_text("Usage: /register <IGN> [Class] [Level]\nExample: /register KokoroMapper Bowmaster 275")
        return
    ign = ctx.args[0]
    cls = ctx.args[1] if len(ctx.args) > 1 else None
    level = None
    if len(ctx.args) > 2:
        try:
            level = int(ctx.args[2])
        except ValueError:
            pass
    ok = db.add_character(update.effective_user.id, ign, cls, level)
    if ok:
        parts = [f"✅ Registered *{ign}*"]
        if cls:
            parts.append(f"Class: {cls}")
        if level:
            parts.append(f"Level: {level}")
        await update.message.reply_text(" | ".join(parts), parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ IGN *{ign}* is already registered.", parse_mode="Markdown")


async def cmd_chars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    chars = db.get_characters(update.effective_user.id)
    if not chars:
        await update.message.reply_text("No characters yet. Use /register.")
        return
    lines = ["👤 *Your Characters*\n"]
    for ch in chars:
        line = f"• *{ch['ign']}*"
        if ch["class"]:
            line += f" — {ch['class']}"
        if ch["level"]:
            line += f" Lv.{ch['level']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_allchars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chars = db.get_all_characters()
    if not chars:
        await update.message.reply_text("No characters registered yet.")
        return
    lines = ["🌍 *All Guild Characters*\n"]
    for ch in chars:
        line = f"• *{ch['ign']}*"
        if ch["class"]:
            line += f" — {ch['class']}"
        if ch["level"]:
            line += f" Lv.{ch['level']}"
        lines.append(line)
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_runs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    runs = db.get_active_runs()
    if not runs:
        await update.message.reply_text("📅 No upcoming runs.")
        return
    await update.message.reply_text(fmt_runs_grouped(runs))


async def cmd_myruns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    runs = db.get_user_runs(update.effective_user.id)
    if not runs:
        await update.message.reply_text("You have no upcoming runs.")
        return
    await update.message.reply_text(fmt_runs_grouped(runs))


async def cmd_chatid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    msg = f"Chat ID: `{chat.id}`"
    if thread_id:
        msg += f"\nThread ID: `{thread_id}`"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_linkdiscord(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    code = db.create_link_code(update.effective_user.id)
    await update.message.reply_text(
        f"🔗 Your Discord linking code:\n\n`{code}`\n\n"
        f"This code expires in 10 minutes.\n\n"
        f"On Discord, type:\n`/linkaccount {code}`",
        parse_mode="Markdown",
    )


async def cmd_linkstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_user.id, update.effective_user.username or "")
    linked = db.get_link_status(update.effective_user.id)
    if linked:
        await update.message.reply_text(
            f"✅ Linked to Discord account @{linked['username']}\n"
            f"You'll receive run invites on both platforms."
        )
    else:
        await update.message.reply_text(
            "❌ No Discord account linked.\n\n"
            "Use /linkdiscord to generate a code, then use `/linkaccount <code>` on Discord."
        )


# ── APScheduler jobs ──────────────────────────────────────────────────────────

async def _job_send_reminders(app):
    """Send 8am SGT reminders for confirmed runs. Wrapped in try/except so APScheduler doesn't die."""
    try:
        runs = db.get_runs_due_for_reminder()
        for run in runs:
            members = db.get_run_members(run["id"])
            sgt = get_run_dt(run) + timedelta(hours=8)
            time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
            reminder_msg = (
                f"⏰ *Reminder!* Run #{run['id']} is today!\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                f"📅 {time_str}"
            )
            for m in members:
                tg_id = m.get("telegram_id")
                if not tg_id or tg_id < 0:
                    continue
                try:
                    await app.bot.send_message(chat_id=tg_id, text=reminder_msg, parse_mode="Markdown")
                except Exception as e:
                    log.warning(f"Reminder send failed → {m['ign']}: {e}")

            if GROUP_CHAT_ID:
                try:
                    await app.bot.send_message(
                        chat_id=GROUP_CHAT_ID,
                        message_thread_id=GROUP_THREAD_ID,
                        is_topic_message=bool(GROUP_THREAD_ID),
                        text=reminder_msg,
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    log.warning(f"Group reminder failed: {e}")

            # Clear remind_at so it doesn't fire again
            db.set_run_reminder(run["id"], None)
            log.info(f"Reminder sent for run #{run['id']}")

    except Exception as e:
        log.error(f"Reminder job error: {e}")


async def _job_expire_pending_runs(app):
    """Auto-cancel pending runs older than 12 hours. Per-run try/except."""
    try:
        expired = db.get_expired_pending_runs(hours=12)
        for run in expired:
            try:
                members = db.get_run_members(run["id"])
                db.cancel_run(run["id"])
                sgt = get_run_dt(run) + timedelta(hours=8)
                msg = (
                    f"⚠️ Run #{run['id']} has been auto-cancelled (no response within 12 hours).\n\n"
                    f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                    f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}"
                )
                for m in members:
                    tg_id = m.get("telegram_id")
                    if not tg_id or tg_id < 0:
                        continue
                    try:
                        await app.bot.send_message(chat_id=tg_id, text=msg)
                    except Exception as e:
                        log.warning(f"Expire notify failed → {m['ign']}: {e}")

                if GROUP_CHAT_ID:
                    try:
                        await app.bot.send_message(
                            chat_id=GROUP_CHAT_ID,
                            message_thread_id=GROUP_THREAD_ID,
                            is_topic_message=bool(GROUP_THREAD_ID),
                            text=msg,
                        )
                    except Exception as e:
                        log.warning(f"Group expire notify failed: {e}")

                log.info(f"Run #{run['id']} auto-cancelled (expired pending)")
            except Exception as e:
                log.error(f"Expire job error for run #{run['id']}: {e}")

    except Exception as e:
        log.error(f"Expire pending runs job error: {e}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["start_time"] = datetime.now(timezone(timedelta(hours=8))).strftime("%d/%m/%Y %H:%M SGT")

    # Conversation timeout: 10 minutes — prevents sessions getting permanently stuck
    CONV_TIMEOUT = 600

    createrun_conv = ConversationHandler(
        entry_points=[CommandHandler("createrun", createrun_start)],
        states={
            SELECT_BOSS:    [CallbackQueryHandler(step_select_boss)],
            SELECT_DIFF:    [CallbackQueryHandler(step_select_diff)],
            SELECT_METHOD:  [CallbackQueryHandler(step_select_method)],
            SELECT_TEAM:    [CallbackQueryHandler(step_select_team)],
            SELECT_MEMBERS: [CallbackQueryHandler(step_toggle_member)],
            SELECT_DATE:    [CallbackQueryHandler(step_select_date)],
            SELECT_HOUR:    [CallbackQueryHandler(step_select_hour)],
            SELECT_MINUTE:  [CallbackQueryHandler(step_select_minute)],
            CONFIRM_RUN:    [CallbackQueryHandler(step_confirm_run)],
        },
        fallbacks=[
            CallbackQueryHandler(createrun_cancel, pattern="^cx$"),
            CommandHandler("cancel", createrun_cancel),
        ],
        conversation_timeout=CONV_TIMEOUT,
    )

    editrun_conv = ConversationHandler(
        entry_points=[CommandHandler("editrun", editrun_start)],
        states={
            EDIT_CHOOSE:  [CallbackQueryHandler(edit_choose)],
            EDIT_DATE:    [CallbackQueryHandler(edit_select_date)],
            EDIT_HOUR:    [CallbackQueryHandler(edit_select_hour)],
            EDIT_MINUTE:  [CallbackQueryHandler(edit_select_minute)],
            EDIT_MEMBERS: [CallbackQueryHandler(edit_toggle_member)],
        },
        fallbacks=[
            CallbackQueryHandler(editrun_cancel, pattern="^cx$"),
            CommandHandler("cancel", editrun_cancel),
        ],
        conversation_timeout=CONV_TIMEOUT,
    )

    createteam_conv = ConversationHandler(
        entry_points=[CommandHandler("createteam", createteam_start)],
        states={
            TEAM_MEMBERS: [CallbackQueryHandler(team_toggle_member)],
            TEAM_CONFIRM: [CallbackQueryHandler(team_confirm)],
        },
        fallbacks=[
            CallbackQueryHandler(createteam_cancel, pattern="^cx$"),
            CommandHandler("cancel", createteam_cancel),
        ],
        conversation_timeout=CONV_TIMEOUT,
    )

    editteam_conv = ConversationHandler(
        entry_points=[CommandHandler("editteam", editteam_start)],
        states={
            ETEAM_CHOOSE:  [CallbackQueryHandler(eteam_choose)],
            ETEAM_MEMBERS: [CallbackQueryHandler(eteam_toggle_member)],
        },
        fallbacks=[
            CallbackQueryHandler(editteam_cancel, pattern="^cx$"),
            CommandHandler("cancel", editteam_cancel),
        ],
        conversation_timeout=CONV_TIMEOUT,
    )

    app.add_handler(createrun_conv)
    app.add_handler(editrun_conv)
    app.add_handler(createteam_conv)
    app.add_handler(editteam_conv)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("chars", cmd_chars))
    app.add_handler(CommandHandler("allchars", cmd_allchars))
    app.add_handler(CommandHandler("runs", cmd_runs))
    app.add_handler(CommandHandler("myruns", cmd_myruns))
    app.add_handler(CommandHandler("cancelrun", cmd_cancelrun))
    app.add_handler(CommandHandler("resendrun", cmd_resendrun))
    app.add_handler(CommandHandler("teams", cmd_teams))
    app.add_handler(CommandHandler("deleteteam", cmd_deleteteam))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("linkdiscord", cmd_linkdiscord))
    app.add_handler(CommandHandler("linkstatus", cmd_linkstatus))

    app.add_handler(CallbackQueryHandler(rsvp_callback, pattern=r"^rsvp_(accept|decline)_\d+$"))

    # APScheduler: reminders every 5 min, expiry check every 30 min
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: asyncio.ensure_future(_job_send_reminders(app)),
        CronTrigger(minute="*/5"),
        id="reminders",
        replace_existing=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        lambda: asyncio.ensure_future(_job_expire_pending_runs(app)),
        CronTrigger(minute="*/30"),
        id="expire_runs",
        replace_existing=True,
        misfire_grace_time=120,
    )
    scheduler.start()

    log.info("🍄 Telegram bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
