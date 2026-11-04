"""Step 2 analyses that work across research sessions:

- compare(a, b): how two people's professional networks overlap, and how they connect.
- discover_all(): people who keep turning up across all of your research, and who to research next.

Both are derived from stored credits and stored research results, so every row still carries the
tracks (and source links) behind it. Popularity never enters any ranking.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict

from ..db import DB
from . import network
from .network import PERSONLIKE, direct_network, load_scope, main_group, score_pair

SECOND_WEIGHT = 0.6   # a second-degree appearance counts 60 % of a direct one in cross-research rankings


def redirects(db: DB) -> dict[int, int]:
    return {r["old_id"]: r["new_id"] for r in db.q("SELECT old_id, new_id FROM entity_redirects")}


def _fix(rd: dict[int, int], eid):
    seen = set()
    while eid in rd and eid not in seen:
        seen.add(eid)
        eid = rd[eid]
    return eid


def _tracks_of(db: DB, eid: int) -> list[int]:
    return [r[0] for r in db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (eid,))]


def _latest_session(db: DB, eid: int):
    return db.one("SELECT id, title, query, result FROM research_sessions WHERE root_entity_id=? AND status='done' "
                  "AND result IS NOT NULL ORDER BY (target_kind='artist') DESC, id DESC LIMIT 1", (eid,))


def person_network(db: DB, eid: int, rd: dict[int, int] | None = None) -> dict:
    """Direct network from every credit in the database; second degree from the person's latest
    finished research (if any)."""
    rd = rd if rd is not None else redirects(db)
    s = load_scope(db, _tracks_of(db, eid))
    ent = db.one("SELECT id, name, entity_type FROM entities WHERE id=?", (eid,))
    direct = direct_network(s, eid) if eid in s.entities else []
    sess = _latest_session(db, eid)
    second = []
    if sess:
        direct_ids = {d["id"] for d in direct}
        for x in json.loads(sess["result"]).get("second", []):
            x = {**x, "id": _fix(rd, x["id"])}
            if x["id"] not in direct_ids and x["id"] != eid:
                second.append(x)
    return {"id": eid, "name": ent["name"] if ent else "?", "type": ent["entity_type"] if ent else "unknown",
            "group": main_group(network.entity_roles(s, eid)) if eid in s.entities else "other",
            "tracks": len(s.by_entity.get(eid, ())), "session_id": sess["id"] if sess else None,
            "direct": direct, "second": second, "scope": s}


def _side(d: dict | None) -> dict | None:
    if not d:
        return None
    out = {k: d.get(k) for k in ("degree", "strength", "tier", "relationship", "shared_tracks", "summary", "why", "roles")}
    if d.get("evidence"):
        out["evidence"] = d["evidence"][:4]
    if d.get("paths"):
        out["via"] = d["paths"][0]["via_name"]
        out["why"] = d["paths"][0]["explanation"]
    return out


def compare(db: DB, a: int, b: int, limit: int = 25) -> dict:
    rd = redirects(db)
    a, b = _fix(rd, a), _fix(rd, b)
    A, B = person_network(db, a, rd), person_network(db, b, rd)
    names = {}
    for P in (A, B):
        for d in P["direct"] + P["second"]:
            names[d["id"]] = d
    a1 = {d["id"]: d for d in A["direct"]}
    b1 = {d["id"]: d for d in B["direct"]}
    a2 = {d["id"]: d for d in A["second"]}
    b2 = {d["id"]: d for d in B["second"]}
    a_all, b_all = {**a2, **a1}, {**b2, **b1}

    # direct link between the two
    link = a1.get(b)
    direct_link = None
    if link:
        direct_link = {"strength": link["strength"], "tier": link["tier"], "relationship": link["relationship"],
                       "summary": link["summary"], "why": link["why"], "evidence": link["evidence"][:8]}

    def row(eid: int) -> dict:
        d = names[eid]
        return {"id": eid, "name": d["name"], "type": d["type"], "group": d["group"],
                "roles": dict(Counter(a_all.get(eid, {}).get("roles", {})) + Counter(b_all.get(eid, {}).get("roles", {})))}

    shared_direct, shared_ext = [], []
    for eid in (set(a_all) & set(b_all)) - {a, b}:
        da, db_ = a_all[eid], b_all[eid]
        r = {**row(eid), "a": _side(da), "b": _side(db_), "score": min(da["strength"], db_["strength"])}
        if eid in a1 and eid in b1:
            r["why"] = (f"Credited with {A['name']} on {da['shared_tracks']} track(s) and with {B['name']} on "
                        f"{db_['shared_tracks']} track(s).")
            shared_direct.append(r)
        else:
            sides = []
            for P, d in ((A, da), (B, db_)):
                sides.append(f"{'direct collaborator of' if d['degree'] == 1 else 'second degree from'} {P['name']}"
                             + (f" (via {d['paths'][0]['via_name']})" if d.get("paths") else ""))
            r["why"] = "In both networks: " + "; ".join(sides) + "."
            shared_ext.append(r)
    key = lambda r: (r["type"] not in PERSONLIKE, -r["score"], r["name"])
    shared_direct.sort(key=key)
    shared_ext.sort(key=key)
    shared_ids = {r["id"] for r in shared_direct}

    # bridges: A — X — Y — B, where X and Y are direct collaborators who worked together
    bridges = []
    ax = [d["id"] for d in A["direct"] if d["type"] in PERSONLIKE and d["id"] not in shared_ids and d["id"] != b][:80]
    by = [d["id"] for d in B["direct"] if d["type"] in PERSONLIKE and d["id"] not in shared_ids and d["id"] != a][:80]
    if ax and by:
        mx, my = ",".join("?" * len(ax)), ",".join("?" * len(by))
        pairs = db.q(f"SELECT c1.entity_id x, c2.entity_id y, COUNT(DISTINCT c1.track_id) n FROM credits c1 "
                     f"JOIN credits c2 ON c1.track_id=c2.track_id WHERE c1.entity_id IN ({mx}) AND c2.entity_id IN ({my}) "
                     f"AND c1.entity_id<>c2.entity_id GROUP BY c1.entity_id, c2.entity_id", ax + by)
        cand = sorted(pairs, key=lambda p: -min(a1[p["x"]]["strength"], b1[p["y"]]["strength"]))[:60]
        tids = set()
        for p in cand:
            tids |= set(_tracks_of(db, p["x"])) & set(_tracks_of(db, p["y"]))
        s = load_scope(db, list(tids))
        for p in cand:
            x, y = p["x"], p["y"]
            if x in (a, b) or y in (a, b):
                continue
            mid = score_pair(s, x, y)
            if not mid:
                continue
            sc = min(a1[x]["strength"], mid["strength"], b1[y]["strength"])
            ex = ", ".join(f"“{e['title']}”" for e in mid["evidence"][:2])
            bridges.append({"x": {"id": x, "name": a1[x]["name"], "group": a1[x]["group"], "strength": a1[x]["strength"]},
                            "y": {"id": y, "name": b1[y]["name"], "group": b1[y]["group"], "strength": b1[y]["strength"]},
                            "middle": {"strength": mid["strength"], "shared_tracks": mid["shared_tracks"],
                                       "evidence": mid["evidence"][:4]},
                            "score": sc,
                            "why": (f"{A['name']} → {a1[x]['name']} ({a1[x]['shared_tracks']} track(s)) → "
                                    f"{b1[y]['name']} (together on {mid['shared_tracks']} track(s), e.g. {ex}) → "
                                    f"{B['name']} ({b1[y]['shared_tracks']} track(s))")})
        bridges.sort(key=lambda r: -r["score"])

    def only(P: dict, other_all: dict) -> list[dict]:
        out = []
        for d in P["direct"]:
            if d["id"] in other_all or d["id"] in (a, b) or d["type"] not in PERSONLIKE:
                continue
            out.append({"id": d["id"], "name": d["name"], "type": d["type"], "group": d["group"], "roles": d["roles"],
                        "score": d["strength"], "why": d["summary"]})
        return out

    only_a, only_b = only(A, b_all), only(B, a_all)

    # overlap of direct (people) networks
    pa = {i for i, d in a1.items() if d["type"] in PERSONLIKE} - {b}
    pb = {i for i, d in b1.items() if d["type"] in PERSONLIKE} - {a}
    union = pa | pb
    by_group = Counter(names[i]["group"] for i in pa & pb)

    # graph: two centres, shared people in the middle, bridges, and each side's strongest unique people
    nodes, edges = [], []
    seen = set()

    def node(eid, label, group, typ, side, strength=0):
        if eid in seen:
            return
        seen.add(eid)
        nodes.append({"id": eid, "label": label, "group": group, "type": typ, "side": side, "strength": strength})

    node(a, A["name"], A["group"], A["type"], "a", 100)
    node(b, B["name"], B["group"], B["type"], "b", 100)
    if direct_link:
        edges.append({"source": a, "target": b, "kind": "direct", "strength": direct_link["strength"], "text": direct_link["summary"]})
    for r in shared_direct[:30]:
        node(r["id"], r["name"], r["group"], r["type"], "shared", r["score"])
        edges.append({"source": a, "target": r["id"], "kind": "direct", "strength": r["a"]["strength"], "text": r["a"]["summary"]})
        edges.append({"source": b, "target": r["id"], "kind": "direct", "strength": r["b"]["strength"], "text": r["b"]["summary"]})
    for br in bridges[:12]:
        x, y = br["x"], br["y"]
        node(x["id"], x["name"], x["group"], "person", "a", x["strength"])
        node(y["id"], y["name"], y["group"], "person", "b", y["strength"])
        edges.append({"source": a, "target": x["id"], "kind": "direct", "strength": x["strength"], "text": a1[x["id"]]["summary"]})
        edges.append({"source": b, "target": y["id"], "kind": "direct", "strength": y["strength"], "text": b1[y["id"]]["summary"]})
        edges.append({"source": x["id"], "target": y["id"], "kind": "bridge", "strength": br["middle"]["strength"], "text": br["why"]})
    for side, lst, centre in (("a", only_a, a), ("b", only_b, b)):
        for r in lst[:10]:
            node(r["id"], r["name"], r["group"], r["type"], side, r["score"])
            edges.append({"source": centre, "target": r["id"], "kind": "direct", "strength": r["score"], "text": r["why"]})
    uniq = {}
    for e in edges:
        uniq.setdefault(frozenset((e["source"], e["target"])), e)

    def head(P: dict) -> dict:
        return {"id": P["id"], "name": P["name"], "type": P["type"], "group": P["group"], "tracks": P["tracks"],
                "direct": len(P["direct"]), "second": len(P["second"]), "session_id": P["session_id"]}

    return {
        "a": head(A), "b": head(B), "direct_link": direct_link,
        "overlap": {"shared_direct": len(pa & pb), "union": len(union),
                    "jaccard": round(100 * len(pa & pb) / len(union)) if union else 0,
                    "shared_extended": len(shared_ext), "by_group": dict(by_group)},
        "shared_direct": shared_direct[:limit * 2], "shared_extended": shared_ext[:limit * 2],
        "bridges": bridges[:limit], "only_a": only_a[:limit], "only_b": only_b[:limit],
        "graph": {"nodes": nodes, "edges": list(uniq.values())},
        "notes": [f"{P['name']} has not been researched as a search of their own yet: second-degree overlap is "
                  f"incomplete. Research them for a fuller comparison." for P in (A, B) if not P["session_id"]],
    }


def discover_all(db: DB, saved_only: bool = False) -> dict:
    """Aggregate every finished research: who appears in several networks, who is worth researching
    next, by role, with location/genre where profiles have it."""
    rd = redirects(db)
    sql = ("SELECT id, title, query, saved, root_entity_id, result FROM research_sessions WHERE status='done' "
           "AND result IS NOT NULL" + (" AND saved=1" if saved_only else "") + " ORDER BY id DESC")
    sessions = db.q(sql)
    # one network per root person: the latest research of each root wins
    nets: dict[int, dict] = {}
    for srow in sessions:
        res = json.loads(srow["result"])
        root = _fix(rd, srow["root_entity_id"]) if srow["root_entity_id"] else None
        if not root or root in nets:
            continue
        nets[root] = {"session_id": srow["id"], "name": res.get("root_name") or srow["query"],
                      "title": srow["title"] or res.get("root_name") or srow["query"], "saved": bool(srow["saved"]),
                      "result": res}
    roots = set(nets)
    people: dict[int, dict] = {}
    for root, net in nets.items():
        res = net["result"]
        seen_here = set()
        for d in res.get("direct", []) + res.get("second", []) + res.get("expanded_people", []):
            eid = _fix(rd, d["id"])
            if eid == root or eid in seen_here or d.get("type") not in PERSONLIKE:
                continue
            seen_here.add(eid)
            p = people.setdefault(eid, {"id": eid, "name": d["name"], "type": d["type"], "groups": Counter(),
                                        "roles": Counter(), "appearances": [], "score": 0.0})
            deg = d.get("degree", 1)
            p["groups"][d.get("group", "other")] += 1
            p["roles"].update(d.get("roles") or {})
            via = d["paths"][0]["via_name"] if d.get("paths") else d.get("via_name")
            p["appearances"].append({"root_id": root, "root": net["name"], "session_id": net["session_id"],
                                     "degree": deg, "strength": d.get("strength", 0), "via": via,
                                     "relationship": d.get("relationship")})
            p["score"] += d.get("strength", 0) * (1 if deg == 1 else SECOND_WEIGHT)
        for cat in ("emerging_artists", "cross_network"):
            for x in (res.get("discover") or {}).get(cat, []):
                eid = _fix(rd, x["id"])
                if eid in people:
                    people[eid].setdefault("flags", set()).add(cat)
    ids = list(people)
    facts: dict[int, dict] = defaultdict(dict)
    crm: dict[int, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        m = ",".join("?" * len(chunk))
        for r in db.q(f"SELECT entity_id, field, value FROM facts WHERE entity_id IN ({m}) AND field IN "
                      f"('area','begin_area','formed_in','born_in','residence','country','genre') ORDER BY id", chunk):
            f = facts[r["entity_id"]]
            if r["field"] == "genre":
                f.setdefault("genres", [])
                if r["value"] not in f["genres"]:
                    f["genres"].append(r["value"])
            elif r["field"] == "country":
                f.setdefault("country", r["value"])
            else:
                f.setdefault("location", r["value"])
        for r in db.q(f"SELECT entity_id, status, tags FROM saved_people WHERE entity_id IN ({m})", chunk):
            crm[r["entity_id"]] = {"status": r["status"], "tags": json.loads(r["tags"])}
        for r in db.q(f"SELECT id, name FROM entities WHERE id IN ({m})", chunk):
            people[r["id"]]["name"] = r["name"]
    out = []
    for eid, p in people.items():
        apps = sorted(p["appearances"], key=lambda x: (x["degree"], -x["strength"]))
        n = len({x["root_id"] for x in apps})
        parts = [f"{x['root']} ({'direct' if x['degree'] == 1 else 'via ' + (x['via'] or '?') if x['degree'] == 2 else 'expanded from ' + (x['via'] or '?')}, {x['strength']})"
                 for x in apps[:4]]
        out.append({"id": eid, "name": p["name"], "type": p["type"], "group": p["groups"].most_common(1)[0][0],
                    "roles": dict(p["roles"]), "networks": n, "score": round(p["score"]),
                    "direct_in": sum(1 for x in apps if x["degree"] == 1),
                    "appearances": apps[:8], "researched": eid in roots,
                    "flags": sorted(p.get("flags", ())), "crm": crm.get(eid),
                    "location": facts[eid].get("location"), "country": facts[eid].get("country"),
                    "genres": facts[eid].get("genres", [])[:6],
                    "why": (f"In {n} of your researched networks: " if n > 1 else "In the network of ")
                           + "; ".join(parts) + (" …" if len(apps) > 4 else "")})
    out.sort(key=lambda x: (-x["networks"], -x["score"], x["name"]))
    return {"networks": [{"root_id": r, "name": v["name"], "title": v["title"], "session_id": v["session_id"],
                          "saved": v["saved"]} for r, v in nets.items()],
            "people": out[:1500],
            "basis": ("Score = sum of relationship strengths across networks; second-degree appearances count "
                      f"{int(SECOND_WEIGHT * 100)} %. Follower counts are never used.")}
