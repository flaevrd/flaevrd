"""Audience metrics from permitted APIs only. Every value carries platform, source URL and
retrieval time. Anything not exposed by an official API is recorded as *unavailable* with the
reason, never estimated."""
from __future__ import annotations

import time

from ..config import settings
from ..http import Http, HttpError
from .base import FactRec, MetricRec, NotConfigured, ProfileData

SPOTIFY_MONTHLY_REASON = ("Spotify monthly listeners are not exposed by the Spotify Web API "
                          "(only shown in the Spotify apps); no permitted source.")
SPOTIFY_STREAMS_REASON = "Per-track Spotify stream counts are not available through the Spotify Web API."


class Spotify:
    """Spotify Web API, client-credentials flow. Since Feb 2026, Development-Mode apps no longer
    receive artist `followers`/`popularity`; we use Spotify for identity + genres and record
    followers only if the API actually returns them (Extended Quota apps)."""
    name = "spotify"
    requires = ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"]

    def __init__(self, http: Http):
        self.http = http
        self._token: str | None = None
        self._exp = 0.0

    def configured(self) -> bool:
        return bool(settings.spotify_id and settings.spotify_secret)

    async def _auth(self) -> str:
        if not self.configured():
            raise NotConfigured("SPOTIFY_CLIENT_ID/SECRET")
        if self._token and time.time() < self._exp - 60:
            return self._token
        js = await self.http.post_form("https://accounts.spotify.com/api/token",
                                       {"grant_type": "client_credentials"},
                                       (settings.spotify_id, settings.spotify_secret))
        self._token, self._exp = js["access_token"], time.time() + js.get("expires_in", 3600)
        return self._token

    async def artist_profile(self, spotify_id: str) -> ProfileData:
        p = ProfileData()
        token = await self._auth()
        r = await self.http.get(f"https://api.spotify.com/v1/artists/{spotify_id}",
                                headers={"Authorization": f"Bearer {token}"}, cache_hours=24)
        a = r.json()
        url = (a.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/artist/{spotify_id}"
        for g in a.get("genres") or []:
            p.facts.append(FactRec("genre", g, "spotify", url, "Spotify artist genres (Web API)"))
        followers = (a.get("followers") or {}).get("total")
        if isinstance(followers, int):
            p.metrics.append(MetricRec("Spotify", "followers", followers, url, "Spotify Web API artist object"))
        else:
            p.unavailable["spotify.followers"] = ("Spotify no longer returns follower counts to Development-Mode "
                                                  "apps (Feb 2026 change); requires Extended Quota access.")
        p.unavailable["spotify.monthly_listeners"] = SPOTIFY_MONTHLY_REASON
        p.unavailable["spotify.track_streams"] = SPOTIFY_STREAMS_REASON
        return p


class YouTube:
    """YouTube Data API v3. channels.list costs 1 quota unit (10,000/day free)."""
    name = "youtube"
    requires = ["YOUTUBE_API_KEY"]

    def __init__(self, http: Http):
        self.http = http

    def configured(self) -> bool:
        return bool(settings.youtube_key)

    async def channel_profile(self, channel_id: str | None = None, handle: str | None = None) -> ProfileData:
        if not self.configured():
            raise NotConfigured("YOUTUBE_API_KEY")
        p = ProfileData()
        params = {"part": "statistics,snippet", "key": settings.youtube_key}
        if channel_id:
            params["id"] = channel_id
        elif handle:
            params["forHandle"] = handle
        else:
            return p
        r = await self.http.get("https://www.googleapis.com/youtube/v3/channels", params=params, cache_hours=24)
        items = r.json().get("items") or []
        if not items:
            return p
        ch = items[0]
        url = f"https://www.youtube.com/channel/{ch['id']}"
        st = ch.get("statistics", {})
        desc = "YouTube Data API v3 channels.list"
        if not st.get("hiddenSubscriberCount") and st.get("subscriberCount") is not None:
            p.metrics.append(MetricRec("YouTube", "subscribers", int(st["subscriberCount"]), url, desc))
        if st.get("viewCount") is not None:
            p.metrics.append(MetricRec("YouTube", "channel_views", int(st["viewCount"]), url, desc))
        country = (ch.get("snippet") or {}).get("country")
        if country:
            p.facts.append(FactRec("country", country, "youtube", url, "Country set on the YouTube channel"))
        return p


class LastFm:
    """Last.fm API artist.getInfo: listeners/scrobbles among Last.fm users (not streaming totals)."""
    name = "lastfm"
    requires = ["LASTFM_API_KEY"]

    def __init__(self, http: Http):
        self.http = http

    def configured(self) -> bool:
        return bool(settings.lastfm_key)

    async def artist_profile(self, mbid: str | None, name: str) -> ProfileData:
        if not self.configured():
            raise NotConfigured("LASTFM_API_KEY")
        p = ProfileData()
        params = {"method": "artist.getinfo", "api_key": settings.lastfm_key, "format": "json", "autocorrect": 0}
        if mbid:
            params["mbid"] = mbid
        else:
            return p  # name-only lookups are ambiguous for homonyms; require an MBID link
        try:
            r = await self.http.get("https://ws.audioscrobbler.com/2.0/", params=params, cache_hours=24)
        except HttpError:
            return p
        a = r.json().get("artist")
        if not a:
            return p
        url = a.get("url") or "https://www.last.fm"
        st = a.get("stats") or {}
        desc = "Last.fm API artist.getInfo (Last.fm users only)"
        if st.get("listeners"):
            p.metrics.append(MetricRec("Last.fm", "listeners", int(st["listeners"]), url, desc))
        if st.get("playcount"):
            p.metrics.append(MetricRec("Last.fm", "scrobbles", int(st["playcount"]), url, desc))
        return p
