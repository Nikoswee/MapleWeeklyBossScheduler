# 🍄 MapleStory Weekly Boss Scheduler

A bot for scheduling MapleStory boss runs with your guild — available on both **Telegram** and **Discord**. Create runs, invite members, track acceptances, and get automatic reminders. No spreadsheets, no hassle.

Works across platforms — create a run on Telegram, members get notified on Discord too, and vice versa.

---

## Quick Links
- [Setting up for your guild](#setting-up-for-your-guild)
- [Getting started as a member](#getting-started-as-a-member)
- [Creating a boss run](#creating-a-boss-run)
- [All commands](#all-commands)

---

## Setting up for your guild

> This section is for whoever is hosting the bot. Guild members can skip to [Getting started as a member](#getting-started-as-a-member).

### What you need
- A free [Railway](https://railway.app) account
- A free [GitHub](https://github.com) account
- 15 minutes

---

### Step 1 — Fork the repository

1. Go to the GitHub repo page
2. Click **Fork** (top right) → **Create fork**
3. You now have your own copy of the code

---

### Step 2 — Create a Telegram bot

1. Open Telegram → search for **@BotFather** → tap Start
2. Send `/newbot`
3. Choose a name (e.g. `MyGuild Boss Scheduler`)
4. Choose a username ending in `bot` (e.g. `myguild_bossbot`)
5. Copy the **bot token** — looks like `123456789:AAFxxx...`
6. Go to **Bot Settings → Group Privacy → Turn off** so it can read group messages

---

### Step 3 — Create a Discord bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → give it a name
3. Go to **Bot** tab → click **Reset Token** → copy the token
4. Under **Bot**, enable **Message Content Intent**
5. Go to **OAuth2 → URL Generator**:
   - Under **Scopes**: check `bot` and `applications.commands`
   - Under **Bot Permissions**: check `Send Messages`, `Embed Links`, `Read Message History`, `Use Slash Commands`
6. Copy the generated URL at the bottom and open it in your browser
7. Select your Discord server → click **Authorise**

---

### Step 4 — Deploy on Railway

1. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
2. Select your forked repository

#### Set up the database
3. In your project → **New** → **Database** → **Add PostgreSQL**
4. Railway automatically connects it — no extra setup needed

#### Deploy the Telegram bot
5. Click on your service → **Settings** → **Deploy**
6. The `Procfile` already sets the start command to `python bot.py`
7. Go to **Variables** tab and add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token |
| `GROUP_CHAT_ID` | Your Telegram group chat ID (see below) |
| `GROUP_THREAD_ID` | Your boss scheduling topic ID (if using forum groups) |

#### Deploy the Discord bot
8. Click **New Service** → **GitHub repo** (same repo)
9. Go to **Settings** → **Deploy** → set **Custom Start Command** to `python discord_bot.py`
10. Go to **Variables** and add:

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | Your Discord bot token |
| `DISCORD_GUILD_ID` | Your Discord server ID |
| `RUNS_CHANNEL_ID` | Channel ID where run invites are posted |
| `BOT_TOKEN` | Same Telegram bot token (for cross-platform notifications) |

> **How to get IDs:** Enable Developer Mode in Discord (Settings → Advanced → Developer Mode), then right-click any server/channel and select **Copy ID**.

---

### Step 5 — Get your Telegram group and thread IDs

1. Add your Telegram bot to your guild group
2. Go to the channel where you want run announcements posted
3. Type `/chatid` — the bot replies with the **Chat ID** and **Thread ID**
4. Add these to Railway as `GROUP_CHAT_ID` and `GROUP_THREAD_ID`

---

### Step 6 — Verify everything works

- **Telegram:** Send `/version` — the bot should reply with its start time
- **Discord:** Type `/start` — slash commands should appear and the bot should respond

---

### Runtime file

The repo includes a `runtime.txt` pinned to `python-3.11.11`. If Railway has trouble installing Python, change this to the latest 3.11.x version available.

---

## Getting started as a member

### Step 1 — Start the bot

**Telegram:** Open a private chat with the bot → tap **Start**
*(This is required before you can receive run invites via DM)*

**Discord:** Type `/start` in any channel

### Step 2 — Register your character

```
/register YourIGN Bowmaster 275
```
Class and level are optional.

### Step 3 — Link your accounts (recommended)

If your guild uses both Telegram and Discord, linking your accounts means you'll get run invites on both platforms no matter where the run was created.

1. On **Telegram**, type `/linkdiscord`
2. Copy the 8-character code (e.g. `AB12CD34`) — expires in 10 minutes
3. On **Discord**, type `/linkaccount AB12CD34`

---

## Creating a boss run

Type `/createrun` and follow the prompts:

1. **Pick a boss** — recently run bosses appear at the top as quick picks
2. **Pick difficulty**
3. **Add members** — load from a preset team or select individually
4. **Pick a date** — only the next 4 weeks are shown
5. **Pick a time** — common times (8pm–11pm) are shown as one-tap shortcuts
6. **Confirm** — review and post

A 30-minute reminder is set automatically. After creating, the bot tells you the run ID so you can edit or cancel it later.

---

## What happens after you create a run

```
Run created
    ↓
Every member gets a DM with ✅ Accept / ❌ Decline buttons
    ↓
Leader gets progress updates as each member responds (e.g. 3/6 accepted)
    ↓
All accepted → 🎉 RUN CONFIRMED — everyone notified
    ↓
Anyone declines → ❌ RUN CANCELLED — everyone notified
    ↓
30 minutes before → ⏰ REMINDER sent to all members
```

Runs that get no response within 12 hours are automatically cancelled.

---

## Preset Teams

Save your regular party so you don't have to select members every week.

```
/createteam Lotus Party     ← then select members
/teams                       ← see all teams
/editteam Lotus Party        ← change members
/deleteteam Lotus Party      ← remove a team
```

When creating a run, tap **Load from Team** to instantly pre-select your saved team.

---

## All Commands

### Characters
| Command | What it does |
|---|---|
| `/register <IGN> [Class] [Level]` | Register your character |
| `/chars` | Your characters |
| `/allchars` | All guild characters |

### Scheduling
| Command | What it does |
|---|---|
| `/createrun` | Create a boss run |
| `/quickrun <boss> <difficulty>` | Skip straight to member selection (Discord) |
| `/editrun <run ID>` | Change date/time or party members |
| `/cancelrun <run ID>` | Cancel a run |
| `/resendrun <run ID>` | Resend invites to members who haven't responded |
| `/myruns` | Runs you're invited to |
| `/runs` | All upcoming guild runs |

### Teams
| Command | What it does |
|---|---|
| `/createteam <name>` | Create a preset team |
| `/teams` | List all teams |
| `/editteam <name>` | Edit a team |
| `/deleteteam <name>` | Delete a team |

### Account
| Command | What it does |
|---|---|
| `/linkdiscord` | Generate a linking code (Telegram) |
| `/linkaccount <code>` | Link your Telegram account (Discord) |
| `/linkstatus` | Check if your accounts are linked |

---

## Member platform indicators

When the run summary is shown, each member displays a tag:

| Tag | Meaning |
|---|---|
| `[TG+DC]` | Linked on both platforms — notified on both |
| `[TG]` | Telegram only |
| `[DC]` | Discord only |
| `[⚠️]` | No platform linked — may not receive the invite |

---

## Tips

- All times are in **SGT (UTC+8)**
- Telegram members must send `/start` to the bot **privately** before they can receive DMs
- Discord members are notified via channel mentions in the runs channel
- If a member doesn't receive an invite, use `/resendrun <run ID>`
- Runs created on either platform notify members on **both** platforms if accounts are linked

---

## Boss List

| Boss | Difficulties |
|---|---|
| Lotus | Extreme |
| Kalos | Normal, Chaos, Extreme |
| Kaling | Normal, Hard, Extreme |
| First Adversary | Normal, Hard, Extreme |
| Black Mage | Normal, Hard, Extreme |
| Seren | Normal, Hard, Extreme |
| Malefic | Normal, Hard, Extreme |
| Limbo | Normal, Hard |
| Baldrix | Normal, Hard |

To update the boss list, edit `BOSSES` in `db.py` and run this in your Railway Postgres console:
```sql
DELETE FROM bosses WHERE name NOT IN (
  'Lotus','Kalos','Kaling','First Adversary',
  'Black Mage','Seren','Malefic','Limbo','Baldrix'
);
```
