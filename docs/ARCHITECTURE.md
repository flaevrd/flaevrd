# Architecture (Steps 1–2)

```
Browser (app/static, no build step)
   │  JSON
FastAPI (app/main.py) ── research jobs run as asyncio background tasks
   │
Research engine (app/engine)
   ├─ research.py   orchestration: suggest → identify → collect credits → expand → enrich → compute
   ├─ store.py      persistence + entity resolution (identity rules below)
   ├─ network.py    relationships, strength score, 2nd degree, discovery categories, graph
   ├─ insights.py   Step 2: compare two networks, Discover across all research
   ├─ profile.py    person profile with per-fact sources, discrepancies, contacts, metrics
   └─ normalize.py  name/title normalization, role vocabulary
   │
Provider adapters (app/providers) → provider-neutral records (base.py)
   musicbrainz · genius · discogs · wikidata(+wikipedia) · metrics(spotify, youtube, lastfm) · website
   │
Http (app/http.py): per-host rate limiting, SQLite response cache, retries, robots.txt
   │
SQLite (app/db.py)
```

Adding a provider means writing one adapter that returns `TrackRec` / `ProfileData`. The engine
needs no changes.

## Research flow

1. **Suggest**: the query is searched as artist, song and album at the same time (MusicBrainz, plus
   Genius when configured). "Song - Artist" and "Song by Artist" are split, and the reversed order is
   tried too. Homonyms stay separate candidates with their disambiguation. The user picks the exact
   entity, so a search never silently researches the wrong person.
2. **Identify**: the MusicBrainz artist record gives authoritative cross-links (Discogs, Wikidata,
   Spotify, YouTube, Genius URL). Genius/Discogs are linked by those cross-links. For the main search
   only, an exact, unique name match is also accepted, and it is labelled as a *name match* on the profile.
   Producers rarely appear as a song's primary artist, so their Genius ID is found by matching a few
   of their MusicBrainz songs on Genius and linking through the shared credit.
3. **Collect credits** (per source in parallel): own discography (albums/EPs, then singles), recordings,
   releases and works the person is credited on, Genius songs, and Discogs releases. Only tracks that
   actually credit the person are kept.
4. **Expand** (depth 2): the top N direct collaborators (producers, engineers and writers first) are
   researched lightly. This finds the people *they* work with.
5. **Enrich**: background, location, contacts and metrics for the main person and the top collaborators.
6. **Compute**: relationships, second-degree paths, discovery categories and graph. Saved on the session,
   so reopening a research is instant. `POST /api/research/{id}/recompute` rebuilds it.

## Entity resolution

Applied in order, first hit wins (`engine/store.py`):

1. Same provider ID → same entity.
2. Authoritative cross-link (MusicBrainz/Wikidata/Discogs profile links the other provider's page) →
   same entity. Existing duplicates are merged.
3. Same normalized name credited on the **same track** by another provider → same entity.
4. Same normalized name **and** already co-credited with someone on this track → same entity.

Otherwise a new entity is created. If another entity has the same name, the pair is stored in
`identity_questions` and shown on both profiles as a possible duplicate, instead of being merged.
Two different MusicBrainz artists with the same name are never merged by name. Every identifier stores
its `match_method`, so each merge can be audited.

## Relationship strength (0–100)

```
base        = 30 · log2(1 + shared_tracks)        1 track → 30, 3 → 60, 7 → 90
projects    = +5 per extra distinct release (max +15)
recency     = × (0.6 + 0.4·r)   r = 1 if latest shared work ≤ 2 yrs old, halves every 5 yrs after
reliability = × mean per-track credit reliability: 1.0 confirmed by ≥2 providers,
              0.85 MusicBrainz/Discogs only, 0.75 Genius only
```

Tiers: strong ≥ 70, solid 40–69, light < 40. Each relationship shows its components ("4 shared
tracks across 2 projects, most recent 2024; 3 confirmed by two or more sources") and the exact tracks
with source links. **Popularity and follower counts never enter the score.**

Second degree: each path scores min(root→via, via→target). A person's score is their best path plus
8 per extra independent path (cap 95). Label/studio paths count half. Path types: shared producer,
shared engineer, shared songwriter, shared artist, shared label, studio connection, or indirect
professional connection.

Discovery categories (`network.discoveries`): similar network (same role, cosine overlap of
collaborator sets, ≥ 2 shared), new collaborators, emerging artists (earliest release found ≤ 3 years
ago, labelled as a signal only), frequently shared engineers, connected songwriters, and
cross-network connectors.

## Data model

| Table | Purpose |
|---|---|
| `sources` | provider + exact URL + description + retrieval time; referenced by every fact/credit/contact/metric |
| `entities` | people, groups, labels, studios |
| `entity_identifiers` | provider IDs with `match_method` (primary, cross_link, same_track_name, name_and_shared_collaborator, name_match, user) |
| `aliases`, `identity_questions` | alternate names; unresolved possible duplicates |
| `facts` | per-source facts (bio, area, country, genre, occupation…). Conflicts are kept and shown |
| `releases`, `tracks` (+ `_identifiers`) | albums and songs, matched across providers |
| `credits` | track × entity × canonical role × provider, with the source's own role wording |
| `contacts` | type, value, purpose, context, status (verified / publicly_listed / unverified), source, last checked |
| `metrics` | platform, metric, value, source, retrieved_at (history kept; YouTube pruned after 30 days) |
| `unavailable` | explicit "looked, not available" outcomes with the reason |
| `research_sessions`, `session_tracks` | history, saved research projects, which tracks each search brought in and through whom |
| `saved_people` | CRM: status, notes, tags (= lists), follow-up date |

Relationships are derived from `credits` and never stored separately, so their evidence can't drift
from the facts.

## Security & privacy

- API keys are read only from environment/`.env` on the server, never sent to the browser, and never
  part of cache keys or logs. `.env` and the database are git-ignored.
- All external text is HTML-escaped in the UI; only http(s) links are rendered.
- Contacts: only what official sites or credible profiles publish. No guessing, no pattern generation,
  no third-party contact databases. Birth dates and home addresses are not collected.
- Deleting a research session keeps people/credits. Removing a saved person only removes your CRM data.
  The HTTP cache can be cleared via `POST /api/maintenance/clear-cache`.

## Step 2: expand, compare, discover

### Expanding a node (`POST /api/research/{id}/expand/{entity}`)

Any person in a finished research's graph can be expanded. The job runs in the background:

1. Every track already in the database that credits the person joins the session (`session_tracks.degree = 3`,
   `via_entity_id` = the person). This is instant.
2. The person is researched live from their first identifier (MusicBrainz, then Genius, then Discogs), a bit
   deeper than automatic second degree (4 releases, 15 credited recordings, 12 Genius songs).
3. Their profile is enriched (location, contacts, metrics), then the research result is recomputed.

`network.add_expansions` puts up to 15 of the person's strongest collaborators into the graph, one ring further
out (`expanded_from` on the node), and draws links to people who are already in the graph, so clusters show.
Every new node keeps its evidence (tracks, sources). Expanding a direct collaborator also strengthens second-degree
results, since their new credits are part of the session. Status is in `session_expansions`
(`GET /api/research/{id}/expansions`).

### Comparing two networks (`GET /api/compare?a=&b=`)

Each side's direct network comes from every credit in the database. Their second degree comes from their latest
finished research. The comparison shows:

- **Direct link**: tracks the two share, with sources.
- **Shared collaborators**: credited with both. Score = the weaker of the two relationships.
- **Bridges**: A → X → Y → B, where X is A's collaborator, Y is B's, and X and Y share a track. Score = the
  weakest link in the chain.
- **In both extended networks**: people within two steps of both.
- **Only A / only B**: each side's strongest people the other side doesn't reach.
- **Overlap**: shared direct people ÷ all direct people (Jaccard), split by role.

### Discover across all research (`GET /api/discover?saved_only=`)

Reads the stored result of every finished research (latest per root person; optionally saved projects only).
For each person: in how many networks they appear, how (direct, via whom, or found by expansion), and a score
= sum of relationship strengths, second-degree appearances counted at 60 %. The UI groups this into: in several
networks, research next (not researched yet), producers, engineers, songwriters, artists, emerging artists and
connectors, with filters for role, location, genre and "hide people already in my database".

A second-degree appearance reached only through another researched person is not counted again (that
person's own network already counts it). Discover reads the stored result, which keeps each research's top 150
direct and 150 second-degree people, so very large networks contribute their strongest people only.

Entity merges after a research was stored are followed through `entity_redirects`, so old results still point
at the right person.

## Known limits

- Coverage depends on community databases. Credits missing from MusicBrainz/Genius/Discogs won't appear.
- Research is bounded by `FLAEVRD_MAX_*` settings. Raise them for deeper but slower searches
  (MusicBrainz allows only 1 request/second).
- Jobs run in-process. A server restart marks running jobs as failed (re-run them).
- Location/genre graph filters only work for people whose profiles were enriched.
