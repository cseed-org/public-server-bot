import sqlite3
from pathlib import Path


class State:
    """Contains Discord IDs and timing/preferences only, never names or email."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY, opted_out INTEGER NOT NULL DEFAULT 0,
            next_reminder REAL NOT NULL DEFAULT 0, next_reply REAL NOT NULL DEFAULT 0
        )""")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
        for column in ("next_check", "next_command"):
            if column not in columns:
                self.db.execute(f"ALTER TABLE users ADD COLUMN {column} REAL NOT NULL DEFAULT 0")
        self.db.commit()

    def get(self, user_id):
        row = self.db.execute(
            "SELECT opted_out, next_reminder, next_reply FROM users WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        return row or (0, 0, 0)

    def deadline(self, user_id, field):
        if field not in {"next_check", "next_command"}:
            raise ValueError("Invalid deadline field")
        row = self.db.execute(f"SELECT {field} FROM users WHERE user_id=?", (str(user_id),)).fetchone()
        return row[0] if row else 0

    def update(self, user_id, **values):
        if not values.keys() <= {"opted_out", "next_reminder", "next_reply", "next_check", "next_command"}:
            raise ValueError("Invalid state field")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (str(user_id),))
            for key, value in values.items():
                self.db.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, str(user_id)))

    def reserve_reminder(self, user_id, now, interval):
        """Commit the daily attempt before sending, including across connections."""
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (str(user_id),))
            result = self.db.execute(
                "UPDATE users SET next_reminder=? "
                "WHERE user_id=? AND opted_out=0 AND next_reminder<=?",
                (now + max(86400, interval), str(user_id), now),
            )
        return result.rowcount == 1

    def close(self):
        self.db.close()
