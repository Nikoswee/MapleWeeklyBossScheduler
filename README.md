# 🍄 MapleStory Guild Boss Scheduler — Telegram Bot

A Telegram bot for scheduling MapleStory boss runs with your guild. Create runs, invite party members, track acceptances, and get reminders — all through inline buttons.

---

## Features

- **Guided run creation** — step-by-step button flow, no typing needed
- **Party invitations** — members get a private DM with Accept/Decline buttons
- **Auto-cancel on decline** — if anyone declines, the run is cancelled and everyone is notified
- **Reminders** — set a reminder 1 hour, 30 mins, or 15 mins before the run
- **Edit runs** — change date/time or party members after creation
- **Resend invites** — re-DM members who haven't responded
- **Auto-cancel pending runs** — runs with no response auto-cancel after 12 hours
- **Grouped run list** — `/runs` shows confirmed and pending runs separately
- **Multi-character support** — each user can register multiple IGNs

---

## Stack

| Component | Technology |
|---|---|
| Bot framework | python-telegram-bot 20.7 |
| Database | PostgreSQL (via psycopg2-binary) |
| Scheduler | APScheduler |
| Hosting | Railway |

---

## Setup

### 1. Clone / upload files

Make sure your repository contains:
```
bot.py
db.py
requirements.txt
Procfile
runtime.txt
```

### 2. Create a Telegram bot

1. Open Telegram → message `@BotFather`
2. Send `/newbot` and follow the prompts
3. Copy your bot token (looks like `123456789:AAFxxx...`)
4. Go to **Bot Settings → Group Privacy → Turn off** so the bot can read group messages

### 3. Deploy on Railway

1. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
2. Select your repository
3. Add a **PostgreSQL** database: **New → Database → Add PostgreSQL**
4. Go to your bot service → **Variables** tab and add:

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token |
| `GROUP_CHAT_ID` | Your guild group chat ID (optional, see below) |

Railway automatically sets `DATABASE_URL` from the PostgreSQL service — no need to add it manually.

### 4. Get your group chat ID

1. Add your bot to the guild Telegram group
2. Send `/chatid` in the group
3. The bot replies with the chat ID (e.g. `-1001234567890`)
4. Add it as `GROUP_CHAT_ID` in Railway variables

### 5. Verify deployment

Type `/version` in Telegram — the bot replies with its start time in SGT.

---

## Bot Commands

### Characters
| Command | Description |
|---|---|
| `/register <IGN> [Class] [Level]` | Register a character (e.g. `/register Ayumilove Bowmaster 275`) |
| `/chars` | List your own characters |
| `/allchars` | List all guild characters |
| `/removechar <IGN>` | Remove one of your characters |

### Bosses
| Command | Description |
|---|---|
| `/bosses` | Show all bosses and available difficulties |

### Scheduling (Party Leaders)
| Command | Description |
|---|---|
| `/createrun` | Create a boss run — guided button flow |
| `/editrun <run_id>` | Edit date/time or party members of a run |
| `/cancelrun <run_id>` | Cancel a run and notify all members |
| `/resendrun <run_id>` | Resend invite DMs to members who haven't responded |

### Members
| Command | Description |
|---|---|
| `/myruns` | See all runs you're invited to |
| `/runs` | See all upcoming guild runs |

### Utility
| Command | Description |
|---|---|
| `/start` | Register yourself and see the welcome message |
| `/help` | Show all commands |
| `/chatid` | Get the current chat's ID |
| `/version` | Show bot start time |

---

## Boss Run Flow

```
Leader: /createrun
  → Pick boss (buttons)
  → Pick difficulty (buttons)
  → Select party members (toggle buttons)
  → Pick date (calendar)
  → Pick hour (grid)
  → Pick minute (quick picks + fine control)
  → Set reminder (1hr / 30min / 15min / none)
  → Confirm summary

Bot: DMs each member with Accept / Decline buttons
Bot: Posts announcement to group chat

Member taps Accept:
  → Leader gets progress update (e.g. "3/6 accepted")
  → When all accept → everyone gets CONFIRMED notification

Member taps Decline:
  → Run is auto-cancelled
  → All members + group notified with who declined
```

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

To change the boss list, edit the `BOSSES` list in `db.py`, then run this SQL on your Railway PostgreSQL database to clear old entries:

```sql
DELETE FROM bosses WHERE name NOT IN (
  'Lotus','Kalos','Kaling','First Adversary',
  'Black Mage','Seren','Malefic','Limbo','Baldrix'
);
```

---

## Scheduler

| Job | Schedule | Description |
|---|---|---|
| Reminders | Every 15 minutes | Sends reminder DMs for confirmed runs at the set reminder time |
| Auto-cancel | Every hour | Cancels pending runs older than 12 hours with no response |

---

## Important Notes

- **Members must DM the bot first** — every guild member needs to open a private chat with the bot and send `/start` before they can receive DM invitations
- **All times are SGT** (UTC+8)
- **IGNs are unique** across all users — each in-game name can only be registered once
- **Editing a run resets it to PENDING** — all members need to re-accept after any edit

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot logic, commands, conversation handlers |
| `db.py` | PostgreSQL database access layer |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway process definition |
| `runtime.txt` | Python version pin (3.11.9) |
