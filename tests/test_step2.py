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
    # Artist D appears in B's research only through Artist A, who has a research of their own
    A_via = {"id": ids["d"], "name": "Artist D", "type": "person", "group": "artist", "strength": 50, "degree": 2,
             "roles": {"main_artist": 1}, "paths": [{"via_id": ids["a"], "via_name": "Artist A"}]}
    _session(db, ids["b"], [{**P, "strength": 40}], [A_via])
    out = insights.discover_all(db)
    top = out["people"][0]
    assert top["name"] == "Producer P" and top["networks"] == 2 and top["score"] == 100
    y = next(p for p in out["people"] if p["name"] == "Writer Y")
    assert y["score"] == round(30 * insights.SECOND_WEIGHT) and "via Producer X" in y["why"]
    assert not any(p["name"] == "Artist D" for p in out["people"])  # only reachable through a researched root
    assert {n["root_id"] for n in out["networks"]} == {ids["a"], ids["b"]}
    assert len(insights.discover_all(db, saved_only=True)["networks"]) == 1
    # an entity merged after the research was stored is followed to the surviving id
    st.merge(ids["e"], ids["p"])
    out = insights.discover_all(db)
    assert out["people"][0]["id"] == ids["e"] and out["people"][0]["networks"] == 2


def test_compute_includes_graph_and_expansions():
    """End to end through Researcher.compute: the graph survives next to contacts and carries the
    expansion ring."""
    from app.engine.research import Researcher
    db, st, ids = _world()
    r = Researcher(db, http=None)
    sid = db.x("INSERT INTO research_sessions(query,target_kind,status,root_entity_id,created_at) "
               "VALUES('Artist A','artist','done',?,?)", (ids["a"], now()))
    for row in db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (ids["a"],)):
        r._add_session_track(sid, row[0], None, 1)
    for row in db.q("SELECT DISTINCT track_id FROM credits WHERE entity_id=?", (ids["x"],)):
        r._add_session_track(sid, row[0], ids["x"], 3)
    db.x("INSERT INTO session_expansions(session_id,entity_id,status,requested_at) VALUES(?,?,'done',?)",
         (sid, ids["x"], now()))
    res = r.compute(sid)
    assert {"nodes", "edges"} <= set(res["graph"])
    assert res["expanded"] == [ids["x"]]
    nodes = {n["id"]: n for n in res["graph"]["nodes"]}
    assert nodes[ids["x"]].get("expanded") is True
    # Expanding a direct collaborator deepens second degree: X's other artist now shows up there
    assert nodes[ids["d"]]["degree"] == 2
    assert any(p["name"] == "Artist D" for p in res["second"])


def test_cli_summary_is_plain_and_small():
    from app.cli import summarize
    from app.engine.research import Researcher
    db, st, ids = _world()
    r = Researcher(db, http=None)
    sid = db.x("INSERT INTO research_sessions(query,target_kind,status,root_entity_id,created_at) "
               "VALUES('Artist A','artist','done',?,?)", (ids["a"], now()))
    for row in db.q("SELECT id FROM tracks"):
        r._add_session_track(sid, row[0], None, 1)
    db.x("UPDATE research_sessions SET result=? WHERE id=?", (json.dumps(r.compute(sid)), sid))
    out = summarize(db, sid)
    assert out["name"] == "Artist A"
    prod = next(g for g in out["people"] if g["group"] == "producer")
    p = prod["people"][0]
    assert p["name"] == "Producer P" and p["songs_together"] == 2 and p["closeness"]
    assert p["songs"][0]["source"].startswith("https://")
    assert len(json.dumps(out)) < 240_000
    assert "socials" in p and out["deep"] is True  # sessions default to depth 2


def test_cli_query_prefixes():
    from app.cli import parse_query
    assert parse_query("Hit-Boy") == ("Hit-Boy", "artist", False)
    assert parse_query("deep: Hit-Boy") == ("Hit-Boy", "artist", True)
    assert parse_query("deep: song: Luther - Kendrick Lamar") == ("Luther - Kendrick Lamar", "song", True)
    assert parse_query("Album:GNX") == ("GNX", "album", False)


def test_socials_dedupe_and_instagram_handle():
    from app.cli import socials
    from app.providers.base import ContactRec, ProfileData
    db, st, ids = _world()
    p = ids["p"]
    st.apply_profile(p, ProfileData(contacts=[
        ContactRec("instagram", "https://www.instagram.com/producer.p", "publicly_listed", "genius", "https://genius.com/a/1", "Genius"),
        ContactRec("instagram", "https://instagram.com/producer.p/", "publicly_listed", "discogs", "https://discogs.com/a/1", "Discogs"),
        ContactRec("website", "https://producerp.com", "publicly_listed", "musicbrainz", "https://musicbrainz.org/a/1", "MB"),
        ContactRec("x", "https://twitter.com/producerp", "publicly_listed", "musicbrainz", "https://musicbrainz.org/a/1", "MB"),
    ]))
    out = socials(db, p)
    assert [s["kind"] for s in out] == ["instagram", "x", "website"]
    assert out[0]["handle"] == "producer.p" and sorted(out[0]["found_on"]) == ["discogs", "genius"]
