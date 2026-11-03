"""Persisting provider records + entity resolution.

Identity rules (in order; first hit wins):
1. Same provider ID already known                         -> same entity
2. Authoritative cross-link (MusicBrainz/Wikidata/Discogs profile links to the other provider's
   page)                                                  -> same entity (merges duplicates)
3. Same normalized name credited on the *same track* by another provider -> same entity
4. Same normalized name AND already co-credited with someone on this track -> same entity
Otherwise a new entity is created. If another entity has the same name but none of the rules
apply, the pair is stored in `identity_questions` and shown as an ambiguity instead of merged.
An entity never absorbs a second ID from the same provider by name alone (two MusicBrainz
artists with the same name stay two people).
"""
from __future__ import annotations

from ..db import DB, now
from ..providers.base import PersonRef, ProfileData, TrackRec
from .normalize import norm_name, norm_title

# cross-link keys from ProfileData.links -> identifier provider name
LINK_PROVIDERS = {"discogs": "discogs", "wikidata": "wikidata", "spotify": "spotify", "genius_url": "genius_url",
                  "youtube_channel": "youtube", "youtube_handle": "youtube_handle", "musicbrainz": "musicbrainz",
                  "genius_slug": "genius_slug", "lastfm": "lastfm"}


class Store:
    def __init__(self, db: DB):
        self.db = db

    # ---- entities ------------------------------------------------------------------
    def ident(self, entity_id: int, provider: str) -> str | None:
        row = self.db.one("SELECT external_id FROM entity_identifiers WHERE entity_id=? AND provider=? "
                          "AND external_id NOT LIKE 'label:%' AND external_id NOT LIKE 'place:%'",
                          (entity_id, provider))
        return row["external_id"] if row else None

    def by_ident(self, provider: str, external_id: str) -> int | None:
        row = self.db.one("SELECT entity_id FROM entity_identifiers WHERE provider=? AND external_id=?",
                          (provider, external_id))
        return row["entity_id"] if row else None

    def create_entity(self, ref: PersonRef) -> int:
        t = now()
        eid = self.db.x("INSERT INTO entities(name,norm_name,entity_type,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (ref.name, norm_name(ref.name), ref.entity_type, t, t))
        self.db.x("INSERT OR IGNORE INTO entity_identifiers(entity_id,provider,external_id,url,match_method) "
                  "VALUES(?,?,?,?,?)", (eid, ref.provider, ref.external_id, ref.url, "primary"))
        return eid

    def _has_provider(self, entity_id: int, provider: str) -> bool:
        return self.db.one("SELECT 1 FROM entity_identifiers WHERE entity_id=? AND provider=?",
                           (entity_id, provider)) is not None

    def _name_candidates(self, ref: PersonRef) -> list[int]:
        n = norm_name(ref.name)
        rows = self.db.q("SELECT id FROM entities WHERE norm_name=? UNION "
                         "SELECT entity_id FROM aliases WHERE lower(alias)=lower(?)", (n, ref.name))
        return [r[0] for r in rows if not self._has_provider(r[0], ref.provider)]

    def resolve(self, ref: PersonRef, track_id: int | None = None, cocredited: set[int] | None = None) -> int:
        eid = self.by_ident(ref.provider, ref.external_id)
        if eid:
            return eid
        cands = self._name_candidates(ref)
        if track_id and cands:
            for c in cands:
                if self.db.one("SELECT 1 FROM credits WHERE track_id=? AND entity_id=?", (track_id, c)):
                    self.link(c, ref.provider, ref.external_id, ref.url, "same_track_name")
                    return c
        if cocredited and cands:
            marks = ",".join("?" * len(cocredited))
            for c in cands:
                row = self.db.one(
                    f"SELECT 1 FROM credits a JOIN credits b ON a.track_id=b.track_id "
                    f"WHERE a.entity_id=? AND b.entity_id IN ({marks}) LIMIT 1", (c, *cocredited))
                if row:
                    self.link(c, ref.provider, ref.external_id, ref.url, "name_and_shared_collaborator")
                    return c
        eid = self.create_entity(ref)
        for c in cands:
            self.db.x("INSERT OR IGNORE INTO identity_questions(a_id,b_id,reason) VALUES(?,?,?)",
                      (min(c, eid), max(c, eid),
                       f"Same name \"{ref.name}\" on {ref.provider} and another source, but no shared track, "
                       f"collaborator or cross-link confirms they are the same person."))
        return eid

    def link(self, entity_id: int, provider: str, external_id: str, url: str | None, method: str) -> int:
        """Attach an identifier. If it belongs to another entity and the method is authoritative,
        merge that entity into this one. Returns the surviving entity id."""
        other = self.by_ident(provider, external_id)
        if other == entity_id:
            return entity_id
        if other is not None:
            if method in ("cross_link", "user"):
                self.merge(entity_id, other)
            return entity_id
        self.db.x("INSERT OR IGNORE INTO entity_identifiers(entity_id,provider,external_id,url,match_method) "
                  "VALUES(?,?,?,?,?)", (entity_id, provider, external_id, url, method))
        return entity_id

    def merge(self, keep: int, drop: int) -> None:
        if keep == drop:
            return
        db = self.db
        with db.lock:
            for table in ("credits", "entity_identifiers", "aliases", "facts", "contacts", "unavailable"):
                db.x(f"UPDATE OR IGNORE {table} SET entity_id=? WHERE entity_id=?", (keep, drop))
                db.x(f"DELETE FROM {table} WHERE entity_id=?", (drop,))
            db.x("UPDATE metrics SET entity_id=? WHERE entity_id=?", (keep, drop))
            db.x("UPDATE OR IGNORE saved_people SET entity_id=? WHERE entity_id=?", (keep, drop))
            db.x("DELETE FROM saved_people WHERE entity_id=?", (drop,))
            db.x("UPDATE OR IGNORE session_tracks SET via_entity_id=? WHERE via_entity_id=?", (keep, drop))
            db.x("DELETE FROM session_tracks WHERE via_entity_id=?", (drop,))
            db.x("UPDATE research_sessions SET root_entity_id=? WHERE root_entity_id=?", (keep, drop))
            db.x("DELETE FROM identity_questions WHERE (a_id=? AND b_id=?) OR (a_id=? AND b_id=?)",
                 (keep, drop, drop, keep))
            db.x("UPDATE OR IGNORE identity_questions SET a_id=? WHERE a_id=?", (keep, drop))
            db.x("UPDATE OR IGNORE identity_questions SET b_id=? WHERE b_id=?", (keep, drop))
            db.x("DELETE FROM identity_questions WHERE a_id=? OR b_id=?", (drop, drop))
            name = db.one("SELECT name FROM entities WHERE id=?", (drop,))
            if name:
                db.x("INSERT OR IGNORE INTO aliases(entity_id,alias) VALUES(?,?)", (keep, name["name"]))
            db.x("DELETE FROM entities WHERE id=?", (drop,))

    # ---- releases & tracks --------------------------------------------------------------
    def _release(self, rec: TrackRec) -> int | None:
        r = rec.release
        if not r or not r.title:
            return None
        row = self.db.one("SELECT release_id FROM release_identifiers WHERE provider=? AND external_id=?",
                          (r.provider, r.external_id))
        if row:
            return row["release_id"]
        nt = norm_title(r.title)
        rid = None
        for cand in self.db.q("SELECT id, artist_text FROM releases WHERE norm_title=?", (nt,)):
            if norm_name(cand["artist_text"] or "") == norm_name(r.artist_text or rec.artist_text or ""):
                rid = cand["id"]
                break
        if rid is None:
            rid = self.db.x("INSERT INTO releases(title,norm_title,artist_text,release_date,release_type,created_at) "
                            "VALUES(?,?,?,?,?,?)", (r.title, nt, r.artist_text or rec.artist_text, r.release_date,
                                                   r.release_type, now()))
        self.db.x("INSERT OR IGNORE INTO release_identifiers(release_id,provider,external_id,url) VALUES(?,?,?,?)",
                  (rid, r.provider, r.external_id, r.url))
        if r.release_date:
            self.db.x("UPDATE releases SET release_date=? WHERE id=? AND (release_date IS NULL OR release_date>?)",
                      (r.release_date, rid, r.release_date))
        return rid

    def save_track(self, rec: TrackRec) -> int:
        mains = [c.person.name for c in rec.credits if c.role == "main_artist"]
        nartist = norm_name(mains[0] if mains else rec.artist_text.split(",")[0])
        nt = norm_title(rec.title)
        row = self.db.one("SELECT track_id FROM track_identifiers WHERE provider=? AND external_id=?",
                          (rec.provider, rec.external_id))
        tid = row["track_id"] if row else None
        if tid is None:
            hit = self.db.one("SELECT id FROM tracks WHERE norm_title=? AND norm_artist=?", (nt, nartist))
            tid = hit["id"] if hit else None
        rid = self._release(rec)
        if tid is None:
            tid = self.db.x("INSERT INTO tracks(title,norm_title,artist_text,norm_artist,release_date,release_id,created_at) "
                            "VALUES(?,?,?,?,?,?,?)", (rec.title, nt, rec.artist_text, nartist, rec.release_date, rid, now()))
        else:
            self.db.x("UPDATE tracks SET release_id=COALESCE(release_id, ?) WHERE id=?", (rid, tid))
            if rec.release_date:
                self.db.x("UPDATE tracks SET release_date=? WHERE id=? AND (release_date IS NULL OR release_date>?)",
                          (rec.release_date, tid, rec.release_date))
        self.db.x("INSERT OR IGNORE INTO track_identifiers(track_id,provider,external_id,url) VALUES(?,?,?,?)",
                  (tid, rec.provider, rec.external_id, rec.url))
        src = self.db.source(rec.provider, rec.url, rec.source_description)

        resolved: set[int] = set()
        known = [c for c in rec.credits if self.by_ident(c.person.provider, c.person.external_id)]
        unknown = [c for c in rec.credits if c not in known]
        for c in known + unknown:
            eid = self.resolve(c.person, tid, resolved)
            resolved.add(eid)
            self.db.x("INSERT OR IGNORE INTO credits(track_id,entity_id,role,role_detail,credited_as,source_id,provider) "
                      "VALUES(?,?,?,?,?,?,?)", (tid, eid, c.role, c.role_detail, c.person.credited_as, src, rec.provider))
        return tid

    # ---- profiles -------------------------------------------------------------------
    def apply_profile(self, entity_id: int, p: ProfileData) -> int:
        db, t = self.db, now()
        for key, value in p.links.items():
            prov = LINK_PROVIDERS.get(key)
            if prov and value:
                entity_id = self.link(entity_id, prov, value, None, "cross_link")
        for n in p.names:
            db.x("INSERT OR IGNORE INTO aliases(entity_id,alias) VALUES(?,?)", (entity_id, n))
        for f in p.facts:
            sid = db.source(f.source_provider, f.source_url, f.source_description)
            db.x("INSERT OR IGNORE INTO facts(entity_id,field,value,source_id,retrieved_at) VALUES(?,?,?,?,?)",
                 (entity_id, f.field, f.value, sid, t))
        for c in p.contacts:
            sid = db.source(c.source_provider, c.source_url, c.source_description)
            db.x("INSERT INTO contacts(entity_id,contact_type,purpose,value,context,status,source_id,last_checked_at) "
                 "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(entity_id,contact_type,value,source_id) "
                 "DO UPDATE SET last_checked_at=excluded.last_checked_at, status=excluded.status",
                 (entity_id, c.contact_type, c.purpose, c.value, c.context, c.status, sid, t))
        for m in p.metrics:
            sid = db.source(m.platform.lower().replace(".", ""), m.source_url, m.source_description)
            db.x("INSERT INTO metrics(entity_id,platform,metric,value,source_id,retrieved_at) VALUES(?,?,?,?,?,?)",
                 (entity_id, m.platform, m.metric, m.value, sid, t))
        for topic, reason in p.unavailable.items():
            db.x("INSERT OR REPLACE INTO unavailable(entity_id,topic,reason,checked_at) VALUES(?,?,?,?)",
                 (entity_id, topic, reason, t))
        return entity_id

    def mark_unavailable(self, entity_id: int, topic: str, reason: str) -> None:
        self.db.x("INSERT OR REPLACE INTO unavailable(entity_id,topic,reason,checked_at) VALUES(?,?,?,?)",
                  (entity_id, topic, reason, now()))
