# Data provider strategy

Researched November 2026. Terms and limits change, so re-check the linked pages before any
commercial use. **Status** describes this repository. "Built, untested live" means the adapter is written
against the provider's documented API and covered by offline parsing tests. It has not yet been run
against the live service, because the build environment's network policy blocked these hosts.

| Provider | Used for | Access | Rate limit | Cost | Commercial use | Status |
|---|---|---|---|---|---|---|
| **MusicBrainz** | Search, discographies, recording/release/work credits (producer, engineer, mix, mastering, writer, studio, label), cross-links to Discogs/Wikidata/Spotify/YouTube/Genius, area/origin, official homepage, social links | No key. User-Agent with contact required | 1 req/s per IP | Free | Core data CC0. Heavy commercial use is asked to support MetaBrainz | Built, untested live |
| **Genius API** | Song credits: producers, writers, featured artists, and "custom performances" (mixing/mastering/recording engineers, A&R, labels, studios, musicians); artist bios and social handles | Free API client → access token | Not published; we stay ≤ ~3 req/s | Free | API terms restrict commercial use without Genius's agreement; lyrics are never requested | Built, untested live |
| **Discogs API** | Release credits ("extraartists": engineers, mastering, studios, A&R…), artist profiles, links | Personal access token | 60 req/min authenticated | Free | Release data CC0. Attribution required; images restricted | Built, untested live |
| **Wikidata + Wikipedia** | Location (born in / formed in / residence / country), occupations, genres, labels, official website, Instagram/X/TikTok/SoundCloud/LinkedIn IDs, YouTube channel & Spotify IDs, Wikipedia summary | No key | Polite use | Free | Wikidata CC0; Wikipedia text CC BY-SA (attributed via source link) | Built, untested live |
| **Spotify Web API** | Identity link + genres only | Client ID/secret (client-credentials) | Rolling window | Free, **but the app owner needs Spotify Premium** (Feb 2026) | Developer Terms; no ML training on data | Built, untested live |
| **YouTube Data API v3** | Channel subscribers + total views | API key | 10,000 units/day; `channels.list` = 1 unit | Free | API Services policies: stored data must be refreshed or deleted within 30 days (the app auto-prunes) | Built, untested live |
| **Last.fm API** | Listeners + scrobbles among Last.fm users | API key | ~5 req/s | Free | Non-commercial by default; commercial needs agreement | Built, untested live |
| **Official websites** | Published business emails/phones (mailto, visible text, tel: links), socials linked from the site | None. Only sites linked as the person's official homepage on MusicBrainz/Wikidata | 1 req/s per site, robots.txt respected | Free | Only what the site itself publishes | Built, tested offline |

## Not integrated (and why)

| Source | Why not | Legitimate alternative |
|---|---|---|
| **Spotify monthly listeners / stream counts** | Never exposed by the Web API. Since Feb 2026, Development-Mode apps also lose artist `followers` and `popularity`. Extended Quota is limited to established businesses. | Shown as *Unavailable* with this reason. For real streaming data, use Spotify for Artists (your own artists only) or a licensed analytics vendor (Chartmetric, Soundcharts, Luminate; paid, contact sales). |
| **Apple Music API** | Needs the Apple Developer Program ($99/yr). Catalog metadata only: no credits beyond composer and no play counts. Adds little for Step 1. | Can be added later as an identity/catalog adapter. |
| **SoundCloud** | API app registration is restricted and its terms don't cover third-party analytics. | Profile links are shown when MusicBrainz/Wikidata/official sites list them. |
| **LinkedIn** | No permitted API for looking up other people's profiles; scraping is prohibited. | LinkedIn URLs are shown only when an official site, MusicBrainz or Wikidata links them. |
| **Instagram** | Graph API Business Discovery only works through your own professional account and doesn't return contact details. | Handles from Genius/Wikidata/MusicBrainz/official sites. A future option is Business Discovery via a FLAEVRD account. |
| **Industry publications, interviews, label/publisher sites** | No uniform API; sites have different terms. | Step 2+: curated adapters per site (robots.txt-respecting) or a news API. |
| **Contact databases / email-finder services** | Excluded by design: guessed or leaked addresses are not permitted. | n/a |

## How to get credentials

1. **FLAEVRD_CONTACT_EMAIL**: any address you read. MusicBrainz blocks anonymous clients.
2. **Genius**: <https://genius.com/api-clients> → *New API Client* (app website can be your site) → *Generate Access Token* → `GENIUS_ACCESS_TOKEN`.
3. **Discogs**: <https://www.discogs.com/settings/developers> → *Generate new token* → `DISCOGS_TOKEN`.
4. **YouTube**: <https://console.cloud.google.com> → new project → *APIs & Services* → enable *YouTube Data API v3* → *Credentials* → *Create API key* (restrict it to that API) → `YOUTUBE_API_KEY`.
5. **Last.fm**: <https://www.last.fm/api/account/create> → `LASTFM_API_KEY`.
6. **Spotify** (optional, low value since Feb 2026): <https://developer.spotify.com/dashboard> → create app → `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`. The account needs Premium.

Without keys the tool still works on MusicBrainz + Wikidata + official websites. Each missing provider
appears as "Waiting for configuration" under **Sources** in the app and in the research log. Nothing is
substituted with made-up data.

## Data freshness

- API responses are cached in SQLite for `FLAEVRD_CACHE_HOURS` (default 7 days). Metrics are cached for 24 h.
- Every fact, contact and metric stores its retrieval time. Metrics keep history (each refresh adds a
  row), and the profile shows the latest value plus earlier values.
- "Refresh profile" on any person re-fetches background, contacts and metrics.
