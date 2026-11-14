"""Relationship extraction, transparent scoring, second-degree discovery and similar-network
recommendations. Everything here is derived from stored credits, so each claim can list the exact
tracks and source URLs behind it.

Strength score (0-100), deliberately simple so it can be explained in one sentence:
    base        = 30 * log2(1 + shared_tracks)          1 track=30, 3=60, 7=90
    projects    = +5 per extra distinct release (max +15)
    recency     = x (0.6 + 0.4 * r), r = 1 if latest shared work <= 2 years old, halving every 5 years after
    reliability = x average per-track credit reliability: 1.0 if the credit pair is confirmed by >= 2
                  providers, 0.85 MusicBrainz/Discogs only, 0.75 Genius only
Popularity/follower counts are never part of the score.
Second-degree: per path min(strength(root, via), strength(via, target)); best path + 8 per extra
independent path (cap 95). Label/studio paths count half.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..db import DB
from .normalize import ROLE_LABELS, ROLE_TO_GROUP

PROVIDER_RELIABILITY = {"musicbrainz": 0.85, "discogs": 0.85, "genius": 0.75}
PERSONLIKE = {"person", "group", "unknown"}
GROUP_LABEL = {"artist": "artist", "producer": "producer", "writer": "songwriter", "engineer": "engineer",
               "musician": "musician", "business": "label/business", "studio": "studio", "other": "collaborator"}


@dataclass
class Scope:
    tracks: dict[int, dict] = field(default_factory=dict)                 # tid -> info
    credits: dict[int, dict[int, dict[str, set]]] = field(default_factory=dict)  # tid -> eid -> role -> providers
    sources: dict[int, list[dict]] = field(default_factory=dict)          # tid -> [{provider,url}]
    entities: dict[int, dict] = field(default_factory=dict)
    by_entity: dict[int, set[int]] = field(default_factory=lambda: defaultdict(set))


def load_scope(db: DB, track_ids: list[int]) -> Scope:
    s = Scope()
    if not track_ids:
        return s
    for i in range(0, len(track_ids), 500):
        chunk = track_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.q(f"SELECT t.id, t.title, t.artist_text, t.release_date, t.release_id, r.title AS release_title, "
                      f"r.release_date AS release_date2 FROM tracks t LEFT JOIN releases r ON r.id=t.release_id "
                      f"WHERE t.id IN ({marks})", chunk):
            d = r["release_date"] or r["release_date2"] or ""
            s.tracks[r["id"]] = {"id": r["id"], "title": r["title"], "artist": r["artist_text"], "date": d,
                                 "year": int(d[:4]) if d[:4].isdigit() else None,
                                 "release_id": r["release_id"], "release": r["release_title"]}
        for r in db.q(f"SELECT c.track_id, c.entity_id, c.role, c.provider, src.url FROM credits c "
                      f"JOIN sources src ON src.id=c.source_id WHERE c.track_id IN ({marks})", chunk):
            s.credits.setdefault(r["track_id"], {}).setdefault(r["entity_id"], {}).setdefault(r["role"], set()).add(r["provider"])
            s.by_entity[r["entity_id"]].add(r["track_id"])
            lst = s.sources.setdefault(r["track_id"], [])
            if not any(x["url"] == r["url"] for x in lst):
                lst.append({"provider": r["provider"], "url": r["url"]})
    ids = list(s.by_entity)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.q(f"SELECT id, name, entity_type FROM entities WHERE id IN ({marks})", chunk):
            s.entities[r["id"]] = {"id": r["id"], "name": r["name"], "type": r["entity_type"]}
        for r in db.q(f"SELECT entity_id, field, value FROM facts WHERE entity_id IN ({marks}) AND field IN "
                      f"('area','begin_area','formed_in','born_in','residence','country','genre') ORDER BY id", chunk):
            e = s.entities.get(r["entity_id"])
            if not e:
                continue
            if r["field"] == "genre":
                e.setdefault("genres", [])
                if r["value"] not in e["genres"]:
                    e["genres"].append(r["value"])
            elif r["field"] == "country":
                e.setdefault("country", r["value"])
            else:
                e.setdefault("location", r["value"])
    return s


def entity_roles(s: Scope, eid: int, tids: set[int] | None = None) -> Counter:
    c = Counter()
    for tid in (tids if tids is not None else s.by_entity.get(eid, ())):
        for role in s.credits.get(tid, {}).get(eid, {}):
            c[role] += 1
    return c


def main_group(roles: Counter) -> str:
    groups = Counter()
    for role, n in roles.items():
        g = ROLE_TO_GROUP.get(role, "other")
        groups[g] += n * (0.6 if role in {"main_artist", "featured_artist"} and len(roles) > 1 else 1)
    return groups.most_common(1)[0][0] if groups else "other"


def _role_phrase(roles: Counter) -> str:
    items = [ROLE_LABELS.get(r, r).lower() for r, _ in roles.most_common(3)]
    return " / ".join(items) if items else "credited"


def _recency(year: int | None) -> float:
    if not year:
        return 0.6
    age = max(0, date.today().year - year)
    return 1.0 if age <= 2 else max(0.25, 0.5 ** ((age - 2) / 5))


def score_pair(s: Scope, a: int, b: int) -> dict | None:
    shared = s.by_entity.get(a, set()) & s.by_entity.get(b, set())
    if not shared:
        return None
    rel = []
    projects = set()
    years = []
    evidence = []
    for tid in shared:
        t = s.tracks.get(tid, {})
        provs_a = set().union(*s.credits[tid][a].values())
        provs_b = set().union(*s.credits[tid][b].values())
        both = provs_a & provs_b
        if len(both) >= 2:
            rel.append(1.0)
        else:
            rel.append(max(PROVIDER_RELIABILITY.get(p, 0.7) for p in (both or provs_a | provs_b)))
        projects.add(t.get("release_id") or f"t{tid}")
        if t.get("year"):
            years.append(t["year"])
        evidence.append({"track_id": tid, "title": t.get("title"), "artist": t.get("artist"), "year": t.get("year"),
                         "release": t.get("release"),
                         "roles_a": sorted(s.credits[tid][a]), "roles_b": sorted(s.credits[tid][b]),
                         "confirmed_by": sorted(both or (provs_a | provs_b)),
                         "sources": s.sources.get(tid, [])})
    n = len(shared)
    latest = max(years) if years else None
    recency = _recency(latest)
    reliability = sum(rel) / len(rel)
    base = 30 * math.log2(1 + n) + 5 * min(len(projects) - 1, 3)
    strength = min(100, round(base * (0.6 + 0.4 * recency) * reliability))
    multi = sum(1 for e in evidence if len(e["confirmed_by"]) >= 2)
    evidence.sort(key=lambda e: (-(e["year"] or 0), e["title"] or ""))
    return {
        "strength": strength,
        "tier": "strong" if strength >= 70 else "solid" if strength >= 40 else "light",
        "shared_tracks": n, "projects": len(projects), "latest_year": latest,
        "first_year": min(years) if years else None,
        "multi_source_tracks": multi, "reliability": round(reliability, 2),
        "evidence": evidence,
        "why": (f"{n} shared track{'s' if n != 1 else ''} across {len(projects)} project{'s' if len(projects) != 1 else ''}"
                + (f", most recent {latest}" if latest else ", release years unknown")
                + (f"; {multi} confirmed by two or more sources" if multi else "; single-source credits")),
    }


def describe_direct(s: Scope, root: int, other: int, sc: dict) -> tuple[str, str]:
    tids = {e["track_id"] for e in sc["evidence"]}
    r_roles, o_roles = entity_roles(s, root, tids), entity_roles(s, other, tids)
    n = sc["shared_tracks"]
    rn, on = s.entities[root]["name"], s.entities[other]["name"]
    if o_roles.get("featured_artist") and r_roles.get("main_artist"):
        rtype = "Featured artist"
    elif r_roles.get("featured_artist") and o_roles.get("main_artist"):
        rtype = "Featured artist"
    elif ROLE_TO_GROUP.get(o_roles.most_common(1)[0][0]) == "business":
        # A label/publisher on the searched person's own releases: their label, not a "shared" one.
        rtype = "Label" if o_roles.get("label") else "Publisher" if o_roles.get("publisher") else "Business connection"
    elif o_roles.get("studio"):
        rtype = "Recording studio"
    else:
        rtype = "Direct collaboration"
    titles = ", ".join(f"“{e['title']}”" for e in sc["evidence"][:4]) + (" …" if n > 4 else "")
    text = f"{on} — {_role_phrase(o_roles)}; {rn} — {_role_phrase(r_roles)} on {n} track{'s' if n != 1 else ''}: {titles}"
    return rtype, text


def direct_network(s: Scope, root: int) -> list[dict]:
    out = []
    others = set()
    for tid in s.by_entity.get(root, ()):
        others |= set(s.credits.get(tid, {}))
    others.discard(root)
    for o in others:
        if o not in s.entities:
            continue
        sc = score_pair(s, root, o)
        if not sc:
            continue
        rtype, text = describe_direct(s, root, o, sc)
        roles = entity_roles(s, o, {e["track_id"] for e in sc["evidence"]})
        out.append({**s.entities[o], "degree": 1, "relationship": rtype, "summary": text,
                    "roles": dict(roles), "group": main_group(roles), **sc})
    out.sort(key=people_first)
    return out


def people_first(x: dict) -> tuple:
    """Rank people and groups above labels, publishers and studios: the goal is finding people to
    contact. Business entities stay in the list (and in the graph), just after the people."""
    return (0 if x["type"] in PERSONLIKE else 1, -x["strength"], -x.get("independent_tracks", 0),
            -x.get("path_count", 0), x["name"])


def second_degree(s: Scope, root: int, direct: list[dict], limit_paths: int = 4) -> list[dict]:
    direct_ids = {d["id"] for d in direct}
    dstr = {d["id"]: d for d in direct}
    root_tracks = s.by_entity.get(root, set())
    found: dict[int, list[dict]] = defaultdict(list)
    for via in direct:
        vid = via["id"]
        via_tracks = s.by_entity.get(vid, set()) - root_tracks
        targets = set()
        for tid in via_tracks:
            targets |= set(s.credits.get(tid, {}))
        targets -= direct_ids | {root, vid}
        for y in targets:
            if y not in s.entities:
                continue
            sc = score_pair(s, vid, y)
            if not sc:
                continue
            sc["evidence"] = [e for e in sc["evidence"] if e["track_id"] not in root_tracks]
            if not sc["evidence"]:
                continue
            ytids = {e["track_id"] for e in sc["evidence"]}
            v_roles_y = entity_roles(s, vid, ytids)
            v_roles_r = entity_roles(s, vid, {e["track_id"] for e in via["evidence"]})
            y_roles = entity_roles(s, y, ytids)
            weight = 0.5 if via["type"] in ("label", "studio") else 1.0
            g1, g2 = main_group(v_roles_r), main_group(v_roles_y)
            ytype = s.entities[y].get("type")
            if ytype == "label":
                ptype = "Label connection"
            elif ytype == "studio":
                ptype = "Studio connection"
            elif via["type"] == "label":
                ptype = "Shared label"
            elif via["type"] == "studio":
                ptype = "Studio connection"
            elif g1 == g2 and g1 in ("producer", "engineer", "writer", "artist"):
                ptype = f"Shared {GROUP_LABEL[g1]}"
            else:
                ptype = "Indirect professional connection"
            ex1 = ", ".join(f"“{e['title']}”" for e in via["evidence"][:2])
            ex2 = ", ".join(f"“{e['title']}”" for e in sc["evidence"][:2])
            found[y].append({
                "via_id": vid, "via_name": via["name"], "type": ptype,
                "strength": round(min(via["strength"], sc["strength"]) * weight),
                "explanation": (f"{via['name']} ({_role_phrase(v_roles_r)}) worked with "
                                f"{s.entities[root]['name']} on {via['shared_tracks']} track(s), e.g. {ex1}; and "
                                f"({_role_phrase(v_roles_y)}) with {s.entities[y]['name']} "
                                f"({_role_phrase(y_roles)}) on {len(sc['evidence'])} track(s), e.g. {ex2}."),
                "evidence": sc["evidence"][:6], "y_roles": dict(y_roles),
            })
    out = []
    for y, paths in found.items():
        paths.sort(key=lambda p: -p["strength"])
        roles = Counter()
        for p in paths:
            roles.update(p["y_roles"])
        if set(roles) <= {"other"}:
            continue  # only non-music credits (thanks, artwork...): not a professional connection
        # Independent paths = different connecting songs. Ten collaborators all reached through one
        # posse cut are one connection, not ten.
        y_tracks = {e["track_id"] for p in paths for e in p["evidence"]}
        strength = min(95, paths[0]["strength"] + 8 * (min(len(y_tracks), len({p["via_id"] for p in paths})) - 1))
        via_ids = {p["via_id"] for p in paths}
        vias = [s.entities[v]["name"] for v in sorted(
            via_ids, key=lambda v: (s.entities[v].get("type") not in PERSONLIKE, s.entities[v]["name"]))]
        out.append({**s.entities[y], "degree": 2, "strength": strength, "roles": dict(roles),
                    "group": main_group(roles), "paths": paths[:limit_paths], "path_count": len(paths),
                    "relationship": paths[0]["type"],
                    "independent_tracks": len(y_tracks),
                    "why": (f"Reached through {len(vias)} collaborator{'s' if len(vias) != 1 else ''} on "
                            f"{len(y_tracks)} different song{'s' if len(y_tracks) != 1 else ''}: "
                            + ", ".join(vias[:4]) + (" …" if len(vias) > 4 else ""))})
    out.sort(key=people_first)
    return out


def _collaborators(s: Scope, eid: int) -> dict[int, set[int]]:
    """collaborator -> tracks, people only."""
    out: dict[int, set[int]] = defaultdict(set)
    for tid in s.by_entity.get(eid, ()):
        for o in s.credits.get(tid, {}):
            if o != eid and s.entities.get(o, {}).get("type") in PERSONLIKE:
                out[o].add(tid)
    return out


def discoveries(s: Scope, root: int, direct: list[dict], second: list[dict]) -> dict:
    """Similar-network and discovery categories, each item with its reasons."""
    root_roles = entity_roles(s, root)
    root_group = main_group(root_roles)
    rc = _collaborators(s, root)
    pool = {d["id"]: d for d in direct + second if d["type"] in PERSONLIKE}
    names = {e: s.entities[e]["name"] for e in s.entities}
    this_year = date.today().year

    similar = []
    for eid, d in pool.items():
        if d["group"] != root_group or eid == root:
            continue
        cc = _collaborators(s, eid)
        shared = (set(rc) & set(cc)) - {eid, root}
        if len(shared) < 2:
            continue
        sim = len(shared) / math.sqrt(max(1, len(rc)) * max(1, len(cc)))
        top = sorted(shared, key=lambda x: -(len(rc[x]) + len(cc[x])))[:5]
        similar.append({"id": eid, "name": d["name"], "type": d["type"], "group": d["group"],
                        "score": round(100 * sim), "shared": len(shared),
                        "why": (f"Shares {len(shared)} collaborators with {names[root]}: "
                                + ", ".join(names[x] for x in top) + (" …" if len(shared) > 5 else ""))})
    similar.sort(key=lambda x: (-x["score"], -x["shared"]))

    def spread(eid: int) -> set[int]:
        arts = set()
        for tid in s.by_entity.get(eid, ()):
            for o, roles in s.credits.get(tid, {}).items():
                if "main_artist" in roles and o != eid:
                    arts.add(o)
        return arts

    engineers, writers, connectors, emerging = [], [], [], []
    direct_ids = {d["id"] for d in direct}
    for eid, d in pool.items():
        arts = spread(eid)
        if d["group"] == "engineer" and len(arts) >= 2:
            engineers.append({"id": eid, "name": d["name"], "type": d["type"], "group": d["group"], "score": len(arts),
                              "why": f"Engineering credits for {len(arts)} different artists in this network: "
                                     + ", ".join(names[a] for a in list(arts)[:5])})
        if d["group"] == "writer" and len(arts) >= 2:
            writers.append({"id": eid, "name": d["name"], "type": d["type"], "group": d["group"], "score": len(arts),
                            "why": f"Writing credits for {len(arts)} different artists in this network: "
                                   + ", ".join(names[a] for a in list(arts)[:5])})
        if d["degree"] == 2 and d.get("path_count", 0) >= 2 and d.get("independent_tracks", 0) >= 2:
            vias = sorted({p["via_name"] for p in d["paths"]})
            if len(vias) >= 2:
                connectors.append({"id": eid, "name": d["name"], "type": d["type"], "group": d["group"],
                                   "score": len(vias),
                                   "why": f"Connects {len(vias)} of {names[root]}'s collaborators: " + ", ".join(vias[:5])})
        if d["degree"] == 2 and "main_artist" in d["roles"]:
            years = [s.tracks[t]["year"] for t in s.by_entity.get(eid, ()) if s.tracks.get(t, {}).get("year")]
            if years and min(years) >= this_year - 3:
                emerging.append({"id": eid, "name": d["name"], "type": d["type"], "group": d["group"],
                                 "score": d["strength"], "first_year": min(years),
                                 "why": (f"Earliest release found in the researched sources: {min(years)}. "
                                         f"{d['why']}. Early-career signal only; based on the credits fetched, "
                                         f"not a complete discography.")})
    newcollab = [{"id": d["id"], "name": d["name"], "type": d["type"], "group": d["group"], "score": d["strength"],
                  "why": d["paths"][0]["explanation"]}
                 for d in second if d["type"] in PERSONLIKE and d["id"] not in direct_ids][:12]
    for lst in (engineers, writers, connectors, emerging):
        lst.sort(key=lambda x: -x["score"])
    return {
        "similar": similar[:12],
        "emerging_artists": emerging[:12],
        "shared_engineers": engineers[:12],
        "connected_songwriters": writers[:12],
        "cross_network": connectors[:12],
        "new_collaborators": newcollab,
        "basis": f"{names[root]} is analysed as a {GROUP_LABEL.get(root_group, root_group)} "
                 f"(most frequent credited role in the researched tracks).",
    }


def graph(s: Scope, root: int, direct: list[dict], second: list[dict], max_direct: int = 40,
          max_second: int = 40, center: dict | None = None) -> dict:
    nodes, edges = [], []
    r = s.entities.get(root, {"name": "?", "type": "person"})
    nodes.append({"id": root, "label": r["name"], "type": r["type"], "group": main_group(entity_roles(s, root)),
                  "degree": 0, "strength": 100, "location": r.get("location") or r.get("country"),
                  "genres": r.get("genres", [])})
    keep = direct[:max_direct]
    keep_ids = {d["id"] for d in keep}
    for d in keep:
        nodes.append({"id": d["id"], "label": d["name"], "type": d["type"], "group": d["group"], "degree": 1,
                      "strength": d["strength"], "location": s.entities[d["id"]].get("location") or s.entities[d["id"]].get("country"),
                      "genres": s.entities[d["id"]].get("genres", [])})
        edges.append({"source": root, "target": d["id"], "kind": "direct", "strength": d["strength"],
                      "label": d["relationship"], "text": d["summary"]})
    for y in second[:max_second]:
        paths = [p for p in y["paths"] if p["via_id"] in keep_ids]
        if not paths:
            continue
        nodes.append({"id": y["id"], "label": y["name"], "type": y["type"], "group": y["group"], "degree": 2,
                      "strength": y["strength"], "location": s.entities[y["id"]].get("location") or s.entities[y["id"]].get("country"),
                      "genres": s.entities[y["id"]].get("genres", [])})
        for p in paths:
            edges.append({"source": p["via_id"], "target": y["id"], "kind": "indirect", "strength": p["strength"],
                          "label": p["type"], "text": p["explanation"]})
    return {"nodes": nodes, "edges": edges, "center": center}


def add_expansions(s: Scope, g: dict, expanded: list[int], per_node: int = 15) -> list[dict]:
    """Step 2: grow the graph around people the user expanded. Each expanded person's own
    collaborators (from every credit collected for them) are added as new nodes, one ring further
    out, and links to people already in the graph are drawn too, so clusters become visible.
    Returns one relationship row per added person (evidence included) for the side panel."""
    if not g:
        return []
    nodes = {n["id"]: n for n in g["nodes"]}
    pairs = {frozenset((e["source"], e["target"])) for e in g["edges"]}
    rows = []
    for x in expanded:
        if x not in nodes or x not in s.entities:
            continue
        nodes[x]["expanded"] = True
        added = 0
        for d in direct_network(s, x):
            y = d["id"]
            if y == x:
                continue
            key = frozenset((x, y))
            if y not in nodes:
                if added >= per_node or d["type"] not in PERSONLIKE:
                    continue
                ent = s.entities[y]
                n = {"id": y, "label": d["name"], "type": d["type"], "group": d["group"],
                     "degree": min(3, nodes[x]["degree"] + 1), "strength": d["strength"],
                     "location": ent.get("location") or ent.get("country"), "genres": ent.get("genres", []),
                     "expanded_from": x}
                nodes[y] = n
                g["nodes"].append(n)
                added += 1
                rows.append({**d, "degree": n["degree"], "via_id": x, "via_name": s.entities[x]["name"],
                             "evidence": d["evidence"][:6]})
            if key in pairs:
                continue
            pairs.add(key)
            g["edges"].append({"source": x, "target": y, "kind": "expanded", "strength": d["strength"],
                               "label": d["relationship"], "text": f"{d['summary']}. {d['why']}."})
    return rows
