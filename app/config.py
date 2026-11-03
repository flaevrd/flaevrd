"""Settings loaded from environment variables (and an optional local `.env` file).

Secrets only ever live in the environment / `.env`; they are never sent to the frontend.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "FLAEVRD-Network"
APP_VERSION = "0.1.0"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    contact_email: str
    genius_token: str
    discogs_token: str
    youtube_key: str
    lastfm_key: str
    spotify_id: str
    spotify_secret: str
    web_contacts: bool
    db_path: Path
    max_releases: int
    max_credit_recordings: int
    max_expansions: int
    cache_hours: int

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "no-contact-configured"
        return f"{APP_NAME}/{APP_VERSION} ( {contact} )"


def load_settings() -> Settings:
    db = Path(os.environ.get("FLAEVRD_DB_PATH", "data/flaevrd.db"))
    if not db.is_absolute():
        db = ROOT / db
    return Settings(
        contact_email=os.environ.get("FLAEVRD_CONTACT_EMAIL", "").strip(),
        genius_token=os.environ.get("GENIUS_ACCESS_TOKEN", "").strip(),
        discogs_token=os.environ.get("DISCOGS_TOKEN", "").strip(),
        youtube_key=os.environ.get("YOUTUBE_API_KEY", "").strip(),
        lastfm_key=os.environ.get("LASTFM_API_KEY", "").strip(),
        spotify_id=os.environ.get("SPOTIFY_CLIENT_ID", "").strip(),
        spotify_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip(),
        web_contacts=_bool("FLAEVRD_WEB_CONTACTS", True),
        db_path=db,
        max_releases=_int("FLAEVRD_MAX_RELEASES", 8),
        max_credit_recordings=_int("FLAEVRD_MAX_CREDIT_RECORDINGS", 25),
        max_expansions=_int("FLAEVRD_MAX_EXPANSIONS", 6),
        cache_hours=_int("FLAEVRD_CACHE_HOURS", 168),
    )


settings = load_settings()
