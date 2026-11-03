# FLAEVRD Network

**Search one name. Discover an entire network.**

FLAEVRD's music-industry research tool. Search an artist, producer, engineer, songwriter, song or
album. It collects the credits behind the music from several sources and maps who worked with whom,
on which tracks. It also follows those connections to people one step further out, and shows
publicly listed professional contacts. Every fact links back to its source.

This is **Step 1** (research and discovery engine). It already includes a first network graph,
second-degree discovery, similar-network recommendations, and a basic people database with CSV
export, so research results can be kept.

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
6. **Save** people with a status, tags (used as lists, e.g. "UK Network"), notes and a follow-up date.
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
