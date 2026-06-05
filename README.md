# 🍄 MapleStory Weekly Boss Scheduler

A bot for scheduling MapleStory boss runs with your guild — available on both **Telegram** and **Discord**. Create runs, invite members, track who's accepted, and get automatic reminders. No spreadsheets, no hassle.

---

## What it does

- **Create a boss run** in a few taps — pick the boss, select your party, choose a date and time
- **Automatically invites** every party member via private message with Accept / Decline buttons
- **Tracks responses** in real time — the leader sees who has accepted and who hasn't
- **Confirms the run** when everyone accepts and notifies the whole party
- **Cancels the run** automatically if anyone declines and notifies everyone
- **Sends a reminder** 30 minutes before every run
- **Works across platforms** — create a run on Telegram, members get notified on Discord too, and vice versa

---

## Getting started

### Step 1 — Start the bot

**Telegram:** Search for the bot username and tap **Start**

**Discord:** Type `/start` in any channel

### Step 2 — Register your character

**Telegram:**
```
/register YourIGN Bowmaster 275
```

**Discord:**
```
/register YourIGN Bowmaster 275
```

Class and level are optional — just the IGN is enough to get started.

### Step 3 — Link your accounts (optional but recommended)

If your guild uses both Telegram and Discord, linking your accounts means you'll receive run invites on both platforms no matter where the run was created.

1. On **Telegram**, type `/linkdiscord`
2. You'll get an 8-character code (e.g. `AB12CD34`) — it expires in 10 minutes
3. On **Discord**, type `/linkaccount AB12CD34`

Done — your accounts are now linked.

---

## Creating a boss run

Type `/createrun` (Telegram) or `/createrun` (Discord) and follow the prompts:

1. **Pick a boss** — recently run bosses appear at the top for quick selection
2. **Pick difficulty**
3. **Add members** — choose from a preset team or select individually
4. **Pick a date** — only the next 4 weeks are shown
5. **Pick a time** — common times (8pm, 9pm, 10pm, 11pm) are shown as one-tap shortcuts
6. **Confirm** — review the summary and post the run

The bot will automatically DM every invited member with Accept / Decline buttons. A 30-minute reminder is set automatically.

---

## Preset Teams

If you run with the same group every week, save them as a preset team so you don't have to select members every time.

**Create a team:**
```
/createteam Lotus Party
```
Then select the members and confirm.

**When creating a run**, tap **Load from Team** to instantly pre-select your saved team.

**Manage teams:**
```
/teams            — see all saved teams
/editteam Lotus Party   — change the members
/deleteteam Lotus Party — delete the team
```

---

## All Commands

### Characters
| Command | What it does |
|---|---|
| `/register <IGN> [Class] [Level]` | Register your character |
| `/chars` | See your characters |
| `/allchars` | See all guild characters |

### Scheduling
| Command | What it does |
|---|---|
| `/createrun` | Create a boss run (guided) |
| `/editrun <run ID>` | Change the date/time or party members |
| `/cancelrun <run ID>` | Cancel a run |
| `/resendrun <run ID>` | Resend invites to members who haven't responded |
| `/myruns` | See runs you're invited to |
| `/runs` | See all upcoming guild runs |

### Teams
| Command | What it does |
|---|---|
| `/createteam <name>` | Create a preset team |
| `/teams` | List all preset teams |
| `/editteam <name>` | Edit a team's members |
| `/deleteteam <name>` | Delete a team |

### Account
| Command | What it does |
|---|---|
| `/linkdiscord` | Generate a code to link your Discord account (Telegram only) |
| `/linkaccount <code>` | Link your Telegram account (Discord only) |
| `/linkstatus` | Check if your accounts are linked |

---

## Run flow — what happens after you create a run

```
You create the run
        ↓
Every member gets a DM with Accept / Decline buttons
        ↓
As each member accepts, you get a progress update (e.g. 3/6 accepted)
        ↓
Once everyone accepts → RUN CONFIRMED — all members notified
        ↓
If anyone declines → RUN CANCELLED — all members notified
        ↓
30 minutes before the run → REMINDER sent to all members
```

---

## Member invite indicators

When you review the run summary before confirming, each member shows a platform tag:

| Tag | Meaning |
|---|---|
| `[TG+DC]` | Has both Telegram and Discord linked — will be notified on both |
| `[TG]` | Telegram only |
| `[DC]` | Discord only |
| `[⚠️]` | No platform linked — may not receive the invite |

---

## Tips

- **All times are in SGT (UTC+8)**
- **Members must start the bot first** — on Telegram, each member needs to open a private chat with the bot and send `/start` before they can receive invites
- **To edit or cancel a run**, the bot will tell you the run ID after creation (e.g. `To edit: /editrun 5`)
- **Runs auto-cancel** if not everyone responds within 12 hours
- **Discord quick run** — use `/quickrun Lotus Hard` to skip straight to member selection
