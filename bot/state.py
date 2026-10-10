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
        self.db.commit()

    def get(self, user_id):
        row = self.db.execute(
            "SELECT opted_out, next_reminder, next_reply FROM users WHERE user_id=?",
            (str(user_id),),
        ).fetchone()
        return row or (0, 0, 0)

    def update(self, user_id, **values):
        if not values.keys() <= {"opted_out", "next_reminder", "next_reply"}:
            raise ValueError("Invalid state field")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (str(user_id),))
            for key, value in values.items():
                self.db.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, str(user_id)))

    def close(self):
        self.db.close()
