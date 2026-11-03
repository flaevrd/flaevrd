"""Wikidata entity data + Wikipedia page summaries. No key; CC0 (Wikidata) / CC BY-SA (Wikipedia
summaries, attributed via source URL). Used for background, location, genres, occupations,
official websites and verified social handles. Only reached through an explicit cross-link
(MusicBrainz -> Wikidata), never by name guessing."""
from __future__ import annotations

from urllib.parse import quote

from ..http import Http, HttpError
from .base import ContactRec, FactRec, ProfileData
from .links import normalize_url

NAME = "wikidata"

# property -> (fact field | contact type, url prefix for contact handles)
FACT_PROPS = {"P19": "born_in", "P740": "formed_in", "P27": "country", "P495": "country",
              "P106": "occupation", "P136": "genre", "P264": "record_label", "P551": "residence"}
HANDLE_PROPS = {"P2003": ("instagram", "https://www.instagram.com/"),
                "P2002": ("x", "https://x.com/"),
                "P7085": ("tiktok", "https://www.tiktok.com/@"),
                "P3040": ("soundcloud", "https://soundcloud.com/"),
                "P6634": ("linkedin", "https://www.linkedin.com/in/"),
                "P4264": ("linkedin", "https://www.linkedin.com/company/")}
ID_PROPS = {"P2397": "youtube_channel", "P1902": "spotify", "P1953": "discogs", "P434": "musicbrainz",
            "P2373": "genius_slug"}


def _values(claims: dict, prop: str) -> list:
    out = []
    for c in claims.get(prop, []):
        if c.get("rank") == "deprecated":
            continue
        quals = c.get("qualifiers", {})
        if "P582" in quals:  # has end time -> no longer current
            continue
        dv = (c.get("mainsnak") or {}).get("datavalue")
        if dv:
            out.append(dv.get("value"))
    return out


class Wikidata:
    name = NAME
    requires = []

    def __init__(self, http: Http):
        self.http = http

    def configured(self) -> bool:
        return True

    async def profile(self, qid: str) -> ProfileData:
        p = ProfileData()
        r = await self.http.get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
        ent = r.json().get("entities", {}).get(qid) or next(iter(r.json().get("entities", {}).values()), {})
        claims = ent.get("claims", {})
        src = f"https://www.wikidata.org/wiki/{qid}"
        desc = "Wikidata item (community-maintained, CC0)"
        en_desc = (ent.get("descriptions", {}).get("en") or {}).get("value")
        if en_desc:
            p.facts.append(FactRec("summary", en_desc, NAME, src, desc))
        p.names = [a["value"] for a in (ent.get("aliases", {}).get("en") or [])][:10]

        # resolve referenced items' labels in one call
        refs: dict[str, list[str]] = {}
        for prop, field in FACT_PROPS.items():
            for v in _values(claims, prop):
                if isinstance(v, dict) and v.get("id"):
                    refs.setdefault(v["id"], []).append(field)
        labels = await self._labels(list(refs))
        for item, fields in refs.items():
            if labels.get(item):
                for field in fields:
                    p.facts.append(FactRec(field, labels[item], NAME, src, desc))

        for url in _values(claims, "P856"):
            u = normalize_url(url)
            p.websites.append(u)
            p.contacts.append(ContactRec("website", u, "publicly_listed", NAME, src,
                                         "Official website as recorded on Wikidata (P856)"))
        for prop, (typ, prefix) in HANDLE_PROPS.items():
            for handle in _values(claims, prop):
                p.contacts.append(ContactRec(typ, prefix + str(handle), "publicly_listed", NAME, src,
                                             f"{typ} account recorded on Wikidata ({prop})"))
        for prop, key in ID_PROPS.items():
            vals = _values(claims, prop)
            if vals:
                p.links[key] = str(vals[0])

        title = (ent.get("sitelinks", {}).get("enwiki") or {}).get("title")
        if title:
            try:
                s = await self.http.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}")
                js = s.json()
                if js.get("extract") and js.get("type") != "disambiguation":
                    page = (js.get("content_urls", {}).get("desktop", {}) or {}).get("page") or \
                        f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
                    p.facts.append(FactRec("bio", js["extract"][:1500], "wikipedia", page,
                                           "Wikipedia article summary (CC BY-SA)"))
            except HttpError:
                pass
        return p

    async def _labels(self, ids: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            r = await self.http.get("https://www.wikidata.org/w/api.php", params={
                "action": "wbgetentities", "ids": "|".join(chunk), "props": "labels",
                "languages": "en", "format": "json"})
            for qid, ent in r.json().get("entities", {}).items():
                lab = (ent.get("labels", {}).get("en") or {}).get("value")
                if lab:
                    out[qid] = lab
        return out
