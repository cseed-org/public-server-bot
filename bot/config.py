import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("D1 table/column names must be simple SQL identifiers")
    return f'"{value}"'


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    role_id: int
    account: str
    database: str
    cf_token: str
    gemini_key: str
    model: str
    table: str
    first_name_column: str
    last_name_column: str
    uw_email_column: str
    cs_email_column: str
    status_column: str
    status_value: str
    message_file: Path
    state_file: Path
    scan_seconds: float
    reminder_seconds: float
    cooldown_seconds: float
    error_message_file: Path = Path("messages/error.txt")
    failure_message_file: Path = Path("messages/verification_failed.txt")
    max_concurrent_api: int = 2
    max_api_per_minute: int = 60

    @classmethod
    def load(cls):
        load_dotenv()
        def required(key):
            value = os.getenv(key, "").strip()
            if not value:
                raise ValueError(f"Missing {key}; configure .env")
            return value
        def positive(key, default):
            value = float(os.getenv(key, default))
            if not 0 < value < float("inf"):
                raise ValueError(f"{key} must be positive and finite")
            return value
        def bounded_integer(key, default, maximum):
            value = int(os.getenv(key, default))
            if not 1 <= value <= maximum:
                raise ValueError(f"{key} must be an integer between 1 and {maximum}")
            return value
        config = cls(
            required("DISCORD_TOKEN"), int(required("DISCORD_GUILD_ID")),
            int(required("DISCORD_MEMBER_ROLE_ID")), required("CLOUDFLARE_ACCOUNT_ID"),
            required("CLOUDFLARE_D1_DATABASE_ID"), required("CLOUDFLARE_API_TOKEN"),
            required("GEMINI_API_KEY"), required("GEMINI_MODEL"),
            os.getenv("D1_TABLE", "People"),
            os.getenv("D1_FIRST_NAME_COLUMN", "first_name"),
            os.getenv("D1_LAST_NAME_COLUMN", "last_name"),
            os.getenv("D1_UW_EMAIL_COLUMN", "uw_email"),
            os.getenv("D1_CS_EMAIL_COLUMN", "cs_email"),
            os.getenv("D1_STATUS_COLUMN", ""),
            os.getenv("D1_STATUS_VALUE", "active"),
            Path(os.getenv("MESSAGE_FILE", "messages/verification.txt")),
            Path(os.getenv("STATE_DB", "data/state.sqlite3")),
            positive("SCAN_INTERVAL_SECONDS", "900"),
            positive("REMINDER_INTERVAL_HOURS", "24") * 3600,
            positive("DM_COOLDOWN_SECONDS", "60"),
            Path(os.getenv("ERROR_MESSAGE_FILE", "messages/error.txt")),
            Path(os.getenv("FAILURE_MESSAGE_FILE", "messages/verification_failed.txt")),
            bounded_integer("MAX_CONCURRENT_API", "2", 16),
            bounded_integer("MAX_API_PER_MINUTE", "60", 600),
        )
        for value in (config.table, config.first_name_column, config.last_name_column,
                      config.uw_email_column, config.cs_email_column):
            identifier(value)
        if config.status_column:
            identifier(config.status_column)
        config.message()
        config.message("error")
        config.message("failure")
        return config

    def message(self, kind="verification"):
        path = {"verification": self.message_file, "error": self.error_message_file,
                "failure": self.failure_message_file}[kind]
        text = path.read_text(encoding="utf-8").strip()
        if not text or len(text) > 2000:
            raise ValueError(f"{kind} message must contain between 1 and 2000 characters")
        return text
