"""Person profile assembly: facts with per-source attribution and discrepancies, contacts,
metrics (latest + history), credits, collaborators, identity notes, CRM state."""
from __future__ import annotations

import json
from collections import defaultdict

from ..db import DB
from ..providers.links import contact_key
from .network import direct_network, entity_roles, load_scope, main_group
from .normalize import ROLE_LABELS

SINGLE_VALUE_FIELDS = {"area", "begin_area", "formed_in", "born_in", "country", "type", "active_since", "residence"}
CONTACT_ORDER = {"email": 0, "phone": 1, "website": 2, "instagram": 3, "linkedin": 4}
STATUS_ORDER = {"verified": 0, "publicly_listed": 1, "unverified": 2}
PROVIDER_URLS = {
    "musicbrainz": lambda i: f"https://musicbrainz.org/{i.replace(':', '/', 1)}" if ":" in i else f"https://musicbrainz.org/artist/{i}",
    "discogs": lambda i: f"https://www.discogs.com/{i.replace(':', '/', 1)}" if ":" in i else f"https://www.discogs.com/artist/{i}",
    "wikidata": lambda i: f"https://www.wikidata.org/wiki/{i}",
    "spotify": lambda i: f"https://open.spotify.com/artist/{i}",
    "youtube": lambda i: f"https://www.youtube.com/channel/{i}",
    "genius_url": lambda i: i,
}


def confidence(n_sources: int, authoritative: bool) -> str:
    if authoritative:
        return "verified"            # 1. verified by an authoritative source
    if n_sources >= 2:
        return "multi_source"        # 2. supported by multiple independent sources
    return "single_source"           # 3. found in one credible source


def build_profile(db: DB, eid: int) -> dict | None:
    e = db.one("SELECT * FROM entities WHERE id=?", (eid,))
    if not e:
        return None
    ids = [dict(r) for r in db.q("SELECT provider, external_id, url, match_method FROM entity_identifiers WHERE entity_id=?", (eid,))]
    for i in ids:
        if not i["url"] and i["provider"] in PROVIDER_URLS:
            i["url"] = PROVIDER_URLS[i["provider"]](i["external_id"])
    aliases = [r["alias"] for r in db.q("SELECT alias FROM aliases WHERE entity_id=? ORDER BY alias", (eid,))]

    facts: dict[str, list[dict]] = defaultdict(list)
    for r in db.q("SELECT f.field, f.value, f.retrieved_at, s.provider, s.url, s.description FROM facts f "
                  "JOIN sources s ON s.id=f.source_id WHERE f.entity_id=? ORDER BY f.id", (eid,)):
        facts[r["field"]].append(dict(r))
    fact_view = {}
    for field, rows in facts.items():
        values: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            values[r["value"]].append({"provider": r["provider"], "url": r["url"], "retrieved_at": r["retrieved_at"],
                                       "description": r["description"]})
        items = [{"value": v, "sources": srcs, "confidence": confidence(len({s["provider"] for s in srcs}), False)}
                 for v, srcs in values.items()]
        items.sort(key=lambda x: -len(x["sources"]))
        fact_view[field] = {
            "values": items,
            "discrepancy": field in SINGLE_VALUE_FIELDS and len(items) > 1,
        }

    contacts = []
    for r in db.q("SELECT c.*, s.provider, s.url AS source_url, s.description AS source_description FROM contacts c "
                  "JOIN sources s ON s.id=c.source_id WHERE c.entity_id=?", (eid,)):
        contacts.append(dict(r))
    contact_list = group_contacts(contacts)

    metrics = defaultdict(list)
    for r in db.q("SELECT m.platform, m.metric, m.value, m.retrieved_at, s.url, s.description FROM metrics m "
                  "JOIN sources s ON s.id=m.source_id WHERE m.entity_id=? ORDER BY m.retrieved_at DESC", (eid,)):
        metrics[(r["platform"], r["metric"])].append(dict(r))
    metric_list = [{"platform": p, "metric": m, "latest": rows[0], "history": rows[1:10]} for (p, m), rows in metrics.items()]
    unavailable = [dict(r) for r in db.q("SELECT topic, reason, checked_at FROM unavailable WHERE entity_id=?", (eid,))]

    # credits across everything stored
    track_ids = [r["track_id"] for r in db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (eid,))]
    s = load_scope(db, track_ids)
    credits = []
    for tid in sorted(track_ids, key=lambda t: -(s.tracks.get(t, {}).get("year") or 0)):
        t = s.tracks.get(tid)
        if not t:
            continue
        roles = s.credits.get(tid, {}).get(eid, {})
        credits.append({**t, "roles": [{"role": r, "label": ROLE_LABELS.get(r, r), "providers": sorted(p)} for r, p in roles.items()],
                        "sources": s.sources.get(tid, [])})
    roles = entity_roles(s, eid)
    collaborators = direct_network(s, eid)[:40]
    for c in collaborators:
        c["evidence"] = c["evidence"][:5]

    questions = []
    for r in db.q("SELECT q.reason, e.id, e.name FROM identity_questions q JOIN entities e ON "
                  "e.id = CASE WHEN q.a_id=? THEN q.b_id ELSE q.a_id END WHERE q.a_id=? OR q.b_id=?", (eid, eid, eid)):
        questions.append(dict(r))
    name_matches = [i for i in ids if i["match_method"] in ("name_match",)]

    crm = db.one("SELECT * FROM saved_people WHERE entity_id=?", (eid,))
    crm_view = None
    if crm:
        crm_view = {**dict(crm), "tags": json.loads(crm["tags"] or "[]")}

    years = [c["year"] for c in credits if c.get("year")]
    return {
        "id": eid, "name": e["name"], "type": e["entity_type"], "disambiguation": e["disambiguation"],
        "profiled_at": e["profiled_at"], "aliases": aliases, "identifiers": ids,
        "primary_role": ROLE_LABELS.get(roles.most_common(1)[0][0]) if roles else None,
        "group": main_group(roles), "roles": [{"role": r, "label": ROLE_LABELS.get(r, r), "count": n} for r, n in roles.most_common()],
        "facts": fact_view, "contacts": contact_list, "metrics": metric_list, "unavailable": unavailable,
        "credits": credits, "credit_count": len(credits),
        "active_years": [min(years), max(years)] if years else None,
        "collaborators": collaborators, "identity_questions": questions, "name_matched_ids": name_matches,
        "crm": crm_view,
    }


def group_contacts(rows: list[dict]) -> list[dict]:
    """One entry per distinct contact, listing every source that published it. The status is the
    best status any source gives it; confidence rises with independent sources."""
    grouped: dict[tuple, dict] = {}
    for c in rows:
        key = contact_key(c["contact_type"], c["value"])
        g = grouped.setdefault(key, {"contact_type": c["contact_type"], "value": c["value"], "purpose": c.get("purpose"),
                                     "context": c.get("context"), "status": c["status"], "sources": []})
        if STATUS_ORDER.get(c["status"], 9) < STATUS_ORDER.get(g["status"], 9):
            g["status"] = c["status"]
            g["value"] = c["value"]
        g["purpose"] = g["purpose"] or c.get("purpose")
        g["context"] = g["context"] or c.get("context")
        src = {"provider": c["provider"], "url": c.get("source_url") or c.get("url"),
               "description": c.get("source_description"), "last_checked_at": c["last_checked_at"], "status": c["status"]}
        if (src["provider"], src["url"]) not in {(s["provider"], s["url"]) for s in g["sources"]}:
            g["sources"].append(src)
    out = sorted(grouped.values(), key=lambda g: (STATUS_ORDER.get(g["status"], 9), CONTACT_ORDER.get(g["contact_type"], 9)))
    for g in out:
        g["sources"].sort(key=lambda s: STATUS_ORDER.get(s["status"], 9))
        g["confidence"] = confidence(len({s["provider"] for s in g["sources"]}), g["status"] == "verified")
        g["last_checked_at"] = max((s["last_checked_at"] or "") for s in g["sources"]) or None
    return out
