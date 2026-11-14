"""HTTP API + static UI. Run: `uvicorn app.main:app --reload` (see README)."""
from __future__ import annotations

import csv
import io
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import settings
from .db import DB, now
from .engine import insights
from .engine.profile import build_profile
from .engine.research import Researcher
from .http import Http

STATIC = Path(__file__).parent / "static"
STATUSES = ["discovered", "to_research", "potential_contact", "contacted", "in_conversation", "collaborated"]

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = DB(settings.db_path)
    # research jobs don't survive a restart; mark them so the UI doesn't wait forever
    db.x("UPDATE research_sessions SET status='failed' WHERE status IN ('queued','running')")
    prune(db)
    http = Http(db)
    state["db"], state["http"], state["r"] = db, http, Researcher(db, http)
    yield
    await http.close()


app = FastAPI(title="FLAEVRD Network", lifespan=lifespan)


def db() -> DB:
    return state["db"]


def researcher() -> Researcher:
    return state["r"]


# ---- status & search ------------------------------------------------------------------------
@app.get("/api/status")
def status():
    return {"providers": researcher().provider_status(), "contact_email_set": bool(settings.contact_email),
            "limits": {"max_releases": settings.max_releases, "max_credit_recordings": settings.max_credit_recordings,
                       "max_expansions": settings.max_expansions}}


@app.get("/api/suggest")
async def suggest(q: str, kind: str = "auto"):
    if len(q.strip()) < 2:
        return {"query": q, "candidates": [], "detected": None, "errors": []}
    if kind not in ("auto", "artist", "song", "album"):
        raise HTTPException(400, "kind must be auto|artist|song|album")
    return await researcher().suggest(q, kind)


class ResearchIn(BaseModel):
    query: str
    kind: str = Field(pattern="^(artist|song|album)$")
    provider: str = Field(pattern="^(musicbrainz|genius|discogs)$")
    external_id: str
    depth: int = Field(2, ge=1, le=2)


@app.post("/api/research")
async def start_research(body: ResearchIn):
    sid = researcher().start(body.query, body.kind, body.provider, body.external_id, body.depth)
    return {"session_id": sid}


@app.post("/api/people/{eid}/research")
async def research_from_person(eid: int, depth: int = 2):
    """Start a new search from any discovered person."""
    store = researcher().store
    for p in ("musicbrainz", "genius", "discogs"):
        xid = store.ident(eid, p)
        if xid:
            name = db().one("SELECT name FROM entities WHERE id=?", (eid,))["name"]
            return {"session_id": researcher().start(name, "artist", p, xid, depth)}
    raise HTTPException(409, "This entity has no MusicBrainz/Genius/Discogs identifier to research from.")


@app.get("/api/research")
def history(limit: int = 100):
    rows = db().q("SELECT s.id, s.query, s.target_kind, s.status, s.created_at, s.finished_at, s.saved, s.title, "
                  "s.root_entity_id, e.name AS root_name, "
                  "(SELECT COUNT(DISTINCT track_id) FROM session_tracks WHERE session_id=s.id) AS tracks "
                  "FROM research_sessions s LEFT JOIN entities e ON e.id=s.root_entity_id "
                  "ORDER BY s.id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        res = db().one("SELECT result FROM research_sessions WHERE id=?", (r["id"],))["result"]
        if res:
            js = json.loads(res)
            d["counts"] = js.get("counts")
        out.append(d)
    return out


@app.get("/api/research/{sid}")
def get_research(sid: int):
    r = db().one("SELECT * FROM research_sessions WHERE id=?", (sid,))
    if not r:
        raise HTTPException(404)
    return {"id": sid, "query": r["query"], "kind": r["target_kind"], "status": r["status"],
            "created_at": r["created_at"], "finished_at": r["finished_at"], "saved": bool(r["saved"]),
            "title": r["title"], "log": json.loads(r["log"]), "result": json.loads(r["result"]) if r["result"] else None,
            "expansions": researcher().expansion_status(sid)}


# ---- step 2: expand, compare, discover ---------------------------------------------------------------
@app.post("/api/research/{sid}/expand/{eid}")
async def expand_node(sid: int, eid: int):
    """Research one person of this research's graph and add their own collaborators to it."""
    try:
        return researcher().expand_node(sid, eid)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/research/{sid}/expansions")
def expansions(sid: int):
    return researcher().expansion_status(sid)


@app.get("/api/compare")
def compare(a: int, b: int):
    if a == b:
        raise HTTPException(400, "Pick two different people.")
    for eid in (a, b):
        if not db().one("SELECT 1 FROM entities WHERE id=?", (eid,)) and not db().one(
                "SELECT 1 FROM entity_redirects WHERE old_id=?", (eid,)):
            raise HTTPException(404, f"Unknown person {eid}")
    return insights.compare(db(), a, b)


@app.get("/api/discover")
def discover(saved_only: bool = False):
    return insights.discover_all(db(), saved_only)


@app.post("/api/research/{sid}/recompute")
def recompute(sid: int):
    r = db().one("SELECT status FROM research_sessions WHERE id=?", (sid,))
    if not r or r["status"] != "done":
        raise HTTPException(409, "Only finished research can be recomputed.")
    result = researcher().compute(sid)
    db().x("UPDATE research_sessions SET result=? WHERE id=?", (json.dumps(result), sid))
    return {"ok": True}


class SaveIn(BaseModel):
    title: str


@app.post("/api/research/{sid}/save")
def save_research(sid: int, body: SaveIn):
    db().x("UPDATE research_sessions SET saved=1, title=? WHERE id=?", (body.title.strip()[:200], sid))
    return {"ok": True}


@app.delete("/api/research/{sid}")
def delete_research(sid: int):
    db().x("DELETE FROM research_sessions WHERE id=?", (sid,))
    return {"ok": True}


# ---- people -------------------------------------------------------------------------------------
@app.get("/api/people/{eid}")
def person(eid: int):
    p = build_profile(db(), eid)
    if not p:
        raise HTTPException(404)
    return p


@app.post("/api/people/{eid}/refresh")
async def refresh(eid: int):
    await researcher().refresh_profile(eid)
    return build_profile(db(), eid)


class CrmIn(BaseModel):
    status: str = "discovered"
    notes: str = ""
    tags: list[str] = []
    follow_up: str | None = None


@app.put("/api/people/{eid}/crm")
def save_person(eid: int, body: CrmIn):
    if body.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {STATUSES}")
    t = now()
    db().x("INSERT INTO saved_people(entity_id,status,notes,tags,follow_up,saved_at,updated_at) VALUES(?,?,?,?,?,?,?) "
           "ON CONFLICT(entity_id) DO UPDATE SET status=excluded.status, notes=excluded.notes, tags=excluded.tags, "
           "follow_up=excluded.follow_up, updated_at=excluded.updated_at",
           (eid, body.status, body.notes, json.dumps([x.strip() for x in body.tags if x.strip()]), body.follow_up, t, t))
    return {"ok": True}


@app.delete("/api/people/{eid}/crm")
def unsave_person(eid: int):
    db().x("DELETE FROM saved_people WHERE entity_id=?", (eid,))
    return {"ok": True}


def _saved_rows():
    return db().q("SELECT e.id, e.name, e.entity_type, sp.* FROM saved_people sp JOIN entities e ON e.id=sp.entity_id "
                  "ORDER BY sp.updated_at DESC")


@app.get("/api/saved")
def saved_people():
    out = []
    for r in _saved_rows():
        p = build_profile(db(), r["id"])
        loc = next((p["facts"][f]["values"][0]["value"] for f in ("area", "formed_in", "begin_area", "country")
                    if f in p["facts"]), None)
        out.append({"id": r["id"], "name": r["name"], "type": r["entity_type"], "status": r["status"],
                    "notes": r["notes"], "tags": json.loads(r["tags"]), "follow_up": r["follow_up"],
                    "updated_at": r["updated_at"], "primary_role": p["primary_role"], "location": loc,
                    "credit_count": p["credit_count"],
                    "best_contact": next((c for c in p["contacts"] if c["contact_type"] in ("email", "phone")), None)})
    return out


@app.get("/api/saved/export.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "type", "primary_role", "location", "status", "tags", "follow_up", "notes",
                "contact_type", "contact", "contact_purpose", "contact_status", "contact_source", "last_checked"])
    for r in _saved_rows():
        p = build_profile(db(), r["id"])
        loc = next((p["facts"][f]["values"][0]["value"] for f in ("area", "formed_in", "begin_area", "country")
                    if f in p["facts"]), "")
        base = [r["name"], r["entity_type"], p["primary_role"] or "", loc, r["status"],
                ", ".join(json.loads(r["tags"])), r["follow_up"] or "", r["notes"]]
        contacts = p["contacts"] or [None]
        for c in contacts:
            if c:
                w.writerow(base + [c["contact_type"], c["value"], c["purpose"] or "", c["status"],
                                   c["sources"][0]["url"], c["sources"][0]["last_checked_at"]])
            else:
                w.writerow(base + ["", "", "", "unavailable", "", ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=flaevrd-network-people.csv"})


@app.get("/api/people")
def search_people(q: str = "", limit: int = 50):
    from .engine.normalize import norm_name
    nq = norm_name(q) if q.strip() else ""
    rows = db().q("SELECT e.id, e.name, e.entity_type, e.disambiguation, "
                  "(SELECT COUNT(DISTINCT track_id) FROM credits c WHERE c.entity_id=e.id) n, "
                  "(SELECT COUNT(*) FROM research_sessions s WHERE s.root_entity_id=e.id AND s.status='done') researched "
                  "FROM entities e WHERE e.norm_name LIKE ? OR e.id IN (SELECT entity_id FROM aliases WHERE lower(alias) LIKE ?) "
                  "ORDER BY researched DESC, n DESC LIMIT ?", (f"%{nq}%", f"%{q.lower().strip()}%", limit))
    return [dict(r) for r in rows]


# ---- data retention -----------------------------------------------------------------------------
def prune(d: DB) -> None:
    """YouTube API Services policy: stored API data must be refreshed or deleted within 30 days."""
    d.x("DELETE FROM metrics WHERE platform='YouTube' AND replace(substr(retrieved_at,1,19),'T',' ') < datetime('now', '-30 days')")


@app.post("/api/maintenance/prune")
def prune_endpoint():
    prune(db())
    return {"ok": True}


@app.post("/api/maintenance/clear-cache")
def clear_cache():
    db().x("DELETE FROM http_cache")
    return {"ok": True}


# ---- UI ---------------------------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
