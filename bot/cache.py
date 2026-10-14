import sqlite3
from pathlib import Path


REFRESH_SECONDS = 6 * 60 * 60
JOIN_REFRESH_SECONDS = 60


class CacheUnavailable(Exception):
    pass


class MembershipCache:
    """Only names/emails from D1, plus local refresh timing metadata."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS members (
                full_name TEXT, uw_email TEXT, cs_email TEXT
            );
            CREATE INDEX IF NOT EXISTS members_name ON members(lower(full_name));
            CREATE TABLE IF NOT EXISTS refresh (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                next_attempt REAL NOT NULL, valid_until REAL NOT NULL
            );
            INSERT OR IGNORE INTO refresh (id, next_attempt, valid_until) VALUES (1, 0, 0);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(refresh)")}
        if "next_join_attempt" not in columns:
            self.db.execute("ALTER TABLE refresh ADD COLUMN next_join_attempt REAL NOT NULL DEFAULT 0")
            self.db.execute("""UPDATE refresh SET next_join_attempt=CASE
                WHEN valid_until=next_attempt AND valid_until>0 THEN next_attempt-?+?
                ELSE next_attempt END""", (REFRESH_SECONDS, JOIN_REFRESH_SECONDS))
        self.db.commit()

    def reserve_refresh(self, now, *, on_join=False):
        # Persist before the request: errors, concurrent callers and restarts
        # must not produce another remote query after a failed refresh.
        deadline = "next_join_attempt" if on_join else "next_attempt"
        with self.db:
            result = self.db.execute(
                f"UPDATE refresh SET next_attempt=?, next_join_attempt=? WHERE id=1 AND {deadline}<=?",
                (now + REFRESH_SECONDS, now + REFRESH_SECONDS, now),
            )
        return result.rowcount == 1

    def replace(self, rows):
        # Preserve duplicate people and both emails on the same record.
        # A failed import rolls back the entire snapshot.
        with self.db:
            self.db.execute("DELETE FROM members")
            self.db.executemany("INSERT INTO members VALUES (?, ?, ?)", rows)
            self.db.execute("UPDATE refresh SET valid_until=next_attempt WHERE id=1")
            self.db.execute("UPDATE refresh SET next_join_attempt=next_attempt-?+? WHERE id=1",
                            (REFRESH_SECONDS, JOIN_REFRESH_SECONDS))

    def contains_any_name(self, names):
        # Presence, not uniqueness or freshness: ambiguous names are still found.
        return any(self.db.execute(
            "SELECT 1 FROM members WHERE lower(full_name)=lower(trim(?)) LIMIT 1", (name,)
        ).fetchone() for name in names)

    def matches(self, name, email, now):
        valid_until = self.db.execute("SELECT valid_until FROM refresh WHERE id=1").fetchone()[0]
        if now >= valid_until:
            raise CacheUnavailable("Membership cache needs a successful refresh")
        where = "lower(full_name) = lower(trim(?))"
        params = [name.strip()]
        if email is not None:
            where += " AND (lower(trim(uw_email)) = lower(trim(?)) OR lower(trim(cs_email)) = lower(trim(?)))"
            params.extend([email.strip(), email.strip()])
        rows = self.db.execute(f"SELECT 1 FROM members WHERE {where} LIMIT 2", params).fetchall()
        return len(rows) == 1

    def close(self):
        self.db.close()
