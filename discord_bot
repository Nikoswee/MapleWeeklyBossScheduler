"""
MapleStory Guild Boss Scheduler — Discord Bot
Slash commands + buttons, shares PostgreSQL DB with Telegram bot.
"""

import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
import calendar

import discord
from discord import app_commands
from discord.ext import tasks

import db

# ── Config ────────────────────────────────────────────────────────────────────

DISCORD_TOKEN      = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
DISCORD_GUILD_ID   = int(os.environ.get("DISCORD_GUILD_ID", "0"))   # Your server ID
RUNS_CHANNEL_ID    = int(os.environ.get("RUNS_CHANNEL_ID", "0"))    # Channel for run announcements

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
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

def fmt_run_embed(run, members=None):
    """Format a run as a Discord embed."""
    icon     = diff_icon(run["difficulty"])
    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")

    status_map = {"confirmed": "✅ CONFIRMED", "pending": "⏳ PENDING", "cancelled": "❌ CANCELLED"}
    status     = status_map.get(run["status"], run["status"].upper())
    color_map  = {"confirmed": discord.Color.green(), "pending": discord.Color.orange(), "cancelled": discord.Color.red()}
    color      = color_map.get(run["status"], discord.Color.blurple())

    embed = discord.Embed(
        title=f"⚔️ Run #{run['id']} — {icon} {run['boss_name']} {run['difficulty']}",
        color=color
    )
    embed.add_field(name="📅 Date & Time", value=time_str, inline=True)
    embed.add_field(name="📋 Status",      value=status,   inline=True)
    embed.add_field(name="👑 Leader",      value=f"@{run['leader_username']}", inline=True)

    if members:
        total    = len(members)
        accepted = sum(1 for m in members if m["accepted"] == 1)
        waiting  = [m for m in members if m["accepted"] != 1]

        party_lines = []
        for m in members:
            icon_m = {1: "✅", -1: "❌", 0: "⏳"}[m["accepted"]]
            line   = f"{icon_m} **{m['ign']}**"
            if m.get("discord_id"):
                line += f" (<@{m['discord_id']}>)"
            party_lines.append(line)

        embed.add_field(
            name=f"👥 Party ({accepted}/{total} accepted)",
            value="\n".join(party_lines) or "None",
            inline=False
        )

    return embed

def fmt_runs_grouped_embed(runs):
    """Create embeds grouped by status."""
    pending   = [r for r in runs if r["status"] == "pending"]
    confirmed = [r for r in runs if r["status"] == "confirmed"]
    embeds    = []

    if confirmed:
        for run in confirmed:
            members = db.get_run_members_discord(run["id"])
            embeds.append(fmt_run_embed(run, members))

    if pending:
        for run in pending:
            members = db.get_run_members_discord(run["id"])
            embeds.append(fmt_run_embed(run, members))

    return embeds

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

    db.upsert_discord_user(interaction.user.id, interaction.user.name)
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
            run = db.get_run(run_id)
            embed = fmt_run_embed(run, members)
            embed.title = f"🎉 Run #{run_id} CONFIRMED! — {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}"

            # Update the original message
            if run.get("discord_message_id") and run.get("discord_channel_id"):
                try:
                    ch  = interaction.client.get_channel(run["discord_channel_id"])
                    msg = await ch.fetch_message(run["discord_message_id"])
                    await msg.edit(embed=embed, view=None)
                except Exception as e:
                    log.warning(f"Could not update run message: {e}")

            # Post confirmation
            if RUNS_CHANNEL_ID:
                ch = interaction.client.get_channel(RUNS_CHANNEL_ID)
                if ch:
                    # Mention all members
                    mentions = " ".join(
                        f"<@{m['discord_id']}>" for m in members if m.get("discord_id")
                    )
                    await ch.send(
                        content=f"🎉 **Run #{run_id} is CONFIRMED!** {mentions}",
                        embed=embed
                    )
            await interaction.followup.send(f"✅ You accepted Run #{run_id}! All members confirmed.", ephemeral=True)
        else:
            pending  = [m for m in members if m["accepted"] == 0]
            total    = len(members)
            accepted_count = sum(1 for m in members if m["accepted"] == 1)

            # Update the original message embed
            if run.get("discord_message_id") and run.get("discord_channel_id"):
                try:
                    ch  = interaction.client.get_channel(run["discord_channel_id"])
                    msg = await ch.fetch_message(run["discord_message_id"])
                    await msg.edit(embed=fmt_run_embed(run, members))
                except Exception as e:
                    log.warning(f"Could not update run message: {e}")

            await interaction.followup.send(
                f"✅ Accepted! ({accepted_count}/{total} so far)\n"
                f"Still waiting on: {', '.join(m['ign'] for m in pending)}",
                ephemeral=True
            )
    else:
        # Decline — auto-cancel run
        db.cancel_run(run_id)
        run     = db.get_run(run_id)
        sgt     = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")

        cancel_msg = (
            f"❌ **Run #{run_id} has been cancelled.**\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {time_str}\n\n"
            f"{rm['ign']} (<@{interaction.user.id}>) declined the invite."
        )

        # Update original message
        if run.get("discord_message_id") and run.get("discord_channel_id"):
            try:
                ch  = interaction.client.get_channel(run["discord_channel_id"])
                msg = await ch.fetch_message(run["discord_message_id"])
                cancelled_embed = fmt_run_embed(run, members)
                await msg.edit(embed=cancelled_embed, view=None, content=cancel_msg)
            except Exception as e:
                log.warning(f"Could not update run message: {e}")

        # Post cancellation notice
        if RUNS_CHANNEL_ID:
            ch = interaction.client.get_channel(RUNS_CHANNEL_ID)
            if ch:
                mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
                await ch.send(content=f"{cancel_msg}\n{mentions}")

        await interaction.followup.send(f"❌ You declined. Run #{run_id} has been cancelled.", ephemeral=True)

# ── CreateRun Modal ───────────────────────────────────────────────────────────

class DateTimeModal(discord.ui.Modal, title="Set Run Date & Time (SGT)"):
    date  = discord.ui.TextInput(label="Date (DD/MM/YYYY)", placeholder="28/06/2026", max_length=10)
    time  = discord.ui.TextInput(label="Time (HH:MM, 24h SGT)", placeholder="21:00", max_length=5)

    def __init__(self, run_data: dict):
        super().__init__()
        self.run_data = run_data

    async def on_submit(self, interaction: discord.Interaction):
        try:
            naive = datetime.strptime(f"{self.date.value} {self.time.value}", "%d/%m/%Y %H:%M")
        except ValueError:
            await interaction.response.send_message(
                "⚠️ Invalid format. Use DD/MM/YYYY and HH:MM (e.g. 28/06/2026 21:00)",
                ephemeral=True
            )
            return

        sgt_tz = timezone(timedelta(hours=8))
        sgt_dt = naive.replace(tzinfo=sgt_tz)
        if sgt_dt <= datetime.now(sgt_tz):
            await interaction.response.send_message("⚠️ That date/time is in the past.", ephemeral=True)
            return

        self.run_data["run_at_iso"] = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.run_data["time_str"]   = sgt_dt.strftime("%d/%m/%Y %H:%M SGT")

        # Show member selection
        view = MemberSelectView(self.run_data)
        await interaction.response.send_message(
            f"⚔️ **Create a Boss Run**\n\n"
            f"Boss: **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n"
            f"Date: **{self.run_data['time_str']}**\n\n"
            f"Select party members:",
            view=view,
            ephemeral=True
        )

class MemberSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data
        all_chars = db.get_all_characters_discord()
        options   = [
            discord.SelectOption(
                label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""),
                value=str(ch["id"]),
                description=ch["class"] or "No class set"
            )
            for ch in all_chars[:25]  # Discord limit: 25 options
        ]
        select = discord.ui.Select(
            placeholder="Select party members...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        selected_ids = [int(v) for v in interaction.data["values"]]
        self.run_data["selected_chars"] = selected_ids

        # Show reminder selection
        view = ReminderSelectView(self.run_data)
        await interaction.response.edit_message(
            content=(
                f"⚔️ **Create a Boss Run**\n\n"
                f"Boss: **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n"
                f"Date: **{self.run_data['time_str']}**\n"
                f"Members: **{len(selected_ids)} selected**\n\n"
                f"Set a reminder?"
            ),
            view=view
        )

class ReminderSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="⏰ 1 hour before",  style=discord.ButtonStyle.secondary)
    async def r60(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._confirm(interaction, 60)

    @discord.ui.button(label="⏰ 30 mins before", style=discord.ButtonStyle.secondary)
    async def r30(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._confirm(interaction, 30)

    @discord.ui.button(label="⏰ 15 mins before", style=discord.ButtonStyle.secondary)
    async def r15(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._confirm(interaction, 15)

    @discord.ui.button(label="🚫 No reminder", style=discord.ButtonStyle.secondary)
    async def r0(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._confirm(interaction, 0)

    async def _confirm(self, interaction: discord.Interaction, mins: int):
        reminder_map = {60: "1 hour before", 30: "30 mins before", 15: "15 mins before", 0: "None"}
        self.run_data["reminder_mins"] = mins
        chars        = [db.get_character_by_id(cid) for cid in self.run_data["selected_chars"]]
        member_names = ", ".join(ch["ign"] for ch in chars if ch)

        view = ConfirmRunView(self.run_data)
        await interaction.response.edit_message(
            content=(
                f"📋 **Run Summary — Please confirm:**\n\n"
                f"⚔️ {diff_icon(self.run_data['difficulty'])} "
                f"**{self.run_data['boss_name']} {self.run_data['difficulty']}**\n"
                f"📅 {self.run_data['time_str']}\n"
                f"⏰ Reminder: {reminder_map[mins]}\n\n"
                f"👥 Party ({len(chars)}): {member_names}\n\n"
                f"Tap **Confirm** to create and post the run."
            ),
            view=view
        )

class ConfirmRunView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="✅ Confirm & Post", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        data    = self.run_data
        boss    = db.find_boss(data["boss_name"], data["difficulty"])
        run_id  = db.create_run_discord(boss["id"], interaction.user.id, data["run_at_iso"])

        for char_id in data["selected_chars"]:
            db.add_run_member(run_id, char_id)

        if data["reminder_mins"] > 0:
            sgt_dt    = datetime.fromisoformat(data["run_at_iso"].replace("Z","")).replace(tzinfo=timezone.utc)
            remind_dt = sgt_dt - timedelta(minutes=data["reminder_mins"])
            if remind_dt > datetime.now(timezone.utc):
                db.set_run_reminder(run_id, remind_dt.strftime("%Y-%m-%d %H:%M:%S"))

        run     = db.get_run(run_id)
        members = db.get_run_members_discord(run_id)
        embed   = fmt_run_embed(run, members)
        view    = RSVPView(run_id)

        # Build mentions
        mentions = " ".join(
            f"<@{m['discord_id']}>" for m in members if m.get("discord_id")
        )
        reminder_map = {60: "1 hour before", 30: "30 mins before", 15: "15 mins before", 0: "None"}

        # Post to runs channel
        if RUNS_CHANNEL_ID:
            ch = interaction.client.get_channel(RUNS_CHANNEL_ID)
            if ch:
                msg = await ch.send(
                    content=(
                        f"📢 **New Boss Run!** {mentions}\n"
                        f"⏰ Reminder: {reminder_map[data['reminder_mins']]}\n"
                        f"Accept or decline below:"
                    ),
                    embed=embed,
                    view=view
                )
                db.set_run_discord_message(run_id, msg.id, ch.id)
        else:
            msg = await interaction.channel.send(
                content=f"📢 **New Boss Run!** {mentions}\nAccept or decline below:",
                embed=embed,
                view=view
            )
            db.set_run_discord_message(run_id, msg.id, interaction.channel.id)

        await interaction.followup.send(f"✅ Run #{run_id} created and posted!", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)

# ── Discord Bot Client ────────────────────────────────────────────────────────

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
        # Re-attach persistent RSVP views on restart
        self.add_view(RSVPView(run_id=0))
        # Start scheduler
        scheduler_loop.start()

    async def on_ready(self):
        log.info(f"🍄 Discord bot ready as {self.user}")
        await self.change_presence(activity=discord.Game(name="/help for commands"))

client = MapleBot()

# ── Slash Commands ────────────────────────────────────────────────────────────

@client.tree.command(name="start", description="Register yourself with the bot")
async def slash_start(interaction: discord.Interaction):
    db.upsert_discord_user(interaction.user.id, interaction.user.name)
    await interaction.response.send_message(
        "🍄 **MapleStory Boss Scheduler**\n\n"
        "You're registered! Here's how to get started:\n\n"
        "`/register <IGN> [Class] [Level]` — add your character\n"
        "`/bosses` — see available bosses\n"
        "`/createrun` — create a boss run\n"
        "`/runs` — see all upcoming runs\n\n"
        "Type `/help` for all commands.",
        ephemeral=True
    )

@client.tree.command(name="help", description="Show all commands")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(
        "📋 **All Commands**\n\n"
        "**Characters**\n"
        "`/register <IGN> [Class] [Level]`\n"
        "`/chars` — your characters\n"
        "`/allchars` — all guild characters\n\n"
        "**Bosses**\n"
        "`/bosses` — boss list\n\n"
        "**Preset Teams**\n"
        "`/createteam <name>` — create a preset team\n"
        "`/teams` — list all teams\n"
        "`/editteam <name>` — edit a team\n"
        "`/deleteteam <name>` — delete a team\n\n"
        "**Scheduling**\n"
        "`/createrun` — create a run (guided)\n"
        "`/cancelrun <run_id>` — cancel a run\n"
        "`/resendrun <run_id>` — resend/repost invite\n"
        "`/myruns` — your invitations\n"
        "`/runs` — all upcoming runs\n\n"
        "📅 All times SGT (UTC+8)",
        ephemeral=True
    )

@client.tree.command(name="register", description="Register a MapleStory character")
@app_commands.describe(ign="Your in-game name", cls="Your class (optional)", level="Your level (optional)")
async def slash_register(interaction: discord.Interaction, ign: str, cls: str = None, level: int = None):
    db.upsert_discord_user(interaction.user.id, interaction.user.name)
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
    chars = db.get_characters_discord(interaction.user.id)
    if not chars:
        await interaction.response.send_message("No characters yet. Use `/register`.", ephemeral=True)
        return
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
        await interaction.response.send_message("No characters registered yet.", ephemeral=True)
        return
    lines = ["🌍 **All Guild Characters**\n"]
    for ch in chars:
        line = f"• **{ch['ign']}**"
        if ch["class"]: line += f" — {ch['class']}"
        if ch["level"]: line += f" Lv.{ch['level']}"
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

@client.tree.command(name="createrun", description="Create a boss run")
async def slash_createrun(interaction: discord.Interaction):
    db.upsert_discord_user(interaction.user.id, interaction.user.name)
    bosses  = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])

    view = BossSelectView(grouped, interaction.user.id)
    await interaction.response.send_message(
        "⚔️ **Create a Boss Run**\n\nStep 1 — Which boss?",
        view=view,
        ephemeral=True
    )

class BossSelectView(discord.ui.View):
    def __init__(self, grouped: dict, creator_id: int):
        super().__init__(timeout=300)
        self.grouped    = grouped
        self.creator_id = creator_id
        options = [discord.SelectOption(label=name, value=name) for name in grouped]
        select  = discord.ui.Select(placeholder="Select a boss...", options=options[:25])
        select.callback = self.on_boss_select
        self.add_item(select)

    async def on_boss_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        boss_name = interaction.data["values"][0]
        diffs     = self.grouped[boss_name]
        view      = DiffSelectView(boss_name, diffs, self.creator_id)
        await interaction.response.edit_message(
            content=f"⚔️ **Create a Boss Run**\n\nBoss: **{boss_name}**\n\nStep 2 — Difficulty?",
            view=view
        )

class DiffSelectView(discord.ui.View):
    def __init__(self, boss_name: str, diffs: list, creator_id: int):
        super().__init__(timeout=300)
        self.boss_name  = boss_name
        self.creator_id = creator_id
        options = [discord.SelectOption(label=f"{diff_icon(d)} {d}", value=d) for d in diffs]
        select  = discord.ui.Select(placeholder="Select difficulty...", options=options)
        select.callback = self.on_diff_select
        self.add_item(select)

    async def on_diff_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        difficulty = interaction.data["values"][0]
        run_data   = {"boss_name": self.boss_name, "difficulty": difficulty, "creator_id": self.creator_id}

        # Check if there are teams
        teams = db.get_all_teams()
        if teams:
            view = MethodSelectView(run_data)
            await interaction.response.edit_message(
                content=(
                    f"⚔️ **Create a Boss Run**\n\n"
                    f"Boss: **{self.boss_name} {difficulty}**\n\n"
                    f"Step 3 — How would you like to add members?"
                ),
                view=view
            )
        else:
            await interaction.response.send_modal(DateTimeModal(run_data))

class MethodSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="👥 Load from Team", style=discord.ButtonStyle.primary)
    async def load_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        teams   = db.get_all_teams()
        options = []
        for t in teams:
            members = db.get_team_members(t["id"])
            names   = ", ".join(m["ign"] for m in members)
            options.append(discord.SelectOption(
                label=t["name"],
                value=str(t["id"]),
                description=f"{len(members)} members: {names[:50]}"
            ))
        select = discord.ui.Select(placeholder="Select a team...", options=options[:25])
        view   = discord.ui.View(timeout=300)

        async def on_team_select(inter: discord.Interaction):
            if inter.user.id != self.run_data["creator_id"]:
                await inter.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
            team_id = int(inter.data["values"][0])
            members = db.get_team_members(team_id)
            self.run_data["selected_chars"] = [m["id"] for m in members]
            await inter.response.send_modal(DateTimeModal(self.run_data))

        select.callback = on_team_select
        view.add_item(select)
        await interaction.response.edit_message(content="Select a preset team:", view=view)

    @discord.ui.button(label="👤 Select Individually", style=discord.ButtonStyle.secondary)
    async def select_individual(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True); return
        await interaction.response.send_modal(DateTimeModal(self.run_data))

@client.tree.command(name="cancelrun", description="Cancel a boss run")
@app_commands.describe(run_id="The run ID to cancel")
async def slash_cancelrun(interaction: discord.Interaction, run_id: int):
    db.upsert_discord_user(interaction.user.id, interaction.user.name)
    run = db.get_run(run_id)
    if not run:
        await interaction.response.send_message(f"⚠️ Run #{run_id} not found.", ephemeral=True); return
    if run["leader_id"] != -interaction.user.id:
        await interaction.response.send_message("⚠️ Only the run creator can cancel.", ephemeral=True); return
    if run["status"] == "cancelled":
        await interaction.response.send_message("ℹ️ Already cancelled.", ephemeral=True); return

    db.cancel_run(run_id)
    members = db.get_run_members_discord(run_id)

    sgt      = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
    msg_text = (
        f"❌ **Run #{run_id} cancelled.**\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {time_str}\nCancelled by <@{interaction.user.id}>."
    )

    if RUNS_CHANNEL_ID:
        ch = client.get_channel(RUNS_CHANNEL_ID)
        if ch: await ch.send(f"{msg_text}\n{mentions}")

    # Update original message if exists
    if run.get("discord_message_id") and run.get("discord_channel_id"):
        try:
            ch  = client.get_channel(run["discord_channel_id"])
            msg = await ch.fetch_message(run["discord_message_id"])
            await msg.edit(content=msg_text, embed=fmt_run_embed(run, members), view=None)
        except Exception as e:
            log.warning(f"Could not update run message: {e}")

    await interaction.response.send_message(f"🗑️ Run #{run_id} cancelled.", ephemeral=True)

@client.tree.command(name="resendrun", description="Repost run invite for pending members")
@app_commands.describe(run_id="The run ID to resend")
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
    embed    = fmt_run_embed(run, members)
    view     = RSVPView(run_id)

    if RUNS_CHANNEL_ID:
        ch = client.get_channel(RUNS_CHANNEL_ID)
        if ch:
            await ch.send(
                content=f"📨 **Reminder — please respond to Run #{run_id}!** {mentions}",
                embed=embed,
                view=view
            )
    await interaction.response.send_message(f"✅ Resent invite to {len(pending)} pending member(s).", ephemeral=True)

@client.tree.command(name="myruns", description="See your upcoming run invitations")
async def slash_myruns(interaction: discord.Interaction):
    db.upsert_discord_user(interaction.user.id, interaction.user.name)
    runs = db.get_user_runs_discord(interaction.user.id)
    if not runs:
        await interaction.response.send_message("You have no upcoming run invitations.", ephemeral=True); return
    embeds = fmt_runs_grouped_embed(runs)
    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)

@client.tree.command(name="runs", description="See all upcoming guild runs")
async def slash_runs(interaction: discord.Interaction):
    runs = db.get_active_runs_discord()
    if not runs:
        await interaction.response.send_message("No upcoming runs scheduled.", ephemeral=True); return
    embeds = fmt_runs_grouped_embed(runs)
    await interaction.response.send_message(embeds=embeds[:10], ephemeral=True)

@client.tree.command(name="createteam", description="Create a preset party team")
@app_commands.describe(name="Team name (e.g. Lotus Party)")
async def slash_createteam(interaction: discord.Interaction, name: str):
    db.upsert_discord_user(interaction.user.id, interaction.user.name)
    all_chars = db.get_all_characters_discord()
    if not all_chars:
        await interaction.response.send_message("No characters registered yet.", ephemeral=True); return

    options = [
        discord.SelectOption(
            label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""),
            value=str(ch["id"]),
            description=ch["class"] or "No class"
        )
        for ch in all_chars[:25]
    ]
    select = discord.ui.Select(
        placeholder="Select team members...",
        min_values=1,
        max_values=min(len(options), 25),
        options=options
    )
    view = discord.ui.View(timeout=300)

    async def on_select(inter: discord.Interaction):
        selected_ids = [int(v) for v in inter.data["values"]]
        team_id, err = db.create_team(name, -interaction.user.id, selected_ids)
        if err:
            await inter.response.edit_message(content=f"⚠️ {err}", view=None)
        else:
            chars   = [db.get_character_by_id(cid) for cid in selected_ids]
            members = ", ".join(ch["ign"] for ch in chars if ch)
            await inter.response.edit_message(
                content=f"✅ Team **{name}** saved!\nMembers ({len(chars)}): {members}",
                view=None
            )

    select.callback = on_select
    view.add_item(select)
    await interaction.response.send_message(
        f"👥 **Create Team: {name}**\n\nSelect members:",
        view=view,
        ephemeral=True
    )

@client.tree.command(name="teams", description="List all preset teams")
async def slash_teams(interaction: discord.Interaction):
    teams = db.get_all_teams()
    if not teams:
        await interaction.response.send_message("No preset teams yet. Use `/createteam`.", ephemeral=True); return
    lines = ["👥 **Preset Teams**\n"]
    for t in teams:
        members = db.get_team_members(t["id"])
        names   = " · ".join(m["ign"] for m in members)
        lines.append(f"**{t['name']}** ({len(members)} members)\n  {names}\n")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@client.tree.command(name="deleteteam", description="Delete a preset team")
@app_commands.describe(name="Team name to delete")
async def slash_deleteteam(interaction: discord.Interaction, name: str):
    team = db.get_team_by_name(name)
    if not team:
        await interaction.response.send_message(f"⚠️ Team not found. Use `/teams` to see all.", ephemeral=True); return
    db.delete_team(team["id"])
    await interaction.response.send_message(f"🗑️ Team **{name}** deleted.", ephemeral=True)

@client.tree.command(name="editteam", description="Edit members of a preset team")
@app_commands.describe(name="Team name to edit")
async def slash_editteam(interaction: discord.Interaction, name: str):
    team = db.get_team_by_name(name)
    if not team:
        await interaction.response.send_message(f"⚠️ Team not found. Use `/teams` to see all.", ephemeral=True); return

    current  = db.get_team_members(team["id"])
    cur_ids  = {m["id"] for m in current}
    all_chars = db.get_all_characters_discord()
    options  = [
        discord.SelectOption(
            label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""),
            value=str(ch["id"]),
            description=ch["class"] or "No class",
            default=ch["id"] in cur_ids
        )
        for ch in all_chars[:25]
    ]
    select = discord.ui.Select(
        placeholder="Select members...",
        min_values=1,
        max_values=min(len(options), 25),
        options=options
    )
    view = discord.ui.View(timeout=300)

    async def on_select(inter: discord.Interaction):
        selected_ids = [int(v) for v in inter.data["values"]]
        ok, err = db.update_team(team["id"], name, selected_ids)
        if ok:
            chars   = [db.get_character_by_id(cid) for cid in selected_ids]
            members = ", ".join(ch["ign"] for ch in chars if ch)
            await inter.response.edit_message(
                content=f"✅ Team **{name}** updated!\nMembers ({len(chars)}): {members}",
                view=None
            )
        else:
            await inter.response.edit_message(content=f"⚠️ {err}", view=None)

    select.callback = on_select
    view.add_item(select)
    await interaction.response.send_message(
        f"✏️ **Edit Team: {name}**\n\nCurrent members pre-selected. Update as needed:",
        view=view,
        ephemeral=True
    )

# ── Scheduler ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=15)
async def scheduler_loop():
    await send_reminders()
    await auto_cancel_pending()

async def send_reminders():
    runs = db.get_runs_due_for_reminder_discord()
    for run in runs:
        members  = db.get_run_members_discord(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
        mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))

        if run.get("discord_channel_id"):
            ch = client.get_channel(run["discord_channel_id"])
        elif RUNS_CHANNEL_ID:
            ch = client.get_channel(RUNS_CHANNEL_ID)
        else:
            continue

        if ch:
            try:
                await ch.send(
                    f"⏰ **Boss Run Reminder!** {mentions}\n"
                    f"⚔️ {diff_icon(run['difficulty'])} **{run['boss_name']} {run['difficulty']}**\n"
                    f"📅 Starting at **{time_str}**"
                )
            except Exception as e:
                log.warning(f"Reminder failed: {e}")

async def auto_cancel_pending():
    expired = db.get_expired_pending_runs(hours=12)
    for run in expired:
        db.cancel_run(run["id"])
        members  = db.get_run_members_discord(run["id"])
        sgt      = get_run_dt(run) + timedelta(hours=8)
        time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
        pending  = [m for m in members if m["accepted"] == 0]
        mentions = " ".join(f"<@{m['discord_id']}>" for m in pending if m.get("discord_id"))

        msg = (
            f"⏰ **Run #{run['id']} auto-cancelled** — no response within 12 hours.\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {time_str}\n"
            f"No response from: {', '.join(m['ign'] for m in pending)}"
        )
        if run.get("discord_channel_id"):
            ch = client.get_channel(run["discord_channel_id"])
            if ch:
                try:
                    await ch.send(f"{msg}\n{mentions}")
                except Exception as e:
                    log.warning(f"Auto-cancel notify failed: {e}")
        log.info(f"Auto-cancelled run #{run['id']} (pending >12h)")

@scheduler_loop.before_loop
async def before_scheduler():
    await client.wait_until_ready()

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    db.init_db()
    log.info("Database initialised.")
    if DISCORD_TOKEN == "YOUR_DISCORD_TOKEN_HERE":
        log.error("❌ Set DISCORD_TOKEN as an environment variable.")
        return
    if not DISCORD_GUILD_ID:
        log.error("❌ Set DISCORD_GUILD_ID as an environment variable.")
        return
    log.info("🍄 Discord bot starting...")
    client.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()
