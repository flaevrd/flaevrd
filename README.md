# FLAEVRD Network

**Search one name. Discover an entire network.**

FLAEVRD's music-industry research tool. Search an artist, producer, engineer, songwriter, song or
album. It collects the credits behind the music from several sources and maps who worked with whom,
on which tracks. It also follows those connections to people one step further out, and shows
publicly listed professional contacts. Every fact links back to its source.

**Step 1** is the research and discovery engine: network graph, second-degree discovery,
similar-network recommendations, and a basic people database with CSV export.
**Step 2** adds network exploration: expand any person in the graph, compare two people's networks,
and a Discover area that works across all your research.

## Simple search page (no install)

For everyday use there is a simple page on claude.ai: type a name, and about 5 minutes later you see who
they work with, who to meet next, and how to reach them. A background job (a scheduled task in the Claude
project) clones this branch and runs `python -m app.cli lookup "<name>"`. It then saves the plain-language
summary (`app/cli.py`) into the page's database. Every connection keeps its songs and source links.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set FLAEVRD_CONTACT_EMAIL at minimum; add API keys as you get them
uvicorn app.main:app --port 8000
# open http://localhost:8000
```

It runs with no API keys at all, using MusicBrainz, Wikidata/Wikipedia and official websites. Add
Genius and Discogs tokens for much richer credits (engineers, A&R, studios), and YouTube/Last.fm keys
for audience metrics. See [docs/PROVIDERS.md](docs/PROVIDERS.md) for what each source provides, its
limits and costs, and how to get the keys.

## Using it

1. **Search**: type a name or "Song – Artist". Pick the exact match from the suggestions. Same-name
   people are listed separately on purpose.
2. Choose **depth**: *Direct only* or *+ 2nd degree* (also researches the top collaborators' other work).
3. While the research runs, a live log shows each source and any provider waiting for configuration.
4. Results tabs: **Overview · People & Credits · Network Graph · Similar Network · Contact Information · Sources**.
5. Click anyone to open their **profile**: background, where they're based (with sources, and a flag
   if sources disagree), credits, collaborators with evidence, contacts with verification status,
   and audience metrics or the reason they're unavailable.
6. **Expand** (Network Graph): click anyone, then *Expand network*. Their own collaborators are researched
   and added to the graph one ring further out.
7. **Compare**: pick two people to see shared collaborators, bridges between their circles, and who
   each one knows that the other doesn't. Also reachable from any profile or research ("Compare with…").
8. **Discover**: people who turn up across several of your researched networks, who to research next,
   and the strongest producers, engineers and songwriters across everything, with role/location/genre filters.
9. **Save** people with a status, tags (used as lists, e.g. "UK Network"), notes and a follow-up date.
   Export them to CSV from **People Database**. Save a research as a project from its results page.

## Principles

- No mock data and no estimates. If something isn't available from a permitted source, the tool says so.
- Contacts are never guessed. Status labels: **Verified** (on the official website),
  **Publicly listed** (credible profile/database), **Unverified**, **Unavailable**.
- Relationship strength is based on shared credits, how often and how recently people worked
  together, and how reliable the sources are. Popularity is never part of it ([docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- Platform terms, rate limits and robots.txt are respected. Lyrics are never fetched.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Layout: `app/providers` (one adapter per source), `app/engine` (resolution, network, orchestration),
`app/main.py` (API), `app/static` (UI; cytoscape.js vendored under MIT license).
