"""
MapleStory Guild Boss Scheduler — Discord Bot
- Account linking with Telegram (one-time code)
- Auto-register on first command
- Autocomplete on cancelrun/editrun
- /quickrun command
- Full back navigation
- Progress bar
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import List

import discord
from discord import app_commands
from discord.ext import tasks

import db

# ── Config ────────────────────────────────────────────────────────────────────

DISCORD_TOKEN    = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
DISCORD_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
RUNS_CHANNEL_ID  = int(os.environ.get("RUNS_CHANNEL_ID", "0"))

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

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

def progress_bar(step, total=6):
    return f"`{'█' * step}{'░' * (total - step)}` Step {step}/{total}"

def auto_register(user: discord.User):
    """Auto-register a Discord user on first interaction."""
    db.upsert_discord_user(user.id, user.name)

def fmt_run_embed(run, members=None):
    icon      = diff_icon(run["difficulty"])
    sgt       = get_run_dt(run) + timedelta(hours=8)
    time_str  = sgt.strftime("%d/%m/%Y %H:%M SGT")
    color_map = {"confirmed": discord.Color.green(), "pending": discord.Color.orange(), "cancelled": discord.Color.red()}
    embed = discord.Embed(
        title=f"⚔️ Run #{run['id']} — {icon} {run['boss_name']} {run['difficulty']}",
        color=color_map.get(run["status"], discord.Color.blurple())
    )
    embed.add_field(name="📅 Date & Time", value=time_str, inline=True)
    embed.add_field(name="👑 Leader",      value=f"@{run['leader_username']}", inline=True)
    status_map = {"confirmed": "✅ CONFIRMED", "pending": "⏳ PENDING", "cancelled": "❌ CANCELLED"}
    embed.add_field(name="📋 Status", value=status_map.get(run["status"], run["status"].upper()), inline=True)
    if members:
        total    = len(members)
        accepted = sum(1 for m in members if m["accepted"] == 1)
        lines    = []
        for m in members:
            icon_m = {1: "✅", -1: "❌", 0: "⏳"}[m["accepted"]]
            line   = f"{icon_m} **{m['ign']}**"
            if m.get("discord_id"): line += f" (<@{m['discord_id']}>)"
            lines.append(line)
        embed.add_field(name=f"👥 Party ({accepted}/{total})", value="\n".join(lines) or "None", inline=False)
    return embed

def fmt_runs_grouped_embeds(runs):
    pending   = [r for r in runs if r["status"] == "pending"]
    confirmed = [r for r in runs if r["status"] == "confirmed"]
    return [fmt_run_embed(r, db.get_run_members_discord(r["id"])) for r in confirmed + pending]

async def _post_to_runs_channel(bot, content, embed=None, view=None):
    if RUNS_CHANNEL_ID:
        ch = bot.get_channel(RUNS_CHANNEL_ID)
        if ch:
            try:
                kwargs = {"content": content}
                if embed: kwargs["embed"] = embed
                if view:  kwargs["view"]  = view
                return await ch.send(**kwargs)
            except discord.Forbidden:
                log.warning("Missing permissions in runs channel")
            except Exception as e:
                log.warning(f"Could not post to runs channel: {e}")
    return None

async def _update_run_message(bot, run, embed, view=discord.utils.MISSING, content=None):
    if run.get("discord_message_id") and run.get("discord_channel_id"):
        try:
            ch  = bot.get_channel(run["discord_channel_id"])
            msg = await ch.fetch_message(run["discord_message_id"])
            kwargs = {"embed": embed}
            if view is not discord.utils.MISSING: kwargs["view"]    = view
            if content:                            kwargs["content"] = content
            await msg.edit(**kwargs)
        except Exception as e:
            log.warning(f"Could not update run message: {e}")

# ── Reusable buttons ──────────────────────────────────────────────────────────

class BackButton(discord.ui.Button):
    def __init__(self, callback_fn, row=1):
        super().__init__(label="◀ Back", style=discord.ButtonStyle.secondary, row=row)
        self._cb = callback_fn

    async def callback(self, interaction: discord.Interaction):
        rd = getattr(self.view, "run_data", {})
        if rd.get("creator_id") and interaction.user.id != rd["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        await self._cb(interaction)

class CancelButton(discord.ui.Button):
    def __init__(self, row=1):
        super().__init__(label="❌ Cancel", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)


async def _notify_via_telegram(run_id, members, run, data):
    """Send Telegram DM invites to ALL members who have a Telegram account.
    Works for both linked and unlinked members."""
    import httpx
    tg_token = os.environ.get("BOT_TOKEN")
    if not tg_token:
        return [], []

    sgt          = get_run_dt(run) + timedelta(hours=8)
    time_str     = sgt.strftime("%d/%m/%Y %H:%M SGT")
    reminder_str = REMINDER_MAP.get(data.get("reminder_mins", 0), "No reminder")

    # Build accept/decline buttons for Telegram
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    invite_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept",  callback_data=f"rsvp_accept_{run_id}"),
        InlineKeyboardButton("❌ Decline", callback_data=f"rsvp_decline_{run_id}"),
    ]])

    notified = []
    skipped  = []

    for m in members:
        # Get the character to find telegram_id
        char = db.get_character_by_id(m["character_id"] if "character_id" in m else m["id"])
        if not char:
            continue
        tg_id = char.get("telegram_id")
        if not tg_id or tg_id < 0:
            # Discord-only user, no Telegram
            skipped.append(m["ign"])
            continue

        # Build full party list for context
        all_members = db.get_run_members_discord(run_id)
        party_lines = "\n".join(
            f"  {'✅' if mbr['accepted']==1 else '⏳' if mbr['accepted']==0 else '❌'} {mbr['ign']}"
            for mbr in all_members
        )

        invite_text = (
            f"📨 You've been invited to a boss run (via Discord)!\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {time_str}\n"
            f"⏰ Reminder: {reminder_str}\n\n"
            f"Your character: {m['ign']}\n\n"
            f"👥 Party:\n{party_lines}\n\n"
            f"Tap the buttons below to respond:"
        )

        try:
            async with httpx.AsyncClient() as client_http:
                resp = await client_http.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={
                        "chat_id": tg_id,
                        "text": invite_text,
                        "reply_markup": {
                            "inline_keyboard": [[
                                {"text": "✅ Accept",  "callback_data": f"rsvp_accept_{run_id}"},
                                {"text": "❌ Decline", "callback_data": f"rsvp_decline_{run_id}"}
                            ]]
                        }
                    }
                )
                if resp.status_code == 200:
                    notified.append(m["ign"])
                    log.info(f"Telegram DM sent to {m['ign']} (tg_id:{tg_id})")
                else:
                    log.warning(f"Telegram notify failed for {m['ign']}: {resp.text}")
                    skipped.append(m["ign"])
        except Exception as e:
            log.warning(f"Telegram notify error for {m['ign']}: {e}")
            skipped.append(m["ign"])

    return notified, skipped

# ── RSVP View ─────────────────────────────────────────────────────────────────

class RSVPView(discord.ui.View):
    def __init__(self, run_id: int):
        super().__init__(timeout=None)
        self.run_id = run_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, custom_id="rsvp_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_rsvp(interaction, self.run_id, accepted=1)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger, custom_id="rsvp_decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_rsvp(interaction, self.run_id, accepted=-1)

async def handle_rsvp(interaction: discord.Interaction, run_id: int, accepted: int):
    await interaction.response.defer(ephemeral=True)
    auto_register(interaction.user)
    run = db.get_run(run_id)
    if not run:
        await interaction.followup.send(f"⚠️ Run #{run_id} not found.", ephemeral=True); return
    if run["status"] == "cancelled":
        await interaction.followup.send(f"⚠️ Run #{run_id} has been cancelled.", ephemeral=True); return
    rm = db.get_run_member_by_discord(run_id, interaction.user.id)
    if not rm:
        await interaction.followup.send("⚠️ You're not invited to this run.", ephemeral=True); return

    db.set_member_response(run_id, rm["character_id"], accepted)
    members = db.get_run_members_discord(run_id)

    if accepted == 1:
        all_confirmed = db.check_and_confirm_run(run_id)
        if all_confirmed:
            run   = db.get_run(run_id)
            embed = fmt_run_embed(run, members)
            embed.title = f"🎉 Run #{run_id} CONFIRMED! — {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}"
            embed.color = discord.Color.green()
            await _update_run_message(interaction.client, run, embed, view=None)
            mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
            await _post_to_runs_channel(interaction.client, f"🎉 **Run #{run_id} is CONFIRMED!** {mentions}", embed=embed)
            await interaction.followup.send("✅ You accepted! Run is confirmed — everyone notified.", ephemeral=True)
        else:
            pending        = [m for m in members if m["accepted"] == 0]
            accepted_count = sum(1 for m in members if m["accepted"] == 1)
            await _update_run_message(interaction.client, run, fmt_run_embed(run, members))
            await interaction.followup.send(
                f"✅ Accepted! ({accepted_count}/{len(members)})\nStill waiting: {', '.join(m['ign'] for m in pending)}",
                ephemeral=True
            )
    else:
        db.cancel_run(run_id)
        run      = db.get_run(run_id)
        sgt      = get_run_dt(run) + timedelta(hours=8)
        mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
        cancel_text = (
            f"❌ **Run #{run_id} has been cancelled.**\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n"
            f"{rm['ign']} (<@{interaction.user.id}>) declined."
        )
        await _update_run_message(interaction.client, run, fmt_run_embed(run, members), view=None, content=cancel_text)
        await _post_to_runs_channel(interaction.client, f"{cancel_text}\n{mentions}")
        await interaction.followup.send(f"❌ You declined. Run #{run_id} cancelled.", ephemeral=True)


class DatePickerPromptView(discord.ui.View):
    """Shown after a warning when we can\'t directly open the modal."""
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="📅 Set Date & Time", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        await interaction.response.send_modal(DateTimeModal(self.run_data))

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)

# ── Date/time modal ────────────────────────────────────────────────────────────

class DateTimeModal(discord.ui.Modal, title="Set Run Date & Time (SGT)"):
    date = discord.ui.TextInput(label="Date (DD/MM/YYYY)", placeholder="28/06/2026", max_length=10)
    time = discord.ui.TextInput(label="Time (HH:MM, 24h SGT)", placeholder="21:00", max_length=5)

    def __init__(self, run_data: dict):
        super().__init__()
        self.run_data = run_data

    async def on_submit(self, interaction: discord.Interaction):
        try:
            naive = datetime.strptime(f"{self.date.value} {self.time.value}", "%d/%m/%Y %H:%M")
        except ValueError:
            await interaction.response.send_message("⚠️ Invalid format. Use DD/MM/YYYY and HH:MM.", ephemeral=True); return
        sgt_tz = timezone(timedelta(hours=8))
        sgt_dt = naive.replace(tzinfo=sgt_tz)
        if sgt_dt <= datetime.now(sgt_tz):
            await interaction.response.send_message("⚠️ That date/time is in the past.", ephemeral=True); return
        self.run_data["run_at_iso"] = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.run_data["time_str"]   = sgt_dt.strftime("%d/%m/%Y %H:%M SGT")
        if self.run_data.get("selected_chars"):
            view = ReminderView(self.run_data)
            await interaction.response.send_message(
                f"{progress_bar(5)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**"
                f" — {self.run_data['time_str']}\n\n⏰ Set a reminder?",
                view=view, ephemeral=True
            )
        else:
            view = MemberSelectView(self.run_data)
            await interaction.response.send_message(
                f"{progress_bar(4)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**"
                f" — {self.run_data['time_str']}\n\n👥 Select party members:",
                view=view, ephemeral=True
            )

# ── Step 1: Boss ──────────────────────────────────────────────────────────────

class BossSelectView(discord.ui.View):
    def __init__(self, boss_map: dict, creator_id: int):
        super().__init__(timeout=300)
        self.run_data   = {"boss_map": boss_map, "creator_id": creator_id}
        options = [discord.SelectOption(label=name, value=name) for name in boss_map]
        select  = discord.ui.Select(placeholder="Choose a boss...", options=options[:25])
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(CancelButton())

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        self.run_data["boss_name"] = interaction.data["values"][0]
        view = DiffSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(2)}\n\nBoss: **{self.run_data['boss_name']}**\n\n🎯 Select difficulty:",
            view=view
        )

# ── Step 2: Difficulty ────────────────────────────────────────────────────────

class DiffSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data
        diffs   = run_data["boss_map"][run_data["boss_name"]]
        options = [discord.SelectOption(label=f"{diff_icon(d)} {d}", value=d) for d in diffs]
        select  = discord.ui.Select(placeholder="Choose difficulty...", options=options)
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(BackButton(self._go_back))
        self.add_item(CancelButton())

    async def _go_back(self, interaction: discord.Interaction):
        view = BossSelectView(self.run_data["boss_map"], self.run_data["creator_id"])
        await interaction.response.edit_message(content=f"{progress_bar(1)}\n\n⚔️ Select a boss:", view=view)

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        self.run_data["difficulty"] = interaction.data["values"][0]
        teams = db.get_all_teams()
        if teams:
            view = MethodSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 How would you like to add members?",
                view=view
            )
        else:
            view = MemberSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 Select party members:",
                view=view
            )

# ── Step 3: Method ────────────────────────────────────────────────────────────

class MethodSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="👥 Load from Team", style=discord.ButtonStyle.primary, row=0)
    async def load_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        teams   = db.get_all_teams()
        options = [
            discord.SelectOption(
                label=t["name"],
                value=str(t["id"]),
                description=(", ".join(m["ign"] for m in db.get_team_members(t["id"])))[:100]
            )
            for t in teams
        ]
        select = discord.ui.Select(placeholder="Choose a team...", options=options[:25])
        view   = discord.ui.View(timeout=300)
        run_data = self.run_data

        async def on_team_select(inter: discord.Interaction):
            if inter.user.id != run_data["creator_id"]:
                await inter.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
            team_id = int(inter.data["values"][0])
            members = db.get_team_members(team_id)
            run_data["selected_chars"] = [m["id"] for m in members]
            await inter.response.send_modal(DateTimeModal(run_data))

        select.callback = on_team_select
        view.add_item(select)
        view.add_item(BackButton(self._go_back))
        view.add_item(CancelButton())
        await interaction.response.edit_message(
            content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n📋 Select a preset team:",
            view=view
        )

    @discord.ui.button(label="👤 Select Individually", style=discord.ButtonStyle.secondary, row=0)
    async def select_individual(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        view = MemberSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 Select party members:",
            view=view
        )

    async def _go_back(self, interaction: discord.Interaction):
        view = DiffSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(2)}\n\nBoss: **{self.run_data['boss_name']}**\n\n🎯 Select difficulty:",
            view=view
        )

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        await self._go_back(interaction)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)

# ── Step 3b: Individual member picker ─────────────────────────────────────────

class MemberSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data
        all_chars = db.get_all_characters_discord()
        options   = [
            discord.SelectOption(
                label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""),
                value=str(ch["id"]),
                description=ch["class"] or "No class"
            )
            for ch in all_chars[:25]
        ]
        select = discord.ui.Select(placeholder="Select party members...", min_values=1, max_values=min(len(options), 25), options=options)
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(BackButton(self._go_back))
        self.add_item(CancelButton())

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        selected_ids = [int(v) for v in interaction.data["values"]]
        self.run_data["selected_chars"] = selected_ids

        # Check for unlinked members and warn creator
        all_chars = db.get_all_characters_discord()
        char_map  = {ch["id"]: ch for ch in all_chars}
        unlinked  = [char_map[cid]["ign"] for cid in selected_ids if cid in char_map and not char_map[cid].get("discord_id")]
        if unlinked:
            warning = f"⚠️ **Heads up:** {', '.join(unlinked)} have no Discord account linked — they won't see the invite on Discord. They can still be notified via Telegram if accounts are linked."
            await interaction.response.send_message(warning, ephemeral=True)
            # Still open the modal after a short warning
            await interaction.followup.send("Proceeding to date/time selection...", ephemeral=True)
            # Can't send modal after send_message, so edit to show date picker via view
            view = DatePickerPromptView(self.run_data)
            await interaction.followup.send(
                f"{progress_bar(4)}\n\nSet the date and time for this run:",
                view=view, ephemeral=True
            )
        else:
            await interaction.response.send_modal(DateTimeModal(self.run_data))

    async def _go_back(self, interaction: discord.Interaction):
        teams = db.get_all_teams()
        if teams:
            view = MethodSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 How would you like to add members?",
                view=view
            )
        else:
            view = DiffSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(2)}\n\nBoss: **{self.run_data['boss_name']}**\n\n🎯 Select difficulty:",
                view=view
            )

# ── Step 5: Reminder ──────────────────────────────────────────────────────────

REMINDER_MAP = {60: "1 hour before", 30: "30 mins before", 15: "15 mins before", 0: "No reminder"}

class ReminderView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="⏰ 1 hour before",  style=discord.ButtonStyle.secondary, row=0)
    async def r60(self, i, b): await self._set(i, 60)
    @discord.ui.button(label="⏰ 30 mins before", style=discord.ButtonStyle.secondary, row=0)
    async def r30(self, i, b): await self._set(i, 30)
    @discord.ui.button(label="⏰ 15 mins before", style=discord.ButtonStyle.secondary, row=1)
    async def r15(self, i, b): await self._set(i, 15)
    @discord.ui.button(label="🚫 No reminder",    style=discord.ButtonStyle.secondary, row=1)
    async def r0(self, i, b):  await self._set(i, 0)

    async def _set(self, interaction: discord.Interaction, mins: int):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        self.run_data["reminder_mins"] = mins
        chars        = [db.get_character_by_id(cid) for cid in self.run_data["selected_chars"]]
        member_names = ", ".join(ch["ign"] for ch in chars if ch)
        view         = ConfirmRunView(self.run_data)
        await interaction.response.edit_message(
            content=(
                f"{progress_bar(6)}\n\n📋 **Run Summary — Please confirm:**\n\n"
                f"⚔️ {diff_icon(self.run_data['difficulty'])} **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n"
                f"📅 {self.run_data['time_str']}\n"
                f"⏰ Reminder: {REMINDER_MAP[mins]}\n\n"
                f"👥 Party ({len(chars)}): {member_names}"
            ),
            view=view
        )

    @discord.ui.button(label="◀ Back (change date/time)", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        self.run_data.pop("run_at_iso", None)
        self.run_data.pop("time_str", None)
        view = MemberSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\nAdjust members or proceed to re-enter date:",
            view=view
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)

# ── Step 6: Confirm ───────────────────────────────────────────────────────────

class ConfirmRunView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="✅ Confirm & Post", style=discord.ButtonStyle.success, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        data   = self.run_data
        boss   = db.find_boss(data["boss_name"], data["difficulty"])
        run_id = db.create_run_discord(boss["id"], interaction.user.id, data["run_at_iso"])
        for char_id in data["selected_chars"]:
            db.add_run_member(run_id, char_id)
        if data.get("reminder_mins", 0) > 0:
            sgt_dt    = datetime.fromisoformat(data["run_at_iso"]).replace(tzinfo=timezone.utc)
            remind_dt = sgt_dt - timedelta(minutes=data["reminder_mins"])
            if remind_dt > datetime.now(timezone.utc):
                db.set_run_reminder(run_id, remind_dt.strftime("%Y-%m-%d %H:%M:%S"))
        run      = db.get_run(run_id)
        members  = db.get_run_members_discord(run_id)
        embed    = fmt_run_embed(run, members)
        view     = RSVPView(run_id)

        # Split members into Discord-linked and unlinked
        linked   = [m for m in members if m.get("discord_id")]
        unlinked = [m for m in members if not m.get("discord_id")]
        mentions = " ".join(f"<@{m['discord_id']}>" for m in linked)

        ch_target = interaction.client.get_channel(RUNS_CHANNEL_ID) if RUNS_CHANNEL_ID else interaction.channel
        if ch_target:
            try:
                msg = await ch_target.send(
                    content=f"📢 **New Boss Run!** {mentions}\n⏰ Reminder: {REMINDER_MAP.get(data.get('reminder_mins',0),'No reminder')}\nAccept or decline below:",
                    embed=embed, view=view
                )
                db.set_run_discord_message(run_id, msg.id, ch_target.id)

                # Notify ALL members via Telegram too
                tg_notified, tg_skipped = await _notify_via_telegram(run_id, members, run, data)

                # Build summary message
                summary = f"✅ **Run #{run_id} created and posted!** Check <#{ch_target.id}>."

                if tg_notified:
                    summary += f"\n📱 Telegram notified: {', '.join(tg_notified)}"

                if unlinked:
                    names = ", ".join(m["ign"] for m in unlinked)
                    summary += (
                        f"\n\n⚠️ **No Discord link:** {names}\n"
                        f"They can only accept/decline via Telegram."
                    )

                if tg_skipped and not tg_notified:
                    summary += f"\n\n💡 Tip: Members can link their Telegram with `/linkaccount` to receive cross-platform invites."

                await interaction.edit_original_response(content=summary, view=None)
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ Bot lacks permission to post in the runs channel.\n"
                    "Go to the channel → Edit Channel → Permissions → add the bot with **Send Messages** + **Embed Links**.",
                    ephemeral=True
                )

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        view = ReminderView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(5)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}** — {self.run_data['time_str']}\n\n⏰ Set a reminder?",
            view=view
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=0)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)

# ── Bot client ────────────────────────────────────────────────────────────────

class MapleBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        guild = discord.Object(id=DISCORD_GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
        self.add_view(RSVPView(run_id=0))
        scheduler_loop.start()

    async def on_ready(self):
        log.info(f"🍄 Discord bot ready as {self.user}")
        await self.change_presence(activity=discord.Game(name="/help for commands"))

client = MapleBot()

# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def autocomplete_my_runs(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[int]]:
    runs = db.get_user_runs_discord(interaction.user.id)
    choices = []
    for r in runs:
        sgt  = get_run_dt(r) + timedelta(hours=8)
        label = f"#{r['id']} {r['boss_name']} {r['difficulty']} — {sgt.strftime('%d/%m %H:%M')}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return choices[:25]

async def autocomplete_active_runs(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[int]]:
    runs = db.get_active_runs_discord()
    choices = []
    for r in runs:
        sgt   = get_run_dt(r) + timedelta(hours=8)
        label = f"#{r['id']} {r['boss_name']} {r['difficulty']} — {sgt.strftime('%d/%m %H:%M')}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return choices[:25]

# ── Slash Commands ────────────────────────────────────────────────────────────

@client.tree.command(name="start", description="Register yourself with the bot")
async def slash_start(interaction: discord.Interaction):
    auto_register(interaction.user)
    await interaction.response.send_message(
        "🍄 **MapleStory Boss Scheduler**\n\nYou're registered!\n\n"
        "`/register` — add your character\n"
        "`/linkaccount` — link your Telegram account\n"
        "`/bosses` — see available bosses\n"
        "`/createrun` — create a boss run\n"
        "`/runs` — see upcoming runs\n"
        "`/help` — all commands",
        ephemeral=True
    )

@client.tree.command(name="help", description="Show all commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(
        "📋 **All Commands**\n\n"
        "**Account**\n"
        "`/start` `/register` `/chars` `/allchars`\n"
        "`/linkaccount <code>` — link to Telegram account\n"
        "`/linkstatus` — check link status\n\n"
        "**Preset Teams**\n"
        "`/createteam` `/teams` `/editteam` `/deleteteam`\n\n"
        "**Scheduling**\n"
        "`/createrun` — full guided flow with back navigation\n"
        "`/quickrun <boss> <difficulty>` — skip to members\n"
        "`/cancelrun` `/resendrun` `/myruns` `/runs`\n\n"
        "📅 All times SGT (UTC+8)",
        ephemeral=True
    )

@client.tree.command(name="linkaccount", description="Link your Discord to your Telegram account")
@app_commands.describe(code="The 8-character code from /linkdiscord on Telegram")
async def slash_linkaccount(interaction: discord.Interaction, code: str):
    auto_register(interaction.user)
    telegram_id, err = db.consume_link_code(code, interaction.user.id, interaction.user.name)
    if err:
        await interaction.response.send_message(f"⚠️ {err}", ephemeral=True); return
    chars = db.get_characters_discord(interaction.user.id)
    await interaction.response.send_message(
        f"✅ **Accounts linked successfully!**\n\n"
        f"Your Discord is now linked to your Telegram account.\n"
        f"Characters shared: {len(chars)}\n\n"
        f"You can now accept/decline runs on both platforms.",
        ephemeral=True
    )

@client.tree.command(name="linkstatus", description="Check your account link status")
async def slash_linkstatus(interaction: discord.Interaction):
    auto_register(interaction.user)
    linked = db.get_discord_link_status(interaction.user.id)
    if linked:
        await interaction.response.send_message(
            f"✅ Linked to Telegram account @{linked['tg_username']}\n"
            f"Characters are shared across both platforms.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            "❌ No Telegram account linked.\n\n"
            "To link:\n"
            "1. Open your Telegram bot\n"
            "2. Send `/linkdiscord`\n"
            "3. Copy the 8-character code\n"
            "4. Use `/linkaccount <code>` here",
            ephemeral=True
        )

@client.tree.command(name="register", description="Register a MapleStory character")
@app_commands.describe(ign="Your in-game name", cls="Your class", level="Your level")
async def slash_register(interaction: discord.Interaction, ign: str, cls: str = None, level: int = None):
    auto_register(interaction.user)
    ok = db.add_character_discord(interaction.user.id, ign, cls, level)
    if ok:
        parts = [f"✅ Registered **{ign}**"]
        if cls:   parts.append(f"Class: {cls}")
        if level: parts.append(f"Level: {level}")
        await interaction.response.send_message(" | ".join(parts), ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ IGN **{ign}** is already registered.", ephemeral=True)

@client.tree.command(name="chars", description="List your registered characters")
async def slash_chars(interaction: discord.Interaction):
    auto_register(interaction.user)
    chars = db.get_characters_discord(interaction.user.id)
    if not chars:
        await interaction.response.send_message("No characters yet. Use `/register`.", ephemeral=True); return
    lines = ["👤 **Your Characters**\n"]
    for ch in chars:
        line = f"• **{ch['ign']}**"
        if ch["class"]: line += f" — {ch['class']}"
        if ch["level"]: line += f" Lv.{ch['level']}"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@client.tree.command(name="allchars", description="List all registered guild characters")
async def slash_allchars(interaction: discord.Interaction):
    chars = db.get_all_characters_discord()
    if not chars:
        await interaction.response.send_message("No characters registered yet.", ephemeral=True); return
    lines = ["🌍 **All Guild Characters**\n"]
    for ch in chars:
        line = f"• **{ch['ign']}**"
        if ch["class"]:          line += f" — {ch['class']}"
        if ch["level"]:          line += f" Lv.{ch['level']}"
        if ch.get("discord_id"): line += f" (<@{ch['discord_id']}>)"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@client.tree.command(name="bosses", description="List all available bosses")
async def slash_bosses(interaction: discord.Interaction):
    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    lines = ["⚔️ **Available Bosses**\n"]
    for name, diffs in grouped.items():
        icons = "  ".join(f"{diff_icon(d)} {d}" for d in diffs)
        lines.append(f"**{name}**\n  {icons}\n")
    lines.append("🟢Easy 🔵Normal 🟠Hard 🔴Chaos ⚫Extreme")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@client.tree.command(name="createrun", description="Create a boss run — guided flow with back navigation")
async def slash_createrun(interaction: discord.Interaction):
    auto_register(interaction.user)
    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    view = BossSelectView(grouped, interaction.user.id)
    await interaction.response.send_message(
        f"{progress_bar(1)}\n\n⚔️ **Create a Boss Run**\n\nSelect a boss:",
        view=view, ephemeral=True
    )

@client.tree.command(name="quickrun", description="Skip to member selection for a known boss")
@app_commands.describe(boss="Boss name", difficulty="Difficulty")
async def slash_quickrun(interaction: discord.Interaction, boss: str, difficulty: str):
    auto_register(interaction.user)
    boss_obj = db.find_boss(boss, difficulty)
    if not boss_obj:
        await interaction.response.send_message(
            f"⚠️ **{boss} {difficulty}** not found. Use `/bosses` to see the list.",
            ephemeral=True
        ); return
    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    run_data = {"boss_name": boss, "difficulty": difficulty, "boss_map": grouped, "creator_id": interaction.user.id}
    teams    = db.get_all_teams()
    if teams:
        view = MethodSelectView(run_data)
        await interaction.response.send_message(
            f"{progress_bar(3)}\n\n⚔️ **{boss} {difficulty}**\n\n👥 How would you like to add members?",
            view=view, ephemeral=True
        )
    else:
        view = MemberSelectView(run_data)
        await interaction.response.send_message(
            f"{progress_bar(3)}\n\n⚔️ **{boss} {difficulty}**\n\n👥 Select party members:",
            view=view, ephemeral=True
        )

@client.tree.command(name="cancelrun", description="Cancel a boss run")
@app_commands.describe(run_id="The run to cancel")
@app_commands.autocomplete(run_id=autocomplete_my_runs)
async def slash_cancelrun(interaction: discord.Interaction, run_id: int):
    auto_register(interaction.user)
    run = db.get_run(run_id)
    if not run:
        await interaction.response.send_message(f"⚠️ Run #{run_id} not found.", ephemeral=True); return
    if run["leader_id"] != -interaction.user.id:
        await interaction.response.send_message("⚠️ Only the run creator can cancel.", ephemeral=True); return
    if run["status"] == "cancelled":
        await interaction.response.send_message("ℹ️ Already cancelled.", ephemeral=True); return
    db.cancel_run(run_id)
    members  = db.get_run_members_discord(run_id)
    sgt      = get_run_dt(run) + timedelta(hours=8)
    mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
    cancel_text = (
        f"❌ **Run #{run_id} cancelled.**\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\nCancelled by <@{interaction.user.id}>."
    )
    await _update_run_message(client, run, fmt_run_embed(run, members), view=None, content=cancel_text)
    await _post_to_runs_channel(client, f"{cancel_text}\n{mentions}")
    await interaction.response.send_message(f"🗑️ Run #{run_id} cancelled.", ephemeral=True)

@client.tree.command(name="resendrun", description="Repost run invite for pending members")
@app_commands.describe(run_id="The run to resend")
@app_commands.autocomplete(run_id=autocomplete_my_runs)
async def slash_resendrun(interaction: discord.Interaction, run_id: int):
    run = db.get_run(run_id)
    if not run:
        await interaction.response.send_message(f"⚠️ Run #{run_id} not found.", ephemeral=True); return
    if run["leader_id"] != -interaction.user.id:
        await interaction.response.send_message("⚠️ Only the run leader can resend.", ephemeral=True); return
    members = db.get_run_members_discord(run_id)
    pending = [m for m in members if m["accepted"] == 0]
    if not pending:
        await interaction.response.send_message("ℹ️ No pending members.", ephemeral=True); return
    mentions = " ".join(f"<@{m['discord_id']}>" for m in pending if m.get("discord_id"))
    await _post_to_runs_channel(
        client,
        f"📨 **Reminder — please respond to Run #{run_id}!** {mentions}",
        embed=fmt_run_embed(run, members),
        view=RSVPView(run_id)
    )
    await interaction.response.send_message(f"✅ Resent to {len(pending)} pending member(s).", ephemeral=True)

@client.tree.command(name="myruns", description="See your upcoming run invitations")
async def slash_myruns(interaction: discord.Interaction):
    auto_register(interaction.user)
    runs = db.get_user_runs_discord(interaction.user.id)
    if not runs:
        await interaction.response.send_message("You have no upcoming run invitations.", ephemeral=True); return
    embeds = fmt_runs_grouped_embeds(runs)
    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)

@client.tree.command(name="runs", description="See all upcoming guild runs")
async def slash_runs(interaction: discord.Interaction):
    runs = db.get_active_runs_discord()
    if not runs:
        await interaction.response.send_message("No upcoming runs scheduled.", ephemeral=True); return
    embeds = fmt_runs_grouped_embeds(runs)
    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)

@client.tree.command(name="createteam", description="Create a preset party team")
@app_commands.describe(name="Team name")
async def slash_createteam(interaction: discord.Interaction, name: str):
    auto_register(interaction.user)
    all_chars = db.get_all_characters_discord()
    if not all_chars:
        await interaction.response.send_message("No characters registered yet.", ephemeral=True); return
    options = [
        discord.SelectOption(label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""), value=str(ch["id"]), description=ch["class"] or "No class")
        for ch in all_chars[:25]
    ]
    select = discord.ui.Select(placeholder="Select team members...", min_values=1, max_values=min(len(options), 25), options=options)
    view   = discord.ui.View(timeout=300)

    async def on_select(inter: discord.Interaction):
        selected_ids = [int(v) for v in inter.data["values"]]
        team_id, err = db.create_team(name, -interaction.user.id, selected_ids)
        if err:
            await inter.response.edit_message(content=f"⚠️ {err}", view=None)
        else:
            chars = [db.get_character_by_id(cid) for cid in selected_ids]
            await inter.response.edit_message(content=f"✅ Team **{name}** saved! Members: {', '.join(ch['ign'] for ch in chars if ch)}", view=None)

    select.callback = on_select
    view.add_item(select)
    await interaction.response.send_message(f"👥 **Create Team: {name}**\n\nSelect members:", view=view, ephemeral=True)

@client.tree.command(name="teams", description="List all preset teams")
async def slash_teams(interaction: discord.Interaction):
    teams = db.get_all_teams()
    if not teams:
        await interaction.response.send_message("No preset teams yet. Use `/createteam`.", ephemeral=True); return
    lines = ["👥 **Preset Teams**\n"]
    for t in teams:
        members = db.get_team_members(t["id"])
        lines.append(f"**{t['name']}** ({len(members)} members)\n  {' · '.join(m['ign'] for m in members)}\n")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@client.tree.command(name="deleteteam", description="Delete a preset team")
@app_commands.describe(name="Team name")
async def slash_deleteteam(interaction: discord.Interaction, name: str):
    team = db.get_team_by_name(name)
    if not team:
        await interaction.response.send_message("⚠️ Team not found. Use `/teams`.", ephemeral=True); return
    db.delete_team(team["id"])
    await interaction.response.send_message(f"🗑️ Team **{name}** deleted.", ephemeral=True)

@client.tree.command(name="editteam", description="Edit members of a preset team")
@app_commands.describe(name="Team name")
async def slash_editteam(interaction: discord.Interaction, name: str):
    team = db.get_team_by_name(name)
    if not team:
        await interaction.response.send_message("⚠️ Team not found. Use `/teams`.", ephemeral=True); return
    current   = db.get_team_members(team["id"])
    cur_ids   = {m["id"] for m in current}
    all_chars = db.get_all_characters_discord()
    options   = [
        discord.SelectOption(label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""), value=str(ch["id"]), description=ch["class"] or "No class", default=ch["id"] in cur_ids)
        for ch in all_chars[:25]
    ]
    select = discord.ui.Select(placeholder="Select members...", min_values=1, max_values=min(len(options), 25), options=options)
    view   = discord.ui.View(timeout=300)

    async def on_select(inter: discord.Interaction):
        selected_ids = [int(v) for v in inter.data["values"]]
        ok, err = db.update_team(team["id"], name, selected_ids)
        if ok:
            chars = [db.get_character_by_id(cid) for cid in selected_ids]
            await inter.response.edit_message(content=f"✅ Team **{name}** updated! Members: {', '.join(ch['ign'] for ch in chars if ch)}", view=None)
        else:
            await inter.response.edit_message(content=f"⚠️ {err}", view=None)

    select.callback = on_select
    view.add_item(select)
    await interaction.response.send_message(f"✏️ **Edit Team: {name}**\n\nCurrent members pre-selected:", view=view, ephemeral=True)

# ── Scheduler ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=15)
async def scheduler_loop():
    await send_reminders()
    await auto_cancel_pending()

async def send_reminders():
    for run in db.get_runs_due_for_reminder_discord():
        members  = db.get_run_members_discord(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
        ch_id    = run.get("discord_channel_id") or RUNS_CHANNEL_ID
        if ch_id:
            ch = client.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(
                        f"⏰ **Boss Run Reminder!** {mentions}\n"
                        f"⚔️ {diff_icon(run['difficulty'])} **{run['boss_name']} {run['difficulty']}**\n"
                        f"📅 Starting at **{sgt.strftime('%d/%m/%Y %H:%M SGT')}**"
                    )
                except Exception as e:
                    log.warning(f"Reminder failed: {e}")

async def auto_cancel_pending():
    for run in db.get_expired_pending_runs(hours=12):
        db.cancel_run(run["id"])
        members  = db.get_run_members_discord(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        pending  = [m for m in members if m["accepted"] == 0]
        mentions = " ".join(f"<@{m['discord_id']}>" for m in pending if m.get("discord_id"))
        msg = (
            f"⏰ **Run #{run['id']} auto-cancelled** — no response within 12 hours.\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n"
            f"No response from: {', '.join(m['ign'] for m in pending)}"
        )
        ch_id = run.get("discord_channel_id") or RUNS_CHANNEL_ID
        if ch_id:
            ch = client.get_channel(ch_id)
            if ch:
                try:
                    await ch.send(f"{msg}\n{mentions}")
                except Exception as e:
                    log.warning(f"Auto-cancel failed: {e}")
        log.info(f"Auto-cancelled run #{run['id']}")

@scheduler_loop.before_loop
async def before_scheduler():
    await client.wait_until_ready()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()
    log.info("Database initialised.")
    if DISCORD_TOKEN == "YOUR_DISCORD_TOKEN_HERE":
        log.error("❌ Set DISCORD_TOKEN."); return
    if not DISCORD_GUILD_ID:
        log.error("❌ Set DISCORD_GUILD_ID."); return
    log.info("🍄 Discord bot starting...")
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
