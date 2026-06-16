"""
MapleStory Guild Boss Scheduler — Discord Bot

Fixes applied:
- Runs post to the channel where /createrun was executed (guild-scoped), not a global RUNS_CHANNEL_ID
- @mentions work for players who /register without linking Telegram
- Scheduler and reminders scoped to the originating channel/guild
- Telegram cross-notify only fires for actually linked accounts
- No global slash command pollution — synced only to DISCORD_GUILD_ID
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

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "YOUR_DISCORD_TOKEN_HERE")
DISCORD_GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "0"))
# RUNS_CHANNEL_ID is now optional — used only as a fallback if command is run in DM
RUNS_CHANNEL_ID = int(os.environ.get("RUNS_CHANNEL_ID", "0"))

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


def progress_bar(step, total=4):
    return f"`{'█' * step}{'░' * (total - step)}` Step {step}/{total}"


def auto_register(user: discord.User):
    """Ensure Discord user exists in discord_users table."""
    db.upsert_discord_user(user.id, user.name)


def fmt_run_embed(run, members=None):
    icon = diff_icon(run["difficulty"])
    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    color_map = {
        "confirmed": discord.Color.green(),
        "pending": discord.Color.orange(),
        "cancelled": discord.Color.red(),
    }
    embed = discord.Embed(
        title=f"⚔️ Run #{run['id']} — {icon} {run['boss_name']} {run['difficulty']}",
        color=color_map.get(run["status"], discord.Color.blurple()),
    )
    embed.add_field(name="📅 Date & Time", value=time_str, inline=True)
    embed.add_field(name="👑 Leader", value=f"@{run['leader_username']}", inline=True)
    status_map = {"confirmed": "✅ CONFIRMED", "pending": "⏳ PENDING", "cancelled": "❌ CANCELLED"}
    embed.add_field(name="📋 Status", value=status_map.get(run["status"], run["status"].upper()), inline=True)

    if members:
        total = len(members)
        accepted = sum(1 for m in members if m["accepted"] == 1)
        lines = []
        for m in members:
            icon_m = {1: "✅", -1: "❌", 0: "⏳"}[m["accepted"]]
            line = f"{icon_m} **{m['ign']}**"
            if m.get("discord_id"):
                line += f" (<@{m['discord_id']}>)"
            lines.append(line)
        embed.add_field(name=f"👥 Party ({accepted}/{total})", value="\n".join(lines) or "None", inline=False)

    return embed


async def _send_telegram(telegram_id: int, text: str, run_id: int = None):
    """Send a Telegram DM via Bot API."""
    import httpx
    tg_token = os.environ.get("BOT_TOKEN")
    if not tg_token or not telegram_id or telegram_id < 0:
        return False
    payload = {"chat_id": telegram_id, "text": text}
    if run_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✅ Accept", "callback_data": f"rsvp_accept_{run_id}"},
                {"text": "❌ Decline", "callback_data": f"rsvp_decline_{run_id}"},
            ]]
        }
    try:
        async with httpx.AsyncClient() as c:
            resp = await c.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json=payload,
            )
            return resp.status_code == 200
    except Exception as e:
        log.warning(f"Telegram send failed (id:{telegram_id}): {e}")
        return False


async def _notify_all_via_telegram(run_id: int, members, run, text: str, include_buttons=False):
    """Send Telegram DMs to members who have a real (positive) telegram_id."""
    notified = []
    for m in members:
        char = db.get_character_by_id(m.get("character_id") or m.get("id"))
        if not char:
            continue
        tg_id = char.get("telegram_id")
        if not tg_id or tg_id < 0:
            continue
        ok = await _send_telegram(tg_id, text, run_id if include_buttons else None)
        if ok:
            notified.append(m["ign"])
    return notified


async def _update_telegram_invite(run_id, telegram_id, ign, accepted, run):
    """Notify Telegram that a response was recorded via Discord."""
    import httpx
    tg_token = os.environ.get("BOT_TOKEN")
    if not tg_token or not telegram_id or telegram_id < 0:
        return
    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    status = "✅ accepted" if accepted == 1 else "❌ declined"
    msg = (
        f"ℹ️ Your response has been recorded via Discord.\n\n"
        f"You {status} Run #{run_id}\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {time_str}"
    )
    try:
        async with httpx.AsyncClient() as c:
            await c.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": telegram_id, "text": msg},
            )
    except Exception as e:
        log.warning(f"Telegram update failed for {ign}: {e}")


async def _notify_via_telegram(run_id: int, members, run, data):
    """Send Telegram DM invites to members who have a real telegram_id linked."""
    import httpx
    tg_token = os.environ.get("BOT_TOKEN")
    if not tg_token:
        return [], []

    sgt = get_run_dt(run) + timedelta(hours=8)
    time_str = sgt.strftime("%d/%m/%Y %H:%M SGT")
    reminder_str = data.get("reminder_label", "8:00 AM SGT on the day of the run")

    notified = []
    skipped = []

    for m in members:
        char = db.get_character_by_id(m["character_id"] if "character_id" in m else m["id"])
        if not char:
            continue
        tg_id = char.get("telegram_id")
        if not tg_id or tg_id < 0:
            skipped.append(m["ign"])
            continue

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
                                {"text": "✅ Accept", "callback_data": f"rsvp_accept_{run_id}"},
                                {"text": "❌ Decline", "callback_data": f"rsvp_decline_{run_id}"},
                            ]]
                        },
                    },
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


async def _post_to_channel(bot, channel_id: int, content, embed=None, view=None):
    """Post to a specific channel by ID."""
    if not channel_id:
        return None
    ch = bot.get_channel(channel_id)
    if not ch:
        return None
    try:
        kwargs = {"content": content}
        if embed:
            kwargs["embed"] = embed
        if view:
            kwargs["view"] = view
        return await ch.send(**kwargs)
    except discord.Forbidden:
        log.warning(f"Missing permissions in channel {channel_id}")
    except Exception as e:
        log.warning(f"Could not post to channel {channel_id}: {e}")
    return None


async def _update_run_message(bot, run, embed, view=discord.utils.MISSING, content=None):
    if run.get("discord_message_id") and run.get("discord_channel_id"):
        try:
            ch = bot.get_channel(run["discord_channel_id"])
            if not ch:
                return
            msg = await ch.fetch_message(run["discord_message_id"])
            kwargs = {"embed": embed}
            if view is not discord.utils.MISSING:
                kwargs["view"] = view
            if content:
                kwargs["content"] = content
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
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        await self._cb(interaction)


class CancelButton(discord.ui.Button):
    def __init__(self, row=1):
        super().__init__(label="❌ Cancel", style=discord.ButtonStyle.danger, row=row)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)


# ── RSVP ──────────────────────────────────────────────────────────────────────

def make_rsvp_view(run_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    accept_btn = discord.ui.Button(
        label="✅ Accept", style=discord.ButtonStyle.success,
        custom_id=f"rsvp_accept_{run_id}",
    )
    decline_btn = discord.ui.Button(
        label="❌ Decline", style=discord.ButtonStyle.danger,
        custom_id=f"rsvp_decline_{run_id}",
    )

    async def on_accept(interaction: discord.Interaction):
        await handle_rsvp(interaction, run_id, accepted=1)

    async def on_decline(interaction: discord.Interaction):
        await handle_rsvp(interaction, run_id, accepted=-1)

    accept_btn.callback = on_accept
    decline_btn.callback = on_decline
    view.add_item(accept_btn)
    view.add_item(decline_btn)
    return view


async def handle_rsvp(interaction: discord.Interaction, run_id: int, accepted: int):
    await interaction.response.defer(ephemeral=True)
    auto_register(interaction.user)

    run = db.get_run(run_id)
    if not run:
        await interaction.followup.send(f"⚠️ Run #{run_id} not found.", ephemeral=True)
        return

    if run["status"] == "confirmed":
        await interaction.followup.send(
            f"ℹ️ Run #{run_id} is already confirmed — no further responses needed.",
            ephemeral=True,
        )
        return

    if run["status"] == "cancelled":
        try:
            cancelled_embed = fmt_run_embed(run, db.get_run_members_discord(run_id))
            cancelled_embed.set_footer(text="❌ This run has been cancelled")
            await _update_run_message(interaction.client, run, cancelled_embed, view=None)
        except Exception:
            pass
        await interaction.followup.send(
            f"⚠️ Run #{run_id} has been cancelled.",
            ephemeral=True,
        )
        return

    rm = db.get_run_member_by_discord(run_id, interaction.user.id)
    if not rm:
        await interaction.followup.send("⚠️ You're not invited to this run.", ephemeral=True)
        return

    db.set_member_response(run_id, rm["character_id"], accepted)
    members = db.get_run_members_discord(run_id)

    # Notify Telegram that response was recorded
    char = db.get_character_by_id(rm["character_id"])
    if char and char.get("telegram_id") and char["telegram_id"] > 0:
        await _update_telegram_invite(run_id, char["telegram_id"], rm["ign"], accepted, run)

    # Resolve the channel to post updates to — use originating channel, fallback to RUNS_CHANNEL_ID
    target_channel_id = run.get("discord_channel_id") or RUNS_CHANNEL_ID

    if accepted == 1:
        all_confirmed = db.check_and_confirm_run(run_id)
        if all_confirmed:
            run = db.get_run(run_id)
            embed = fmt_run_embed(run, members)
            embed.title = f"🎉 Run #{run_id} CONFIRMED! — {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}"
            embed.color = discord.Color.green()
            embed.set_footer(text="✅ All members confirmed — buttons removed")

            await _update_run_message(interaction.client, run, embed, view=None)

            mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
            await _post_to_channel(
                interaction.client, target_channel_id,
                f"🎉 **Run #{run_id} is CONFIRMED!** {mentions}", embed=embed,
            )

            sgt_c = get_run_dt(run) + timedelta(hours=8)
            tg_msg_c = (
                f"🎉 Run #{run_id} is CONFIRMED! All members accepted.\n\n"
                f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
                f"📅 {sgt_c.strftime('%d/%m/%Y %H:%M SGT')}\n\n"
                f"See you there!"
            )
            await _notify_all_via_telegram(run_id, members, run, tg_msg_c)

            await interaction.followup.send(
                f"✅ **Run #{run_id} is CONFIRMED!** All members accepted.",
                ephemeral=True,
            )
        else:
            pending = [m for m in members if m["accepted"] == 0]
            accepted_count = sum(1 for m in members if m["accepted"] == 1)
            updated_embed = fmt_run_embed(run, members)
            updated_embed.set_footer(text=f"✅ {rm['ign']} just accepted · {accepted_count}/{len(members)} confirmed")
            await _update_run_message(interaction.client, run, updated_embed, view=make_rsvp_view(run_id))
            await interaction.followup.send(
                f"✅ **You accepted Run #{run_id}!** ({accepted_count}/{len(members)} confirmed)\n"
                f"Still waiting on: {', '.join(m['ign'] for m in pending)}",
                ephemeral=True,
            )
    else:
        db.cancel_run(run_id)
        run = db.get_run(run_id)
        sgt = get_run_dt(run) + timedelta(hours=8)
        mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
        cancel_text = (
            f"❌ **Run #{run_id} has been cancelled.**\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n"
            f"{rm['ign']} (<@{interaction.user.id}>) declined."
        )
        await _update_run_message(
            interaction.client, run, fmt_run_embed(run, members),
            view=None, content=cancel_text,
        )
        await _post_to_channel(
            interaction.client, target_channel_id,
            f"{cancel_text}\n{mentions}",
        )

        tg_decline_msg = (
            f"❌ Run #{run_id} has been cancelled.\n\n"
            f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
            f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n"
            f"{rm['ign']} declined on Discord."
        )
        await _notify_all_via_telegram(run_id, members, run, tg_decline_msg)

        await interaction.followup.send(
            f"❌ **You declined Run #{run_id}.** The run has been cancelled and all members notified.",
            ephemeral=True,
        )


# ── Date/time modal ────────────────────────────────────────────────────────────

class DatePickerPromptView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="📅 Set Date & Time", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        await interaction.response.send_modal(DateTimeModal(self.run_data))

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Run creation cancelled.", view=None)


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
            await interaction.response.send_message(
                "⚠️ Invalid format. Use DD/MM/YYYY and HH:MM.", ephemeral=True
            )
            return

        sgt_tz = timezone(timedelta(hours=8))
        sgt_dt = naive.replace(tzinfo=sgt_tz)
        if sgt_dt <= datetime.now(sgt_tz):
            await interaction.response.send_message("⚠️ That date/time is in the past.", ephemeral=True)
            return

        self.run_data["run_at_iso"] = sgt_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.run_data["time_str"] = sgt_dt.strftime("%d/%m/%Y %H:%M SGT")

        # Set 8am SGT reminder on run day (skip if same day)
        now_sgt = datetime.now(sgt_tz)
        run_8am = sgt_dt.replace(hour=8, minute=0, second=0, microsecond=0)
        if run_8am.date() > now_sgt.date():
            self.run_data["reminder_8am_iso"] = run_8am.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.run_data["reminder_label"] = "8:00 AM SGT on the day of the run"
        else:
            self.run_data["reminder_8am_iso"] = None
            self.run_data["reminder_label"] = "None (same-day run)"

        if self.run_data.get("selected_chars"):
            chars = [db.get_character_by_id(cid) for cid in self.run_data["selected_chars"]]
            platform_map = db.get_character_platform_info(self.run_data["selected_chars"])
            member_lines = [f"• {ch['ign']} [{platform_map.get(ch['id'], '⚠️')}]" for ch in chars if ch]
            view = ConfirmRunView(self.run_data)
            await interaction.response.send_message(
                f"{progress_bar(4)}\n\n📋 **Run Summary — Please confirm:**\n\n"
                f"⚔️ {diff_icon(self.run_data['difficulty'])} **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n"
                f"📅 {self.run_data['time_str']}\n"
                f"⏰ Reminder: {self.run_data.get('reminder_label', '8:00 AM SGT on the day of the run')}\n\n"
                f"👥 Party ({len(chars)}):\n" + "\n".join(member_lines),
                view=view, ephemeral=True,
            )
        else:
            view = MemberSelectView(self.run_data)
            await interaction.response.send_message(
                f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**"
                f" — {self.run_data['time_str']}\n\n👥 Select party members:",
                view=view, ephemeral=True,
            )


# ── Step 1: Boss ──────────────────────────────────────────────────────────────

class BossSelectView(discord.ui.View):
    def __init__(self, boss_map: dict, creator_id: int, recent=None):
        super().__init__(timeout=300)
        self.run_data = {"boss_map": boss_map, "creator_id": creator_id}

        options = []
        seen = set()
        if recent:
            for r in recent:
                key = f"{r['name']}||{r['difficulty']}"
                if key not in seen and r["name"] in boss_map:
                    options.append(discord.SelectOption(
                        label=f"⭐ {r['name']} — {r['difficulty']}",
                        value=f"recent||{r['name']}||{r['difficulty']}",
                        description="Recently scheduled",
                    ))
                    seen.add(key)
        for name in boss_map:
            options.append(discord.SelectOption(label=name, value=name))

        select = discord.ui.Select(placeholder="Choose a boss...", options=options[:25])
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(CancelButton())

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        val = interaction.data["values"][0]
        if val.startswith("recent||"):
            _, boss_name, difficulty = val.split("||")
            self.run_data["boss_name"] = boss_name
            self.run_data["difficulty"] = difficulty
            teams = db.get_all_teams()
            view = MethodSelectView(self.run_data) if teams else MemberSelectView(self.run_data)
            label = "How would you like to add members?" if teams else "👥 Select party members:"
            await interaction.response.edit_message(
                content=f"{progress_bar(2)}\n\n⚔️ **{boss_name} {difficulty}**\n\n{label}",
                view=view,
            )
            return

        self.run_data["boss_name"] = val
        view = DiffSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(2)}\n\nBoss: **{self.run_data['boss_name']}**\n\n🎯 Select difficulty:",
            view=view,
        )


# ── Step 2: Difficulty ────────────────────────────────────────────────────────

class DiffSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

        diffs = run_data["boss_map"][run_data["boss_name"]]
        options = [discord.SelectOption(label=f"{diff_icon(d)} {d}", value=d) for d in diffs]
        select = discord.ui.Select(placeholder="Choose difficulty...", options=options)
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(BackButton(self._go_back))
        self.add_item(CancelButton())

    async def _go_back(self, interaction: discord.Interaction):
        view = BossSelectView(self.run_data["boss_map"], self.run_data["creator_id"])
        await interaction.response.edit_message(
            content=f"{progress_bar(1)}\n\n⚔️ Select a boss:", view=view
        )

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        self.run_data["difficulty"] = interaction.data["values"][0]
        teams = db.get_all_teams()
        if teams:
            view = MethodSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 How would you like to add members?",
                view=view,
            )
        else:
            view = MemberSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 Select party members:",
                view=view,
            )


# ── Step 3: Method ────────────────────────────────────────────────────────────

class MethodSelectView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="👥 Load from Team", style=discord.ButtonStyle.primary, row=0)
    async def load_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        teams = db.get_all_teams()
        options = [
            discord.SelectOption(
                label=t["name"],
                value=str(t["id"]),
                description=(", ".join(m["ign"] for m in db.get_team_members(t["id"])))[:100],
            )
            for t in teams
        ]
        select = discord.ui.Select(placeholder="Choose a team...", options=options[:25])
        view = discord.ui.View(timeout=300)
        run_data = self.run_data

        async def on_team_select(inter: discord.Interaction):
            if inter.user.id != run_data["creator_id"]:
                await inter.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
                return
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
            view=view,
        )

    @discord.ui.button(label="👤 Select Individually", style=discord.ButtonStyle.secondary, row=0)
    async def select_individual(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        view = MemberSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 Select party members:",
            view=view,
        )

    async def _go_back(self, interaction: discord.Interaction):
        view = DiffSelectView(self.run_data)
        await interaction.response.edit_message(
            content=f"{progress_bar(2)}\n\nBoss: **{self.run_data['boss_name']}**\n\n🎯 Select difficulty:",
            view=view,
        )

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
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
        char_ids = [ch["id"] for ch in all_chars]
        platform_map = db.get_character_platform_info(char_ids)
        options = [
            discord.SelectOption(
                label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""),
                value=str(ch["id"]),
                description=f"{ch['class'] or 'No class'} [{platform_map.get(ch['id'], '⚠️')}]",
            )
            for ch in all_chars[:25]
        ]
        select = discord.ui.Select(
            placeholder="Select party members...",
            min_values=1,
            max_values=min(len(options), 25),
            options=options,
        )
        select.callback = self.on_select
        self.add_item(select)
        self.add_item(BackButton(self._go_back))
        self.add_item(CancelButton())

    async def on_select(self, interaction: discord.Interaction):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        selected_ids = [int(v) for v in interaction.data["values"]]
        self.run_data["selected_chars"] = selected_ids

        all_chars = db.get_all_characters_discord()
        char_map = {ch["id"]: ch for ch in all_chars}
        unlinked = [char_map[cid]["ign"] for cid in selected_ids if cid in char_map and not char_map[cid].get("discord_id")]

        if unlinked:
            warning = (
                f"⚠️ **Heads up:** {', '.join(unlinked)} have no Discord account linked — "
                f"they won't see the invite on Discord. They can still be notified via Telegram if accounts are linked."
            )
            await interaction.response.send_message(warning, ephemeral=True)
            view = DatePickerPromptView(self.run_data)
            await interaction.followup.send(
                f"{progress_bar(4)}\n\nSet the date and time for this run:",
                view=view, ephemeral=True,
            )
        else:
            await interaction.response.send_modal(DateTimeModal(self.run_data))

    async def _go_back(self, interaction: discord.Interaction):
        teams = db.get_all_teams()
        if teams:
            view = MethodSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 How would you like to add members?",
                view=view,
            )
        else:
            view = DiffSelectView(self.run_data)
            await interaction.response.edit_message(
                content=f"{progress_bar(2)}\n\nBoss: **{self.run_data['boss_name']}**\n\n🎯 Select difficulty:",
                view=view,
            )


# ── Step 4: Confirm ───────────────────────────────────────────────────────────

class ConfirmRunView(discord.ui.View):
    def __init__(self, run_data: dict):
        super().__init__(timeout=300)
        self.run_data = run_data

    @discord.ui.button(label="✅ Confirm & Post", style=discord.ButtonStyle.success, row=0)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        data = self.run_data
        boss = db.find_boss(data["boss_name"], data["difficulty"])
        run_id = db.create_run_discord(boss["id"], interaction.user.id, data["run_at_iso"])

        for char_id in data["selected_chars"]:
            db.add_run_member(run_id, char_id)

        if data.get("reminder_8am_iso"):
            db.set_run_reminder(run_id, data["reminder_8am_iso"])

        run = db.get_run(run_id)
        members = db.get_run_members_discord(run_id)
        embed = fmt_run_embed(run, members)
        view = make_rsvp_view(run_id)

        linked = [m for m in members if m.get("discord_id")]
        unlinked = [m for m in members if not m.get("discord_id")]
        mentions = " ".join(f"<@{m['discord_id']}>" for m in linked)

        # Post to the channel where /createrun was invoked, NOT a global channel
        # interaction.channel is the channel the slash command was used in
        target_channel = interaction.channel
        guild_id = interaction.guild_id

        # Fall back to configured RUNS_CHANNEL_ID if somehow channel is unavailable
        if target_channel is None and RUNS_CHANNEL_ID:
            target_channel = interaction.client.get_channel(RUNS_CHANNEL_ID)

        if target_channel:
            try:
                msg = await target_channel.send(
                    content=(
                        f"📢 **New Boss Run!** {mentions}\n"
                        f"⏰ Reminder: {data.get('reminder_label', '8:00 AM SGT')}\n"
                        f"Accept or decline below:"
                    ),
                    embed=embed,
                    view=view,
                )
                db.set_run_discord_message(run_id, msg.id, target_channel.id, guild_id)

                tg_notified, tg_skipped = await _notify_via_telegram(run_id, members, run, data)

                summary = (
                    f"✅ **Run #{run_id} created and posted!** Check <#{target_channel.id}>\n"
                    f"To edit: `/editrun {run_id}` · To cancel: `/cancelrun {run_id}`"
                )
                if tg_notified:
                    summary += f"\n📱 Telegram notified: {', '.join(tg_notified)}"
                if unlinked:
                    names = ", ".join(m["ign"] for m in unlinked)
                    summary += (
                        f"\n\n⚠️ **No Discord link:** {names}\n"
                        f"They can only accept/decline via Telegram."
                    )
                if tg_skipped and not tg_notified:
                    summary += f"\n\n💡 Members can link Telegram with `/linkaccount` for cross-platform invites."

                await interaction.edit_original_response(content=summary, view=None)

            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ Bot lacks permission to post in this channel.\n"
                    "Go to the channel → Edit Channel → Permissions → add the bot with **Send Messages** + **Embed Links**.",
                    ephemeral=True,
                )
        else:
            await interaction.followup.send(
                "⚠️ Could not find a channel to post in. Make sure `RUNS_CHANNEL_ID` is set or use this command in a server channel.",
                ephemeral=True,
            )

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.run_data["creator_id"]:
            await interaction.response.send_message("⚠️ Only the run creator can use this.", ephemeral=True)
            return
        view = MemberSelectView(self.run_data)
        bosses = db.get_all_bosses()
        grouped = {}
        for b in bosses:
            grouped.setdefault(b["name"], []).append(b["difficulty"])
        self.run_data["boss_map"] = grouped
        await interaction.response.edit_message(
            content=f"{progress_bar(3)}\n\n⚔️ **{self.run_data['boss_name']} {self.run_data['difficulty']}**\n\n👥 Select party members:",
            view=view,
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
        # Re-register persistent RSVP handlers for pending runs on restart
        active_runs = db.get_active_runs_discord()
        pending_runs = [r for r in active_runs if r["status"] == "pending"]
        for run in pending_runs:
            self.add_view(make_rsvp_view(run["id"]))
        log.info(f"Re-registered RSVP views for {len(pending_runs)} pending runs")

        # Sync ONLY to the configured guild — avoids leaking commands to other servers
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=DISCORD_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info(f"Slash commands synced to guild {DISCORD_GUILD_ID}")
        else:
            # No guild configured — sync globally as fallback
            await self.tree.sync()
            log.info("Slash commands synced globally (no DISCORD_GUILD_ID set)")

        scheduler_loop.start()

    async def on_ready(self):
        log.info(f"🍄 Discord bot ready as {self.user}")
        await self.change_presence(activity=discord.Game(name="/help for commands"))

    async def on_guild_join(self, guild: discord.Guild):
        # Only sync if this is our configured guild
        if DISCORD_GUILD_ID and guild.id == DISCORD_GUILD_ID:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info(f"Commands synced to guild: {guild.name} ({guild.id})")
            except Exception as e:
                log.warning(f"Failed to sync commands to {guild.name}: {e}")


client = MapleBot()


# ── Scheduler ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def scheduler_loop():
    """Check for reminders and expired pending runs every minute."""
    try:
        # Reminders for confirmed runs
        due = db.get_runs_due_for_reminder_discord()
        for run in due:
            channel_id = run.get("discord_channel_id")
            if not channel_id:
                continue
            ch = client.get_channel(channel_id)
            if not ch:
                continue
            members = db.get_run_members_discord(run["id"])
            mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
            sgt = get_run_dt(run) + timedelta(hours=8)
            await ch.send(
                f"⏰ **Reminder — Run #{run['id']} starts soon!** {mentions}\n"
                f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']} — {sgt.strftime('%d/%m/%Y %H:%M SGT')}"
            )
            # Clear remind_at so it doesn't fire again
            db.set_run_reminder(run["id"], None)

        # Auto-cancel pending runs older than 12 hours
        expired = db.get_expired_pending_runs(hours=12)
        for run in expired:
            db.cancel_run(run["id"])
            channel_id = run.get("discord_channel_id")
            if channel_id:
                ch = client.get_channel(channel_id)
                if ch:
                    await ch.send(
                        f"⚠️ **Run #{run['id']} auto-cancelled** — no response within 12 hours.\n"
                        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}"
                    )

    except Exception as e:
        log.warning(f"Scheduler error: {e}")


@scheduler_loop.before_loop
async def before_scheduler():
    await client.wait_until_ready()


# ── Autocomplete helpers ──────────────────────────────────────────────────────

async def autocomplete_my_runs(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[int]]:
    runs = db.get_user_runs_discord(interaction.user.id)
    choices = []
    for r in runs:
        sgt = get_run_dt(r) + timedelta(hours=8)
        label = f"#{r['id']} {r['boss_name']} {r['difficulty']} — {sgt.strftime('%d/%m %H:%M')}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=r["id"]))
    return choices[:25]


async def autocomplete_active_runs(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[int]]:
    runs = db.get_active_runs_discord()
    choices = []
    for r in runs:
        sgt = get_run_dt(r) + timedelta(hours=8)
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
        ephemeral=True,
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
        "`/createrun` — full guided flow\n"
        "`/quickrun <boss> <difficulty>` — skip to members\n"
        "`/cancelrun` `/resendrun` `/myruns` `/runs`\n\n"
        "📅 All times SGT (UTC+8)",
        ephemeral=True,
    )


@client.tree.command(name="linkaccount", description="Link your Discord to your Telegram account")
@app_commands.describe(code="The 8-character code from /linkdiscord on Telegram")
async def slash_linkaccount(interaction: discord.Interaction, code: str):
    auto_register(interaction.user)
    telegram_id, err = db.consume_link_code(code, interaction.user.id, interaction.user.name)
    if err:
        await interaction.response.send_message(f"⚠️ {err}", ephemeral=True)
        return
    chars = db.get_characters_discord(interaction.user.id)
    await interaction.response.send_message(
        f"✅ **Accounts linked successfully!**\n\n"
        f"Your Discord is now linked to your Telegram account.\n"
        f"Characters shared: {len(chars)}\n\n"
        f"You can now accept/decline runs on both platforms.",
        ephemeral=True,
    )


@client.tree.command(name="linkstatus", description="Check your account link status")
async def slash_linkstatus(interaction: discord.Interaction):
    auto_register(interaction.user)
    linked = db.get_discord_link_status(interaction.user.id)
    if linked:
        await interaction.response.send_message(
            f"✅ Linked to Telegram account @{linked['tg_username']}\n"
            f"Characters are shared across both platforms.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "❌ No Telegram account linked.\n\n"
            "To link:\n"
            "1. Open your Telegram bot\n"
            "2. Send `/linkdiscord`\n"
            "3. Copy the 8-character code\n"
            "4. Use `/linkaccount <code>` here",
            ephemeral=True,
        )


@client.tree.command(name="register", description="Register a MapleStory character")
@app_commands.describe(ign="Your in-game name", cls="Your class", level="Your level")
async def slash_register(interaction: discord.Interaction, ign: str, cls: str = None, level: int = None):
    auto_register(interaction.user)
    ok = db.add_character_discord(interaction.user.id, ign, cls, level)
    if ok:
        parts = [f"✅ Registered **{ign}**"]
        if cls:
            parts.append(f"Class: {cls}")
        if level:
            parts.append(f"Level: {level}")
        await interaction.response.send_message(" | ".join(parts), ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ IGN **{ign}** is already registered.", ephemeral=True)


@client.tree.command(name="chars", description="List your registered characters")
async def slash_chars(interaction: discord.Interaction):
    auto_register(interaction.user)
    chars = db.get_characters_discord(interaction.user.id)
    if not chars:
        await interaction.response.send_message("No characters yet. Use `/register`.", ephemeral=True)
        return
    lines = ["👤 **Your Characters**\n"]
    for ch in chars:
        line = f"• **{ch['ign']}**"
        if ch["class"]:
            line += f" — {ch['class']}"
        if ch["level"]:
            line += f" Lv.{ch['level']}"
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
        if ch["class"]:
            line += f" — {ch['class']}"
        if ch["level"]:
            line += f" Lv.{ch['level']}"
        if ch.get("discord_id"):
            line += f" (<@{ch['discord_id']}>)"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@client.tree.command(name="bosses", description="List all available bosses")
async def slash_bosses(interaction: discord.Interaction):
    bosses = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    lines = ["⚔️ **Available Bosses**\n"]
    for name, diffs in grouped.items():
        icons = " ".join(f"{diff_icon(d)} {d}" for d in diffs)
        lines.append(f"**{name}**\n  {icons}\n")
    lines.append("🟢Easy 🔵Normal 🟠Hard 🔴Chaos ⚫Extreme")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@client.tree.command(name="createrun", description="Create a boss run — guided flow")
async def slash_createrun(interaction: discord.Interaction):
    auto_register(interaction.user)
    bosses = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    recent = db.get_recent_bosses(3)
    # Store the originating channel so the run posts there
    run_data = {
        "boss_map": grouped,
        "creator_id": interaction.user.id,
        "origin_channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id,
    }
    view = BossSelectView(grouped, interaction.user.id, recent)
    view.run_data = run_data
    await interaction.response.send_message(
        f"{progress_bar(1)}\n\n⚔️ **Create a Boss Run**\n\nSelect a boss:\n⭐ = recently scheduled",
        view=view,
        ephemeral=True,
    )


@client.tree.command(name="quickrun", description="Skip to member selection for a known boss")
@app_commands.describe(boss="Boss name", difficulty="Difficulty")
async def slash_quickrun(interaction: discord.Interaction, boss: str, difficulty: str):
    auto_register(interaction.user)
    boss_obj = db.find_boss(boss, difficulty)
    if not boss_obj:
        await interaction.response.send_message(
            f"⚠️ **{boss} {difficulty}** not found. Use `/bosses` to see the list.",
            ephemeral=True,
        )
        return
    bosses = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])
    run_data = {
        "boss_map": grouped,
        "boss_name": boss_obj["name"],
        "difficulty": boss_obj["difficulty"],
        "creator_id": interaction.user.id,
        "origin_channel_id": interaction.channel_id,
        "guild_id": interaction.guild_id,
    }
    view = MemberSelectView(run_data)
    await interaction.response.send_message(
        f"{progress_bar(3)}\n\n⚔️ **{boss_obj['name']} {boss_obj['difficulty']}**\n\n👥 Select party members:",
        view=view,
        ephemeral=True,
    )


@client.tree.command(name="cancelrun", description="Cancel a run")
@app_commands.describe(run_id="Run ID")
@app_commands.autocomplete(run_id=autocomplete_my_runs)
async def slash_cancelrun(interaction: discord.Interaction, run_id: int):
    auto_register(interaction.user)
    run = db.get_run(run_id)
    if not run:
        await interaction.response.send_message(f"⚠️ Run #{run_id} not found.", ephemeral=True)
        return

    # Allow leader OR any member of the run to cancel
    members = db.get_run_members_discord(run_id)
    is_member = any(m.get("discord_id") == interaction.user.id for m in members)
    is_leader = False
    # Check if the Discord user is the leader (may be a placeholder telegram_id)
    # We check by discord_id on the character matching leader's runs
    leader_char = None
    for m in members:
        char = db.get_character_by_id(m["character_id"])
        if char and char.get("discord_id") == interaction.user.id:
            # Check if this char's telegram_id matches the run leader_id
            if char["telegram_id"] == run["leader_id"] or (-char.get("discord_id", 0)) == run["leader_id"]:
                is_leader = True
                break

    if not is_leader and not is_member:
        await interaction.response.send_message("⚠️ Only run members can cancel a run.", ephemeral=True)
        return

    if run["status"] == "cancelled":
        await interaction.response.send_message(f"⚠️ Run #{run_id} is already cancelled.", ephemeral=True)
        return

    db.cancel_run(run_id)
    run = db.get_run(run_id)
    embed = fmt_run_embed(run, members)
    sgt = get_run_dt(run) + timedelta(hours=8)
    cancel_text = (
        f"❌ **Run #{run_id} cancelled by <@{interaction.user.id}>.**\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}"
    )
    await _update_run_message(interaction.client, run, embed, view=None, content=cancel_text)

    mentions = " ".join(f"<@{m['discord_id']}>" for m in members if m.get("discord_id"))
    target_channel_id = run.get("discord_channel_id") or RUNS_CHANNEL_ID
    await _post_to_channel(interaction.client, target_channel_id, f"{cancel_text}\n{mentions}")

    tg_msg = (
        f"❌ Run #{run_id} has been cancelled.\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}"
    )
    await _notify_all_via_telegram(run_id, members, run, tg_msg)

    await interaction.response.send_message(f"✅ Run #{run_id} has been cancelled.", ephemeral=True)


@client.tree.command(name="resendrun", description="Resend invites to pending members")
@app_commands.describe(run_id="Run ID")
@app_commands.autocomplete(run_id=autocomplete_my_runs)
async def slash_resendrun(interaction: discord.Interaction, run_id: int):
    auto_register(interaction.user)
    run = db.get_run(run_id)
    if not run:
        await interaction.response.send_message(f"⚠️ Run #{run_id} not found.", ephemeral=True)
        return
    if run["status"] == "cancelled":
        await interaction.response.send_message("⚠️ This run has been cancelled.", ephemeral=True)
        return

    members = db.get_run_members_discord(run_id)
    pending = [m for m in members if m["accepted"] == 0]
    if not pending:
        await interaction.response.send_message(
            f"ℹ️ No pending members for Run #{run_id} — everyone has responded.", ephemeral=True
        )
        return

    target_channel_id = run.get("discord_channel_id") or RUNS_CHANNEL_ID
    embed = fmt_run_embed(run, members)
    view = make_rsvp_view(run_id)
    mentions = " ".join(f"<@{m['discord_id']}>" for m in pending if m.get("discord_id"))

    await _post_to_channel(
        interaction.client, target_channel_id,
        f"📨 **Reminder for Run #{run_id}** — pending members: {mentions}\nPlease respond:",
        embed=embed, view=view,
    )

    tg_notified, _ = await _notify_via_telegram(run_id, pending, run, {
        "reminder_label": "8:00 AM SGT on the day of the run"
    })

    summary = f"✅ Resent invite to {len(pending)} pending member(s)."
    if tg_notified:
        summary += f"\n📱 Telegram notified: {', '.join(tg_notified)}"
    await interaction.response.send_message(summary, ephemeral=True)


@client.tree.command(name="myruns", description="Show your upcoming runs")
async def slash_myruns(interaction: discord.Interaction):
    auto_register(interaction.user)
    runs = db.get_user_runs_discord(interaction.user.id)
    if not runs:
        await interaction.response.send_message("You have no upcoming runs.", ephemeral=True)
        return
    embeds = [fmt_run_embed(r, db.get_run_members_discord(r["id"])) for r in runs[:5]]
    await interaction.response.send_message("📅 **Your Upcoming Runs**", embeds=embeds, ephemeral=True)


@client.tree.command(name="runs", description="Show all upcoming guild runs")
async def slash_runs(interaction: discord.Interaction):
    runs = db.get_active_runs_discord()
    if not runs:
        await interaction.response.send_message("No upcoming runs.", ephemeral=True)
        return
    embeds = [fmt_run_embed(r, db.get_run_members_discord(r["id"])) for r in runs[:5]]
    await interaction.response.send_message("📅 **All Upcoming Runs**", embeds=embeds, ephemeral=True)


@client.tree.command(name="editrun", description="Edit a run's date/time or party")
@app_commands.describe(run_id="Run ID")
@app_commands.autocomplete(run_id=autocomplete_my_runs)
async def slash_editrun(interaction: discord.Interaction, run_id: int):
    auto_register(interaction.user)
    run = db.get_run(run_id)
    if not run:
        await interaction.response.send_message(f"⚠️ Run #{run_id} not found.", ephemeral=True)
        return
    if run["status"] == "cancelled":
        await interaction.response.send_message("⚠️ This run has been cancelled.", ephemeral=True)
        return

    members = db.get_run_members_discord(run_id)
    is_member = any(m.get("discord_id") == interaction.user.id for m in members)
    if not is_member:
        await interaction.response.send_message("⚠️ Only run members can edit this run.", ephemeral=True)
        return

    sgt = get_run_dt(run) + timedelta(hours=8)
    bosses = db.get_all_bosses()
    grouped = {}
    for b in bosses:
        grouped.setdefault(b["name"], []).append(b["difficulty"])

    run_data = {
        "boss_map": grouped,
        "boss_name": run["boss_name"],
        "difficulty": run["difficulty"],
        "creator_id": interaction.user.id,
        "edit_run_id": run_id,
        "origin_channel_id": run.get("discord_channel_id") or interaction.channel_id,
        "guild_id": interaction.guild_id,
        "selected_chars": [m["character_id"] for m in members],
    }

    await interaction.response.send_message(
        f"✏️ **Edit Run #{run_id}**\n\n"
        f"⚔️ {diff_icon(run['difficulty'])} {run['boss_name']} {run['difficulty']}\n"
        f"📅 {sgt.strftime('%d/%m/%Y %H:%M SGT')}\n\n"
        f"Select new members and/or date below:",
        view=MemberSelectView(run_data),
        ephemeral=True,
    )


# ── Team commands ─────────────────────────────────────────────────────────────

@client.tree.command(name="createteam", description="Create a preset team")
@app_commands.describe(name="Team name")
async def slash_createteam(interaction: discord.Interaction, name: str):
    auto_register(interaction.user)
    if len(name) > 50:
        await interaction.response.send_message("⚠️ Team name too long (max 50 chars).", ephemeral=True)
        return

    all_chars = db.get_all_characters_discord()
    if not all_chars:
        await interaction.response.send_message("No characters registered yet. Use `/register` first.", ephemeral=True)
        return

    platform_map = db.get_character_platform_info([ch["id"] for ch in all_chars])
    options = [
        discord.SelectOption(
            label=f"{ch['ign']}" + (f" Lv.{ch['level']}" if ch["level"] else ""),
            value=str(ch["id"]),
            description=f"{ch['class'] or 'No class'} [{platform_map.get(ch['id'], '⚠️')}]",
        )
        for ch in all_chars[:25]
    ]

    select = discord.ui.Select(
        placeholder="Select team members...",
        min_values=1,
        max_values=min(len(options), 25),
        options=options,
    )
    view = discord.ui.View(timeout=300)
    creator_id = interaction.user.id
    team_name = name

    async def on_select(inter: discord.Interaction):
        if inter.user.id != creator_id:
            await inter.response.send_message("⚠️ Only the team creator can use this.", ephemeral=True)
            return
        selected_ids = [int(v) for v in inter.data["values"]]
        # Use placeholder telegram_id for teams created by Discord users
        owner_tid = -(creator_id)
        team_id, err = db.create_team(team_name, owner_tid, selected_ids)
        if err:
            await inter.response.send_message(f"⚠️ {err}", ephemeral=True)
        else:
            chars = [db.get_character_by_id(cid) for cid in selected_ids]
            members = ", ".join(ch["ign"] for ch in chars if ch)
            await inter.response.edit_message(
                content=f"✅ Team **{team_name}** saved with {len(chars)} members: {members}",
                view=None,
            )

    select.callback = on_select
    view.add_item(select)
    await interaction.response.send_message(
        f"👥 **Create Team: {name}**\n\nSelect members:", view=view, ephemeral=True
    )


@client.tree.command(name="teams", description="List all preset teams")
async def slash_teams(interaction: discord.Interaction):
    teams = db.get_all_teams()
    if not teams:
        await interaction.response.send_message("No preset teams yet. Use `/createteam`.", ephemeral=True)
        return
    lines = ["👥 **Preset Teams**\n"]
    for t in teams:
        members = db.get_team_members(t["id"])
        names = ", ".join(m["ign"] for m in members)
        lines.append(f"• **{t['name']}** ({len(members)}): {names}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@client.tree.command(name="deleteteam", description="Delete a preset team")
@app_commands.describe(name="Team name")
async def slash_deleteteam(interaction: discord.Interaction, name: str):
    auto_register(interaction.user)
    team = db.get_team_by_name(name)
    if not team:
        await interaction.response.send_message(f"⚠️ Team **{name}** not found.", ephemeral=True)
        return
    db.delete_team(team["id"])
    await interaction.response.send_message(f"🗑️ Team **{name}** deleted.", ephemeral=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    db.init_db()
    client.run(DISCORD_TOKEN)
