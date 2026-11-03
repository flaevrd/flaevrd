"""MusicBrainz Web Service v2 (https://musicbrainz.org/doc/MusicBrainz_API).

No key. 1 request/second per IP and a descriptive User-Agent with contact are mandatory.
Data is CC0. Relationship credits (producer, engineer, mix, mastering, writer...) are
community-edited and cite their sources in edit notes; we treat them as one credible source.
"""
from __future__ import annotations

import re
from typing import Any

from ..engine.normalize import mb_role
from ..http import Http, HttpError
from .base import Candidate, FactRec, PersonRef, ProfileData, ReleaseRef, TrackRec, ContactRec
from .links import normalize_url, provider_ids, social_type

API = "https://musicbrainz.org/ws/2"
WEB = "https://musicbrainz.org"
NAME = "musicbrainz"

_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring|with)\b", re.I)


def _lucene_escape(s: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", s)


def artist_ref(a: dict, credited: str | None = None) -> PersonRef:
    t = (a.get("type") or "").lower()
    etype = "group" if t in {"group", "orchestra", "choir"} else "person" if t in {"person", "character"} else "unknown"
    return PersonRef(NAME, a["id"], a.get("name") or credited or "?", f"{WEB}/artist/{a['id']}", etype, credited)


def credit_text(ac: list[dict] | None) -> str:
    return "".join((c.get("name") or c.get("artist", {}).get("name", "")) + (c.get("joinphrase") or "") for c in ac or [])


class MusicBrainz:
    name = NAME
    requires = []

    def __init__(self, http: Http):
        self.http = http

    def configured(self) -> bool:
        return True

    async def _get(self, path: str, params: dict | None = None) -> dict:
        p = {"fmt": "json", **(params or {})}
        r = await self.http.get(f"{API}/{path}", params=p)
        return r.json()

    async def _get_inc(self, path: str, incs: list[list[str]]) -> dict:
        """Try progressively smaller `inc` sets; the WS returns 400 for unsupported includes."""
        last: Exception | None = None
        for inc in incs:
            try:
                return await self._get(path, {"inc": "+".join(inc)})
            except HttpError as e:
                if e.status != 400:
                    raise
                last = e
        raise last  # type: ignore[misc]

    # ---- search -------------------------------------------------------------------
    async def search_artists(self, q: str, limit: int = 6) -> list[Candidate]:
        data = await self._get("artist", {"query": q, "limit": limit})
        out = []
        for a in data.get("artists", []):
            bits = [a.get("type"), a.get("disambiguation"), (a.get("area") or {}).get("name") or a.get("country")]
            out.append(Candidate("artist", NAME, a["id"], a["name"], " · ".join(b for b in bits if b),
                                 f"{WEB}/artist/{a['id']}", float(a.get("score", 0))))
        return out

    async def search_recordings(self, q: str, limit: int = 6) -> list[Candidate]:
        title, artist = split_query(q)
        query = f'recording:"{_lucene_escape(title)}"' + (f' AND artist:"{_lucene_escape(artist)}"' if artist else "")
        data = await self._get("recording", {"query": query, "limit": limit * 2})
        out, seen = [], set()
        for r in data.get("recordings", []):
            key = (r.get("title", "").lower(), credit_text(r.get("artist-credit")).lower())
            if key in seen:
                continue
            seen.add(key)
            rel = (r.get("releases") or [{}])[0].get("title")
            date = r.get("first-release-date") or ""
            out.append(Candidate("song", NAME, r["id"], r["title"],
                                 " · ".join(b for b in [credit_text(r.get("artist-credit")), rel, date[:4]] if b),
                                 f"{WEB}/recording/{r['id']}", float(r.get("score", 0)), date[:4] or None))
        return out[:limit]

    async def search_release_groups(self, q: str, limit: int = 6) -> list[Candidate]:
        title, artist = split_query(q)
        query = f'releasegroup:"{_lucene_escape(title)}"' + (f' AND artist:"{_lucene_escape(artist)}"' if artist else "")
        data = await self._get("release-group", {"query": query, "limit": limit})
        out = []
        for g in data.get("release-groups", []):
            date = g.get("first-release-date") or ""
            out.append(Candidate("album", NAME, g["id"], g["title"],
                                 " · ".join(b for b in [credit_text(g.get("artist-credit")), g.get("primary-type"), date[:4]] if b),
                                 f"{WEB}/release-group/{g['id']}", float(g.get("score", 0)), date[:4] or None))
        return out

    # ---- artist ---------------------------------------------------------------------
    async def artist(self, mbid: str) -> dict:
        return await self._get_inc(f"artist/{mbid}", [
            ["aliases", "url-rels", "genres", "tags", "recording-rels", "release-rels", "work-rels", "artist-rels"],
            ["aliases", "url-rels", "genres", "recording-rels", "release-rels", "work-rels"],
            ["aliases", "url-rels"],
        ])

    def profile_from_artist(self, a: dict) -> ProfileData:
        p = ProfileData()
        src = f"{WEB}/artist/{a['id']}"
        desc = "MusicBrainz artist record (community-maintained, CC0)"
        p.names = sorted({al["name"] for al in a.get("aliases", []) if al.get("name")})
        def fact(field: str, value: Any) -> None:
            if value:
                p.facts.append(FactRec(field, str(value), NAME, src, desc))
        fact("type", a.get("type"))
        fact("area", (a.get("area") or {}).get("name"))
        fact("begin_area", (a.get("begin-area") or {}).get("name"))
        fact("country", a.get("country"))
        ls = a.get("life-span") or {}
        if a.get("type") == "Group" and ls.get("begin"):
            fact("active_since", ls["begin"][:4])
        for g in sorted(a.get("genres", []), key=lambda g: -g.get("count", 0))[:6]:
            fact("genre", g["name"])
        for rel in a.get("relations", []):
            if rel.get("target-type") != "url":
                continue
            url = normalize_url((rel.get("url") or {}).get("resource", ""))
            typ = rel.get("type", "")
            if rel.get("ended"):
                continue
            ids = provider_ids(url)
            for k, v in ids.items():
                p.links.setdefault(k, v)
            if typ == "official homepage":
                p.websites.append(url)
                p.contacts.append(ContactRec("website", url, "publicly_listed", NAME, src,
                                             "Official homepage as linked on MusicBrainz"))
                continue
            st = social_type(url)
            if st:
                p.contacts.append(ContactRec(st, url, "publicly_listed", NAME, src,
                                             f"{typ or 'profile'} link on MusicBrainz"))
        return p

    def contributor_credits(self, a: dict) -> dict[str, list]:
        """Recordings/releases/works this artist is related to (as producer, engineer, writer...)."""
        recs, rels, works = {}, {}, {}
        for rel in a.get("relations", []):
            role = mb_role(rel.get("type", ""), rel.get("attributes"))
            tt = rel.get("target-type")
            if not role:
                continue
            if tt == "recording":
                recs.setdefault(rel["recording"]["id"], (rel["recording"].get("title"), role))
            elif tt == "release":
                rels.setdefault(rel["release"]["id"], (rel["release"].get("title"), role))
            elif tt == "work":
                works.setdefault(rel["work"]["id"], (rel["work"].get("title"), role))
        return {"recordings": list(recs.items()), "releases": list(rels.items()), "works": list(works.items())}

    async def release_groups(self, mbid: str, types: str = "album|ep") -> list[dict]:
        data = await self._get("release-group", {"artist": mbid, "type": types, "limit": 100})
        groups = data.get("release-groups", [])
        groups.sort(key=lambda g: g.get("first-release-date") or "", reverse=True)
        return groups

    async def pick_release(self, rgid: str) -> str | None:
        data = await self._get("release", {"release-group": rgid, "status": "official", "limit": 50})
        rels = data.get("releases", [])
        if not rels:
            data = await self._get("release", {"release-group": rgid, "limit": 50})
            rels = data.get("releases", [])
        if not rels:
            return None
        rels.sort(key=lambda r: (r.get("date") or "9999", 0 if r.get("country") in {"XW", "US", "GB"} else 1))
        return rels[0]["id"]

    async def release_group(self, rgid: str) -> dict:
        return await self._get_inc(f"release-group/{rgid}", [["artist-credits", "url-rels"], ["artist-credits"]])

    async def work(self, wid: str) -> dict:
        return await self._get_inc(f"work/{wid}", [["recording-rels"]])

    # ---- tracks -----------------------------------------------------------------------
    async def release_tracks(self, release_id: str) -> list[TrackRec]:
        r = await self._get_inc(f"release/{release_id}", [
            ["recordings", "artist-credits", "labels", "release-groups", "artist-rels", "place-rels",
             "label-rels", "work-rels", "recording-level-rels", "work-level-rels"],
            ["recordings", "artist-credits", "labels", "release-groups", "artist-rels", "work-rels",
             "recording-level-rels", "work-level-rels"],
            ["recordings", "artist-credits", "labels", "release-groups", "artist-rels", "recording-level-rels"],
        ])
        rg = r.get("release-group") or {}
        rref = ReleaseRef(NAME, rg.get("id") or r["id"], r.get("title", ""),
                          f"{WEB}/release-group/{rg['id']}" if rg.get("id") else f"{WEB}/release/{r['id']}",
                          rg.get("first-release-date") or r.get("date"), rg.get("primary-type"),
                          credit_text(r.get("artist-credit")))
        release_level: list[tuple[PersonRef, str, str]] = []
        for rel in r.get("relations", []):
            ref, role = _rel_target(rel)
            if ref and role:
                release_level.append((ref, role, f"{rel.get('type')} (whole release)"))
        for li in r.get("label-info", []):
            lab = li.get("label")
            if lab and lab.get("id"):
                release_level.append((PersonRef(NAME, "label:" + lab["id"], lab["name"], f"{WEB}/label/{lab['id']}", "label"),
                                      "label", "Release label"))
        out = []
        for medium in r.get("media", []):
            for t in medium.get("tracks", []):
                rec = t.get("recording") or {}
                tr = self._track_from_recording(rec, t.get("artist-credit") or rec.get("artist-credit"),
                                                rref, f"{WEB}/release/{r['id']}")
                for ref, role, detail in release_level:
                    tr.add(ref, role, detail)
                out.append(tr)
        return out

    async def recording_track(self, rid: str) -> TrackRec:
        rec = await self._get_inc(f"recording/{rid}", [
            ["artist-credits", "releases", "artist-rels", "work-rels", "work-level-rels", "place-rels", "label-rels"],
            ["artist-credits", "releases", "artist-rels", "work-rels", "work-level-rels"],
            ["artist-credits", "releases", "artist-rels", "work-rels"],
        ])
        rel = (sorted(rec.get("releases") or [], key=lambda x: x.get("date") or "9999") or [None])[0]
        rref = ReleaseRef(NAME, "release:" + rel["id"], rel.get("title", ""), f"{WEB}/release/{rel['id']}",
                          rel.get("date")) if rel else None
        return self._track_from_recording(rec, rec.get("artist-credit"), rref, None)

    def _track_from_recording(self, rec: dict, ac: list | None, rref: ReleaseRef | None,
                              release_url: str | None) -> TrackRec:
        url = f"{WEB}/recording/{rec['id']}"
        tr = TrackRec(NAME, rec["id"], rec.get("title", ""), url, credit_text(ac),
                      rec.get("first-release-date") or (rref.release_date if rref else None), rref,
                      source_description="MusicBrainz recording credits")
        featured = False
        for c in ac or []:
            a = c.get("artist")
            if a:
                tr.add(artist_ref(a, c.get("name")), "featured_artist" if featured else "main_artist")
            if _FEAT.search(c.get("joinphrase") or ""):
                featured = True
        for rel in rec.get("relations", []):
            if rel.get("target-type") == "work":
                for wrel in (rel.get("work") or {}).get("relations", []):
                    ref, role = _rel_target(wrel)
                    if ref and role:
                        tr.add(ref, role, f"{wrel.get('type')} of the composition")
                continue
            ref, role = _rel_target(rel)
            if ref and role:
                attrs = ", ".join(rel.get("attributes") or [])
                tr.add(ref, role, rel.get("type") + (f" ({attrs})" if attrs else ""))
        return tr


def _rel_target(rel: dict) -> tuple[PersonRef | None, str | None]:
    tt = rel.get("target-type")
    role = mb_role(rel.get("type", ""), rel.get("attributes"))
    if not role:
        return None, None
    if tt == "artist" and rel.get("artist"):
        return artist_ref(rel["artist"], rel.get("target-credit") or None), role
    if tt == "place" and rel.get("place"):
        pl = rel["place"]
        return PersonRef(NAME, "place:" + pl["id"], pl.get("name", "?"), f"{WEB}/place/{pl['id']}", "studio"), "studio"
    if tt == "label" and rel.get("label"):
        lb = rel["label"]
        return PersonRef(NAME, "label:" + lb["id"], lb.get("name", "?"), f"{WEB}/label/{lb['id']}", "label"), role
    return None, None


def split_query(q: str) -> tuple[str, str | None]:
    """'Song - Artist', 'Song by Artist' -> (Song, Artist). Plain text -> (text, None)."""
    for sep in (" - ", " – ", " — ", " by "):
        if sep in q:
            a, b = q.split(sep, 1)
            return a.strip(), b.strip() or None
    return q.strip(), None
