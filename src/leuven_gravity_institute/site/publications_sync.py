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
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from leuven_gravity_institute.site.content import load_yaml
from leuven_gravity_institute.site.orcid import fetch_crossref_work, fetch_orcid_works

# Above this many authors a paper is rendered as "First Author et al.", with the
# group's own members named, rather than listing hundreds of collaborators.
COLLABORATION_THRESHOLD = 15

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
    "software": "software",
    "report": "report",
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
    "note",
    "links",
]


@dataclass(frozen=True)
class Member:
    """A group member whose ORCID record contributes to the publication list."""

    id: str
    name: str
    orcid: str
    start: date
    end: date | None = None

    @property
    def family(self) -> str:
        """The member's family name, used to emphasise them in author lists."""
        return _fold(self.name.split()[-1]) if self.name.split() else ""

    @property
    def initial(self) -> str:
        """The first letter of the member's given name."""
        parts = self.name.split()
        return _fold(parts[0][:1]) if len(parts) > 1 else ""


@dataclass
class SyncSummary:
    """Outcome of a sync, for reporting to the CLI and the pull request."""

    members_synced: int = 0
    total_fetched: int = 0
    out_of_window: int = 0
    deduplicated: int = 0
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    updated: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether the merge produced any additions, removals, or updates."""
        return bool(self.added) or bool(self.removed) or self.updated > 0


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

    People without an ORCID iD, or without a ``start`` date, contribute nothing
    to the publication list and are skipped.

    Args:
        people: The parsed ``people.yaml`` document.

    Returns:
        One :class:`Member` per person with a usable ORCID iD and start date.

    """
    members: list[Member] = []
    for person in (people or {}).get("items") or []:
        orcid = (person.get("orcid") or "").strip()
        start = parse_date(person.get("start"))
        if not orcid or start is None:
            continue
        members.append(
            Member(
                id=person["id"],
                name=person.get("name", ""),
                orcid=orcid,
                start=start,
                end=parse_date(person.get("end")),
            )
        )
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


def _crossref_venue(message: dict[str, Any]) -> str:
    """Build a human-readable venue string from a Crossref record."""
    titles = message.get("container-title") or []
    journal = str(titles[0]) if titles else ""
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
        return str(journal)
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
    targets = [(m.family, m.initial) for m in members if m.family]
    rendered: list[str] = []
    for author in authors:
        parts = author.split()
        family = _fold(parts[-1]) if parts else ""
        initial = _fold(parts[0][:1]) if len(parts) > 1 else ""
        matched = any(family == fam and (not ini or not initial or initial == ini) for fam, ini in targets)
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
    if len(authors) <= threshold:
        return emphasised, False
    lead = emphasised[0]
    named = [name for name in emphasised if name.startswith("**")]
    incl = f" (incl. {', '.join(named)})" if named and lead not in named else ""
    return [f"{lead} et al.{incl}"], True


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
    return f"title:{re.sub(r'[^a-z0-9]+', '-', _fold(title)).strip('-')}"


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
    title = str(((work.get("title") or {}).get("title") or {}).get("value") or "Untitled").strip()
    if crossref and crossref.get("title"):
        title = str(crossref["title"][0]).strip() or title

    year, month, day = _orcid_publication_date(work)
    if year is None and crossref:
        year, month, day = _crossref_publication_date(crossref)

    authors = _crossref_authors(crossref) if crossref else []
    if not authors:
        authors = _orcid_contributors(work)
    author_count = len(authors)
    rendered_authors, collapsed = build_authors(authors, [member], threshold=threshold)

    venue = _crossref_venue(crossref) if crossref else ""
    if not venue:
        venue = _fallback_venue(work, arxiv, str(work.get("type") or ""))

    url = (work.get("url") or {}).get("value")
    links = []
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
        "type": _publication_type(
            str(work.get("type") or ""),
            str((crossref or {}).get("type") or ""),
            arxiv=arxiv,
            venue=_crossref_venue(crossref) if crossref else "",
        ),
        "key": entry_key(doi, arxiv, title),
        "source": "orcid",
        "members": [member.id],
        "author_count": author_count,
        "collaboration": collapsed,
        "links": links,
    }
    if doi:
        entry["doi"] = doi
    if arxiv:
        entry["arxiv"] = arxiv
    if url:
        entry["url"] = str(url)
    entry["_authors_raw"] = list(authors)
    return entry


def _iso_date(year: int | None, month: int | None, day: int | None) -> str | None:
    """Render a possibly partial date as ``YYYY``, ``YYYY-MM``, or ``YYYY-MM-DD``."""
    if not year:
        return None
    if not month:
        return f"{year:04d}"
    if not day:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}-{month:02d}-{day:02d}"


def deduplicate(entries: Sequence[dict[str, Any]], members_by_id: dict[str, Member]) -> list[dict[str, Any]]:
    """Collapse entries describing the same work into one, merging members.

    The first entry seen for a key wins on metadata, except that a later entry
    carrying a richer author list or a DOI upgrades it. Every contributing
    member is recorded under ``members``, and the author list is re-rendered so
    all of them are emphasised.

    Args:
        entries: Normalized entries, possibly with duplicates across members.
        members_by_id: Lookup from member id to member, for re-emphasising.

    Returns:
        One entry per distinct work, in input order.

    """
    merged: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = entry["key"]
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(entry)
            continue
        for member_id in entry["members"]:
            if member_id not in existing["members"]:
                existing["members"].append(member_id)
        if len(entry.get("_authors_raw") or []) > len(existing.get("_authors_raw") or []):
            existing["_authors_raw"] = entry["_authors_raw"]
            existing["author_count"] = entry["author_count"]
        for richer in ("doi", "arxiv", "url", "venue", "date", "year"):
            if not existing.get(richer) and entry.get(richer):
                existing[richer] = entry[richer]
        if len(entry.get("links") or []) > len(existing.get("links") or []):
            existing["links"] = entry["links"]

    result: list[dict[str, Any]] = []
    for entry in merged.values():
        entry["members"] = sorted(entry["members"])
        credited = [members_by_id[mid] for mid in entry["members"] if mid in members_by_id]
        authors, collapsed = build_authors(entry.pop("_authors_raw", []) or [], credited)
        entry["authors"] = authors
        entry["collaboration"] = collapsed
        result.append(entry)
    return result


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


def collect_entries(
    members: Sequence[Member],
    *,
    summary: SyncSummary,
    works_fetcher: Callable[[str], list[dict[str, Any]]] = fetch_orcid_works,
    crossref_fetcher: Callable[[str], dict[str, Any] | None] = fetch_crossref_work,
    threshold: int = COLLABORATION_THRESHOLD,
) -> list[dict[str, Any]]:
    """Fetch and normalize every in-window work for the given members.

    A member whose ORCID record cannot be fetched is recorded in ``summary``
    and skipped, so one unreachable record never fails the whole sync.

    Args:
        members: The members to fetch works for.
        summary: Summary object updated in place with counts and failures.
        works_fetcher: Callable ``(orcid) -> works`` (injectable for tests).
        crossref_fetcher: Callable ``(doi) -> message | None`` (injectable for tests).
        threshold: Author count above which author lists collapse.

    Returns:
        Normalized entries, still containing cross-member duplicates.

    """
    entries: list[dict[str, Any]] = []
    for member in members:
        try:
            works = works_fetcher(member.orcid)
        except Exception as exc:  # noqa: BLE001 - one bad record must not fail the run
            summary.failures.append(f"{member.name} ({member.orcid}): {exc}")
            continue
        summary.members_synced += 1
        for work in works:
            year, month, day = _orcid_publication_date(work)
            if not in_membership_window(date_span(year, month, day), member):
                summary.out_of_window += 1
                continue
            doi = _external_ids(work).get("doi")
            crossref = crossref_fetcher(doi) if doi else None
            entries.append(normalize_work(work, member, crossref=crossref, threshold=threshold))
    return entries


def sync_publications(
    publications_path: Path,
    people_path: Path,
    *,
    works_fetcher: Callable[[str], list[dict[str, Any]]] = fetch_orcid_works,
    crossref_fetcher: Callable[[str], dict[str, Any] | None] = fetch_crossref_work,
    threshold: int = COLLABORATION_THRESHOLD,
) -> SyncSummary:
    """Refresh ``publications.yaml`` from the members' ORCID records.

    Args:
        publications_path: Path to ``content/publications.yaml``.
        people_path: Path to ``content/people.yaml``.
        works_fetcher: Callable ``(orcid) -> works`` (injectable for tests).
        crossref_fetcher: Callable ``(doi) -> message | None`` (injectable for tests).
        threshold: Author count above which author lists collapse.

    Returns:
        A summary of the changes made.

    Raises:
        LookupError: If no person has both an ORCID iD and a start date.

    """
    members = members_from_people(load_yaml(people_path) if people_path.exists() else None)
    if not members:
        raise LookupError(f"No syncable members in {people_path}: each person needs an `orcid` iD and a `start` date.")

    summary = SyncSummary()
    raw = collect_entries(
        members,
        summary=summary,
        works_fetcher=works_fetcher,
        crossref_fetcher=crossref_fetcher,
        threshold=threshold,
    )
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

    write_items(publications_path, merged)
    return summary
