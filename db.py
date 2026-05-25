"""
db.py — PostgreSQL version for Railway
"""

import os
import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL", "")

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
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username    TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS characters (
            id          SERIAL PRIMARY KEY,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            ign         TEXT NOT NULL UNIQUE,
            class       TEXT,
            level       INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bosses (
            id          SERIAL PRIMARY KEY,
            name        TEXT NOT NULL,
            difficulty  TEXT NOT NULL,
            UNIQUE(name, difficulty)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id           SERIAL PRIMARY KEY,
            boss_id      INTEGER NOT NULL REFERENCES bosses(id),
            leader_id    BIGINT  NOT NULL REFERENCES users(telegram_id),
            run_at       TIMESTAMP NOT NULL,
            remind_at    TIMESTAMP DEFAULT NULL,
            status       TEXT DEFAULT 'pending',
            created_at   TIMESTAMP DEFAULT NOW()
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS run_members (
            id           SERIAL PRIMARY KEY,
            run_id       INTEGER NOT NULL REFERENCES runs(id),
            character_id INTEGER NOT NULL REFERENCES characters(id),
            accepted     SMALLINT DEFAULT 0,
            UNIQUE(run_id, character_id)
        )
    """)

    # Sync bosses — only add new ones, never delete (runs may reference old IDs)
    for name, difficulties in BOSSES:
        for diff in difficulties:
            c.execute(
                "INSERT INTO bosses (name, difficulty) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (name, diff)
            )

    conn.commit()
    conn.close()

def _row(cursor, row):
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))

def _rows(cursor, rows):
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]

# ── Users & Characters ────────────────────────────────────────────────────────

def upsert_user(telegram_id, username):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """INSERT INTO users (telegram_id, username) VALUES (%s, %s)
           ON CONFLICT (telegram_id) DO UPDATE SET username=EXCLUDED.username""",
        (telegram_id, username)
    )
    conn.commit()
    conn.close()

def add_character(telegram_id, ign, cls=None, level=None):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO characters (telegram_id, ign, class, level) VALUES (%s,%s,%s,%s)",
            (telegram_id, ign, cls, level)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()

def remove_character(telegram_id, ign):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM characters WHERE telegram_id=%s AND ign=%s",
        (telegram_id, ign)
    )
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def get_characters(telegram_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM characters WHERE telegram_id=%s ORDER BY ign",
        (telegram_id,)
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def get_all_characters():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT ch.*, u.username FROM characters ch
           JOIN users u ON u.telegram_id=ch.telegram_id
           ORDER BY ch.ign"""
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def get_character_by_ign(ign):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT ch.*, u.username, u.telegram_id AS owner_tid
           FROM characters ch
           JOIN users u ON u.telegram_id=ch.telegram_id
           WHERE LOWER(ch.ign)=LOWER(%s)""",
        (ign,)
    )
    result = _row(c, c.fetchone())
    conn.close()
    return result

def get_character_by_id(char_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT ch.*, u.username FROM characters ch
           JOIN users u ON u.telegram_id=ch.telegram_id
           WHERE ch.id=%s""",
        (char_id,)
    )
    result = _row(c, c.fetchone())
    conn.close()
    return result

# ── Bosses ────────────────────────────────────────────────────────────────────

def get_all_bosses():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM bosses ORDER BY name, difficulty")
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def find_boss(name, difficulty):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM bosses WHERE LOWER(name)=LOWER(%s) AND LOWER(difficulty)=LOWER(%s)",
        (name, difficulty)
    )
    result = _row(c, c.fetchone())
    conn.close()
    return result

# ── Runs ──────────────────────────────────────────────────────────────────────

def create_run(boss_id, leader_id, run_at_iso):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO runs (boss_id, leader_id, run_at) VALUES (%s,%s,%s) RETURNING id",
        (boss_id, leader_id, run_at_iso)
    )
    run_id = c.fetchone()[0]
    conn.commit()
    conn.close()
    return run_id

def set_run_reminder(run_id, remind_at_iso):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE runs SET remind_at=%s WHERE id=%s",
        (remind_at_iso, run_id)
    )
    conn.commit()
    conn.close()

def add_run_member(run_id, character_id):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO run_members (run_id, character_id) VALUES (%s,%s)",
            (run_id, character_id)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()

def set_member_response(run_id, character_id, accepted):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE run_members SET accepted=%s WHERE run_id=%s AND character_id=%s",
        (accepted, run_id, character_id)
    )
    conn.commit()
    conn.close()

def get_run(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT r.*, b.name AS boss_name, b.difficulty,
                  u.username AS leader_username
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           JOIN users  u ON u.telegram_id=r.leader_id
           WHERE r.id=%s""",
        (run_id,)
    )
    result = _row(c, c.fetchone())
    conn.close()
    return result

def get_run_members(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT rm.*, ch.ign, ch.class, ch.level,
                  u.telegram_id, u.username
           FROM run_members rm
           JOIN characters ch ON ch.id=rm.character_id
           JOIN users      u  ON u.telegram_id=ch.telegram_id
           WHERE rm.run_id=%s
           ORDER BY ch.ign""",
        (run_id,)
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def get_run_member_by_char(run_id, character_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM run_members WHERE run_id=%s AND character_id=%s",
        (run_id, character_id)
    )
    result = _row(c, c.fetchone())
    conn.close()
    return result

def check_and_confirm_run(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT accepted FROM run_members WHERE run_id=%s", (run_id,))
    members = c.fetchall()
    if not members:
        conn.close()
        return False
    all_accepted = all(m[0] == 1 for m in members)
    if all_accepted:
        c.execute("UPDATE runs SET status='confirmed' WHERE id=%s", (run_id,))
        conn.commit()
    conn.close()
    return all_accepted

def cancel_run(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE runs SET status='cancelled' WHERE id=%s", (run_id,))
    conn.commit()
    conn.close()

def get_active_runs():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT r.*, b.name AS boss_name, b.difficulty,
                  u.username AS leader_username
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           JOIN users  u ON u.telegram_id=r.leader_id
           WHERE r.status != 'cancelled'
             AND r.run_at > NOW()
           ORDER BY r.run_at"""
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def get_user_runs(telegram_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT DISTINCT r.*, b.name AS boss_name, b.difficulty,
                  u.username AS leader_username
           FROM runs r
           JOIN bosses      b  ON b.id=r.boss_id
           JOIN users       u  ON u.telegram_id=r.leader_id
           JOIN run_members rm ON rm.run_id=r.id
           JOIN characters  ch ON ch.id=rm.character_id
           WHERE ch.telegram_id=%s
             AND r.status != 'cancelled'
             AND r.run_at > NOW()
           ORDER BY r.run_at""",
        (telegram_id,)
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def get_runs_due_for_reminder():
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT r.*, b.name AS boss_name, b.difficulty
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           WHERE r.status='confirmed'
             AND r.remind_at IS NOT NULL
             AND r.remind_at <= NOW()
             AND r.remind_at > NOW() - INTERVAL '15 minutes'
             AND r.run_at > NOW()"""
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def get_expired_pending_runs(hours=12):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        """SELECT r.*, b.name AS boss_name, b.difficulty
           FROM runs r
           JOIN bosses b ON b.id=r.boss_id
           WHERE r.status='pending'
             AND r.created_at <= NOW() - INTERVAL '%s hours'""",
        (hours,)
    )
    result = _rows(c, c.fetchall())
    conn.close()
    return result

def update_run_time(run_id, run_at_iso):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "UPDATE runs SET run_at=%s, remind_at=NULL, status='pending' WHERE id=%s",
        (run_at_iso, run_id)
    )
    conn.commit()
    conn.close()

def reset_run_members(run_id, new_char_ids):
    """Replace all members of a run and reset acceptances."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM run_members WHERE run_id=%s", (run_id,))
    for char_id in new_char_ids:
        try:
            c.execute(
                "INSERT INTO run_members (run_id, character_id) VALUES (%s,%s)",
                (run_id, char_id)
            )
        except Exception:
            pass
    c.execute("UPDATE runs SET status='pending' WHERE id=%s", (run_id,))
    conn.commit()
    conn.close()
