"""Name/title normalization and credit-role normalization shared by all providers."""
from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------------------
# Names & titles

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")
_DISCOGS_SUFFIX = re.compile(r"\s+\(\d+\)$")
_FEAT_PAREN = re.compile(r"\s*[\(\[](?:feat\.?|ft\.?|featuring|with|prod\.?(?: by)?)\s[^\)\]]*[\)\]]", re.I)
_VERSION_TAIL = re.compile(
    r"\s*(?:[\(\[][^\)\]]*(?:remaster(?:ed)?|explicit|clean|album version|single version|radio edit)[^\)\]]*[\)\]]"
    r"|\s-\s.*(?:remaster(?:ed)?|explicit|clean|album version|single version|radio edit).*)$",
    re.I,
)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_name(name: str) -> str:
    """'The Neptunes' == 'Neptunes'; 'Beyoncé' == 'Beyonce'; 'A$AP Rocky' -> 'asap rocky'."""
    s = strip_accents(clean_discogs_name(name)).lower().replace("&", " and ").replace("$", "s")
    s = _PUNCT.sub(" ", s)
    s = _SPACES.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


def norm_title(title: str) -> str:
    t = _FEAT_PAREN.sub("", title or "")
    t = _VERSION_TAIL.sub("", t)
    t = strip_accents(t).lower().replace("&", " and ")
    t = _PUNCT.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def clean_discogs_name(name: str) -> str:
    """Discogs disambiguates homonyms as 'Name (2)'."""
    return _DISCOGS_SUFFIX.sub("", (name or "").strip())


# --------------------------------------------------------------------------------------
# Roles

ROLE_LABELS = {
    "main_artist": "Main artist",
    "featured_artist": "Featured artist",
    "producer": "Producer",
    "co_producer": "Co-producer",
    "additional_producer": "Additional producer",
    "executive_producer": "Executive producer",
    "writer": "Songwriter",
    "composer": "Composer",
    "lyricist": "Lyricist",
    "recording_engineer": "Recording engineer",
    "mixing_engineer": "Mixing engineer",
    "mastering_engineer": "Mastering engineer",
    "engineer": "Engineer",
    "assistant_engineer": "Assistant engineer",
    "programming": "Programming",
    "arranger": "Arranger",
    "vocals": "Vocals",
    "instrument": "Musician",
    "remixer": "Remixer",
    "a_and_r": "A&R",
    "management": "Management",
    "label": "Label",
    "publisher": "Publisher",
    "studio": "Studio",
    "other": "Other",
}

ROLE_GROUPS = {
    "artist": {"main_artist", "featured_artist", "vocals"},
    "producer": {"producer", "co_producer", "additional_producer", "executive_producer", "programming", "remixer"},
    "writer": {"writer", "composer", "lyricist"},
    "engineer": {"recording_engineer", "mixing_engineer", "mastering_engineer", "engineer", "assistant_engineer"},
    "musician": {"instrument", "arranger"},
    "business": {"a_and_r", "management", "label", "publisher"},
    "studio": {"studio"},
    "other": {"other"},
}
ROLE_TO_GROUP = {r: g for g, rs in ROLE_GROUPS.items() for r in rs}

# Ordered (pattern, role): first match wins. Used for free-text roles (Genius custom
# performances, Discogs extraartists).
_TEXT_RULES: list[tuple[re.Pattern, str]] = [(re.compile(p, re.I), r) for p, r in [
    (r"video|photo|artwork|art direction|design|cover|stylist|translat|interpolat|sampl", "other"),
    (r"executive[\s-]*produc", "executive_producer"),
    (r"\bco[\s-]*produc", "co_producer"),
    (r"additional[\s-]*produc|additional production", "additional_producer"),
    (r"vocal[\s-]*produc", "producer"),
    (r"\bproduc", "producer"),
    (r"master(ed|ing)?\s*at|mixed\s*at|recorded\s*at|engineered\s*at|studio", "studio"),
    (r"assistant.*(engineer|mix)|(engineer|mix).*assist", "assistant_engineer"),
    (r"master", "mastering_engineer"),
    (r"\bmix", "mixing_engineer"),
    (r"record(ed|ing)\s*(by|engineer)|recording", "recording_engineer"),
    (r"engineer", "engineer"),
    (r"\ba\s*&\s*r\b|\ba&r\b|artists?\s+and\s+repertoire", "a_and_r"),
    (r"manag", "management"),
    (r"publish", "publisher"),
    (r"^label$|record label|phonographic copyright|copyright|distribut|licensed", "label"),
    (r"lyric", "lyricist"),
    (r"compos|music by", "composer"),
    (r"written|writer|songwrit", "writer"),
    (r"remix", "remixer"),
    (r"programm|drum machine|beat", "programming"),
    (r"arrang", "arranger"),
    (r"featur", "featured_artist"),
    (r"vocal|rap|choir|backing", "vocals"),
    (r"guitar|bass|drum|keyboard|piano|synth|percussion|string|violin|cello|horn|sax|trumpet|organ|instrument", "instrument"),
]]


def role_from_text(text: str) -> str:
    t = (text or "").strip()
    for pat, role in _TEXT_RULES:
        if pat.search(t):
            return role
    return "other"


# MusicBrainz relationship types (artist -> recording/release/work) -> canonical role
MB_REL_ROLES = {
    "producer": "producer",
    "mix": "mixing_engineer",
    "mix-DJ": "mixing_engineer",
    "mastering": "mastering_engineer",
    "recording": "recording_engineer",
    "engineer": "engineer",
    "audio": "engineer",
    "sound": "engineer",
    "balance": "engineer",
    "editor": "engineer",
    "programming": "programming",
    "vocal": "vocals",
    "instrument": "instrument",
    "performer": "instrument",
    "performing orchestra": "instrument",
    "arranger": "arranger",
    "instrument arranger": "arranger",
    "vocal arranger": "arranger",
    "orchestrator": "arranger",
    "remixer": "remixer",
    "composer": "composer",
    "lyricist": "lyricist",
    "writer": "writer",
    "librettist": "lyricist",
    "publishing": "publisher",
    "phonographic copyright": "label",
    "copyright": "label",
    "recorded at": "studio",
    "mixed at": "studio",
    "mastered at": "studio",
    "engineered at": "studio",
    "produced at": "studio",
    "edited at": "studio",
}


def mb_role(rel_type: str, attributes: list[str] | None) -> str | None:
    role = MB_REL_ROLES.get(rel_type)
    if role is None:
        return None
    attrs = {a.lower() for a in (attributes or [])}
    if role == "producer":
        if "executive" in attrs:
            return "executive_producer"
        if "co" in attrs:
            return "co_producer"
        if "additional" in attrs:
            return "additional_producer"
    if role in {"engineer", "mixing_engineer", "recording_engineer", "mastering_engineer"} and "assistant" in attrs:
        return "assistant_engineer"
    return role
