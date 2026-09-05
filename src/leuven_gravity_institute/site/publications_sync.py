"""Sync the group's publication list from the members' ORCID records.

Each person in ``content/people.yaml`` may carry an ``orcid`` iD together with
the dates they joined and (optionally) left the group. The sync fetches every
work on each member's ORCID record and keeps only those published **while that
member was affiliated with the group**, so the list reflects work produced
here rather than a member's whole career.

Metadata comes from ORCID and, where a DOI is available, is enriched from
Crossref, whose author lists and journal details are far more complete than
what ORCID records usually carry.

Two levels of deduplication apply:

1. *Within* an ORCID record, ORCID already groups the summaries contributed by
   different sources; one representative per group is fetched.
2. *Across* members, entries are keyed on DOI, then arXiv id, then a normalized
   title. A paper co-authored by several members therefore appears once, with
   every contributing member listed under ``members`` — which is also what the
   per-member publication pages are filtered on.

The merge is curation-preserving: entries are matched by their stable ``key``,
and for a match the metadata is refreshed while the human decisions
(``include``, ``highlight``, ``note``) are kept. Hand-authored entries (those
without a ``key``) are never modified or removed.
"""

from __future__ import annotations

import calendar
import html
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from leuven_gravity_institute.site.content import load_yaml
from leuven_gravity_institute.site.inspire import fetch_records as fetch_inspire_records
from leuven_gravity_institute.site.openalex import fetch_works as fetch_openalex_works
from leuven_gravity_institute.site.orcid import fetch_crossref_work, fetch_orcid_works

# Above this many authors a paper is treated as a collaboration paper when the
# record names no collaboration, and its author list is rendered as "First
# Author et al." with the group's own members named.
#
# This is a blunt fallback, not the main signal: a named collaboration accounts
# for the overwhelming majority of such papers, and plenty of ordinary
# multi-institution work carries twenty or thirty authors without being a
# collaboration paper at all. The threshold is therefore set high enough to
# catch only what is unambiguously collaboration-scale, and any paper it still
# gets wrong can be corrected by hand with `treat_as_collaboration`.
COLLABORATION_THRESHOLD = 50

_WORK_TYPE_MAP = {
    "journal-article": "journal",
    "preprint": "preprint",
    "working-paper": "preprint",
    "posted-content": "preprint",
    "conference-paper": "conference",
    "conference-abstract": "conference",
    "proceedings-article": "conference",
    "book": "book",
    "book-chapter": "chapter",
    "edited-book": "book",
    "dissertation-thesis": "thesis",
    "supervised-student-publication": "thesis",
    "data-set": "dataset",
    "dataset": "dataset",
    "software": "software",
    # Zenodo mints a fresh DOI for every GitHub release, and OpenAlex indexes
    # each one as a work; without this mapping they fall through to "other".
    "software-paper": "software",
    "libraries": "software",
    "report": "report",
    "review": "journal",
    "letter": "journal",
    "article": "journal",
    "dissertation": "thesis",
}

# Software and datasets are catalogued on the Software & data page from
# software.yaml, not in the publication list. Excluding them here is what keeps
# a few hundred Zenodo release deposits out of the group's publications.
EXCLUDED_PUBLICATION_TYPES = frozenset({"software", "dataset"})

# INSPIRE document types, mapped onto the vocabulary the other sources use.
_INSPIRE_TYPE_MAP = {
    "article": "journal-article",
    "conference paper": "conference-paper",
    "proceedings": "conference-paper",
    "thesis": "dissertation-thesis",
    "book": "book",
    "book chapter": "book-chapter",
}

_HEADER = """\
# Publications of the group — GENERATED FILE.
#
# Managed entries (those with a `key` and `source: orcid`) are written by
# `lgi publications sync`, which reads every member's ORCID record and keeps
# only the works published while that member was affiliated with the group
# (their `start`/`end` dates in content/people.yaml). Papers shared by several
# members are deduplicated into one entry listing all of them under `members`.
#
# Curation below is preserved across syncs:
#   include:   set false to hide an entry from the site.
#   highlight: set true to feature it as selected work (rendered with a star).
#   note:      optional one-line editorial note shown under the entry.
#   treat_as_collaboration:
#              overrides which section an entry appears in. The `collaboration`
#              field below is DERIVED from what the sources said and is
#              rewritten on every sync, so editing it has no lasting effect; add
#              `treat_as_collaboration: false` to move a paper out of the
#              collaboration section, or `true` to move one in.
#
# To add something ORCID does not know about, append an entry *without* a
# `key`; hand-authored entries are never touched by the sync. Wrap a group
# member's name in **double asterisks** to emphasise it.
"""

# The order keys are written within each entry, for stable, readable diffs.
_FIELD_ORDER = [
    "include",
    "highlight",
    "title",
    "authors",
    "venue",
    "year",
    "date",
    "type",
    "doi",
    "arxiv",
    "url",
    "key",
    "source",
    "members",
    "author_count",
    "collaboration",
    "treat_as_collaboration",
    "note",
    "links",
]


@dataclass(frozen=True)
class Member:
    """A group member whose records contribute to the publication list.

    Identifiers are *pinned per person* rather than looked up by name. Both
    lookups are unreliable in ways that quietly corrupt a publication list:
    OpenAlex splits one researcher across several author entities, and a name
    search readily returns a different researcher who happens to share it.
    """

    id: str
    name: str
    start: date
    end: date | None = None
    orcid: str = ""
    inspire: str = ""
    openalex: str = ""

    @property
    def sources(self) -> list[str]:
        """The names of the sources configured for this member."""
        return [
            name
            for name, value in (("orcid", self.orcid), ("inspire", self.inspire), ("openalex", self.openalex))
            if value
        ]

    @property
    def family(self) -> str:
        """The member's family name, used to match them in author lists."""
        return name_parts(self.name)[0]

    @property
    def given(self) -> str:
        """The member's given name, used to tell them from a same-surname author."""
        return name_parts(self.name)[1]


@dataclass
class SyncSummary:
    """Outcome of a sync, for reporting to the CLI and the pull request."""

    members_synced: int = 0
    total_fetched: int = 0
    out_of_window: int = 0
    deduplicated: int = 0
    collaboration: int = 0
    excluded: int = 0
    misattributed: int = 0
    source_counts: dict[str, int] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether the merge produced any additions, removals, or updates."""
        return bool(self.added) or bool(self.removed) or self.updated > 0


def clean_text(value: str) -> str:
    """Flatten a Crossref/ORCID string into plain text.

    Crossref returns titles and journal names containing presentation markup
    and JATS entities — ``Total Mass 190-265 <i>M</i><sub>&#8857;</sub>`` spread
    over several indented lines. Templates escape their input, so those tags
    would otherwise appear literally on the page.

    Args:
        value: The raw string from the upstream API.

    Returns:
        The string with tags removed, entities resolved, and whitespace
        collapsed to single spaces.

    """
    without_tags = re.sub(r"<[^>]+>", " ", str(value))
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def is_collaboration(entry: dict[str, Any]) -> bool:
    """Whether an entry belongs in the collaboration section.

    ``collaboration`` is derived by the sync from what the sources said, and is
    rewritten on every run. ``treat_as_collaboration`` is the human override: it
    is never written by the sync, always preserved across runs, and wins
    outright in either direction.

    Args:
        entry: A publication entry.

    Returns:
        ``True`` when the entry should be listed as a collaboration paper.

    """
    override = entry.get("treat_as_collaboration")
    if override is not None:
        return bool(override)
    return bool(entry.get("collaboration"))


def _fold(text: str) -> str:
    """Casefold and strip accents so names compare across spellings."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def parse_date(value: Any) -> date | None:
    """Parse a ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD`` value into a date.

    Partial values resolve to the first day of the period, which is what the
    membership ``start``/``end`` fields mean in practice.

    Args:
        value: A date, a string, or ``None``.

    Returns:
        The parsed date, or ``None`` if the value is empty or unparsable.

    """
    if isinstance(value, date):
        return value
    if not value:
        return None
    parts = str(value).strip().split("-")
    try:
        year, month, day = (int(parts[index]) if index < len(parts) else 1 for index in range(3))
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


def date_span(year: int | None, month: int | None, day: int | None) -> tuple[date, date] | None:
    """Turn a possibly partial publication date into the span it could fall in.

    ORCID frequently records only a year, or a year and month. Such a date is
    treated as the whole period it names — ``2023`` becomes 1 January to 31
    December 2023 — so that a paper is not excluded merely because its exact
    day is unknown.

    Args:
        year: The publication year, if known.
        month: The publication month, if known.
        day: The publication day, if known.

    Returns:
        The ``(earliest, latest)`` span, or ``None`` if no year is known.

    """
    if not year:
        return None
    if not month:
        return date(year, 1, 1), date(year, 12, 31)
    if not day:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last)
    return date(year, month, day), date(year, month, day)


def in_membership_window(span: tuple[date, date] | None, member: Member) -> bool:
    """Whether a publication date span overlaps a member's time in the group.

    A span with an unknown year is excluded: without a date there is no way to
    tell whether the work belongs to the member's time here.

    Args:
        span: The ``(earliest, latest)`` publication date span.
        member: The member whose ``start``/``end`` dates bound the window.

    Returns:
        ``True`` when the two intervals overlap.

    """
    if span is None:
        return False
    earliest, latest = span
    if latest < member.start:
        return False
    return not (member.end and earliest > member.end)


def members_from_people(people: dict[str, Any] | None) -> list[Member]:
    """Build the syncable members from ``content/people.yaml``.

    A person contributes only when they have a ``start`` date and at least one
    of ``orcid``, ``inspire``, or ``openalex``. Without a start date there is no
    window to bound attribution with, so the person is skipped rather than
    having their whole career attributed to the group.

    Args:
        people: The parsed ``people.yaml`` document.

    Returns:
        One :class:`Member` per person with a start date and an identifier.

    """
    members: list[Member] = []
    for person in (people or {}).get("items") or []:
        start = parse_date(person.get("start"))
        member = Member(
            id=person["id"],
            name=person.get("name", ""),
            start=start or date.min,
            end=parse_date(person.get("end")),
            orcid=(person.get("orcid") or "").strip(),
            inspire=(person.get("inspire") or "").strip(),
            openalex=(person.get("openalex") or "").strip(),
        )
        if start is None or not member.sources:
            continue
        members.append(member)
    return members


def _external_ids(work: dict[str, Any]) -> dict[str, str]:
    """Collect the external identifiers of an ORCID work, keyed by lowercase type."""
    ids: dict[str, str] = {}
    for identifier in ((work.get("external-ids") or {}).get("external-id")) or []:
        id_type = (identifier.get("external-id-type") or "").lower()
        value = identifier.get("external-id-value")
        if id_type and value and id_type not in ids:
            ids[id_type] = str(value)
    return ids


def _orcid_publication_date(work: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Extract the (year, month, day) of an ORCID work, each possibly missing."""
    published = work.get("publication-date") or {}

    def part(name: str) -> int | None:
        value = (published.get(name) or {}).get("value")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    return part("year"), part("month"), part("day")


def _crossref_publication_date(message: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Extract the earliest known (year, month, day) from a Crossref record."""
    for key in ("published-print", "published-online", "published", "issued", "created"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            first = [*parts[0], None, None]
            return first[0], first[1], first[2]
    return None, None, None


def _crossref_authors(message: dict[str, Any]) -> list[str]:
    """Render a Crossref author list as ``"Given Family"`` strings."""
    authors: list[str] = []
    for author in message.get("author") or []:
        if author.get("name"):  # a collaboration rather than a person
            authors.append(str(author["name"]))
            continue
        given = str(author.get("given") or "").strip()
        family = str(author.get("family") or "").strip()
        full = f"{given} {family}".strip()
        if full:
            authors.append(full)
    return authors


def _orcid_contributors(work: dict[str, Any]) -> list[str]:
    """Render the contributor names an ORCID work carries, if any."""
    names = []
    for contributor in ((work.get("contributors") or {}).get("contributor")) or []:
        name = (contributor.get("credit-name") or {}).get("value")
        if name:
            names.append(str(name))
    return names


def _crossref_collaborations(message: dict[str, Any]) -> list[str]:
    """Names of any collaborations credited as authors on a Crossref record.

    Crossref represents a collaboration as an author entry carrying ``name``
    instead of ``given``/``family``.
    """
    return [str(a["name"]) for a in message.get("author") or [] if a.get("name")]


def _crossref_venue(message: dict[str, Any]) -> str:
    """Build a human-readable venue string from a Crossref record."""
    titles = message.get("container-title") or []
    journal = clean_text(titles[0]) if titles else ""
    if not journal:
        return str((message.get("institution") or [{}])[0].get("name") or "") if message.get("institution") else ""
    volume = str(message.get("volume") or "")
    pages = str(message.get("article-number") or message.get("page") or "")
    year = _crossref_publication_date(message)[0]
    venue = journal
    if volume:
        venue += f" {volume}"
    if pages:
        venue += f", {pages}"
    if year:
        venue += f" ({year})"
    return venue


def _fallback_venue(work: dict[str, Any], arxiv: str | None, work_type: str) -> str:
    """Build a venue string from the ORCID record alone."""
    journal = (work.get("journal-title") or {}).get("value")
    if journal:
        return clean_text(journal)
    if arxiv:
        return f"arXiv:{arxiv}"
    return work_type.replace("-", " ").title() if work_type else "Preprint"


def _publication_type(orcid_type: str | None, crossref_type: str | None, *, arxiv: str | None, venue: str) -> str:
    """Map ORCID/Crossref work types onto the site's publication type enum."""
    for raw in (orcid_type, crossref_type):
        mapped = _WORK_TYPE_MAP.get((raw or "").lower())
        if mapped:
            if mapped == "journal" and not venue and arxiv:
                return "preprint"
            return mapped
    return "preprint" if arxiv and not venue else "other"


def name_parts(name: str) -> tuple[str, str]:
    """Split a rendered ``"Given Middle Family"`` name into (family, given).

    Initials are separated from the names they abbreviate, so ``"I. C. F.
    Wong"`` yields ``("wong", "i")`` and ``"Isaac Wong"`` yields
    ``("wong", "isaac")``.
    """
    tokens = _fold(name).replace(".", " ").split()
    if not tokens:
        return "", ""
    return tokens[-1], tokens[0] if len(tokens) > 1 else ""


def names_match(author: str, member: Member) -> bool:
    """Whether an author name refers to a member.

    Family names must be equal. Given names must be *compatible*: when either
    side is a bare initial they need only share a letter, but when both are
    spelled out they must be the same name. Comparing only initials is not
    enough — it makes "Tie-Fu Li" indistinguishable from "Tjonnie G. F. Li",
    which is precisely how another researcher's papers end up on a group page.

    Args:
        author: A rendered author name.
        member: The member to test against.

    Returns:
        ``True`` when the names are consistent with being the same person.

    """
    family, given = name_parts(author)
    if not family or family != member.family:
        return False
    theirs = member.given
    if not given or not theirs:
        return True
    if len(given) == 1 or len(theirs) == 1:
        return given[0] == theirs[0]
    return given == theirs


def member_appears(authors: Sequence[str], member: Member) -> bool:
    """Whether a member is named among a record's authors."""
    return any(names_match(author, member) for author in authors)


def emphasise_members(authors: Sequence[str], members: Iterable[Member]) -> list[str]:
    """Wrap group members' names in ``**`` within an author list.

    Names are matched on family name plus given initial, so a co-author who
    merely shares a surname with a member is not emphasised by mistake.

    Args:
        authors: The rendered author names.
        members: The members to emphasise.

    Returns:
        The author list with matching names wrapped in ``**``.

    """
    targets = [member for member in members if member.family]
    rendered: list[str] = []
    for author in authors:
        matched = any(names_match(author, member) for member in targets)
        rendered.append(f"**{author}**" if matched and not author.startswith("**") else author)
    return rendered


def build_authors(
    authors: Sequence[str],
    members: Sequence[Member],
    *,
    threshold: int = COLLABORATION_THRESHOLD,
) -> tuple[list[str], bool]:
    """Render the author list, collapsing very long ones.

    Args:
        authors: The full author list.
        members: The group members credited on this work.
        threshold: Author count above which the list collapses to "et al.".

    Returns:
        The rendered authors and whether the list was collapsed.

    """
    if not authors:
        return [f"**{member.name}**" for member in members], False
    emphasised = emphasise_members(authors, members)
    named = [name for name in emphasised if name.startswith("**")]
    # Crossref sometimes records a large collaboration as a single author, so no
    # member's name appears in the list at all. Fall back to naming the credited
    # members outright, otherwise the entry gives no clue why it is on the
    # group's page.
    credited = named or [f"**{member.name}**" for member in members]

    if len(authors) > threshold:
        lead = emphasised[0]
        incl = f" (incl. {', '.join(credited)})" if credited and lead not in credited else ""
        return [f"{lead} et al.{incl}"], True
    if not named and credited:
        return [*emphasised[:-1], f"{emphasised[-1]} (incl. {', '.join(credited)})"], False
    return emphasised, False


_ARXIV_DOI_PREFIX = "10.48550/arxiv."


def split_arxiv_doi(doi: str | None, arxiv: str | None) -> tuple[str | None, str | None]:
    """Rewrite arXiv's own DataCite DOI into a plain arXiv identifier.

    OpenAlex records a preprint under ``10.48550/arXiv.2411.17893`` while
    INSPIRE records the published version under the journal's DOI and carries
    the arXiv id separately. Left alone, the same paper is keyed two different
    ways and appears twice.

    Args:
        doi: The DOI as supplied upstream.
        arxiv: The arXiv identifier, if the source gave one.

    Returns:
        The ``(doi, arxiv)`` pair, with an arXiv DOI moved into ``arxiv``.

    """
    if doi and doi.lower().startswith(_ARXIV_DOI_PREFIX):
        return None, arxiv or doi[len(_ARXIV_DOI_PREFIX) :]
    return doi, arxiv


def title_key(title: str) -> str:
    """Return a normalized form of a title, used as a last-resort identity."""
    return f"title:{re.sub(r'[^a-z0-9]+', '-', _fold(title)).strip('-')}"


def identity_keys(entry: dict[str, Any]) -> list[str]:
    """Every identifier by which an entry could be recognised as the same work.

    A paper reaches the sync as a preprint from one source and as the published
    article from another, and the two records rarely share a single identifier:
    one has the journal DOI, the other the arXiv id, and sometimes neither. Two
    records are the same work if they agree on *any* of these.
    """
    keys = []
    if entry.get("doi"):
        keys.append(f"doi:{str(entry['doi']).lower()}")
    if entry.get("arxiv"):
        keys.append(f"arxiv:{str(entry['arxiv']).lower()}")
    keys.append(title_key(entry.get("title", "")))
    return keys


def entry_key(doi: str | None, arxiv: str | None, title: str) -> str:
    """Build the stable identity of a publication.

    DOI is preferred, then the arXiv id, then a normalized title — enough to
    recognise the same paper arriving from two different members' records.

    Args:
        doi: The DOI, if known.
        arxiv: The arXiv identifier, if known.
        title: The publication title.

    Returns:
        A ``"<scheme>:<value>"`` key.

    """
    if doi:
        return f"doi:{doi.lower()}"
    if arxiv:
        return f"arxiv:{arxiv.lower()}"
    return title_key(title)


def _build_entry(  # noqa: PLR0913 - one keyword per metadata field, by design
    *,
    member: Member,
    source: str,
    title: str,
    doi: str | None,
    arxiv: str | None,
    date_parts: tuple[int | None, int | None, int | None],
    authors: Sequence[str],
    author_count: int,
    venue: str,
    work_type: str,
    collaborations: Sequence[str] = (),
    url: str | None = None,
    threshold: int = COLLABORATION_THRESHOLD,
) -> dict[str, Any]:
    """Assemble a publication entry from source-agnostic metadata.

    Every source normalizer funnels through here, so the entry shape, the
    collaboration classification, and the link list stay identical no matter
    which database a record came from.

    A paper counts as a *collaboration* paper when the record names a
    collaboration, or when it has more authors than ``threshold``. That
    classification is what routes it to its own section on the site, and it is
    deliberately separate from whether the author list was collapsed for
    display.

    Returns:
        A publication entry ready to be merged into ``publications.yaml``.

    """
    year, month, day = date_parts
    rendered_authors, _ = build_authors(authors, [member], threshold=threshold)
    is_collaboration = bool(collaborations) or author_count > threshold

    links: list[dict[str, str]] = []
    if doi:
        links.append({"label": "DOI", "url": f"https://doi.org/{doi}"})
    if arxiv:
        links.append({"label": "arXiv", "url": f"https://arxiv.org/abs/{arxiv}"})
    if url and not doi:
        links.append({"label": "Link", "url": str(url)})

    entry: dict[str, Any] = {
        "include": True,
        "highlight": False,
        "title": title,
        "authors": rendered_authors,
        "venue": venue,
        "year": year,
        "date": _iso_date(year, month, day),
        "type": work_type,
        "key": entry_key(doi, arxiv, title),
        "source": source,
        "members": [member.id],
        "author_count": author_count,
        "collaboration": is_collaboration,
        "links": links,
    }
    if doi:
        entry["doi"] = doi
    if arxiv:
        entry["arxiv"] = arxiv
    if url:
        entry["url"] = str(url)
    if collaborations:
        entry["collaborations"] = list(collaborations)
    entry["_authors_raw"] = list(authors)
    return entry


def normalize_work(
    work: dict[str, Any],
    member: Member,
    *,
    crossref: dict[str, Any] | None = None,
    threshold: int = COLLABORATION_THRESHOLD,
) -> dict[str, Any]:
    """Turn one ORCID work (optionally enriched by Crossref) into an entry.

    Args:
        work: The ORCID work document.
        member: The member whose record this work came from.
        crossref: The matching Crossref record, when the work has a DOI.
        threshold: Author count above which the author list collapses.

    Returns:
        A publication entry ready to be merged into ``publications.yaml``.

    """
    ids = _external_ids(work)
    doi = ids.get("doi")
    arxiv = ids.get("arxiv")
    title = clean_text(((work.get("title") or {}).get("title") or {}).get("value") or "Untitled")
    if crossref and crossref.get("title"):
        title = clean_text(crossref["title"][0]) or title

    year, month, day = _orcid_publication_date(work)
    if year is None and crossref:
        year, month, day = _crossref_publication_date(crossref)

    authors = _crossref_authors(crossref) if crossref else []
    if not authors:
        authors = _orcid_contributors(work)

    crossref_venue = _crossref_venue(crossref) if crossref else ""
    venue = crossref_venue or _fallback_venue(work, arxiv, str(work.get("type") or ""))

    return _build_entry(
        member=member,
        source="orcid",
        title=title,
        doi=doi,
        arxiv=arxiv,
        date_parts=(year, month, day),
        authors=authors,
        author_count=len(authors),
        venue=venue,
        work_type=_publication_type(
            str(work.get("type") or ""),
            str((crossref or {}).get("type") or ""),
            arxiv=arxiv,
            venue=crossref_venue,
        ),
        collaborations=_crossref_collaborations(crossref) if crossref else (),
        url=(work.get("url") or {}).get("value"),
        threshold=threshold,
    )


def _inspire_date(metadata: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Extract (year, month, day) from an INSPIRE record, each possibly missing."""
    for info in metadata.get("publication_info") or []:
        if info.get("year"):
            return int(info["year"]), None, None
    for date_field in ("earliest_date", "preprint_date"):
        parts = str(metadata.get(date_field) or "").split("-")
        if parts and parts[0].isdigit():
            numbers = [int(part) for part in parts if part.isdigit()]
            numbers += [None, None]  # type: ignore[list-item]
            return numbers[0], numbers[1], numbers[2]
    return None, None, None


def _inspire_venue(metadata: dict[str, Any], arxiv: str | None) -> str:
    """Build a human-readable venue string from an INSPIRE record."""
    for info in metadata.get("publication_info") or []:
        journal = info.get("journal_title")
        if journal:
            venue = clean_text(journal)
            if info.get("journal_volume"):
                venue += f" {info['journal_volume']}"
            page = info.get("artid") or info.get("page_start")
            if page:
                venue += f", {page}"
            if info.get("year"):
                venue += f" ({info['year']})"
            return venue
        if info.get("pubinfo_freetext"):
            return clean_text(info["pubinfo_freetext"])
    if arxiv:
        return f"arXiv:{arxiv}"
    doc_types = metadata.get("document_type") or []
    return str(doc_types[0]).title() if doc_types else "Preprint"


def normalize_inspire_record(
    metadata: dict[str, Any], member: Member, *, threshold: int = COLLABORATION_THRESHOLD
) -> dict[str, Any]:
    """Turn one INSPIRE-HEP record into a publication entry.

    Args:
        metadata: The INSPIRE record ``metadata`` mapping.
        member: The member whose profile this record came from.
        threshold: Author count above which the author list collapses.

    Returns:
        A publication entry ready to be merged into ``publications.yaml``.

    """
    doi = (metadata.get("dois") or [{}])[0].get("value")
    arxiv = (metadata.get("arxiv_eprints") or [{}])[0].get("value")
    recid = metadata.get("control_number")
    title = clean_text((metadata.get("titles") or [{}])[0].get("title") or "Untitled")
    collaborations = [c["value"] for c in metadata.get("collaborations") or [] if c.get("value")]
    authors = [_format_inspire_name(a.get("full_name", "")) for a in metadata.get("authors") or []]
    author_count = metadata.get("author_count") or len(authors)

    venue = _inspire_venue(metadata, arxiv)
    has_journal = any(info.get("journal_title") for info in metadata.get("publication_info") or [])
    entry = _build_entry(
        member=member,
        source="inspire",
        title=title,
        doi=doi,
        arxiv=arxiv,
        date_parts=_inspire_date(metadata),
        authors=authors,
        author_count=author_count,
        venue=venue,
        work_type=_publication_type(
            _INSPIRE_TYPE_MAP.get((metadata.get("document_type") or [""])[0], ""),
            None,
            arxiv=arxiv,
            venue=venue if has_journal else "",
        ),
        collaborations=collaborations,
        threshold=threshold,
    )
    if recid:
        entry["links"].append({"label": "INSPIRE", "url": f"https://inspirehep.net/literature/{recid}"})
    return entry


def _format_inspire_name(full_name: str) -> str:
    """Convert an INSPIRE ``"Last, First"`` name to ``"First Last"``."""
    if "," in full_name:
        last, first = full_name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return full_name.strip()


def _openalex_authors(work: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Return an OpenAlex work's author names and any collaboration names."""
    names: list[str] = []
    collaborations: list[str] = []
    for authorship in work.get("authorships") or []:
        name = clean_text(
            (authorship.get("author") or {}).get("display_name") or authorship.get("raw_author_name") or ""
        )
        if not name:
            continue
        # OpenAlex records consortium authors among the authorships.
        if any(word in name.lower() for word in ("collaboration", "consortium")):
            collaborations.append(name)
        else:
            names.append(name)
    return names, collaborations


def _openalex_venue(work: dict[str, Any]) -> str:
    """Build a human-readable venue string from an OpenAlex work."""
    source = ((work.get("primary_location") or {}).get("source") or {}).get("display_name")
    if not source:
        return ""
    venue = clean_text(source)
    biblio = work.get("biblio") or {}
    if biblio.get("volume"):
        venue += f" {biblio['volume']}"
    if biblio.get("first_page"):
        venue += f", {biblio['first_page']}"
    if work.get("publication_year"):
        venue += f" ({work['publication_year']})"
    return venue


def _openalex_arxiv(work: dict[str, Any]) -> str | None:
    """Pull an arXiv identifier out of an OpenAlex work's locations, if present."""
    for location in work.get("locations") or []:
        for url in (location.get("landing_page_url"), location.get("pdf_url")):
            if url and "arxiv.org/abs/" in str(url):
                return str(url).rsplit("/abs/", 1)[-1].removesuffix(".pdf")
    return None


def normalize_openalex_work(
    work: dict[str, Any], member: Member, *, threshold: int = COLLABORATION_THRESHOLD
) -> dict[str, Any]:
    """Turn one OpenAlex work into a publication entry.

    Args:
        work: The OpenAlex work document.
        member: The member whose pinned author id this work came from.
        threshold: Author count above which the author list collapses.

    Returns:
        A publication entry ready to be merged into ``publications.yaml``.

    """
    doi = str(work.get("doi") or "").removeprefix("https://doi.org/") or None
    doi, arxiv = split_arxiv_doi(doi, _openalex_arxiv(work))
    authors, collaborations = _openalex_authors(work)
    venue = _openalex_venue(work)
    date = str(work.get("publication_date") or "")
    parts = [int(part) for part in date.split("-") if part.isdigit()] if date else []
    parts += [None, None, None]  # type: ignore[list-item]

    return _build_entry(
        member=member,
        source="openalex",
        title=clean_text(work.get("display_name") or "Untitled"),
        doi=doi,
        arxiv=arxiv,
        date_parts=(parts[0] or work.get("publication_year"), parts[1], parts[2]),
        authors=authors,
        author_count=len(authors) + len(collaborations),
        venue=venue or "Preprint",
        work_type=_publication_type(str(work.get("type") or ""), None, arxiv=arxiv, venue=venue),
        collaborations=collaborations,
        url=str(work.get("id") or "") or None,
        threshold=threshold,
    )


def _iso_date(year: int | None, month: int | None, day: int | None) -> str | None:
    """Render a possibly partial date as ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``."""
    if not year:
        return None
    if not month:
        return f"{year:04d}"
    if not day:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def _merge_group(records: Sequence[dict[str, Any]], members_by_id: dict[str, Member]) -> dict[str, Any]:
    """Fold several records of the same work into one entry.

    The record with the richest author list wins on metadata, since that is the
    one whose source knew the most about the paper. Identifiers, links, and the
    credited members are then taken from across the whole group, so nothing a
    single source knew is lost.
    """
    best = max(records, key=lambda record: len(record.get("_authors_raw") or []))
    entry = dict(best)

    for record in records:
        for member_id in record["members"]:
            if member_id not in entry["members"]:
                entry["members"].append(member_id)
        # A published record beats a preprint on identity and dating, but any
        # field the winner happens to lack is worth taking from a sibling.
        for richer in ("doi", "arxiv", "url", "venue", "year", "date"):
            if not entry.get(richer) and record.get(richer):
                entry[richer] = record[richer]
        if len(record.get("links") or []) > len(entry.get("links") or []):
            entry["links"] = record["links"]
        if record.get("collaboration"):
            entry["collaboration"] = True

    entry["key"] = entry_key(entry.get("doi"), entry.get("arxiv"), entry.get("title", ""))
    entry["members"] = sorted(entry["members"])
    credited = [members_by_id[mid] for mid in entry["members"] if mid in members_by_id]
    authors, _ = build_authors(entry.pop("_authors_raw", []) or [], credited)
    entry["authors"] = authors
    return entry


def deduplicate(entries: Sequence[dict[str, Any]], members_by_id: dict[str, Member]) -> list[dict[str, Any]]:
    """Collapse records describing the same work into one entry.

    Records are grouped transitively: two are the same work when they share
    *any* identifier — DOI, arXiv id, or normalized title. Transitivity matters
    because the link is often indirect. A preprint carrying only an arXiv id and
    a published article carrying only a journal DOI may share nothing directly,
    yet both match a third record that carries the two together.

    Args:
        entries: Normalized entries, with duplicates across members and sources.
        members_by_id: Lookup from member id to member, for re-emphasising.

    Returns:
        One entry per distinct work, in order of first appearance.

    """
    parent: dict[int, int] = {index: index for index in range(len(entries))}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Keep the earliest record as the root so input order is preserved.
            parent[max(left_root, right_root)] = min(left_root, right_root)

    owner: dict[str, int] = {}
    for index, entry in enumerate(entries):
        for key in identity_keys(entry):
            if key in owner:
                union(index, owner[key])
            else:
                owner[key] = index

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, entry in enumerate(entries):
        grouped.setdefault(find(index), []).append(entry)

    return [_merge_group(records, members_by_id) for _, records in sorted(grouped.items())]


def merge_items(
    existing: Sequence[dict[str, Any]], fetched: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], SyncSummary]:
    """Merge freshly fetched entries into the existing list, preserving curation.

    Existing managed entries are matched by ``key``: metadata is taken from the
    fetched entry while ``include``, ``highlight``, and ``note`` are carried
    over. Hand-authored entries (no ``key``) are kept untouched. A managed entry
    the sync no longer returns is dropped — unless the fetch came back empty,
    which is treated as an outage rather than a mass deletion.

    Args:
        existing: The current publication entries.
        fetched: Freshly normalized, deduplicated entries.

    Returns:
        The merged list, newest first, and a summary of what changed.

    """
    summary = SyncSummary(total_fetched=len(fetched))
    by_key = {key: entry for entry in existing if (key := entry.get("key"))}
    manual = [entry for entry in existing if not entry.get("key")]

    managed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in fetched:
        key = entry["key"]
        seen.add(key)
        prior = by_key.get(key)
        if prior is None:
            managed.append(entry)
            summary.added.append(entry["title"])
            continue
        refreshed = dict(entry)
        refreshed["include"] = prior.get("include", entry["include"])
        refreshed["highlight"] = prior.get("highlight", entry["highlight"])
        if prior.get("note"):
            refreshed["note"] = prior["note"]
        if prior.get("treat_as_collaboration") is not None:
            refreshed["treat_as_collaboration"] = prior["treat_as_collaboration"]
        if refreshed != prior:
            summary.updated += 1
        managed.append(refreshed)

    for key, entry in by_key.items():
        if key in seen:
            continue
        if fetched:
            summary.removed.append(entry.get("title", ""))
        else:
            managed.append(entry)

    merged = manual + managed
    merged.sort(
        key=lambda entry: (str(entry.get("date") or entry.get("year") or ""), entry.get("title", "")), reverse=True
    )
    return merged, summary


def _ordered(entry: dict[str, Any]) -> dict[str, Any]:
    """Order an entry's keys for stable, readable diffs."""
    ordered = {name: entry[name] for name in _FIELD_ORDER if name in entry}
    for extra, value in entry.items():  # keep hand-authored fields rather than dropping them
        if extra not in ordered and not extra.startswith("_"):
            ordered[extra] = value
    return ordered


def write_items(path: Path, items: Sequence[dict[str, Any]]) -> None:
    """Write the publication entries to ``path`` with the documented header."""
    body = yaml.safe_dump(
        {"items": [_ordered(item) for item in items]},
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=120,
    )
    path.write_text(f"{_HEADER}\n{body}", encoding="utf-8")


@dataclass(frozen=True)
class Fetchers:
    """The per-source fetch callables, gathered so tests can inject fakes."""

    orcid: Callable[[str], list[dict[str, Any]]] = fetch_orcid_works
    inspire: Callable[[str], list[dict[str, Any]]] = fetch_inspire_records
    openalex: Callable[[str], list[dict[str, Any]]] = fetch_openalex_works
    crossref: Callable[[str], dict[str, Any] | None] = fetch_crossref_work


def _collect_from_source(
    member: Member,
    source: str,
    *,
    summary: SyncSummary,
    fetchers: Fetchers,
    threshold: int,
) -> list[dict[str, Any]]:
    """Fetch and normalize one member's in-window works from one source.

    Raises:
        Exception: Whatever the fetcher raises; the caller records it as a
            per-source failure so one unreachable database cannot fail the run.

    """
    entries: list[dict[str, Any]] = []
    if source == "orcid":
        for work in fetchers.orcid(member.orcid):
            if not in_membership_window(date_span(*_orcid_publication_date(work)), member):
                summary.out_of_window += 1
                continue
            doi = _external_ids(work).get("doi")
            crossref = fetchers.crossref(doi) if doi else None
            entries.append(normalize_work(work, member, crossref=crossref, threshold=threshold))
    elif source == "inspire":
        for record in fetchers.inspire(member.inspire):
            if not in_membership_window(date_span(*_inspire_date(record)), member):
                summary.out_of_window += 1
                continue
            entries.append(normalize_inspire_record(record, member, threshold=threshold))
    elif source == "openalex":
        for work in fetchers.openalex(member.openalex):
            entry = normalize_openalex_work(work, member, threshold=threshold)
            if not in_membership_window(date_span(*_split_iso(entry.get("date"))), member):
                summary.out_of_window += 1
                continue
            entries.append(entry)

    kept = [entry for entry in entries if entry["type"] not in EXCLUDED_PUBLICATION_TYPES]
    summary.excluded += len(entries) - len(kept)

    if source in _AUTO_ASSIGNING_SOURCES:
        credited = [entry for entry in kept if _plausibly_theirs(entry, member, threshold)]
        summary.misattributed += len(kept) - len(credited)
        return credited
    return kept


# ORCID is self-asserted, so whatever it lists is the member's own claim.
# INSPIRE and OpenAlex assign papers to profiles automatically, which keeps them
# current without upkeep but also mis-files work by same-surname researchers.
_AUTO_ASSIGNING_SOURCES = frozenset({"inspire", "openalex"})


def _plausibly_theirs(entry: dict[str, Any], member: Member, threshold: int) -> bool:
    """Whether an automatically assigned record really names the member.

    Applied only to short author lists, where the full set of authors is known
    and the member must therefore appear among them. Large-collaboration papers
    are exempt: their author lists are long, sometimes truncated upstream, and
    a member genuinely may not be listed individually.
    """
    authors = entry.get("_authors_raw") or []
    if entry.get("collaboration") or entry.get("author_count", 0) > threshold or not authors:
        return True
    return member_appears(authors, member)


def _split_iso(value: str | None) -> tuple[int | None, int | None, int | None]:
    """Split a ``YYYY[-MM[-DD]]`` string back into its numeric parts."""
    parts = [int(part) for part in str(value or "").split("-") if part.isdigit()]
    parts += [None, None, None]  # type: ignore[list-item]
    return parts[0], parts[1], parts[2]


def collect_entries(
    members: Sequence[Member],
    *,
    summary: SyncSummary,
    fetchers: Fetchers | None = None,
    threshold: int = COLLABORATION_THRESHOLD,
) -> list[dict[str, Any]]:
    """Fetch and normalize every in-window work for the given members.

    Each member is queried on every source they carry an identifier for, and
    the results are unioned: no single database is complete, and the ones that
    stay current without upkeep are not the ones with the widest subject
    coverage. Cross-source duplicates are expected and are resolved later by
    :func:`deduplicate`, which already handles the same paper reaching the sync
    from several members.

    A source that cannot be reached is recorded in ``summary`` and skipped, so
    one outage never fails the whole run or silently empties the list.

    Args:
        members: The members to fetch works for.
        summary: Summary object updated in place with counts and failures.
        fetchers: The per-source fetch callables (injectable for tests).
        threshold: Author count above which author lists collapse.

    Returns:
        Normalized entries, still containing duplicates across members and
        across sources.

    """
    fetchers = fetchers or Fetchers()
    entries: list[dict[str, Any]] = []
    for member in members:
        fetched_any = False
        for source in member.sources:
            try:
                entries.extend(
                    _collect_from_source(member, source, summary=summary, fetchers=fetchers, threshold=threshold)
                )
            except Exception as exc:  # noqa: BLE001 - one bad source must not fail the run
                summary.failures.append(f"{member.name} via {source}: {exc}")
                continue
            fetched_any = True
            summary.source_counts[source] = summary.source_counts.get(source, 0) + 1
        if fetched_any:
            summary.members_synced += 1
    return entries


def sync_publications(
    publications_path: Path,
    people_path: Path,
    *,
    fetchers: Fetchers | None = None,
    threshold: int = COLLABORATION_THRESHOLD,
) -> SyncSummary:
    """Refresh ``publications.yaml`` from every source the members carry.

    Args:
        publications_path: Path to ``content/publications.yaml``.
        people_path: Path to ``content/people.yaml``.
        fetchers: The per-source fetch callables (injectable for tests).
        threshold: Author count above which author lists collapse.

    Returns:
        A summary of the changes made.

    Raises:
        LookupError: If no person has both a start date and an identifier.

    """
    members = members_from_people(load_yaml(people_path) if people_path.exists() else None)
    if not members:
        raise LookupError(
            f"No syncable members in {people_path}: each person needs a `start` date "
            "and at least one of `orcid`, `inspire`, or `openalex`."
        )

    summary = SyncSummary()
    raw = collect_entries(members, summary=summary, fetchers=fetchers, threshold=threshold)
    members_by_id = {member.id: member for member in members}
    fetched = deduplicate(raw, members_by_id)
    summary.deduplicated = len(raw) - len(fetched)

    existing_doc = load_yaml(publications_path) if publications_path.exists() else None
    existing = (existing_doc or {}).get("items") or []

    merged, merge_summary = merge_items(existing, fetched)
    summary.total_fetched = merge_summary.total_fetched
    summary.added = merge_summary.added
    summary.removed = merge_summary.removed
    summary.updated = merge_summary.updated
    summary.collaboration = sum(1 for entry in merged if is_collaboration(entry))

    write_items(publications_path, merged)
    return summary
