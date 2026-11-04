"""Discogs API v2 (https://www.discogs.com/developers). Personal access token; 60 req/min
authenticated. Release credits ("extraartists") are among the most detailed public credits,
especially for engineers, mastering and studios."""
from __future__ import annotations

import re

from ..config import settings
from ..engine.normalize import clean_discogs_name, role_from_text
from ..http import Http
from .base import auth_header, Candidate, ContactRec, FactRec, NotConfigured, PersonRef, ProfileData, ReleaseRef, TrackRec
from .links import normalize_url, provider_ids, social_type

API = "https://api.discogs.com"
WEB = "https://www.discogs.com"
NAME = "discogs"

_COMPANY_ROLES = {
    "recorded at": "studio", "mixed at": "studio", "mastered at": "studio", "engineered at": "studio",
    "produced at": "studio", "overdubbed at": "studio", "lacquer cut at": None, "pressed by": None,
    "published by": "publisher", "phonographic copyright (p)": "label", "copyright (c)": "label",
    "licensed from": "label", "licensed to": "label", "distributed by": "label", "marketed by": "label",
}
_MARKUP = re.compile(r"\[(?:a|l|m|r)=?([^\]]*)\]|\[/?(?:b|i|u|url[^\]]*)\]", re.I)


def _strip_markup(text: str) -> str:
    return _MARKUP.sub(lambda m: m.group(1) or "", text or "").strip()


def _split_roles(role: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in role or "":
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())
    return parts


def parse_positions(spec: str, positions: list[str]) -> list[str]:
    """'A1 to A3, B2' -> ['A1','A2','A3','B2'] using the release's own track order."""
    if not spec or not spec.strip():
        return list(positions)
    out: list[str] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        m = re.match(r"^(.+?)\s*(?:to|-|–)\s*(.+)$", chunk)
        if m and m.group(1) in positions and m.group(2) in positions:
            i, j = positions.index(m.group(1)), positions.index(m.group(2))
            out.extend(positions[min(i, j):max(i, j) + 1])
        elif chunk in positions:
            out.append(chunk)
    return out or list(positions)


def _person(a: dict) -> PersonRef:
    return PersonRef(NAME, str(a["id"]), clean_discogs_name(a.get("name", "?")), f"{WEB}/artist/{a['id']}",
                     "person", a.get("anv") or None)


class Discogs:
    name = NAME
    requires = ["DISCOGS_TOKEN"]

    def __init__(self, http: Http):
        self.http = http

    def configured(self) -> bool:
        return bool(settings.discogs_token)

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.configured():
            raise NotConfigured("DISCOGS_TOKEN")
        r = await self.http.get(f"{API}{path}", params=params,
                                headers=auth_header("Discogs token=", settings.discogs_token))
        return r.json()

    async def search(self, q: str, kind: str, limit: int = 5) -> list[Candidate]:
        data = await self._get("/database/search", {"q": q, "type": kind, "per_page": limit})
        out = []
        for r in data.get("results", [])[:limit]:
            k = "artist" if kind == "artist" else "album"
            out.append(Candidate(k, NAME, f"{kind}:{r['id']}", clean_discogs_name(r.get("title", "")),
                                 " · ".join(str(b) for b in [r.get("year"), ", ".join(r.get("format", [])[:2])] if b),
                                 WEB + r.get("uri", ""), 0.0, str(r.get("year") or "") or None))
        return out

    async def artist(self, artist_id: str) -> dict:
        return await self._get(f"/artists/{artist_id}")

    def profile_from_artist(self, a: dict) -> ProfileData:
        p = ProfileData()
        src = a.get("uri") or f"{WEB}/artist/{a['id']}"
        desc = "Discogs artist profile (community-maintained)"
        p.names = sorted({clean_discogs_name(n) for n in a.get("namevariations", []) if n})
        bio = _strip_markup(a.get("profile", ""))
        if bio:
            p.facts.append(FactRec("bio", bio[:1200], NAME, src, desc))
        for url in a.get("urls", []) or []:
            url = normalize_url(url)
            for k, v in provider_ids(url).items():
                p.links.setdefault(k, v)
            st = social_type(url)
            p.contacts.append(ContactRec(st or "website", url, "publicly_listed", NAME, src,
                                         "Link listed on Discogs artist profile"))
        return p

    async def artist_release_ids(self, artist_id: str, limit: int) -> list[tuple[str, str]]:
        """(release_id, role) pairs, newest first; masters resolved to their main release."""
        data = await self._get(f"/artists/{artist_id}/releases", {"sort": "year", "sort_order": "desc", "per_page": 100})
        out, seen = [], set()
        for r in data.get("releases", []):
            if r.get("role") == "UnofficialRelease":
                continue
            rid = str(r.get("main_release") or r["id"]) if r.get("type") == "master" else str(r["id"])
            if rid in seen:
                continue
            seen.add(rid)
            out.append((rid, r.get("role", "")))
            if len(out) >= limit:
                break
        return out

    async def master_main_release(self, master_id: str) -> str | None:
        m = await self._get(f"/masters/{master_id}")
        return str(m["main_release"]) if m.get("main_release") else None

    async def release_tracks(self, release_id: str) -> list[TrackRec]:
        r = await self._get(f"/releases/{release_id}")
        url = r.get("uri") or f"{WEB}/release/{release_id}"
        rref = ReleaseRef(NAME, f"release:{release_id}", r.get("title", ""), url,
                          r.get("released") or (str(r["year"]) if r.get("year") else None),
                          ", ".join(f.get("name", "") for f in r.get("formats", [])[:1]) or None,
                          ", ".join(clean_discogs_name(a.get("name", "")) for a in r.get("artists", [])))
        tracks = [t for t in r.get("tracklist", []) if t.get("type_", "track") == "track"]
        positions = [t.get("position", "") for t in tracks]
        by_pos: dict[str, TrackRec] = {}
        for t in tracks:
            artists = t.get("artists") or r.get("artists") or []
            tr = TrackRec(NAME, f"{release_id}:{t.get('position', '')}", t.get("title", ""), url,
                          "".join(clean_discogs_name(a.get("name", "")) + (f" {a['join']} " if a.get("join") else "")
                                  for a in artists).strip(),
                          rref.release_date, rref, source_description="Discogs release credits (community-maintained)")
            for a in artists:
                if a.get("id"):
                    tr.add(_person(a), "main_artist", "Release artist")
            for ea in t.get("extraartists") or []:
                self._add_extra(tr, ea)
            by_pos[t.get("position", "")] = tr
        for ea in r.get("extraartists") or []:
            for pos in parse_positions(ea.get("tracks", ""), positions):
                if pos in by_pos:
                    self._add_extra(by_pos[pos], ea, release_level=not ea.get("tracks"))
        companies = [(c, (c.get("entity_type_name") or "").lower()) for c in r.get("companies", [])]
        companies += [(lb, "label") for lb in r.get("labels", [])]
        for c, kind in companies:
            role = "label" if kind == "label" else _COMPANY_ROLES.get(kind)
            if not role or not c.get("id"):
                continue
            ref = PersonRef(NAME, f"label:{c['id']}", clean_discogs_name(c.get("name", "?")), f"{WEB}/label/{c['id']}",
                            "studio" if role == "studio" else "label")
            for tr in by_pos.values():
                tr.add(ref, role, c.get("entity_type_name") or "Label")
        return list(by_pos.values())

    def _add_extra(self, tr: TrackRec, ea: dict, release_level: bool = False) -> None:
        if not ea.get("id"):
            return  # unlinked free-text credit: no stable identity, skip rather than guess
        for raw in _split_roles(ea.get("role", "")):
            role = role_from_text(raw)
            if role == "other":
                continue
            tr.add(_person(ea), role, raw + (" (whole release)" if release_level else ""))
