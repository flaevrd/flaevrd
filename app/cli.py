"""Command-line research: one name in, one compact, plain-language summary out.

    python -m app.cli lookup "Carter Lang" --out result.json          # quick: direct collaborators + their socials
    python -m app.cli lookup "deep: Carter Lang" --out result.json    # deep: also 2nd degree and expansions
    python -m app.cli lookup "song: Kendrick Lamar - Luther"           # or "album: ..."
    python -m app.cli summary 78 --out result.json        # from research already in the database

Used by the simple FLAEVRD Network page: a background job runs `lookup` and stores the JSON for the page.
The summary keeps the evidence (songs and source links) behind every connection, and stays well
under 250 KB so it fits in one stored document.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys

from .config import settings
from .db import DB, now
from .engine.normalize import ROLE_LABELS, norm_name
from .engine.profile import build_profile
from .engine.research import Researcher
from .http import Http
from .providers.links import contact_key
from .providers.musicbrainz import split_query

GROUP_TITLE = {"producer": "Producers", "writer": "Songwriters", "engineer": "Engineers (recording, mixing, mastering)",
               "artist": "Artists", "musician": "Musicians", "business": "Labels, A&R & management", "studio": "Studios",
               "other": "Other credits"}
GROUP_ORDER = ["producer", "writer", "engineer", "artist", "musician", "business", "studio"]
CLOSENESS = [(70, "Close collaborator"), (40, "Regular collaborator"), (0, "Worked together")]
CONTACT_TYPES = {"email": "Email", "phone": "Phone", "website": "Website", "instagram": "Instagram",
                 "linkedin": "LinkedIn", "x": "X / Twitter", "tiktok": "TikTok", "youtube": "YouTube",
                 "soundcloud": "SoundCloud", "facebook": "Facebook", "bandcamp": "Bandcamp", "other_social": "Social"}
STATUS = {"verified": "Verified", "publicly_listed": "Publicly listed", "unverified": "Unverified"}


def closeness(strength: int) -> str:
    return next(label for cut, label in CLOSENESS if strength >= cut)


def _songs(evidence: list[dict], n: int = 3) -> list[dict]:
    out = []
    for e in evidence[:n]:
        src = (e.get("sources") or [{}])[0]
        out.append({"title": e.get("title"), "artist": e.get("artist"), "year": e.get("year"), "source": src.get("url"),
                    "source_name": src.get("provider")})
    return out


def _fact(p: dict, *fields: str) -> str | None:
    for f in fields:
        if f in p["facts"]:
            return p["facts"][f]["values"][0]["value"]
    return None


def _contacts(p: dict) -> list[dict]:
    out = []
    for c in p["contacts"]:
        src = c["sources"][0]
        out.append({"type": CONTACT_TYPES.get(c["contact_type"], c["contact_type"]), "kind": c["contact_type"],
                    "value": c["value"], "purpose": c.get("purpose"), "status": STATUS.get(c["status"], c["status"]),
                    "source": src["url"], "source_name": src["provider"], "checked": (src.get("last_checked_at") or "")[:10]})
    return out[:14]


SOCIAL_ORDER = ["instagram", "x", "tiktok", "youtube", "soundcloud", "facebook", "linkedin", "bandcamp", "website",
                "email", "phone"]
IG = re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.I)


def _current_id(db: DB, eid: int) -> int:
    """Follow entity merges (two records found to be the same person) to the surviving id."""
    for _ in range(10):
        row = db.one("SELECT new_id FROM entity_redirects WHERE old_id=?", (eid,))
        if not row:
            return eid
        eid = row["new_id"]
    return eid


def socials(db: DB, eid: int) -> list[dict]:
    """Public social accounts and business contacts stored for one person, each with where it was found."""
    rows = db.q("SELECT c.contact_type, c.value, c.purpose, c.status, s.provider, s.url FROM contacts c "
                "JOIN sources s ON s.id=c.source_id WHERE c.entity_id=?", (_current_id(db, eid),))
    best: dict[tuple, dict] = {}
    for c in rows:
        t = c["contact_type"]
        if t not in SOCIAL_ORDER:
            continue
        key = contact_key(t, c["value"])
        item = best.get(key)
        if item:
            if c["provider"] not in item["found_on"]:
                item["found_on"].append(c["provider"])
            continue
        item = {"kind": t, "type": CONTACT_TYPES.get(t, t), "value": c["value"], "purpose": c["purpose"],
                "status": STATUS.get(c["status"], c["status"]), "source": c["url"], "found_on": [c["provider"]]}
        if t == "instagram":
            m = IG.search(c["value"])
            if m:
                item["handle"] = m.group(1)
        best[key] = item
    out = sorted(best.values(), key=lambda x: (SOCIAL_ORDER.index(x["kind"]), -len(x["found_on"])))
    return out[:8]


async def fetch_socials(r: Researcher, ids: list[int], log=None) -> None:
    """Quick public-profile check (MusicBrainz links, Genius, Discogs) for people shown in a result, so their
    Instagram and other accounts are there without researching each one. People already profiled are skipped."""
    sem = asyncio.Semaphore(6)

    async def one(eid: int) -> None:
        cur = lambda: _current_id(r.db, eid)  # other people's profiles can merge this record while we wait on the network
        e = r.db.one("SELECT name, profiled_at FROM entities WHERE id=?", (cur(),))
        if not e or e["profiled_at"]:
            return
        async with sem:
            try:
                mbid = r.store.ident(cur(), "musicbrainz")
                if mbid:
                    a = await r._safe(None, "MusicBrainz profile", r.mb.artist(mbid))
                    if a:
                        r.store.apply_profile(cur(), r.mb.profile_from_artist(a))
                gid = r.store.ident(cur(), "genius")
                if gid and r.genius.configured():
                    g = await r._safe(None, "Genius profile", r.genius.artist(gid))
                    if g:
                        r.store.apply_profile(cur(), r.genius.profile_from_artist(g))
                did = r.store.ident(cur(), "discogs")
                if did and r.discogs.configured():
                    d = await r._safe(None, "Discogs profile", r.discogs.artist(did))
                    if d:
                        r.store.apply_profile(cur(), r.discogs.profile_from_artist(d))
            except Exception as ex:  # one person's socials must never sink the whole search
                print(f"socials skipped for entity {eid}: {type(ex).__name__}: {ex}", file=sys.stderr)

    await asyncio.gather(*(one(i) for i in dict.fromkeys(ids)))


def shown_ids(result: dict, per_group: int = 10, n_next: int = 15) -> list[int]:
    root = result["root_entity_id"]
    counts: dict[str, int] = {}
    ids = []
    for d in result["direct"]:
        g = d["group"] if d["group"] in GROUP_TITLE else "other"
        if d["id"] == root or d["type"] not in ("person", "group", "unknown") or counts.get(g, 0) >= per_group:
            continue
        counts[g] = counts.get(g, 0) + 1
        ids.append(d["id"])
    ids += [d["id"] for d in result["second"] if d["type"] in ("person", "group", "unknown")][:n_next]
    return ids


def summarize(db: DB, sid: int, alternatives: list[dict] | None = None) -> dict:
    row = db.one("SELECT result, depth FROM research_sessions WHERE id=?", (sid,))
    r = json.loads(row["result"])
    root = r["root_entity_id"]
    p = build_profile(db, root)
    bio = _fact(p, "bio", "summary")
    groups: dict[str, list] = {}
    for d in r["direct"]:
        if d["id"] == root:
            continue
        g = d["group"] if d["group"] in GROUP_TITLE else "other"
        lst = groups.setdefault(g, [])
        if len(lst) >= 14:
            continue
        roles = sorted(d["roles"].items(), key=lambda kv: -kv[1])
        lst.append({"id": d["id"], "name": d["name"], "strength": d["strength"], "closeness": closeness(d["strength"]),
                    "roles": [ROLE_LABELS.get(k, k) for k, _ in roles[:3]], "songs_together": d["shared_tracks"],
                    "years": [d.get("first_year"), d.get("latest_year")], "songs": _songs(d["evidence"], 2),
                    "socials": socials(db, d["id"])})
    people = [{"group": g, "title": GROUP_TITLE[g], "total": sum(1 for d in r["direct"] if (d["group"] if d["group"] in GROUP_TITLE else "other") == g),
               "people": groups[g]} for g in GROUP_ORDER + ["other"] if g in groups]
    nxt = []
    for d in r["second"]:
        if d["type"] not in ("person", "group", "unknown") or len(nxt) >= 24:
            continue
        path = d["paths"][0]
        roles = sorted(d["roles"].items(), key=lambda kv: -kv[1])
        nxt.append({"id": d["id"], "name": d["name"], "strength": d["strength"],
                    "roles": [ROLE_LABELS.get(k, k) for k, _ in roles[:3]],
                    "via": path["via_name"], "via_count": len({x["via_id"] for x in d["paths"]}),
                    "how": path["explanation"], "songs": _songs(path["evidence"], 2),
                    "socials": socials(db, d["id"])})
    metrics = [{"platform": m["platform"], "metric": m["metric"].replace("_", " "), "value": m["latest"]["value"],
                "source": m["latest"]["url"], "checked": m["latest"]["retrieved_at"][:10]} for m in p["metrics"]]
    years = p.get("active_years")
    out = {
        "name": p["name"], "entity_id": root, "kind": p["primary_role"] or "Music professional",
        "roles": [x["label"] for x in p["roles"][:5]],
        "location": _fact(p, "area", "residence", "begin_area", "formed_in", "born_in"),
        "country": _fact(p, "country"),
        "genres": [v["value"] for v in p["facts"].get("genre", {}).get("values", [])][:6],
        "bio": bio[:700] if bio else None,
        "bio_source": p["facts"].get("bio", p["facts"].get("summary", {})).get("values", [{}])[0].get("sources", [{}])[0].get("url") if bio else None,
        "active": years, "songs_found": r["counts"]["tracks"],
        "contacts": _contacts(p), "socials": socials(db, root), "metrics": metrics,
        "deep": row["depth"] >= 2,
        "links": [{"name": i["provider"], "url": i["url"]} for i in p["identifiers"] if i.get("url") and i["provider"] in
                  ("musicbrainz", "genius_url", "discogs", "wikidata", "spotify")][:6],
        "people": people, "next": nxt,
        "counts": {"direct": r["counts"]["direct"], "second": r["counts"]["second"], "sources": r["counts"]["sources"]},
        "sources_used": r["providers_used"],
        "alternatives": alternatives or [],
        "researched_at": now(),
    }
    blob = json.dumps(out)
    while len(blob) > 240_000:  # keep it inside one stored document
        for g in out["people"]:
            if len(g["people"]) > 4:
                g["people"].pop()
        if len(out["next"]) > 8:
            out["next"].pop()
        blob = json.dumps(out)
        if all(len(g["people"]) <= 4 for g in out["people"]) and len(out["next"]) <= 8:
            break
    return out


PICK = re.compile(r"^pick:(musicbrainz|genius|discogs):(artist|song|album):(\S+)\s*", re.I)
WHAT = {"artist": "person", "song": "song", "album": "album"}


def parse_query(query: str) -> tuple[str, str, bool]:
    """'deep: song: Name' -> ('Name', 'song', True). No prefix means 'auto': the tool works out whether the text is
    a person, a song or an album. Prefixes are how the page asks for one reading in particular, or for a deep search."""
    q, kind, deep = query.strip(), "auto", False
    while True:
        m = re.match(r"^(deep|song|album|person|artist)\s*:\s*", q, re.I)
        if not m:
            return q, kind, deep
        word = m.group(1).lower()
        if word == "deep":
            deep = True
        else:
            kind = "artist" if word == "person" else word
        q = q[m.end():].strip()


def _alt(c: dict) -> dict:
    """One other thing the search text could have meant; `query` re-runs the search for exactly that one."""
    return {"name": c["title"], "about": c["subtitle"], "provider": c["provider"], "id": c["external_id"], "kind": WHAT[c["kind"]],
            "query": f"pick:{c['provider']}:{c['kind']}:{c['external_id']} {c['title']}"}


async def choose(r: Researcher, q: str, kind: str) -> tuple[dict | None, list[dict]]:
    """Pick what to research for the search text, plus the other readings to offer. Nothing is guessed silently:
    every alternative is returned so the page can show 'meant something else?'."""
    nq = norm_name(q)
    dashed = bool(split_query(q)[1])

    def first(cands, k, prefer_genius=False):
        lst = [c for c in cands if c["kind"] == k]
        if prefer_genius:  # a bare song title matches thousands of songs; Genius ranks by popularity
            lst = [c for c in lst if c["provider"] == "genius"] + [c for c in lst if c["provider"] != "genius"]
        return lst

    if kind != "auto":
        cands = (await r.suggest(q, kind))["candidates"]
        lst = first(cands, kind, kind == "song" and not dashed)
        if kind == "artist":
            lst = [c for c in lst if norm_name(c["title"]) == nq] + [c for c in lst if norm_name(c["title"]) != nq]
        return (lst[0] if lst else None), [_alt(c) for c in lst[1:6]]

    artists = [] if dashed else first((await r.suggest(q, "artist"))["candidates"], "artist")
    res = await r.suggest(q, "auto")
    songs, albums = first(res["candidates"], "song", not dashed), first(res["candidates"], "album")
    sq = lambda t: re.sub(r"[^a-z0-9]", "", (t or "").lower())
    exact = [c for c in artists if norm_name(c["title"]) == nq]
    exact_work = [c for c in albums + songs if sq(c["title"]) == sq(q)]
    if exact:
        pick = exact[0]
    elif dashed:
        works = [c for c in res["candidates"] if c["kind"] in ("song", "album")]
        pick = works[0] if works else None
    elif res["detected"] in ("song", "album") and (songs if res["detected"] == "song" else albums):
        pick = (songs if res["detected"] == "song" else albums)[0]
    elif exact_work:
        pick = exact_work[0]
    else:
        pick = (artists or songs or albums or [None])[0]
    if pick is None:
        return None, []
    near = lambda c: sq(q) in sq(c["title"]) or sq(c["title"]) in sq(q)  # drop loose matches nobody meant
    others = ([c for c in artists if c is not pick and near(c)][:3] + [c for c in songs if c is not pick and near(c)][:2]
              + [c for c in albums if c is not pick and near(c)][:2])
    if pick["kind"] != "artist":
        others.sort(key=lambda c: c["kind"] != pick["kind"])
    return pick, [_alt(c) for c in others]


async def lookup(query: str, depth: int | None = None) -> dict:
    q, kind, deep = parse_query(query)
    if depth is None:
        depth = 2 if deep else 1
    db = DB(settings.db_path)
    http = Http(db)
    r = Researcher(db, http)
    if depth < 2:
        r.profile_top = 0  # quick search: socials for everyone shown (below) instead of full profiles for a few
    try:
        if not q:
            return {"error": "The search was empty."}
        m = PICK.match(q)
        if m:  # the person chose one exact candidate from 'meant something else?'
            title = q[m.end():].strip() or m.group(3)
            pick, alts = {"kind": m.group(2).lower(), "provider": m.group(1).lower(), "external_id": m.group(3), "title": title, "subtitle": ""}, []
        else:
            pick, alts = await choose(r, q, kind)
        if pick is None:
            what = "person, song or album" if kind == "auto" else WHAT[kind]
            return {"error": f"No {what} called “{q}” was found in MusicBrainz or Genius."}
        kind = pick["kind"]
        sid = r.start(pick["title"], kind, pick["provider"], pick["external_id"], depth)
        await r.jobs[sid]
        row = db.one("SELECT status, log, result FROM research_sessions WHERE id=?", (sid,))
        if row["status"] != "done":
            last = [l["msg"] for l in json.loads(row["log"]) if l["level"] == "error"][-1:]
            return {"error": "The research did not finish. " + (last[0] if last else "")}
        await fetch_socials(r, shown_ids(json.loads(row["result"])))
        out = summarize(db, sid, alts)
        out["matched"] = {"name": pick["title"], "about": pick["subtitle"], "provider": pick["provider"], "kind": WHAT[kind]}
        if kind != "artist":
            out["searched_for"] = {"kind": WHAT[kind], "title": pick["title"], "about": pick["subtitle"]}
        return out
    finally:
        await http.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lookup", help="research one person by name")
    a.add_argument("query", nargs="?")
    a.add_argument("--query-file", help="read the search text from this file (safer for automation than passing it inline)")
    a.add_argument("--depth", type=int, default=None, help="1 = quick, 2 = deep (default: quick unless the query starts with deep:)")
    a.add_argument("--out")
    b = sub.add_parser("summary", help="summarize an existing research session")
    b.add_argument("session_id", type=int)
    b.add_argument("--out")
    args = ap.parse_args(argv)
    if args.cmd == "lookup":
        q = open(args.query_file, encoding="utf-8").read().strip() if args.query_file else args.query
        if not q:
            ap.error("give a query or --query-file")
        out = asyncio.run(lookup(q, args.depth))
    else:
        out = summarize(DB(settings.db_path), args.session_id)
    text = json.dumps(out, ensure_ascii=False)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(f"wrote {args.out} ({len(text):,} bytes)" + (f" — error: {out['error']}" if "error" in out else ""))
    else:
        sys.stdout.write(text)
    return 1 if "error" in out else 0


if __name__ == "__main__":
    raise SystemExit(main())
