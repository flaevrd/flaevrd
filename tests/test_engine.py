"""Offline tests of the research engine's pure logic. Fixtures are tiny hand-written payloads in
each provider's documented response shape, using placeholder names (Artist A, Producer B...);
they exist only to test parsing/resolution/scoring and are never used by the app."""
import asyncio

from app.db import DB
from app.engine import network
from app.engine.normalize import mb_role, norm_name, norm_title, role_from_text
from app.engine.store import Store
from app.providers.base import PersonRef, ProfileData, TrackRec
from app.providers.discogs import _split_roles, parse_positions
from app.providers.musicbrainz import MusicBrainz, split_query
from app.providers.website import WebsiteContacts


def test_normalization():
    assert norm_name("The Neptunes") == norm_name("Neptunes")
    assert norm_name("Beyoncé") == "beyonce"
    assert norm_name("A$AP Rocky") == "asap rocky"
    assert norm_name("John Smith (2)") == "john smith"
    assert norm_title("Song (feat. Artist D)") == "song"
    assert norm_title("Song - 2011 Remastered") == "song"
    assert norm_title("Song (Remix)") == "song remix"


def test_roles():
    assert role_from_text("Mixing Engineer") == "mixing_engineer"
    assert role_from_text("Mastered By") == "mastering_engineer"
    assert role_from_text("Recorded At") == "studio"
    assert role_from_text("Co-Producer") == "co_producer"
    assert role_from_text("Executive Producer") == "executive_producer"
    assert role_from_text("Video Producer") == "other"
    assert role_from_text("A&R") == "a_and_r"
    assert role_from_text("Written-By") == "writer"
    assert role_from_text("Phonographic Copyright ℗") == "label"
    assert mb_role("producer", ["executive"]) == "executive_producer"
    assert mb_role("mix", []) == "mixing_engineer"
    assert mb_role("engineer", ["assistant"]) == "assistant_engineer"
    assert mb_role("photography", []) is None


def test_discogs_helpers():
    pos = ["A1", "A2", "A3", "B1", "B2"]
    assert parse_positions("A1 to A3, B2", pos) == ["A1", "A2", "A3", "B2"]
    assert parse_positions("", pos) == pos
    assert _split_roles("Producer [Additional], Mixed By") == ["Producer [Additional]", "Mixed By"]


def test_split_query():
    assert split_query("Song - Artist") == ("Song", "Artist")
    assert split_query("Song by Artist") == ("Song", "Artist")
    assert split_query("Just A Name") == ("Just A Name", None)


def _mb_recording():
    return {
        "id": "rec-1", "title": "Song One", "first-release-date": "2024-03-01",
        "artist-credit": [{"name": "Artist A", "joinphrase": " feat. ", "artist": {"id": "mb-a", "name": "Artist A", "type": "Person"}},
                          {"name": "Artist D", "joinphrase": "", "artist": {"id": "mb-d", "name": "Artist D"}}],
        "relations": [
            {"type": "producer", "target-type": "artist", "attributes": [], "artist": {"id": "mb-b", "name": "Producer B"}},
            {"type": "mix", "target-type": "artist", "attributes": [], "artist": {"id": "mb-c", "name": "Engineer C"}},
            {"type": "recorded at", "target-type": "place", "place": {"id": "pl-1", "name": "Studio S"}},
            {"type": "performance", "target-type": "work", "work": {"id": "w1", "relations": [
                {"type": "composer", "target-type": "artist", "artist": {"id": "mb-w", "name": "Writer W"}}]}},
        ],
    }


def test_mb_track_parsing():
    mb = MusicBrainz(http=None)
    tr = mb._track_from_recording(_mb_recording(), _mb_recording()["artist-credit"], None, None)
    roles = {(c.person.name, c.role) for c in tr.credits}
    assert ("Artist A", "main_artist") in roles
    assert ("Artist D", "featured_artist") in roles
    assert ("Producer B", "producer") in roles
    assert ("Engineer C", "mixing_engineer") in roles
    assert ("Studio S", "studio") in roles
    assert ("Writer W", "composer") in roles


def _track(provider, tid, title, artist_id, credits, year="2024"):
    tr = TrackRec(provider, tid, title, f"https://example.test/{provider}/{tid}", "Artist", f"{year}-01-01")
    for pid, name, role in [(artist_id[0], artist_id[1], "main_artist")] + credits:
        tr.add(PersonRef(provider, pid, name), role)
    return tr


def test_entity_resolution():
    db = DB(":memory:")
    st = Store(db)
    t1 = st.save_track(_track("musicbrainz", "r1", "Song One", ("mb-a", "Artist A"), [("mb-b", "Producer B", "producer")]))
    # Same song on Genius: same names on the same track -> merged into the same entities
    t2 = st.save_track(_track("genius", "g1", "Song One (feat. X)", ("g-a", "Artist A"), [("g-b", "Producer B", "producer"),
                                                                                         ("g-c", "Engineer C", "mixing_engineer")]))
    assert t1 == t2
    assert st.by_ident("genius", "g-b") == st.by_ident("musicbrainz", "mb-b")
    # A different MusicBrainz artist with the same name is NOT merged
    st.save_track(_track("musicbrainz", "r9", "Other Song", ("mb-z", "Artist Z"), [("mb-b2", "Producer B", "producer")]))
    assert st.by_ident("musicbrainz", "mb-b2") != st.by_ident("musicbrainz", "mb-b")
    # A Discogs "Engineer C" on an unrelated track with no shared collaborator: kept apart, flagged
    st.save_track(_track("discogs", "d9:A1", "Unrelated", ("d-y", "Artist Y"), [("d-c", "Engineer C", "mixing_engineer")]))
    assert st.by_ident("discogs", "d-c") != st.by_ident("genius", "g-c")
    assert db.one("SELECT COUNT(*) n FROM identity_questions")["n"] >= 1
    # Authoritative cross-link merges two entities
    st.save_track(_track("discogs", "d1:A1", "Third Song", ("d-q", "Artist Q"), [("d-b", "Prod. B", "producer")]))
    keep = st.by_ident("musicbrainz", "mb-b")
    st.apply_profile(keep, ProfileData(links={"discogs": "d-b"}))
    assert st.by_ident("discogs", "d-b") == keep


def test_network_scoring_and_second_degree():
    db = DB(":memory:")
    st = Store(db)
    tids = []
    # Artist A: 3 songs produced by B, 2 mixed by C
    for i in range(3):
        extra = [("c", "Engineer C", "mixing_engineer")] if i < 2 else []
        tids.append(st.save_track(_track("musicbrainz", f"a{i}", f"A song {i}", ("a", "Artist A"), [("b", "Producer B", "producer")] + extra)))
    # Producer B also produced Artist D, who worked with Producer E
    tids.append(st.save_track(_track("musicbrainz", "d1", "D song", ("d", "Artist D"), [("b", "Producer B", "producer")])))
    tids.append(st.save_track(_track("musicbrainz", "d2", "D song 2", ("d", "Artist D"), [("e", "Producer E", "producer")])))
    root = st.by_ident("musicbrainz", "a")
    s = network.load_scope(db, tids)
    direct = network.direct_network(s, root)
    names = [d["name"] for d in direct]
    assert names[0] == "Producer B" and "Engineer C" in names
    b = direct[0]
    assert b["shared_tracks"] == 3 and b["strength"] > direct[1]["strength"]
    assert "3 shared tracks" in b["why"]
    second = network.second_degree(s, root, direct)
    d = next(x for x in second if x["name"] == "Artist D")
    assert d["paths"][0]["via_name"] == "Producer B"
    assert d["relationship"] == "Shared producer"
    assert "D song" in d["paths"][0]["explanation"]
    g = network.graph(s, root, direct, second)
    assert any(e["kind"] == "indirect" for e in g["edges"])


class _FakeHttp:
    def __init__(self, pages):
        self.pages = pages

    async def robots_allows(self, url):
        return "/private" not in url

    async def get(self, url, **kw):
        from app.http import HttpError, Response
        if url not in self.pages:
            raise HttpError(404, url)
        return Response(200, self.pages[url], url, False)


def test_website_contacts():
    home = ('<html><body><a href="/contact">Contact</a><a href="/private">Private</a>'
            '<a href="https://www.instagram.com/artist.a">IG</a></body></html>')
    contact = ('<html><body><p>Management: <a href="mailto:mgmt@artist-a.test">Email management</a></p>'
               '<p>Booking enquiries: booking [at] agency-x [dot] test</p><a href="tel:+44 20 1234 5678">Call</a>'
               '<p>logo@2x.png</p></body></html>')
    w = WebsiteContacts(_FakeHttp({"https://artist-a.test": home, "https://artist-a.test/contact": contact}))
    found, notes = asyncio.run(w.discover("https://artist-a.test"))
    vals = {(c.contact_type, c.value, c.purpose) for c in found}
    assert ("email", "mgmt@artist-a.test", "management") in vals
    assert ("email", "booking@agency-x.test", "booking") in vals
    assert ("phone", "+442012345678", None) in {(c.contact_type, c.value, c.purpose) for c in found} or \
        any(c.contact_type == "phone" for c in found)
    assert any(c.contact_type == "instagram" for c in found)
    assert all(c.status == "verified" for c in found)
    assert not any("2x.png" in c.value for c in found)


def test_contact_dedup_across_sources():
    from app.engine.profile import group_contacts
    from app.providers.links import contact_key
    assert contact_key("x", "https://twitter.com/artista") == contact_key("x", "https://x.com/ArtistA/")
    assert contact_key("youtube", "https://www.youtube.com/channel/UCx/videos") == contact_key("youtube", "https://youtube.com/channel/UCx")
    rows = [
        {"contact_type": "x", "value": "https://twitter.com/artista", "status": "publicly_listed",
         "provider": "musicbrainz", "url": "https://mb/a", "last_checked_at": "2026-01-01"},
        {"contact_type": "x", "value": "https://x.com/artista", "status": "verified",
         "provider": "website", "url": "https://artista.example", "last_checked_at": "2026-01-02"},
    ]
    out = group_contacts(rows)
    assert len(out) == 1
    assert out[0]["status"] == "verified" and out[0]["value"] == "https://x.com/artista"
    assert [s["provider"] for s in out[0]["sources"]] == ["website", "musicbrainz"]


def test_original_recording_ranks_first():
    from app.providers.musicbrainz import recording_quality, release_rank
    official_album = {"status": "Official", "date": "2017-04-14", "release-group": {"primary-type": "Album"}}
    bootleg_remix = {"status": "Bootleg", "date": "2023", "release-group": {"primary-type": "Album", "secondary-types": ["Remix"]}}
    live = {"disambiguation": "live, 2024: Venue X", "releases": [bootleg_remix]}
    original = {"disambiguation": "", "releases": [official_album, official_album]}
    assert recording_quality(original) > recording_quality(live)
    comp = {"status": "Official", "date": "2016", "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]}}
    assert sorted([comp, bootleg_remix, official_album], key=release_rank)[0] is official_album


def test_genius_roles_and_nicknames():
    from app.engine.normalize import is_video_credit
    assert norm_name("Mauricio “Veto” Iragorri") == norm_name("Mauricio Iragorri")
    assert is_video_credit("Video Director") and not is_video_credit("Vocals")
    assert role_from_text("Ad-Libs") == "vocals"
    assert role_from_text("Viola") == "instrument"


def test_alias_on_same_track_merges(tmp_path):
    db = DB(tmp_path / "t.db")
    st = Store(db)
    st.save_track(_track("musicbrainz", "r1", "Song One", ("mb-a", "Artist A"), [("mb-c", "Engineer C", "mixing_engineer")]))
    st.save_track(_track("genius", "g1", "Song One", ("g-a", "Artist A"), [("g-c", "MixedByC", "mixing_engineer")]))
    eng = st.by_ident("musicbrainz", "mb-c")
    assert st.by_ident("genius", "g-c") != eng
    p = ProfileData()
    p.names = ["MixedByC"]
    st.apply_profile(eng, p)
    assert st.by_ident("genius", "g-c") == eng
