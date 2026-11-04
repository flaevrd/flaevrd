"""Offline tests for Step 2: graph expansion, network comparison and cross-research discovery.
Placeholder names only; fixtures exist to test the logic and are never used by the app."""
import json

from app.db import DB, now
from app.engine import insights, network
from app.engine.store import Store
from tests.test_engine import _track


def _world():
    """Artist A and Artist B share Producer P and Engineer E. A works with X, B with Y, and X and Y
    produced a song together (a bridge). Artist D works only with X."""
    db = DB(":memory:")
    st = Store(db)
    T = lambda *a, **k: st.save_track(_track("musicbrainz", *a, **k))
    T("a1", "A one", ("a", "Artist A"), [("p", "Producer P", "producer"), ("e", "Engineer E", "mixing_engineer")])
    T("a2", "A two", ("a", "Artist A"), [("p", "Producer P", "producer"), ("x", "Producer X", "producer")])
    T("b1", "B one", ("b", "Artist B"), [("p", "Producer P", "producer"), ("e", "Engineer E", "mixing_engineer")])
    T("b2", "B two", ("b", "Artist B"), [("y", "Writer Y", "writer")])
    T("xy", "XY song", ("d", "Artist D"), [("x", "Producer X", "producer"), ("y", "Writer Y", "writer")])
    T("d2", "D two", ("d", "Artist D"), [("x", "Producer X", "producer"), ("z", "Engineer Z", "mixing_engineer")])
    ids = {k: st.by_ident("musicbrainz", k) for k in "abpexydz"}
    return db, st, ids


def test_expand_adds_ring_and_cross_links():
    db, st, ids = _world()
    tids = [r[0] for r in db.q("SELECT id FROM tracks")]
    a_tids = [r[0] for r in db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (ids["a"],))]
    s1 = network.load_scope(db, a_tids)
    direct = network.direct_network(s1, ids["a"])
    s_all = network.load_scope(db, tids)
    g = network.graph(s_all, ids["a"], direct, [])
    before = {n["id"] for n in g["nodes"]}
    assert ids["d"] not in before
    rows = network.add_expansions(s_all, g, [ids["x"]])
    after = {n["id"]: n for n in g["nodes"]}
    # Producer X's own collaborators join one ring further out, with evidence
    assert ids["d"] in after and after[ids["d"]]["degree"] == 2 and after[ids["d"]]["expanded_from"] == ids["x"]
    assert after[ids["x"]]["expanded"] is True
    d_row = next(r for r in rows if r["id"] == ids["d"])
    assert d_row["shared_tracks"] == 2 and d_row["via_name"] == "Producer X"
    assert all(e["kind"] == "expanded" for e in g["edges"] if e["source"] == ids["x"])
    # no duplicate edge for a pair that was already linked (A–X)
    pairs = [frozenset((e["source"], e["target"])) for e in g["edges"]]
    assert len(pairs) == len(set(pairs))


def test_compare_shared_bridges_and_unique():
    db, st, ids = _world()
    c = insights.compare(db, ids["a"], ids["b"])
    shared = {r["name"] for r in c["shared_direct"]}
    assert shared == {"Producer P", "Engineer E"}
    p = next(r for r in c["shared_direct"] if r["name"] == "Producer P")
    assert p["a"]["shared_tracks"] == 2 and p["b"]["shared_tracks"] == 1
    assert c["direct_link"] is None
    br = c["bridges"][0]
    assert (br["x"]["name"], br["y"]["name"]) == ("Producer X", "Writer Y")
    assert br["middle"]["evidence"][0]["title"] == "XY song"
    assert [r["name"] for r in c["only_a"]] == ["Producer X"]
    assert [r["name"] for r in c["only_b"]] == ["Writer Y"]
    assert c["overlap"]["shared_direct"] == 2 and c["overlap"]["union"] == 4 and c["overlap"]["jaccard"] == 50
    assert any(e["kind"] == "bridge" for e in c["graph"]["edges"])
    assert len(c["notes"]) == 2  # neither was researched as its own search


def _session(db, root, direct, second=(), saved=0):
    res = {"root_name": "?", "direct": direct, "second": list(second), "discover": {}}
    return db.x("INSERT INTO research_sessions(query,target_kind,status,root_entity_id,result,saved,created_at) "
                "VALUES('q','artist','done',?,?,?,?)", (root, json.dumps(res), saved, now()))


def test_discover_all_aggregates_across_research_and_follows_merges():
    db, st, ids = _world()
    P = {"id": ids["p"], "name": "Producer P", "type": "person", "group": "producer", "strength": 60, "degree": 1,
         "roles": {"producer": 2}}
    X = {"id": ids["x"], "name": "Producer X", "type": "person", "group": "producer", "strength": 30, "degree": 1,
         "roles": {"producer": 1}}
    Y2 = {"id": ids["y"], "name": "Writer Y", "type": "person", "group": "writer", "strength": 30, "degree": 2,
          "roles": {"writer": 1}, "paths": [{"via_name": "Producer X"}]}
    _session(db, ids["a"], [P, X], [Y2], saved=1)
    _session(db, ids["b"], [{**P, "strength": 40}])
    out = insights.discover_all(db)
    top = out["people"][0]
    assert top["name"] == "Producer P" and top["networks"] == 2 and top["score"] == 100
    y = next(p for p in out["people"] if p["name"] == "Writer Y")
    assert y["score"] == round(30 * insights.SECOND_WEIGHT) and "via Producer X" in y["why"]
    assert {n["root_id"] for n in out["networks"]} == {ids["a"], ids["b"]}
    assert len(insights.discover_all(db, saved_only=True)["networks"]) == 1
    # an entity merged after the research was stored is followed to the surviving id
    st.merge(ids["e"], ids["p"])
    out = insights.discover_all(db)
    assert out["people"][0]["id"] == ids["e"] and out["people"][0]["networks"] == 2
