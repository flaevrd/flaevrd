"""Command-line research: one name in, one compact, plain-language summary out.

    python -m app.cli lookup "Carter Lang" --out result.json
    python -m app.cli summary 78 --out result.json        # from research already in the database

Used by the simple FLAEVRD Network page: a background job runs `lookup` and stores the JSON for the page.
The summary keeps the evidence (songs and source links) behind every connection, and stays well
under 250 KB so it fits in one stored document.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import settings
from .db import DB, now
from .engine.normalize import ROLE_LABELS, norm_name
from .engine.profile import build_profile
from .engine.research import Researcher
from .http import Http

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


def summarize(db: DB, sid: int, alternatives: list[dict] | None = None) -> dict:
    row = db.one("SELECT result FROM research_sessions WHERE id=?", (sid,))
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
                    "years": [d.get("first_year"), d.get("latest_year")], "songs": _songs(d["evidence"])})
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
                    "how": path["explanation"], "songs": _songs(path["evidence"], 2)})
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
        "contacts": _contacts(p), "metrics": metrics,
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


async def lookup(query: str, depth: int = 2) -> dict:
    db = DB(settings.db_path)
    http = Http(db)
    r = Researcher(db, http)
    try:
        res = await r.suggest(query, "artist")
        cands = [c for c in res["candidates"] if c["kind"] == "artist"]
        if not cands:
            return {"error": f"No person called “{query}” was found in MusicBrainz or Genius."}
        nq = norm_name(query)
        exact = [c for c in cands if norm_name(c["title"]) == nq]
        pick = (exact or cands)[0]
        alts = [{"name": c["title"], "about": c["subtitle"], "provider": c["provider"], "id": c["external_id"]}
                for c in cands if c is not pick][:5]
        sid = r.start(pick["title"], "artist", pick["provider"], pick["external_id"], depth)
        await r.jobs[sid]
        row = db.one("SELECT status, log FROM research_sessions WHERE id=?", (sid,))
        if row["status"] != "done":
            last = [l["msg"] for l in json.loads(row["log"]) if l["level"] == "error"][-1:]
            return {"error": "The research did not finish. " + (last[0] if last else "")}
        out = summarize(db, sid, alts)
        out["matched"] = {"name": pick["title"], "about": pick["subtitle"], "provider": pick["provider"]}
        return out
    finally:
        await http.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("lookup", help="research one person by name")
    a.add_argument("query")
    a.add_argument("--depth", type=int, default=2)
    a.add_argument("--out")
    b = sub.add_parser("summary", help="summarize an existing research session")
    b.add_argument("session_id", type=int)
    b.add_argument("--out")
    args = ap.parse_args(argv)
    if args.cmd == "lookup":
        out = asyncio.run(lookup(args.query, args.depth))
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
