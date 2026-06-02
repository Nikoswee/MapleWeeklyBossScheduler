# 🍄 MapleStory Guild Boss Scheduler — Telegram Bot

A Telegram bot for scheduling MapleStory boss runs with your guild. Create runs, invite party members, track acceptances, and get reminders — all through inline buttons, no typing needed.

---

## Features

- **Guided run creation** — step-by-step button flow for boss, difficulty, members, date, time, and reminder
- **Preset teams** — save recurring party lineups and load them in one tap when creating a run
- **Private DM invitations** — each member gets an Accept / Decline button in their DM
- **Auto-cancel on decline** — if anyone declines, the run is cancelled and all members are notified
- **Progress tracking** — leader gets notified as each member accepts (e.g. 3/6 accepted)
- **Run confirmation** — when all members accept, everyone gets a confirmed notification
- **Reminders** — set a reminder 1 hour, 30 mins, or 15 mins before the run
- **Edit runs** — change date/time or swap party members after creation (resets to pending)
- **Resend invites** — re-DM members who haven't responded yet
- **Auto-cancel pending runs** — runs with no response auto-cancel after 12 hours
- **Grouped run list** — `/runs` shows confirmed and pending runs in separate sections
- **Multi-character support** — each user can register multiple IGNs

---

## Stack

| Component | Technology |
|---|---|
| Bot framework | python-telegram-bot 20.7 |
| Database | PostgreSQL (psycopg2-binary) |
| Scheduler | APScheduler |
| Hosting | Railway |
| Python version | 3.11.11 |

---

## Setup

### 1. Repository files

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
3. Copy your **bot token** (e.g. `123456789:AAFxxx...`)
4. Go to **Bot Settings → Group Privacy → Turn off** so the bot can read messages in groups

### 3. Deploy on Railway

1. Go to [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**
2. Select your repository
3. Add a PostgreSQL database: **New → Database → Add PostgreSQL**
4. Go to your bot service → **Variables** tab and set:

| Variable | Value | Required |
|---|---|---|
| `BOT_TOKEN` | Your Telegram bot token | ✅ Yes |
| `GROUP_CHAT_ID` | Your guild group chat ID | Optional |
| `GROUP_THREAD_ID` | Thread/topic ID for announcements | Optional |

Railway sets `DATABASE_URL` automatically from the PostgreSQL service.

### 4. Get your group and thread IDs

1. Add your bot to your guild Telegram group
2. Type `/chatid` in the channel you want announcements posted to
3. The bot replies with both **Chat ID** and **Thread ID**
4. Set these as Railway environment variables

### 5. Verify

Type `/version` in Telegram — the bot replies with its start time in SGT confirming it's live.

---

## All Commands

### Characters

| Command | Description |
|---|---|
| `/register <IGN> [Class] [Level]` | Register a character — e.g. `/register Ayumilove Bowmaster 275` |
| `/chars` | List your own registered characters |
| `/allchars` | List all guild characters |
| `/removechar <IGN>` | Remove one of your characters |

### Bosses

| Command | Description |
|---|---|
| `/bosses` | Show all available bosses and difficulties |

### Preset Teams

| Command | Description |
|---|---|
| `/createteam` | Create a preset party (type name → select members → confirm) |
| `/teams` | List all saved preset teams |
| `/editteam <name>` | Rename a team or change its members |
| `/deleteteam <name>` | Delete a preset team |

### Scheduling (Party Leaders)

| Command | Description |
|---|---|
| `/createrun` | Create a boss run — full guided button flow |
| `/editrun <run_id>` | Edit the date/time or party members of a run |
| `/cancelrun <run_id>` | Cancel a run and notify all members |
| `/resendrun <run_id>` | Re-DM members who haven't responded yet |

### Members

| Command | Description |
|---|---|
| `/myruns` | See all upcoming runs you're invited to |
| `/runs` | See all upcoming guild runs (grouped by status) |

### Utility

| Command | Description |
|---|---|
| `/start` | Register yourself and see the welcome message |
| `/help` | Show all commands |
| `/chatid` | Get the current chat ID and thread ID |
| `/version` | Show bot start time in SGT |

---

## Boss Run Flow

```
Leader: /createrun
  Step 1 → Pick boss (buttons)
  Step 2 → Pick difficulty (buttons)
  Step 3 → Select party members
           - Toggle individuals on/off
           - Tap a 📋 preset team to pre-load that team's members
           - Mix: load a team then add/remove individuals
  Step 4 → Pick date (calendar — past dates hidden)
  Step 5 → Pick hour + minute (grid + fine controls)
  Step 6 → Set reminder (1hr / 30min / 15min / none)
         → Review summary → Confirm

Bot: DMs each member with ✅ Accept / ❌ Decline buttons
Bot: Posts announcement to group/thread

Member taps Accept:
  → Leader gets progress update (e.g. "3/6 accepted")
  → When all accept → CONFIRMED notification to all members + group

Member taps Decline:
  → Run is auto-cancelled
  → All members + group notified with who declined
  → Leader can use /createrun to start a new one
```

---

## Preset Teams Flow

```
Leader: /createteam
  → Type team name (e.g. "Lotus Party")
  → Select members (toggle buttons)
  → Confirm

When creating a run:
  → In Step 3, saved teams appear as 📋 buttons
  → Tap a team to instantly pre-select all its members
  → Still add or remove individuals as needed

/editteam Lotus Party
  → Choose: Rename OR Edit Members
  → Make changes → saved instantly
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

To update the boss list, edit `BOSSES` in `db.py`, then run this in Railway's Postgres query tab to remove old entries:

```sql
DELETE FROM bosses
WHERE name NOT IN (
  'Lotus','Kalos','Kaling','First Adversary',
  'Black Mage','Seren','Malefic','Limbo','Baldrix'
);
```

---

## Scheduler Jobs

| Job | Frequency | Description |
|---|---|---|
| Reminders | Every 15 min | Sends reminder DMs for confirmed runs at their set reminder time (30-min lookback window) |
| Auto-cancel | Every hour | Cancels pending runs older than 12 hours with no full response |

---

## Run List Format

`/runs` and `/myruns` display runs grouped by status:

```
📅 UPCOMING RUNS

✅ CONFIRMED
──────────────
⚔️ #1 · Lotus Hard 🟠
📅 28/06/2026 21:00 SGT
👑 @leader
👥 6/6 accepted
- - - - - - - - - - - - - -
⚔️ #2 · Kaling Extreme ⚫
📅 30/06/2026 20:00 SGT
👑 @leader
👥 4/6 accepted · Pending: IGN1 (@user1), IGN2 (@user2)

⏳ PENDING
──────────────
⚔️ #3 · Baldrix Hard 🟠
📅 01/07/2026 21:00 SGT
👑 @leader2
👥 0/4 accepted · Pending: IGN1, IGN2, IGN3, IGN4
```

---

## Important Notes

- **Every member must DM the bot first** — each guild member must open a private chat with the bot and send `/start` before they can receive DM invitations. Sending `/start` in the group is not enough.
- **All times are SGT** (UTC+8)
- **IGNs are globally unique** — each in-game name can only be registered once across all users
- **Editing a run resets it to PENDING** — all members must re-accept after any edit
- **Declining cancels the run** — if any member declines, the entire run is cancelled and the leader must create a new one
- **Multiple instances** — if you see a "Conflict: terminated by other getUpdates" error, make sure the bot is not running locally AND on Railway at the same time. Restart Railway to fix it.

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot logic — commands, conversation handlers, scheduler |
| `db.py` | PostgreSQL database access layer |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway worker process definition |
| `runtime.txt` | Python version pin (3.11.11) |
