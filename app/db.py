"""SQLite persistence.

Design rules
- Every fact that came from outside (credit, contact, metric, location, bio) points at a row in
  `sources` (provider + exact URL + retrieval time). Attribution lives on the fact, not the profile.
- People/labels/studios are all rows in `entities`; provider IDs live in `entity_identifiers` with
  the method that linked them, so identity merges are auditable and reversible.
- Relationships are *derived* from credits (see engine/network.py), so their evidence can never
  drift away from the underlying facts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    body TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,          -- musicbrainz | genius | discogs | wikidata | wikipedia | spotify | youtube | lastfm | website
    url TEXT NOT NULL,               -- human-viewable URL of the original page/record
    description TEXT,
    retrieved_at TEXT NOT NULL,
    UNIQUE(provider, url)
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    entity_type TEXT NOT NULL DEFAULT 'person',   -- person | group | label | studio | unknown
    disambiguation TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    profiled_at TEXT                             -- last time the full profile (bio/location/contacts/metrics) was refreshed
);
CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name);

CREATE TABLE IF NOT EXISTS entity_identifiers (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    match_method TEXT NOT NULL,      -- primary | cross_link | same_track_name | user
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_ident_entity ON entity_identifiers(entity_id);

CREATE TABLE IF NOT EXISTS aliases (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    source_id INTEGER REFERENCES sources(id),
    UNIQUE(entity_id, alias)
);

-- Possible duplicates that were deliberately NOT merged (shown to the user as ambiguity).
CREATE TABLE IF NOT EXISTS identity_questions (
    a_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    b_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    UNIQUE(a_id, b_id)
);

-- Per-source facts: location, bio, genre, occupation, ... Conflicts are displayed, not hidden.
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    field TEXT NOT NULL,             -- bio | area | begin_area | country | genre | occupation | life_span | image
    value TEXT NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    retrieved_at TEXT NOT NULL,
    UNIQUE(entity_id, field, value, source_id)
);

CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    artist_text TEXT,
    release_date TEXT,
    release_type TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_identifiers (
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    artist_text TEXT,
    norm_artist TEXT,
    release_date TEXT,
    release_id INTEGER REFERENCES releases(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_norm ON tracks(norm_title, norm_artist);
CREATE TABLE IF NOT EXISTS track_identifiers (
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    UNIQUE(provider, external_id)
);

CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- canonical role (see engine/roles.py)
    role_detail TEXT,                -- role as written by the source, e.g. "Additional Production"
    credited_as TEXT,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    provider TEXT NOT NULL,
    UNIQUE(track_id, entity_id, role, provider)
);
CREATE INDEX IF NOT EXISTS idx_credits_entity ON credits(entity_id);
CREATE INDEX IF NOT EXISTS idx_credits_track ON credits(track_id);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    contact_type TEXT NOT NULL,      -- email | phone | website | instagram | linkedin | x | tiktok | youtube | soundcloud | facebook | bandcamp | other_social
    purpose TEXT,                    -- management | booking | press | label | publishing | general | null
    value TEXT NOT NULL,
    context TEXT,                    -- surrounding text on the source page
    status TEXT NOT NULL,            -- verified | publicly_listed | unverified
    source_id INTEGER NOT NULL REFERENCES sources(id),
    last_checked_at TEXT NOT NULL,
    UNIQUE(entity_id, contact_type, value, source_id)
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    metric TEXT NOT NULL,            -- subscribers | channel_views | listeners | scrobbles | followers
    value INTEGER NOT NULL,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    retrieved_at TEXT NOT NULL       -- live value as of this time; older rows are history
);
CREATE INDEX IF NOT EXISTS idx_metrics_entity ON metrics(entity_id, platform, metric, retrieved_at);

-- Explicitly recorded "we looked and it is not available" outcomes, so the UI can say so.
CREATE TABLE IF NOT EXISTS unavailable (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    topic TEXT NOT NULL,             -- e.g. spotify.monthly_listeners
    reason TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    UNIQUE(entity_id, topic)
);

CREATE TABLE IF NOT EXISTS research_sessions (
    id INTEGER PRIMARY KEY,
    query TEXT NOT NULL,
    target_kind TEXT NOT NULL,       -- artist | song | album
    target_provider TEXT,
    target_id TEXT,
    root_entity_id INTEGER REFERENCES entities(id),
    root_track_id INTEGER REFERENCES tracks(id),
    root_release_id INTEGER REFERENCES releases(id),
    depth INTEGER NOT NULL DEFAULT 2,
    status TEXT NOT NULL,            -- queued | running | done | failed
    log TEXT NOT NULL DEFAULT '[]',
    result TEXT,
    title TEXT,                      -- user-given name when saved as a research project
    saved INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS session_tracks (
    session_id INTEGER NOT NULL REFERENCES research_sessions(id) ON DELETE CASCADE,
    track_id INTEGER NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
    via_entity_id INTEGER,           -- which person's research brought this track in (null = root)
    degree INTEGER NOT NULL,
    UNIQUE(session_id, track_id, via_entity_id)
);

-- Personal CRM (Step 3 builds on this; Step 1 already lets you save people)
CREATE TABLE IF NOT EXISTS saved_people (
    entity_id INTEGER PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'discovered',
    notes TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    follow_up TEXT,
    saved_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # -- low level -------------------------------------------------------------------
    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def x(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            return cur.lastrowid

    # -- sources ---------------------------------------------------------------------
    def source(self, provider: str, url: str, description: str | None = None) -> int:
        with self.lock:
            row = self.one("SELECT id FROM sources WHERE provider=? AND url=?", (provider, url))
            if row:
                self.x("UPDATE sources SET retrieved_at=?, description=COALESCE(?, description) WHERE id=?",
                       (now(), description, row["id"]))
                return row["id"]
            return self.x("INSERT INTO sources(provider,url,description,retrieved_at) VALUES(?,?,?,?)",
                          (provider, url, description, now()))

    # -- research session log --------------------------------------------------------
    def log(self, session_id: int, message: str, level: str = "info") -> None:
        with self.lock:
            row = self.one("SELECT log FROM research_sessions WHERE id=?", (session_id,))
            if not row:
                return
            entries = json.loads(row["log"])
            entries.append({"t": now(), "level": level, "msg": message})
            self.x("UPDATE research_sessions SET log=? WHERE id=?", (json.dumps(entries[-400:]), session_id))
