"""Genius API (https://docs.genius.com). Needs a free client access token.

Used for song credits only (producers, writers, featured artists and the "custom performances"
block: engineers, A&R, labels, studios, musicians) plus artist bios/handles. Lyrics are never
requested or stored.
"""
from __future__ import annotations

from ..config import settings
from ..engine.normalize import is_video_credit, norm_name, norm_title, role_from_text
from ..http import Http
from .base import auth_header, Candidate, ContactRec, FactRec, NotConfigured, PersonRef, ProfileData, ReleaseRef, TrackRec

API = "https://api.genius.com"
NAME = "genius"

_BUSINESS_TYPES = {"label": "label", "publisher": "label", "studio": "studio"}


def _ref(a: dict, role: str | None = None) -> PersonRef:
    etype = _BUSINESS_TYPES.get(role or "", "person")
    return PersonRef(NAME, str(a["id"]), a.get("name", "?"), a.get("url"), etype)


class Genius:
    name = NAME
    requires = ["GENIUS_ACCESS_TOKEN"]

    def __init__(self, http: Http):
        self.http = http

    def configured(self) -> bool:
        return bool(settings.genius_token)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.configured():
            raise NotConfigured("GENIUS_ACCESS_TOKEN")
        r = await self.http.get(f"{API}{path}", params=params,
                                headers=auth_header("Bearer ", settings.genius_token))
        return r.json().get("response", {})

    async def search(self, q: str) -> list[dict]:
        data = await self._get("/search", {"q": q, "per_page": 10})
        return [h["result"] for h in data.get("hits", []) if h.get("type") == "song"]

    async def search_songs(self, q: str, limit: int = 6) -> list[Candidate]:
        """Score like MusicBrainz (0-100) so both sources sort together: exact title + artist 100,
        exact title 80, otherwise 40 (covers, parodies, 'type beats' sink below real matches)."""
        from .musicbrainz import split_query
        title, artist = split_query(q)
        nt, na = norm_title(title), norm_name(artist or "")
        out = []
        for s in await self.search(f"{title} {artist}" if artist else title):
            pa = norm_name((s.get("primary_artist") or {}).get("name", ""))
            same_title = norm_title(s.get("title", "")) == nt
            score = 100.0 if same_title and na and (na == pa or na in pa) else 80.0 if same_title and not na else 40.0
            out.append(Candidate("song", NAME, str(s["id"]), s.get("title", ""),
                                 " · ".join(b for b in [s.get("primary_artist", {}).get("name"),
                                                        s.get("release_date_for_display")] if b),
                                 s.get("url"), score))
        out.sort(key=lambda c: -c.score)
        return out[:limit]

    async def search_artists(self, q: str, limit: int = 4) -> list[Candidate]:
        seen, out = set(), []
        for s in await self.search(q):
            a = s.get("primary_artist") or {}
            if a.get("id") and a["id"] not in seen:
                seen.add(a["id"])
                out.append(Candidate("artist", NAME, str(a["id"]), a.get("name", ""), "Genius artist", a.get("url")))
        return out[:limit]

    async def find_artist_by_url(self, url: str, name: str) -> str | None:
        """Map a Genius artist URL (from a MusicBrainz/Wikidata link) to its API id via search."""
        u = url.rstrip("/").lower()
        for s in await self.search(name):
            for a in [s.get("primary_artist") or {}] + (s.get("featured_artists") or []):
                if (a.get("url") or "").rstrip("/").lower() == u:
                    return str(a["id"])
        return None

    async def artist(self, artist_id: str) -> dict:
        return (await self._get(f"/artists/{artist_id}", {"text_format": "plain"})).get("artist", {})

    async def artist_song_ids(self, artist_id: str, limit: int = 20) -> list[str]:
        data = await self._get(f"/artists/{artist_id}/songs", {"sort": "popularity", "per_page": min(limit, 50)})
        return [str(s["id"]) for s in data.get("songs", [])][:limit]

    def profile_from_artist(self, a: dict) -> ProfileData:
        p = ProfileData()
        src = a.get("url") or f"https://genius.com/artists/{a.get('id')}"
        desc = "Genius artist page (community-edited)"
        p.names = [n for n in a.get("alternate_names", []) if n]
        bio = ((a.get("description") or {}).get("plain") or "").strip()
        if bio and bio != "?":
            p.facts.append(FactRec("bio", bio[:1200], NAME, src, desc))
        if a.get("image_url") and "default_avatar" not in a["image_url"]:
            p.facts.append(FactRec("image", a["image_url"], NAME, src, desc))
        for key, typ, base in [("instagram_name", "instagram", "https://www.instagram.com/"),
                               ("twitter_name", "x", "https://x.com/"),
                               ("facebook_name", "facebook", "https://www.facebook.com/")]:
            if a.get(key):
                p.contacts.append(ContactRec(typ, base + a[key], "publicly_listed", NAME, src,
                                             f"{typ} handle listed on Genius artist page"))
        return p

    async def song_track(self, song_id: str) -> TrackRec:
        s = (await self._get(f"/songs/{song_id}", {"text_format": "plain"})).get("song", {})
        album = s.get("album") or {}
        rref = ReleaseRef(NAME, str(album["id"]), album.get("name", ""), album.get("url"),
                          album.get("release_date_for_display")) if album.get("id") else None
        mains = s.get("primary_artists") or ([s["primary_artist"]] if s.get("primary_artist") else [])
        tr = TrackRec(NAME, str(s["id"]), s.get("title", ""), s.get("url") or f"https://genius.com/songs/{s['id']}",
                      ", ".join(a.get("name", "") for a in mains), s.get("release_date"), rref,
                      source_description="Genius song credits (community-edited, Genius-verified where marked)")
        for a in mains:
            tr.add(_ref(a), "main_artist", "Primary artist")
        for a in s.get("featured_artists") or []:
            tr.add(_ref(a), "featured_artist", "Featuring")
        for a in s.get("producer_artists") or []:
            tr.add(_ref(a), "producer", "Produced by")
        for a in s.get("writer_artists") or []:
            tr.add(_ref(a), "writer", "Written by")
        for perf in s.get("custom_performances") or []:
            label = perf.get("label", "")
            if is_video_credit(label):
                continue
            role = role_from_text(label)
            for a in perf.get("artists") or []:
                tr.add(_ref(a, role), role, label)
        return tr
