import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "maplebot.db")

BOSSES = [
    ("Lotus",           ["Extreme"]),
    ("Kalos",           ["Normal", "Chaos", "Extreme"]),
    ("Kaling",          ["Normal", "Hard", "Extreme"]),
    ("First Adversary", ["Normal", "Hard", "Extreme"]),
    ("Black Mage",      ["Normal", "Hard", "Extreme"]),
    ("Seren",           ["Normal", "Hard", "Extreme"]),
    ("Malefic",         ["Normal", "Hard", "Extreme"]),
    ("Limbo",           ["Normal", "Hard"]),
    ("Baldrix",         ["Normal", "Hard"]),
]

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username    TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL,
            ign         TEXT NOT NULL,
            class       TEXT,
            level       INTEGER,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id),
            UNIQUE(ign)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bosses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            UNIQUE(name, difficulty)
        )
    """)

    # A scheduled boss run created by a party leader
    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            boss_id      INTEGER NOT NULL,
            leader_id    INTEGER NOT NULL,
            run_at       TEXT NOT NULL,        -- ISO datetime string (UTC)
            status       TEXT DEFAULT 'pending', -- pending | confirmed | cancelled
            created_at   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (boss_id)   REFERENCES bosses(id),
            FOREIGN KEY (leader_id) REFERENCES users(telegram_id)
        )
    """)

    # Members invited to a run
    c.execute("""
        CREATE TABLE IF NOT EXISTS run_members (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id       INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            accepted     INTEGER DEFAULT 0,   -- 0=pending, 1=accepted, -1=declined
            FOREIGN KEY (run_id)       REFERENCES runs(id),
            FOREIGN KEY (character_id) REFERENCES characters(id),
            UNIQUE(run_id, character_id)
        )
    """)

    for name, difficulties in BOSSES:
        for diff in difficulties:
            c.execute("INSERT OR IGNORE INTO bosses (name, difficulty) VALUES (?,?)", (name, diff))

    conn.commit()
    conn.close()

# ── Users & Characters ────────────────────────────────────────────────────────

def upsert_user(telegram_id, username):
    conn = get_conn()
    conn.execute(
        "INSERT INTO users (telegram_id, username) VALUES (?,?) "
        "ON CONFLICT(telegram_id) DO UPDATE SET username=excluded.username",
        (telegram_id, username)
    )
    conn.commit()
    conn.close()

def add_character(telegram_id, ign, cls=None, level=None):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO characters (telegram_id, ign, class, level) VALUES (?,?,?,?)",
            (telegram_id, ign, cls, level)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def remove_character(telegram_id, ign):
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM characters WHERE telegram_id=? AND ign=?", (telegram_id, ign)
    )
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted

def get_characters(telegram_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM characters WHERE telegram_id=? ORDER BY ign", (telegram_id,)
    ).fetchall()
    conn.close()
    return rows

def get_all_characters():
    conn = get_conn()
    rows = conn.execute(
        "SELECT c.*, u.username FROM characters c JOIN users u ON u.telegram_id=c.telegram_id ORDER BY c.ign"
    ).fetchall()
    conn.close()
    return rows

def get_character_by_ign(ign):
    conn = get_conn()
    row = conn.execute(
        "SELECT c.*, u.username, u.telegram_id as owner_tid FROM characters c "
        "JOIN users u ON u.telegram_id=c.telegram_id WHERE LOWER(c.ign)=LOWER(?)", (ign,)
    ).fetchone()
    conn.close()
    return row

def get_all_bosses():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bosses ORDER BY name, difficulty").fetchall()
    conn.close()
    return rows

def find_boss(name, difficulty):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM bosses WHERE LOWER(name)=LOWER(?) AND LOWER(difficulty)=LOWER(?)",
        (name, difficulty)
    ).fetchone()
    conn.close()
    return row

# ── Runs ──────────────────────────────────────────────────────────────────────

def create_run(boss_id, leader_id, run_at_iso):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO runs (boss_id, leader_id, run_at) VALUES (?,?,?)",
        (boss_id, leader_id, run_at_iso)
    )
    run_id = cur.lastrowid
    conn.commit()
    conn.close()
    return run_id

def add_run_member(run_id, character_id):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO run_members (run_id, character_id) VALUES (?,?)",
            (run_id, character_id)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def set_member_response(run_id, character_id, accepted: int):
    """accepted: 1=yes, -1=no"""
    conn = get_conn()
    conn.execute(
        "UPDATE run_members SET accepted=? WHERE run_id=? AND character_id=?",
        (accepted, run_id, character_id)
    )
    conn.commit()
    conn.close()

def get_run(run_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT r.*, b.name as boss_name, b.difficulty,
                  u.username as leader_username
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           JOIN users u ON u.telegram_id=r.leader_id
           WHERE r.id=?""", (run_id,)
    ).fetchone()
    conn.close()
    return row

def get_run_members(run_id):
    conn = get_conn()
    rows = conn.execute(
        """SELECT rm.*, c.ign, c.class, c.level, u.telegram_id, u.username
           FROM run_members rm
           JOIN characters c ON c.id=rm.character_id
           JOIN users u ON u.telegram_id=c.telegram_id
           WHERE rm.run_id=?
           ORDER BY c.ign""", (run_id,)
    ).fetchall()
    conn.close()
    return rows

def get_run_member_by_char(run_id, character_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM run_members WHERE run_id=? AND character_id=?",
        (run_id, character_id)
    ).fetchone()
    conn.close()
    return row

def check_and_confirm_run(run_id):
    """If all members accepted, mark run as confirmed. Returns True if confirmed."""
    conn = get_conn()
    members = conn.execute(
        "SELECT accepted FROM run_members WHERE run_id=?", (run_id,)
    ).fetchall()
    if not members:
        conn.close()
        return False
    all_accepted = all(m["accepted"] == 1 for m in members)
    if all_accepted:
        conn.execute("UPDATE runs SET status='confirmed' WHERE id=?", (run_id,))
        conn.commit()
    conn.close()
    return all_accepted

def cancel_run(run_id):
    conn = get_conn()
    conn.execute("UPDATE runs SET status='cancelled' WHERE id=?", (run_id,))
    conn.commit()
    conn.close()

def get_active_runs():
    """Get all pending/confirmed runs that haven't happened yet."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, b.name as boss_name, b.difficulty, u.username as leader_username
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           JOIN users u ON u.telegram_id=r.leader_id
           WHERE r.status != 'cancelled' AND r.run_at > datetime('now')
           ORDER BY r.run_at"""
    ).fetchall()
    conn.close()
    return rows

def get_runs_due_for_reminder():
    """Runs that are confirmed, happening today (UTC), reminder not yet sent."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, b.name as boss_name, b.difficulty
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           WHERE r.status='confirmed'
             AND date(r.run_at)=date('now')
             AND r.run_at > datetime('now')"""
    ).fetchall()
    conn.close()
    return rows

def get_user_runs(telegram_id):
    """Runs where this user's characters are invited, not yet done."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT DISTINCT r.*, b.name as boss_name, b.difficulty, u.username as leader_username
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           JOIN users u ON u.telegram_id=r.leader_id
           JOIN run_members rm ON rm.run_id=r.id
           JOIN characters c ON c.id=rm.character_id
           WHERE c.telegram_id=?
             AND r.status != 'cancelled'
             AND r.run_at > datetime('now')
           ORDER BY r.run_at""", (telegram_id,)
    ).fetchall()
    conn.close()
    return rows

def get_character_by_id(char_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT c.*, u.username FROM characters c "
        "JOIN users u ON u.telegram_id=c.telegram_id WHERE c.id=?", (char_id,)
    ).fetchone()
    conn.close()
    return row

def set_run_reminder(run_id, remind_at_iso):
    conn = get_conn()
    conn.execute(
        "UPDATE runs SET remind_at=? WHERE id=?",
        (remind_at_iso, run_id)
    )
    conn.commit()
    conn.close()

def get_runs_due_for_reminder():
    """Confirmed runs whose remind_at has passed but run hasn't started yet."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, b.name as boss_name, b.difficulty
           FROM runs r
           JOIN bosses b ON b.id = r.boss_id
           WHERE r.status = 'confirmed'
             AND r.remind_at IS NOT NULL
             AND r.remind_at <= datetime('now')
             AND r.remind_at > datetime('now', '-1 hour')
             AND r.run_at > datetime('now')"""
    ).fetchall()
    conn.close()
    return rows