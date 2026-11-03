"""Classify profile URLs (from MusicBrainz url-rels, Wikidata, Discogs profiles) into
cross-provider identifiers and professional social/contact links."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("discogs", re.compile(r"discogs\.com/(?:[a-z]{2}/)?artist/(\d+)", re.I)),
    ("wikidata", re.compile(r"wikidata\.org/wiki/(Q\d+)", re.I)),
    ("spotify", re.compile(r"open\.spotify\.com/(?:intl-[a-z]+/)?artist/([A-Za-z0-9]{22})", re.I)),
    ("genius_url", re.compile(r"(https?://genius\.com/artists/[^/?#]+)", re.I)),
    ("youtube_channel", re.compile(r"youtube\.com/channel/(UC[\w-]{22})", re.I)),
    ("youtube_handle", re.compile(r"youtube\.com/(@[\w.\-]+)", re.I)),
    ("lastfm", re.compile(r"last\.fm/music/([^/?#]+)", re.I)),
]

_SOCIAL: list[tuple[str, re.Pattern]] = [
    ("instagram", re.compile(r"instagram\.com/([A-Za-z0-9_.]+)/?$", re.I)),
    ("linkedin", re.compile(r"linkedin\.com/(?:in|company)/[^/?#]+", re.I)),
    ("x", re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]+)/?$", re.I)),
    ("tiktok", re.compile(r"tiktok\.com/@[\w.]+", re.I)),
    ("soundcloud", re.compile(r"soundcloud\.com/[^/?#]+/?$", re.I)),
    ("youtube", re.compile(r"youtube\.com/(?:channel/|@|c/|user/)", re.I)),
    ("facebook", re.compile(r"facebook\.com/[^/?#]+", re.I)),
    ("bandcamp", re.compile(r"\.bandcamp\.com", re.I)),
]

_NOT_SOCIAL_PATHS = {"p", "explore", "share", "sharer", "intent", "hashtag", "watch"}


def provider_ids(url: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, pat in _PATTERNS:
        m = pat.search(url or "")
        if m:
            out[name] = m.group(1)
    return out


def social_type(url: str) -> str | None:
    u = (url or "").strip()
    path = urlsplit(u).path.strip("/").split("/")
    if path and path[0].lower() in _NOT_SOCIAL_PATHS:
        return None
    for name, pat in _SOCIAL:
        if pat.search(u):
            return name
    return None


def normalize_url(url: str) -> str:
    u = (url or "").strip()
    if u and not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    return u.rstrip("/")
