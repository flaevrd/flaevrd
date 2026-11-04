"""Search orchestration: suggestions, research jobs, expansion and profile enrichment."""
from __future__ import annotations

import asyncio
import json
import traceback
from typing import Awaitable, Callable

from ..config import settings
from ..db import DB, now
from ..http import Http, HttpError
from ..providers.base import Candidate, NotConfigured, PersonRef, TrackRec
from ..providers.discogs import Discogs
from ..providers.genius import Genius
from ..providers.links import provider_ids
from ..providers.metrics import SPOTIFY_MONTHLY_REASON, LastFm, Spotify, YouTube
from ..providers.musicbrainz import MusicBrainz, split_query
from ..providers.website import WebsiteContacts
from ..providers.wikidata import Wikidata
from . import network
from .profile import group_contacts
from .normalize import ROLE_TO_GROUP, norm_name, norm_title
from .store import Store

EXPAND_PRIORITY = {"producer": 0, "engineer": 1, "writer": 2, "artist": 3, "musician": 4, "other": 5}


class Researcher:
    def __init__(self, db: DB, http: Http):
        self.db, self.http = db, http
        self.store = Store(db)
        self.mb = MusicBrainz(http)
        self.genius = Genius(http)
        self.discogs = Discogs(http)
        self.wd = Wikidata(http)
        self.spotify = Spotify(http)
        self.youtube = YouTube(http)
        self.lastfm = LastFm(http)
        self.web = WebsiteContacts(http)
        self.jobs: dict[int, asyncio.Task] = {}
        self.expansions: dict[tuple[int, int], asyncio.Task] = {}
        self.compute_lock = asyncio.Lock()

    # ---------------------------------------------------------------------------------
    def provider_status(self) -> list[dict]:
        rows = []
        for p, purpose in [(self.mb, "Credits, discographies, cross-links, locations"),
                           (self.genius, "Song credits (producers, writers, engineers, A&R), bios, handles"),
                           (self.discogs, "Release credits, engineers, studios, profiles"),
                           (self.wd, "Background, location, genres, official sites, social accounts, Wikipedia bio"),
                           (self.spotify, "Identity + genres (followers unavailable for Dev-Mode apps)"),
                           (self.youtube, "Channel subscribers and views"),
                           (self.lastfm, "Last.fm listeners and scrobbles"),
                           (self.web, "Business contacts published on official websites")]:
            configured = p.configured() if hasattr(p, "configured") else True
            if p is self.web:
                configured = settings.web_contacts
            rows.append({"provider": p.name, "configured": configured, "requires": getattr(p, "requires", []),
                         "purpose": purpose})
        return rows

    async def _safe(self, sid: int | None, label: str, coro: Awaitable, default=None):
        try:
            return await coro
        except NotConfigured as e:
            if sid:
                self.db.log(sid, f"{label}: waiting for configuration ({e})", "config")
        except HttpError as e:
            if sid:
                self.db.log(sid, f"{label}: {e} — skipped", "warn")
        except Exception as e:  # keep research going; record the failure
            if sid:
                self.db.log(sid, f"{label}: {type(e).__name__}: {e}", "error")
        return default

    # ---- suggestions ------------------------------------------------------------------
    async def suggest(self, q: str, kind: str = "auto") -> dict:
        q = q.strip()
        tasks: list[tuple[str, Awaitable]] = []
        if kind in ("auto", "artist"):
            tasks.append(("MusicBrainz artists", self.mb.search_artists(q)))
        if kind in ("auto", "song"):
            tasks.append(("MusicBrainz songs", self.mb.search_recordings(q)))
            if self.genius.configured():
                tasks.append(("Genius songs", self.genius.search_songs(q)))
        if kind in ("auto", "album"):
            tasks.append(("MusicBrainz albums", self.mb.search_release_groups(q)))
        if kind == "artist" and self.genius.configured():
            tasks.append(("Genius artists", self.genius.search_artists(q)))
        results = await asyncio.gather(*(self._safe(None, n, c, None) for n, c in tasks))
        errors = [n for (n, _), r in zip(tasks, results) if r is None]
        cands: list[Candidate] = [c for r in results for c in (r or [])]
        # 'Artist - Song' typed the other way round
        if kind in ("auto", "song", "album") and split_query(q)[1]:
            title, artist = split_query(q)
            swapped = f"{artist} - {title}"
            extra = await asyncio.gather(
                self._safe(None, "swap", self.mb.search_recordings(swapped), []),
                self._safe(None, "swap", self.mb.search_release_groups(swapped), []))
            have = {(c.provider, c.external_id) for c in cands}
            cands += [c for r in extra for c in (r or []) if (c.provider, c.external_id) not in have]
        nq = norm_name(split_query(q)[0])

        def rank(c: Candidate) -> float:
            exact = 25 if norm_name(c.title) == nq or norm_title(c.title) == norm_title(q) else 0
            return c.score + exact
        cands.sort(key=rank, reverse=True)
        detected = None
        if cands:
            top = cands[0]
            second = cands[1] if len(cands) > 1 else None
            if rank(top) >= 110 and (not second or rank(top) - rank(second) >= 15 or second.kind == top.kind):
                detected = top.kind
        return {"query": q, "detected": detected, "errors": errors,
                "candidates": [c.__dict__ for c in cands[:18]]}

    # ---- jobs ---------------------------------------------------------------------------
    def start(self, query: str, kind: str, provider: str, external_id: str, depth: int) -> int:
        sid = self.db.x("INSERT INTO research_sessions(query,target_kind,target_provider,target_id,depth,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?)", (query, kind, provider, external_id, depth, "queued", now()))
        self.jobs[sid] = asyncio.create_task(self._run(sid))
        return sid

    async def _run(self, sid: int) -> None:
        row = self.db.one("SELECT * FROM research_sessions WHERE id=?", (sid,))
        self.db.x("UPDATE research_sessions SET status='running' WHERE id=?", (sid,))
        try:
            kind, prov, xid, depth = row["target_kind"], row["target_provider"], row["target_id"], row["depth"]
            root, root_track, root_release = None, None, None
            if kind == "artist":
                root = await self.research_person(sid, prov, xid, degree=1)
            elif kind == "song":
                root_track, root = await self.research_song(sid, prov, xid)
            elif kind == "album":
                root_release, root = await self.research_album(sid, prov, xid)
            if root is None:
                raise RuntimeError("Could not identify the search target in any source.")
            self.db.x("UPDATE research_sessions SET root_entity_id=?, root_track_id=?, root_release_id=? WHERE id=?",
                      (root, root_track, root_release, sid))
            self.db.log(sid, "Building profile for the main person …")
            root = await self.enrich(sid, root, full=True)
            if depth >= 2:
                await self.expand(sid, root, kind)
            await self.enrich_top(sid, root)
            self.db.log(sid, "Computing relationships and recommendations …")
            result = self.compute(sid)
            self.db.x("UPDATE research_sessions SET status='done', result=?, finished_at=? WHERE id=?",
                      (json.dumps(result), now(), sid))
            self.db.log(sid, "Done.")
        except Exception as e:
            self.db.log(sid, f"Research failed: {type(e).__name__}: {e}", "error")
            self.db.log(sid, traceback.format_exc()[-800:], "debug")
            self.db.x("UPDATE research_sessions SET status='failed', finished_at=? WHERE id=?", (now(), sid))

    # ---- helpers ------------------------------------------------------------------------
    def _add_session_track(self, sid: int, tid: int, via: int | None, degree: int) -> None:
        self.db.x("INSERT OR IGNORE INTO session_tracks(session_id,track_id,via_entity_id,degree) VALUES(?,?,?,?)",
                  (sid, tid, via, degree))

    def _credited(self, tid: int, eid: int) -> bool:
        return self.db.one("SELECT 1 FROM credits WHERE track_id=? AND entity_id=?", (tid, eid)) is not None

    def _anchor(self, provider: str, xid: str) -> int | None:
        return self.store.by_ident(provider, xid)

    async def _save_tracks(self, sid: int, recs: list[TrackRec], anchor: tuple[str, str] | None, via_root: bool,
                           degree: int, require_credit: bool = True) -> list[int]:
        out = []
        for rec in recs:
            tid = self.store.save_track(rec)
            eid = self._anchor(*anchor) if anchor else None
            if require_credit and eid and not self._credited(tid, eid):
                continue
            self._add_session_track(sid, tid, None if via_root else eid, degree)
            out.append(tid)
        return out

    # ---- person research --------------------------------------------------------------------
    async def research_person(self, sid: int, provider: str, xid: str, degree: int,
                              limits: tuple[int, int, int] | None = None) -> int | None:
        """Collect a person's credits from every configured source. Returns the entity id.
        `limits` = (releases, credited recordings, Genius songs) overrides the light defaults used
        for connections (expansions from the graph go a bit deeper than automatic 2nd degree)."""
        full = degree == 1
        anchor = (provider, xid)
        eid = self._anchor(provider, xid)
        label = "main search" if full else "connection"

        # 1. identity + cross-links
        if provider == "musicbrainz":
            a = await self._safe(sid, "MusicBrainz artist", self.mb.artist(xid))
            if not a:
                return eid
            if eid is None:
                eid = self.store.create_entity(PersonRef("musicbrainz", xid, a["name"], f"https://musicbrainz.org/artist/{xid}",
                                                         "group" if a.get("type") == "Group" else "person"))
            if a.get("disambiguation"):
                self.db.x("UPDATE entities SET disambiguation=? WHERE id=?", (a["disambiguation"], eid))
            eid = self.store.apply_profile(eid, self.mb.profile_from_artist(a))
            name = a["name"]
            mb_artist = a
        elif provider == "genius":
            g = await self._safe(sid, "Genius artist", self.genius.artist(xid))
            if not g:
                return eid
            if eid is None:
                eid = self.store.create_entity(PersonRef("genius", xid, g["name"], g.get("url")))
            eid = self.store.apply_profile(eid, self.genius.profile_from_artist(g))
            name, mb_artist = g["name"], None
        elif provider == "discogs":
            d = await self._safe(sid, "Discogs artist", self.discogs.artist(xid))
            if not d:
                return eid
            from .normalize import clean_discogs_name
            if eid is None:
                eid = self.store.create_entity(PersonRef("discogs", xid, clean_discogs_name(d["name"]), d.get("uri")))
            eid = self.store.apply_profile(eid, self.discogs.profile_from_artist(d))
            name, mb_artist = clean_discogs_name(d["name"]), None
        else:
            return eid
        self.db.log(sid, f"Researching {name} ({label}) …")

        mbid = self.store.ident(eid, "musicbrainz")
        if mbid and mb_artist is None:
            mb_artist = await self._safe(sid, "MusicBrainz artist", self.mb.artist(mbid))
            if mb_artist:
                eid = self.store.apply_profile(eid, self.mb.profile_from_artist(mb_artist))

        # Genius id via authoritative URL link, or exact-name match for the main search only
        if self.genius.configured() and not self.store.ident(eid, "genius"):
            gurl = self.store.ident(eid, "genius_url")
            gid = None
            if gurl:
                gid = await self._safe(sid, "Genius link", self.genius.find_artist_by_url(gurl, name))
                if gid:
                    eid = self.store.link(eid, "genius", gid, gurl, "cross_link")
            elif full:
                hits = await self._safe(sid, "Genius search", self.genius.search_artists(name, 10), []) or []
                exact = [h for h in hits if norm_name(h.title) == norm_name(name)]
                if len(exact) == 1:
                    gid = exact[0].external_id
                    eid = self.store.link(eid, "genius", gid, exact[0].url, "name_match")
                    self.db.log(sid, f"Genius profile for {name} matched by exact name only (no cross-link). "
                                     f"Shown as a name match on the profile.", "warn")
        if self.discogs.configured() and not self.store.ident(eid, "discogs") and full:
            hits = await self._safe(sid, "Discogs search", self.discogs.search(name, "artist", 10), []) or []
            exact = [h for h in hits if norm_name(h.title) == norm_name(name)]
            if len(exact) == 1:
                did = exact[0].external_id.split(":", 1)[1]
                eid = self.store.link(eid, "discogs", did, exact[0].url, "name_match")
                self.db.log(sid, f"Discogs profile for {name} matched by exact name only.", "warn")

        # Re-anchor on a stable identifier (merges may have happened)
        anchor = next(((p, self.store.ident(eid, p)) for p in ("musicbrainz", "genius", "discogs")
                       if self.store.ident(eid, p)), anchor)

        # 2. credits from every source, in parallel (rate limits are per host)
        n_rel = settings.max_releases if full else 2
        n_rec = settings.max_credit_recordings if full else 8
        n_gen = 20 if full else 8
        if limits and not full:
            n_rel, n_rec, n_gen = limits
        jobs = []
        if mb_artist:
            jobs.append(self._mb_person_tracks(sid, mb_artist, anchor, degree, n_rel, n_rec))
        gid = self.store.ident(eid, "genius")
        if gid and self.genius.configured():
            jobs.append(self._genius_person_tracks(sid, gid, anchor, degree, n_gen))
        did = self.store.ident(eid, "discogs")
        if did and self.discogs.configured():
            jobs.append(self._discogs_person_tracks(sid, did, anchor, degree, n_rel))
        counts = list(await asyncio.gather(*jobs))

        # Producers/engineers are rarely a song's primary artist, so Genius search by name misses them.
        # Bootstrap instead: match a few of their MusicBrainz tracks on Genius; if the same name is
        # credited on the same song there, the Genius id is linked by track context (rule 3).
        if full and self.genius.configured() and not self.store.ident(self._anchor(*anchor) or eid, "genius"):
            eid = self._anchor(*anchor) or eid
            mine = self.db.q("SELECT t.id, t.title, t.artist_text FROM tracks t JOIN credits c ON c.track_id=t.id "
                             "JOIN session_tracks st ON st.track_id=t.id AND st.session_id=? "
                             "WHERE c.entity_id=? GROUP BY t.id LIMIT 5", (sid, eid))
            for t in mine:
                g = await self._genius_match(sid, t["title"], t["artist_text"] or "")
                if g:
                    self.store.save_track(g)
                gid = self.store.ident(self._anchor(*anchor) or eid, "genius")
                if gid:
                    self.db.log(sid, f"Linked {name}'s Genius profile through a shared song credit.")
                    counts.append(await self._genius_person_tracks(sid, gid, anchor, degree, 20))
                    break
        self.db.log(sid, f"{name}: {sum(c or 0 for c in counts)} credited tracks collected.")
        return self._anchor(*anchor) or eid

    async def _mb_person_tracks(self, sid, a, anchor, degree, n_rel, n_rec) -> int:
        n = 0
        groups = await self._safe(sid, "MusicBrainz discography", self.mb.release_groups(a["id"]), []) or []
        if len(groups) < n_rel:
            groups += (await self._safe(sid, "MusicBrainz singles", self.mb.release_groups(a["id"], "single"), []) or [])
        for g in groups[:n_rel]:
            rid = await self._safe(sid, "MusicBrainz release", self.mb.pick_release(g["id"]))
            if rid:
                recs = await self._safe(sid, f"MusicBrainz “{g.get('title')}”", self.mb.release_tracks(rid), []) or []
                n += len(await self._save_tracks(sid, recs, anchor, degree == 1, degree))
        cc = self.mb.contributor_credits(a)
        for rec_id, (title, role) in cc["recordings"][:n_rec]:
            tr = await self._safe(sid, f"MusicBrainz recording “{title}”", self.mb.recording_track(rec_id))
            if tr:
                n += len(await self._save_tracks(sid, [tr], anchor, degree == 1, degree))
        for rel_id, (title, role) in cc["releases"][: max(1, n_rel // 2)]:
            recs = await self._safe(sid, f"MusicBrainz release “{title}”", self.mb.release_tracks(rel_id), []) or []
            n += len(await self._save_tracks(sid, recs, anchor, degree == 1, degree))
        for wid, (title, role) in cc["works"][: (6 if degree == 1 else 2)]:
            w = await self._safe(sid, f"MusicBrainz work “{title}”", self.mb.work(wid))
            recs = [r for r in (w or {}).get("relations", []) if r.get("target-type") == "recording"]
            if recs:
                tr = await self._safe(sid, f"MusicBrainz recording “{title}”", self.mb.recording_track(recs[0]["recording"]["id"]))
                if tr:
                    n += len(await self._save_tracks(sid, [tr], anchor, degree == 1, degree))
        return n

    async def _genius_person_tracks(self, sid, gid, anchor, degree, limit) -> int:
        ids = await self._safe(sid, "Genius songs", self.genius.artist_song_ids(gid, limit), []) or []
        n = 0
        for song_id in ids:
            tr = await self._safe(sid, f"Genius song {song_id}", self.genius.song_track(song_id))
            if tr:
                n += len(await self._save_tracks(sid, [tr], anchor, degree == 1, degree))
        return n

    async def _discogs_person_tracks(self, sid, did, anchor, degree, limit) -> int:
        rels = await self._safe(sid, "Discogs releases", self.discogs.artist_release_ids(did, limit), []) or []
        n = 0
        for rid, role in rels:
            recs = await self._safe(sid, f"Discogs release {rid}", self.discogs.release_tracks(rid), []) or []
            n += len(await self._save_tracks(sid, recs, anchor, degree == 1, degree))
        return n

    # ---- songs & albums ------------------------------------------------------------------------
    async def _genius_match(self, sid: int, title: str, artist: str) -> TrackRec | None:
        if not self.genius.configured():
            return None
        hits = await self._safe(sid, "Genius search", self.genius.search(f"{title} {artist}"), []) or []
        nt, na = norm_title(title), norm_name(artist.split(",")[0].split(" feat")[0])
        for h in hits:
            if norm_title(h.get("title", "")) == nt and norm_name((h.get("primary_artist") or {}).get("name", "")) in (na, ""):
                return await self._safe(sid, "Genius song", self.genius.song_track(str(h["id"])))
        for h in hits:  # primary artist written differently (e.g. 'A & B'): require artist containment
            pa = norm_name((h.get("primary_artist") or {}).get("name", ""))
            if norm_title(h.get("title", "")) == nt and na and (na in pa or pa in na):
                return await self._safe(sid, "Genius song", self.genius.song_track(str(h["id"])))
        return None

    async def research_song(self, sid: int, provider: str, xid: str) -> tuple[int | None, int | None]:
        recs: list[TrackRec] = []
        if provider == "musicbrainz":
            mb = await self._safe(sid, "MusicBrainz recording", self.mb.recording_track(xid))
            if mb:
                recs.append(mb)
                self.db.log(sid, f"Song: {mb.title} — {mb.artist_text}")
                g = await self._genius_match(sid, mb.title, mb.artist_text)
                if g:
                    recs.append(g)
                    self.db.log(sid, "Matched the song on Genius by exact title + artist.")
        elif provider == "genius":
            g = await self._safe(sid, "Genius song", self.genius.song_track(xid))
            if g:
                recs.append(g)
                self.db.log(sid, f"Song: {g.title} — {g.artist_text}")
                cands = await self._safe(sid, "MusicBrainz search", self.mb.search_recordings(f"{g.title} - {g.artist_text.split(',')[0]}"), []) or []
                for c in cands:
                    if c.score >= 90 and norm_title(c.title) == norm_title(g.title) and \
                            norm_name(g.artist_text.split(",")[0]) in norm_name(c.subtitle.split("·")[0]):
                        mb = await self._safe(sid, "MusicBrainz recording", self.mb.recording_track(c.external_id))
                        if mb:
                            recs.append(mb)
                            self.db.log(sid, "Matched the song on MusicBrainz by exact title + artist.")
                        break
        tid = None
        for r in recs:
            tid = self.store.save_track(r)
        if tid is None:
            return None, None
        self._add_session_track(sid, tid, None, 1)
        main = self.db.one("SELECT entity_id FROM credits WHERE track_id=? AND role='main_artist' ORDER BY id LIMIT 1", (tid,))
        return tid, main["entity_id"] if main else None

    async def research_album(self, sid: int, provider: str, xid: str) -> tuple[int | None, int | None]:
        recs: list[TrackRec] = []
        discogs_release = None
        if provider == "musicbrainz":
            rg = await self._safe(sid, "MusicBrainz album", self.mb.release_group(xid))
            rid = await self._safe(sid, "MusicBrainz release", self.mb.pick_release(xid))
            if rid:
                recs += await self._safe(sid, "MusicBrainz tracklist", self.mb.release_tracks(rid), []) or []
            for rel in (rg or {}).get("relations", []):
                url = (rel.get("url") or {}).get("resource", "")
                if "discogs.com/master/" in url:
                    discogs_release = ("master", url.rstrip("/").split("/")[-1].split("-")[0])
                elif "discogs.com/release/" in url and not discogs_release:
                    discogs_release = ("release", url.rstrip("/").split("/")[-1].split("-")[0])
        elif provider == "discogs":
            k, _, i = xid.partition(":")
            discogs_release = (k, i)
        if discogs_release and self.discogs.configured():
            k, i = discogs_release
            rel_id = i if k == "release" else await self._safe(sid, "Discogs master", self.discogs.master_main_release(i))
            if rel_id:
                drecs = await self._safe(sid, "Discogs release", self.discogs.release_tracks(rel_id), []) or []
                recs += drecs
                self.db.log(sid, f"Discogs: {len(drecs)} tracks with release credits.")
        elif discogs_release:
            self.db.log(sid, "Discogs release linked but DISCOGS_TOKEN is not configured.", "config")
        if not recs:
            return None, None
        base = [r for r in recs if r.provider == recs[0].provider]
        self.db.log(sid, f"Album: {base[0].release.title if base[0].release else ''} — {len(base)} tracks")
        tids = []
        for r in recs:
            tids.append(self.store.save_track(r))
        if self.genius.configured():
            self.db.log(sid, "Looking up each track's credits on Genius …")
            for r in base:
                g = await self._genius_match(sid, r.title, r.artist_text)
                if g:
                    tids.append(self.store.save_track(g))
        for t in dict.fromkeys(tids):
            self._add_session_track(sid, t, None, 1)
        rel = self.db.one("SELECT release_id FROM tracks WHERE id=?", (tids[0],))
        main = self.db.one("SELECT entity_id, COUNT(*) n FROM credits WHERE role='main_artist' AND track_id IN (%s) "
                           "GROUP BY entity_id ORDER BY n DESC LIMIT 1" % ",".join("?" * len(tids)), tids)
        return (rel["release_id"] if rel else None), (main["entity_id"] if main else None)

    # ---- expansion & enrichment -----------------------------------------------------------------
    def _session_track_ids(self, sid: int, degree: int | None = None) -> list[int]:
        sql = "SELECT DISTINCT track_id FROM session_tracks WHERE session_id=?" + (" AND degree=?" if degree else "")
        return [r[0] for r in self.db.q(sql, (sid, degree) if degree else (sid,))]

    def _expansion_targets(self, sid: int, root: int, kind: str) -> list[int]:
        s = network.load_scope(self.db, self._session_track_ids(sid, 1))
        if kind == "artist":
            direct = network.direct_network(s, root)
        else:  # song/album: everyone on the researched tracks
            ids = {e for tid in s.credits for e in s.credits[tid]}
            direct = []
            for e in ids:
                if e not in s.entities:
                    continue
                roles = network.entity_roles(s, e)
                direct.append({**s.entities[e], "group": network.main_group(roles), "strength": sum(roles.values())})
        people = [d for d in direct if d["type"] in network.PERSONLIKE and d["id"] != root]
        people.sort(key=lambda d: (EXPAND_PRIORITY.get(d["group"], 9) > 3, -d["strength"]))
        return [d["id"] for d in people[: settings.max_expansions]]

    async def expand(self, sid: int, root: int, kind: str) -> None:
        targets = self._expansion_targets(sid, root, kind)
        if kind != "artist" and root not in targets:
            targets = [root] + targets[: settings.max_expansions - 1]
        if not targets:
            return
        self.db.log(sid, f"Second-degree discovery: expanding {len(targets)} collaborators …")
        for eid in targets:
            for p in ("musicbrainz", "genius", "discogs"):
                xid = self.store.ident(eid, p)
                if xid and (p != "genius" or self.genius.configured()) and (p != "discogs" or self.discogs.configured()):
                    await self.research_person(sid, p, xid, degree=2)
                    break

    async def enrich_top(self, sid: int, root: int) -> None:
        s = network.load_scope(self.db, self._session_track_ids(sid, 1))
        direct = [d for d in network.direct_network(s, root) if d["type"] in network.PERSONLIKE]
        top = [d["id"] for d in direct[: settings.max_expansions]]
        if top:
            self.db.log(sid, f"Profiling top {len(top)} direct collaborators (background, location, contacts, metrics) …")
        for eid in top:
            await self.enrich(sid, eid, full=False)

    async def enrich(self, sid: int | None, eid: int, full: bool) -> int:
        """Background, location, contacts and metrics for one entity, from every linked source."""
        name = (self.db.one("SELECT name FROM entities WHERE id=?", (eid,)) or {"name": "?"})["name"]
        anchor = next(((p, self.store.ident(eid, p)) for p in ("musicbrainz", "genius", "discogs") if self.store.ident(eid, p)), None)
        mbid = self.store.ident(eid, "musicbrainz")
        if mbid:
            a = await self._safe(sid, "MusicBrainz profile", self.mb.artist(mbid))
            if a:
                eid = self.store.apply_profile(eid, self.mb.profile_from_artist(a))
        gurl = self.store.ident(eid, "genius_url")
        if gurl and self.genius.configured() and not self.store.ident(eid, "genius"):
            gid = await self._safe(sid, "Genius link", self.genius.find_artist_by_url(gurl, name))
            if gid:
                eid = self.store.link(eid, "genius", gid, gurl, "cross_link")
        qid = self.store.ident(eid, "wikidata")
        if qid:
            p = await self._safe(sid, "Wikidata", self.wd.profile(qid))
            if p:
                eid = self.store.apply_profile(eid, p)
        gid = self.store.ident(eid, "genius")
        if gid and self.genius.configured():
            g = await self._safe(sid, "Genius profile", self.genius.artist(gid))
            if g:
                eid = self.store.apply_profile(eid, self.genius.profile_from_artist(g))
        did = self.store.ident(eid, "discogs")
        if did and self.discogs.configured():
            d = await self._safe(sid, "Discogs profile", self.discogs.artist(did))
            if d:
                eid = self.store.apply_profile(eid, self.discogs.profile_from_artist(d))
        if anchor:
            eid = self._anchor(*anchor) or eid

        # metrics
        spid = self.store.ident(eid, "spotify")
        if spid and self.spotify.configured():
            p = await self._safe(sid, "Spotify", self.spotify.artist_profile(spid))
            if p:
                self.store.apply_profile(eid, p)
        else:
            self.store.mark_unavailable(eid, "spotify.monthly_listeners", SPOTIFY_MONTHLY_REASON)
        yt = self.store.ident(eid, "youtube")
        yth = self.store.ident(eid, "youtube_handle")
        if (yt or yth) and self.youtube.configured():
            p = await self._safe(sid, "YouTube", self.youtube.channel_profile(yt, None if yt else yth))
            if p:
                self.store.apply_profile(eid, p)
        elif not (yt or yth):
            self.store.mark_unavailable(eid, "youtube", "No YouTube channel is linked from MusicBrainz or Wikidata "
                                                        "(channels are not matched by name to avoid wrong identities).")
        if mbid and self.lastfm.configured():
            p = await self._safe(sid, "Last.fm", self.lastfm.artist_profile(mbid, name))
            if p:
                self.store.apply_profile(eid, p)

        # contacts from official websites
        if settings.web_contacts:
            sites = [r["value"] for r in self.db.q(
                "SELECT DISTINCT c.value FROM contacts c JOIN sources s ON s.id=c.source_id WHERE c.entity_id=? "
                "AND c.contact_type='website' AND s.provider IN ('musicbrainz','wikidata')", (eid,))]
            for site in sites[:2]:
                found, notes = await self._safe(sid, f"Website {site}", self.web.discover(site), ([], [])) or ([], [])
                for n in notes:
                    if sid:
                        self.db.log(sid, n, "warn")
                if found:
                    from ..providers.base import ProfileData
                    self.store.apply_profile(eid, ProfileData(contacts=found))
                    if sid:
                        self.db.log(sid, f"{name}: {sum(1 for c in found if c.contact_type in ('email', 'phone'))} "
                                         f"business contact(s) found on {site}")
        has_direct = self.db.one("SELECT 1 FROM contacts WHERE entity_id=? AND contact_type IN ('email','phone')", (eid,))
        if not has_direct:
            self.store.mark_unavailable(eid, "contacts.email_phone",
                                        "No email or phone number is published on the official sources checked.")
        else:
            self.db.x("DELETE FROM unavailable WHERE entity_id=? AND topic='contacts.email_phone'", (eid,))
        self.db.x("UPDATE entities SET profiled_at=? WHERE id=?", (now(), eid))
        return eid

    # ---- step 2: expand a node of the graph ------------------------------------------------------
    def expand_node(self, sid: int, eid: int) -> dict:
        """Research one more person inside an existing research session. Their collaborators join
        the session's graph one ring further out. Runs in the background; poll expansion_status."""
        row = self.db.one("SELECT status, root_entity_id FROM research_sessions WHERE id=?", (sid,))
        if not row or row["status"] != "done":
            raise ValueError("Only a finished research can be expanded.")
        if eid == row["root_entity_id"]:
            raise ValueError("This is the centre of the research; it is already fully researched.")
        task = self.expansions.get((sid, eid))
        if task and not task.done():
            return {"status": "running"}
        self.db.x("INSERT INTO session_expansions(session_id,entity_id,status,requested_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(session_id,entity_id) DO UPDATE SET status='running', requested_at=excluded.requested_at, "
                  "finished_at=NULL", (sid, eid, "running", now()))
        self.expansions[(sid, eid)] = asyncio.create_task(self._run_expand(sid, eid))
        return {"status": "running"}

    async def _run_expand(self, sid: int, eid: int) -> None:
        name = (self.db.one("SELECT name FROM entities WHERE id=?", (eid,)) or {"name": "?"})["name"]
        try:
            # 1. everything already in the database for this person (instant, from earlier research)
            for r in self.db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (eid,)):
                self._add_session_track(sid, r["track_id"], eid, 3)
            # 2. live research from the first identifier we have
            researched = False
            for p in ("musicbrainz", "genius", "discogs"):
                xid = self.store.ident(eid, p)
                if xid and (p != "genius" or self.genius.configured()) and (p != "discogs" or self.discogs.configured()):
                    self.db.log(sid, f"Expanding {name} from the graph …")
                    new = await self.research_person(sid, p, xid, degree=3, limits=(4, 15, 12))
                    eid = new or eid
                    researched = True
                    break
            if not researched:
                self.db.log(sid, f"{name} has no MusicBrainz/Genius/Discogs identifier; expanded from credits "
                                 f"already in the database only.", "warn")
            eid = await self.enrich(sid, eid, full=False)
            for r in self.db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (eid,)):
                self._add_session_track(sid, r["track_id"], eid, 3)
            n = self.db.one("SELECT COUNT(DISTINCT track_id) n FROM session_tracks WHERE session_id=? AND via_entity_id=?",
                            (sid, eid))["n"]
            self.db.x("UPDATE session_expansions SET status='done', tracks=?, finished_at=? WHERE session_id=? AND entity_id=?",
                      (n, now(), sid, eid))
            async with self.compute_lock:
                result = self.compute(sid)
                self.db.x("UPDATE research_sessions SET result=? WHERE id=?", (json.dumps(result), sid))
            self.db.log(sid, f"Expanded {name}: {n} credited tracks in this research.")
        except Exception as e:
            self.db.log(sid, f"Expanding {name} failed: {type(e).__name__}: {e}", "error")
            self.db.x("UPDATE session_expansions SET status='failed', finished_at=? WHERE session_id=? AND entity_id=?",
                      (now(), sid, eid))

    def expansion_status(self, sid: int) -> list[dict]:
        rows = [dict(r) for r in self.db.q(
            "SELECT x.entity_id, e.name, x.status, x.tracks, x.requested_at, x.finished_at FROM session_expansions x "
            "LEFT JOIN entities e ON e.id=x.entity_id WHERE x.session_id=? ORDER BY x.requested_at", (sid,))]
        for r in rows:  # a server restart kills running tasks
            t = self.expansions.get((sid, r["entity_id"]))
            if r["status"] == "running" and (t is None or t.done()):
                r["status"] = "failed"
        return rows

    # ---- result ----------------------------------------------------------------------------------
    def compute(self, sid: int) -> dict:
        row = self.db.one("SELECT * FROM research_sessions WHERE id=?", (sid,))
        root = row["root_entity_id"]
        d1 = self._session_track_ids(sid, 1)
        all_t = self._session_track_ids(sid)
        s1 = network.load_scope(self.db, d1)
        s_all = network.load_scope(self.db, all_t)
        direct = network.direct_network(s1, root) if root in s1.entities else []
        # direct relationships use first-degree tracks; second-degree uses everything collected
        direct_full = [{**d, **(network.score_pair(s_all, root, d["id"]) or {})} for d in direct]
        second = network.second_degree(s_all, root, direct_full)
        disc = network.discoveries(s_all, root, direct_full, second) if root in s_all.entities else {}

        expanded = [r["entity_id"] for r in self.db.q(
            "SELECT entity_id FROM session_expansions WHERE session_id=? AND status='done' ORDER BY requested_at", (sid,))]
        g = network.graph(s_all, root, direct_full, second, center=None) if root in s_all.entities else None
        expanded_people = network.add_expansions(s_all, g, expanded) if g else []

        center = None
        if row["root_track_id"]:
            t = s1.tracks.get(row["root_track_id"], {})
            center = {"kind": "song", "title": t.get("title"), "artist": t.get("artist")}
        elif row["root_release_id"]:
            r = self.db.one("SELECT * FROM releases WHERE id=?", (row["root_release_id"],))
            center = {"kind": "album", "title": r["title"], "artist": r["artist_text"]} if r else None

        tracks = []
        for tid, t in sorted(s1.tracks.items(), key=lambda kv: (-(kv[1].get("year") or 0), kv[1]["title"])):
            cr = []
            for eid, roles in s1.credits.get(tid, {}).items():
                for role, provs in roles.items():
                    cr.append({"entity_id": eid, "name": s1.entities.get(eid, {}).get("name"),
                               "type": s1.entities.get(eid, {}).get("type"), "role": role,
                               "group": ROLE_TO_GROUP.get(role, "other"), "providers": sorted(provs),
                               "confidence": "multi_source" if len(provs) > 1 else "single_source"})
            tracks.append({**t, "credits": cr, "sources": s1.sources.get(tid, [])})

        recurring = []
        if center and center["kind"] == "album":
            for eid in s1.entities:
                n = len(s1.by_entity.get(eid, set()))
                if n >= 2 and eid != root:
                    recurring.append({"id": eid, "name": s1.entities[eid]["name"], "tracks": n,
                                      "roles": dict(network.entity_roles(s1, eid))})
            recurring.sort(key=lambda x: -x["tracks"])

        contacts = []
        for e in [root] + [d["id"] for d in direct[:12]]:
            rows = [dict(c) for c in self.db.q(
                "SELECT c.contact_type, c.value, c.purpose, c.context, c.status, c.last_checked_at, s.url, s.provider, "
                "s.description AS source_description FROM contacts c JOIN sources s ON s.id=c.source_id WHERE c.entity_id=?", (e,))]
            for g in group_contacts(rows):
                contacts.append({"entity_id": e, "name": s_all.entities.get(e, {}).get("name"), **g})

        sources = {}
        for t in list(s_all.sources.values()):
            for src in t:
                sources[src["url"]] = src
        for r in self.db.q("SELECT DISTINCT s.provider, s.url, s.description, s.retrieved_at FROM sources s WHERE s.id IN "
                           "(SELECT source_id FROM facts WHERE entity_id=? UNION SELECT source_id FROM contacts WHERE entity_id=? "
                           "UNION SELECT source_id FROM metrics WHERE entity_id=?)", (root, root, root)):
            sources[r["url"]] = dict(r)
        by_prov: dict[str, int] = {}
        for src in sources.values():
            by_prov[src["provider"]] = by_prov.get(src["provider"], 0) + 1

        log = json.loads(row["log"] or "[]")
        return {
            "session_id": sid, "query": row["query"], "kind": row["target_kind"], "root_entity_id": root,
            "root_name": s_all.entities.get(root, {}).get("name"), "center": center,
            "counts": {"tracks": len(d1), "tracks_total": len(all_t), "direct": len(direct), "second": len(second),
                       "sources": len(sources)},
            "providers_used": by_prov,
            "warnings": [l["msg"] for l in log if l["level"] in ("warn", "config", "error")][-30:],
            "tracks": tracks[:300], "recurring": recurring[:40],
            "direct": [{**d, "evidence": d["evidence"][:8]} for d in direct_full[:150]],
            "second": second[:150], "discover": disc,
            "graph": {**g, "center": center} if g else None, "expanded": expanded,
            "expanded_people": expanded_people[:300],
            "contacts": contacts, "sources": sorted(sources.values(), key=lambda x: (x["provider"], x["url"]))[:600],
        }

    async def refresh_profile(self, eid: int) -> None:
        await self.enrich(None, eid, full=True)
