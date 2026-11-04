"""Professional contact discovery on official websites.

Only sites that MusicBrainz/Wikidata link as the person's *official homepage* are crawled; at most
a handful of same-site pages that look like contact/booking/management/press pages. robots.txt is
respected. Only addresses literally published on the page are extracted (mailto:, visible text,
tel: links). Nothing is guessed, pattern-generated, or looked up in third-party databases.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..http import Http, HttpError
from .base import ContactRec
from .links import social_type

NAME = "website"
_CONTACT_HINT = re.compile(r"contact|booking|book|management|mgmt|press|about|enquir|inquir|impressum|imprint|kontakt|info", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_OBFUSCATED = re.compile(r"([A-Za-z0-9._%+\-]+)\s*[\[\(]\s*at\s*[\]\)]\s*([A-Za-z0-9.\-]+)\s*[\[\(]\s*dot\s*[\]\)]\s*([A-Za-z]{2,})", re.I)
_BAD_DOMAINS = {"example.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "domain.com", "email.com"}
_BAD_LOCAL = {"noreply", "no-reply", "donotreply", "privacy", "dpo", "abuse", "webmaster", "postmaster"}
_PURPOSES = [("booking", re.compile(r"book|agent|agency|live|tour", re.I)),
             ("management", re.compile(r"manag|mgmt", re.I)),
             ("press", re.compile(r"press|pr\b|media|publicity", re.I)),
             ("publishing", re.compile(r"publish|sync|licens", re.I)),
             ("label", re.compile(r"label|records|a&r|demo", re.I)),
             ("general", re.compile(r"info|contact|hello|enquir|inquir|general", re.I))]


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []
        self._href: str | None = None
        self._atext: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._atext = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._atext).strip()))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._href is not None:
            self._atext.append(data.strip())


def _purpose(*texts: str) -> str | None:
    blob = " ".join(t for t in texts if t)
    for name, pat in _PURPOSES:
        if pat.search(blob):
            return name
    return None


def _ok_email(e: str) -> bool:
    local, _, domain = e.lower().partition("@")
    if domain in _BAD_DOMAINS or local in _BAD_LOCAL:
        return False
    return not re.search(r"\.(png|jpe?g|gif|webp|svg|css|js)$", domain)


class WebsiteContacts:
    name = NAME
    requires = []

    def __init__(self, http: Http):
        self.http = http

    async def _fetch(self, url: str) -> _Page | None:
        if not await self.http.robots_allows(url):
            return None
        try:
            r = await self.http.get(url, accept="text/html,application/xhtml+xml", cache_hours=72)
        except HttpError:
            return None
        if "<" not in r.text[:2000]:
            return None
        page = _Page()
        try:
            page.feed(r.text[:800_000])
        except Exception:  # malformed HTML: use what was parsed
            pass
        return page

    async def discover(self, homepage: str, max_pages: int = 4) -> tuple[list[ContactRec], list[str]]:
        """Returns (contacts, notes). Notes explain e.g. robots.txt refusals."""
        notes: list[str] = []
        host = urlsplit(homepage).hostname or ""
        site = host.removeprefix("www.")
        home = await self._fetch(homepage)
        if home is None:
            notes.append(f"{host}: not fetched (robots.txt disallows, or the site did not respond)")
            return [], notes
        pages = [(homepage, home)]
        candidates = []
        for href, text in home.links:
            if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
                continue
            absu = urljoin(homepage, href).split("#")[0]
            if (urlsplit(absu).hostname or "").removeprefix("www.") != site:
                continue
            if _CONTACT_HINT.search(href) or _CONTACT_HINT.search(text):
                if absu not in candidates and absu.rstrip("/") != homepage.rstrip("/"):
                    candidates.append(absu)
        for u in candidates[:max_pages]:
            pg = await self._fetch(u)
            if pg:
                pages.append((u, pg))

        out: dict[tuple[str, str], ContactRec] = {}
        for url, pg in pages:
            desc = f"Published on official website {host}"
            for href, text in pg.links:
                if not href:
                    continue
                if href.lower().startswith("mailto:"):
                    email = href[7:].split("?")[0].strip()
                    if _EMAIL.fullmatch(email) and _ok_email(email):
                        out.setdefault(("email", email.lower()), ContactRec(
                            "email", email, "verified", NAME, url, desc, _purpose(email.split("@")[0], text), text[:160]))
                elif href.lower().startswith("tel:"):
                    tel = re.sub(r"[^\d+]", "", href[4:])
                    if len(tel) >= 7:
                        out.setdefault(("phone", tel), ContactRec("phone", tel, "verified", NAME, url, desc,
                                                                  _purpose(text), text[:160]))
                else:
                    absu = urljoin(url, href)
                    st = social_type(absu)
                    if st:
                        out.setdefault((st, absu.rstrip("/").lower()), ContactRec(
                            st, absu.rstrip("/"), "verified", NAME, url, f"Linked from official website {host}"))
            text = " ".join(pg.text)
            found = set(_EMAIL.findall(text))
            found |= {f"{a}@{b}.{c}" for a, b, c in _OBFUSCATED.findall(text)}
            for email in found:
                if not _ok_email(email) or ("email", email.lower()) in out:
                    continue
                i = text.find(email.split("@")[0])
                ctx = re.sub(r"\s+", " ", text[max(0, i - 80): i + len(email) + 20]).strip()
                out[("email", email.lower())] = ContactRec("email", email, "verified", NAME, url, desc,
                                                           _purpose(email.split("@")[0], ctx), ctx[:200])
        return list(out.values()), notes
