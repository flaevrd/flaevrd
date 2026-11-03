"""Provider-neutral records. Every adapter converts its API payloads into these, so the research
engine never deals with provider-specific JSON."""
from __future__ import annotations

from dataclasses import dataclass, field


class NotConfigured(Exception):
    """Raised when a provider needs credentials that are not set."""


@dataclass
class PersonRef:
    provider: str
    external_id: str
    name: str
    url: str | None = None
    entity_type: str = "person"        # person | group | label | studio | unknown
    credited_as: str | None = None


@dataclass
class CreditRec:
    person: PersonRef
    role: str                          # canonical role
    role_detail: str | None = None     # wording used by the source


@dataclass
class ReleaseRef:
    provider: str
    external_id: str
    title: str
    url: str | None = None
    release_date: str | None = None
    release_type: str | None = None
    artist_text: str | None = None


@dataclass
class TrackRec:
    provider: str
    external_id: str
    title: str
    url: str                           # human-viewable source page for these credits
    artist_text: str
    release_date: str | None = None
    release: ReleaseRef | None = None
    credits: list[CreditRec] = field(default_factory=list)
    source_description: str | None = None

    def add(self, person: PersonRef, role: str, detail: str | None = None) -> None:
        for c in self.credits:
            if (c.person.provider, c.person.external_id, c.role) == (person.provider, person.external_id, role):
                return
        self.credits.append(CreditRec(person, role, detail))


@dataclass
class Candidate:
    """A search suggestion shown to the user before research starts."""
    kind: str                          # artist | song | album
    provider: str
    external_id: str
    title: str
    subtitle: str = ""
    url: str | None = None
    score: float = 0.0
    year: str | None = None


@dataclass
class ContactRec:
    contact_type: str
    value: str
    status: str                        # verified | publicly_listed | unverified
    source_provider: str
    source_url: str
    source_description: str
    purpose: str | None = None
    context: str | None = None


@dataclass
class FactRec:
    field: str
    value: str
    source_provider: str
    source_url: str
    source_description: str | None = None


@dataclass
class MetricRec:
    platform: str
    metric: str
    value: int
    source_url: str
    source_description: str


@dataclass
class ProfileData:
    """Everything a provider knows about one person/group beyond credits."""
    names: list[str] = field(default_factory=list)          # aliases
    facts: list[FactRec] = field(default_factory=list)
    contacts: list[ContactRec] = field(default_factory=list)
    metrics: list[MetricRec] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)      # provider -> external id discovered via cross-links
    websites: list[str] = field(default_factory=list)       # official homepages
    unavailable: dict[str, str] = field(default_factory=dict)
