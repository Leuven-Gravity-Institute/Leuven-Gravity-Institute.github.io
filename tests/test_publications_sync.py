"""Tests for the ORCID-backed publication sync.

The network is never touched: ORCID and Crossref responses are injected as
fakes, so every rule the sync enforces — membership windows, cross-member
deduplication, author emphasis, and curation preservation — is exercised
offline.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from leuven_gravity_institute.site.publications_sync import (
    Fetchers,
    Member,
    SyncSummary,
    build_authors,
    clean_text,
    collect_entries,
    date_span,
    deduplicate,
    emphasise_members,
    entry_key,
    identity_keys,
    in_membership_window,
    is_collaboration,
    members_from_people,
    merge_items,
    normalize_inspire_record,
    normalize_openalex_work,
    normalize_work,
    parse_date,
    split_arxiv_doi,
    sync_publications,
    write_items,
)

MEMBER = Member(id="isaac-wong", name="Isaac Wong", orcid="0000-0003-2166-0027", start=date(2024, 9, 1))
OTHER = Member(id="jane-doe", name="Jane Doe", orcid="0000-0002-1825-0097", start=date(2024, 1, 1))


def orcid_work(
    *,
    title: str = "A paper",
    doi: str | None = "10.1000/abc",
    arxiv: str | None = None,
    year: int | None = 2025,
    month: int | None = 3,
    day: int | None = 4,
    work_type: str = "journal-article",
) -> dict[str, Any]:
    """Build a minimal ORCID work document."""
    ids = []
    if doi:
        ids.append({"external-id-type": "doi", "external-id-value": doi})
    if arxiv:
        ids.append({"external-id-type": "arxiv", "external-id-value": arxiv})
    published: dict[str, Any] = {}
    if year:
        published["year"] = {"value": str(year)}
    if month:
        published["month"] = {"value": f"{month:02d}"}
    if day:
        published["day"] = {"value": f"{day:02d}"}
    return {
        "title": {"title": {"value": title}},
        "external-ids": {"external-id": ids},
        "publication-date": published or None,
        "type": work_type,
        "journal-title": {"value": "Some Journal"},
    }


def crossref_message(authors: list[dict[str, str]], *, title: str = "A paper") -> dict[str, Any]:
    """Build a minimal Crossref message."""
    return {
        "title": [title],
        "author": authors,
        "container-title": ["Physical Review D"],
        "volume": "111",
        "article-number": "022001",
        "issued": {"date-parts": [[2025, 3, 4]]},
        "type": "journal-article",
    }


class TestDates:
    """Parsing and window logic for partial publication dates."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("2024", date(2024, 1, 1)), ("2024-05", date(2024, 5, 1)), ("2024-05-06", date(2024, 5, 6))],
    )
    def test_parse_date_accepts_partial_values(self, value: str, expected: date) -> None:
        assert parse_date(value) == expected

    def test_parse_date_rejects_nonsense(self) -> None:
        assert parse_date("not-a-date") is None
        assert parse_date(None) is None

    def test_date_span_expands_partial_dates_to_their_period(self) -> None:
        assert date_span(2024, None, None) == (date(2024, 1, 1), date(2024, 12, 31))
        assert date_span(2024, 2, None) == (date(2024, 2, 1), date(2024, 2, 29))
        assert date_span(2024, 2, 5) == (date(2024, 2, 5), date(2024, 2, 5))
        assert date_span(None, None, None) is None

    def test_work_before_joining_is_out_of_window(self) -> None:
        assert not in_membership_window(date_span(2024, 3, 1), MEMBER)

    def test_work_during_membership_is_in_window(self) -> None:
        assert in_membership_window(date_span(2025, 1, 1), MEMBER)

    def test_work_after_leaving_is_out_of_window(self) -> None:
        leaver = Member(id="x", name="X Y", orcid="0", start=date(2024, 1, 1), end=date(2024, 12, 31))
        assert not in_membership_window(date_span(2025, 6, 1), leaver)
        assert in_membership_window(date_span(2024, 6, 1), leaver)

    def test_year_only_date_counts_when_the_year_straddles_the_join_date(self) -> None:
        # Joined 2024-09-01; a work dated only "2024" could fall either side, and
        # is kept rather than silently dropped.
        assert in_membership_window(date_span(2024, None, None), MEMBER)

    def test_undated_work_is_excluded(self) -> None:
        assert not in_membership_window(None, MEMBER)


class TestMembers:
    """Deriving syncable members from people.yaml."""

    def test_only_people_with_orcid_and_start_are_synced(self) -> None:
        people = {
            "items": [
                {"id": "a", "name": "A B", "orcid": "0000-0001-0000-0000", "start": "2024-01-01"},
                {"id": "b", "name": "C D"},  # no ORCID
                {"id": "c", "name": "E F", "orcid": "0000-0002-0000-0000"},  # no start date
            ]
        }
        members = members_from_people(people)
        assert [member.id for member in members] == ["a"]
        assert members[0].start == date(2024, 1, 1)
        assert members[0].end is None

    def test_family_and_given_names_come_from_the_display_name(self) -> None:
        assert MEMBER.family == "wong"
        assert MEMBER.given == "isaac"


class TestCleanText:
    """Flattening upstream markup, which templates would otherwise escape."""

    def test_presentation_markup_is_stripped_and_whitespace_collapsed(self) -> None:
        raw = "GW231123: Total Mass 190-265\n                    <i>M</i>\n                    <sub>&#8857;</sub>"
        assert clean_text(raw) == "GW231123: Total Mass 190-265 M \u2299"

    def test_entities_are_resolved(self) -> None:
        assert clean_text("Nuclear R&amp;D") == "Nuclear R&D"

    def test_plain_titles_pass_through_unchanged(self) -> None:
        assert clean_text("A perfectly ordinary title") == "A perfectly ordinary title"


class TestAuthors:
    """Rendering author lists."""

    def test_members_are_emphasised_by_family_name_and_initial(self) -> None:
        rendered = emphasise_members(["Isaac Wong", "Someone Else"], [MEMBER])
        assert rendered == ["**Isaac Wong**", "Someone Else"]

    def test_a_different_person_with_the_same_surname_is_not_emphasised(self) -> None:
        rendered = emphasise_members(["Brian Wong"], [MEMBER])
        assert rendered == ["Brian Wong"]

    def test_a_shared_surname_and_initial_is_not_enough_to_match(self) -> None:
        # "Tie-Fu Li" and "Tjonnie G. F. Li" share a surname and a first
        # initial but are different researchers; matching on the initial alone
        # put another Li's quantum-computing papers on this group's page.
        li = Member(id="tjonnie-li", name="Tjonnie G. F. Li", start=date(2021, 10, 1), orcid="x")
        assert emphasise_members(["Tie-Fu Li"], [li]) == ["Tie-Fu Li"]
        assert emphasise_members(["Tjonnie Guang Feng Li"], [li]) == ["**Tjonnie Guang Feng Li**"]

    def test_an_initial_still_matches_the_name_it_abbreviates(self) -> None:
        assert emphasise_members(["I. C. F. Wong"], [MEMBER]) == ["**I. C. F. Wong**"]

    def test_accented_spellings_still_match(self) -> None:
        member = Member(id="x", name="Renée Müller", orcid="0", start=date(2024, 1, 1))
        assert emphasise_members(["Renee Muller"], [member]) == ["**Renee Muller**"]

    def test_long_author_lists_collapse_to_et_al_naming_members(self) -> None:
        authors = [f"Author {index}" for index in range(30)] + ["Isaac Wong"]
        rendered, collapsed = build_authors(authors, [MEMBER], threshold=15)
        assert collapsed is True
        assert rendered == ["Author 0 et al. (incl. **Isaac Wong**)"]

    def test_short_author_lists_are_listed_in_full(self) -> None:
        rendered, collapsed = build_authors(["Isaac Wong", "Jane Doe"], [MEMBER, OTHER])
        assert collapsed is False
        assert rendered == ["**Isaac Wong**", "**Jane Doe**"]

    def test_a_collaboration_listed_as_one_author_still_names_the_members(self) -> None:
        # Crossref records some collaboration papers with a single "author"
        # entry, so no member name appears and the list never reaches the
        # collapse threshold; the entry must still show who is credited.
        rendered, collapsed = build_authors(["Virgo Collaboration"], [MEMBER])
        assert rendered == ["Virgo Collaboration (incl. **Isaac Wong**)"]
        assert collapsed is False

    def test_several_members_are_all_named_on_such_a_paper(self) -> None:
        rendered, _ = build_authors(["LIGO Scientific Collaboration"], [MEMBER, OTHER])
        assert rendered == ["LIGO Scientific Collaboration (incl. **Isaac Wong**, **Jane Doe**)"]

    def test_a_long_list_that_names_no_member_still_credits_them(self) -> None:
        authors = [f"Author {index}" for index in range(30)]
        rendered, collapsed = build_authors(authors, [MEMBER], threshold=15)
        assert rendered == ["Author 0 et al. (incl. **Isaac Wong**)"]
        assert collapsed is True

    def test_missing_author_list_falls_back_to_the_credited_members(self) -> None:
        rendered, collapsed = build_authors([], [MEMBER])
        assert rendered == ["**Isaac Wong**"]
        assert collapsed is False


class TestNormalize:
    """Turning ORCID/Crossref records into publication entries."""

    def test_crossref_metadata_wins_over_the_sparser_orcid_record(self) -> None:
        entry = normalize_work(
            orcid_work(),
            MEMBER,
            crossref=crossref_message([{"given": "Isaac", "family": "Wong"}, {"given": "Jane", "family": "Doe"}]),
        )
        assert entry["venue"] == "Physical Review D 111, 022001 (2025)"
        assert entry["authors"] == ["**Isaac Wong**", "Jane Doe"]
        assert entry["type"] == "journal"
        assert entry["date"] == "2025-03-04"
        assert entry["members"] == ["isaac-wong"]

    def test_falls_back_to_orcid_metadata_when_crossref_is_unavailable(self) -> None:
        entry = normalize_work(orcid_work(), MEMBER, crossref=None)
        assert entry["venue"] == "Some Journal"
        assert entry["authors"] == ["**Isaac Wong**"]

    def test_arxiv_only_work_is_typed_as_a_preprint(self) -> None:
        work = orcid_work(doi=None, arxiv="2501.00001", work_type="preprint")
        work["journal-title"] = None
        entry = normalize_work(work, MEMBER)
        assert entry["type"] == "preprint"
        assert entry["venue"] == "arXiv:2501.00001"
        assert entry["key"] == "arxiv:2501.00001"
        assert {link["label"] for link in entry["links"]} == {"arXiv"}


class TestKeysAndDeduplication:
    """The identity rules that make cross-member deduplication work."""

    def test_key_prefers_doi_then_arxiv_then_title(self) -> None:
        assert entry_key("10.1000/ABC", "2501.1", "T") == "doi:10.1000/abc"
        assert entry_key(None, "2501.1", "T") == "arxiv:2501.1"
        assert entry_key(None, None, "A Nice Paper!") == "title:a-nice-paper"

    def test_same_paper_from_two_members_becomes_one_entry(self) -> None:
        crossref = crossref_message([{"given": "Isaac", "family": "Wong"}, {"given": "Jane", "family": "Doe"}])
        entries = [
            normalize_work(orcid_work(), MEMBER, crossref=crossref),
            normalize_work(orcid_work(), OTHER, crossref=crossref),
        ]
        merged = deduplicate(entries, {MEMBER.id: MEMBER, OTHER.id: OTHER})
        assert len(merged) == 1
        assert merged[0]["members"] == ["isaac-wong", "jane-doe"]
        # Both members are emphasised once the entry knows about both of them.
        assert merged[0]["authors"] == ["**Isaac Wong**", "**Jane Doe**"]

    def test_deduplication_keeps_the_richer_metadata(self) -> None:
        sparse = normalize_work(orcid_work(doi=None, arxiv="2501.1"), MEMBER, crossref=None)
        rich = normalize_work(
            orcid_work(doi=None, arxiv="2501.1"),
            OTHER,
            crossref=crossref_message([{"given": "Jane", "family": "Doe"}, {"given": "Isaac", "family": "Wong"}]),
        )
        merged = deduplicate([sparse, rich], {MEMBER.id: MEMBER, OTHER.id: OTHER})
        assert len(merged) == 1
        assert merged[0]["author_count"] == 2

    def test_differently_cased_dois_are_the_same_paper(self) -> None:
        first = normalize_work(orcid_work(doi="10.1000/ABC"), MEMBER)
        second = normalize_work(orcid_work(doi="10.1000/abc"), OTHER)
        assert len(deduplicate([first, second], {MEMBER.id: MEMBER, OTHER.id: OTHER})) == 1


class TestMerge:
    """Preserving human curation across syncs."""

    def test_curation_survives_a_refresh(self) -> None:
        existing = [
            {
                "key": "doi:10.1000/abc",
                "title": "Old title",
                "include": False,
                "highlight": True,
                "note": "Kept by hand.",
            }
        ]
        fetched = [{"key": "doi:10.1000/abc", "title": "New title", "include": True, "highlight": False}]
        merged, summary = merge_items(existing, fetched)
        assert merged[0]["title"] == "New title"
        assert merged[0]["include"] is False
        assert merged[0]["highlight"] is True
        assert merged[0]["note"] == "Kept by hand."
        assert summary.updated == 1

    def test_hand_authored_entries_are_never_touched(self) -> None:
        existing = [{"title": "By hand", "include": True}]
        merged, summary = merge_items(existing, [{"key": "doi:1", "title": "New", "include": True, "highlight": False}])
        assert {"title": "By hand", "include": True} in merged
        assert summary.removed == []

    def test_entries_no_longer_returned_are_dropped(self) -> None:
        existing = [{"key": "doi:gone", "title": "Gone"}]
        merged, summary = merge_items(existing, [{"key": "doi:1", "title": "New", "include": True, "highlight": False}])
        assert [entry["key"] for entry in merged] == ["doi:1"]
        assert summary.removed == ["Gone"]

    def test_an_empty_fetch_is_treated_as_an_outage_not_a_deletion(self) -> None:
        existing = [{"key": "doi:kept", "title": "Kept"}]
        merged, summary = merge_items(existing, [])
        assert [entry["key"] for entry in merged] == ["doi:kept"]
        assert summary.removed == []


class TestSyncEndToEnd:
    """The full sync, with the network stubbed out."""

    @pytest.fixture
    def people_file(self, tmp_path: Path) -> Path:
        path = tmp_path / "people.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "items": [
                        {
                            "id": "isaac-wong",
                            "name": "Isaac Wong",
                            "orcid": "0000-0003-2166-0027",
                            "start": "2024-09-01",
                        },
                        {
                            "id": "jane-doe",
                            "name": "Jane Doe",
                            "orcid": "0000-0002-1825-0097",
                            "start": "2024-01-01",
                            "end": "2025-01-31",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_sync_filters_by_window_and_deduplicates(self, tmp_path: Path, people_file: Path) -> None:
        shared = orcid_work(title="Shared paper", doi="10.1000/shared", year=2024, month=11, day=1)
        too_early = orcid_work(title="Before joining", doi="10.1000/early", year=2023, month=5, day=1)
        after_leaving = orcid_work(title="After leaving", doi="10.1000/late", year=2025, month=6, day=1)

        works = {
            "0000-0003-2166-0027": [shared, too_early],
            "0000-0002-1825-0097": [shared, after_leaving],
        }
        crossref = crossref_message(
            [{"given": "Isaac", "family": "Wong"}, {"given": "Jane", "family": "Doe"}], title="Shared paper"
        )

        publications = tmp_path / "publications.yaml"
        summary = sync_publications(
            publications,
            people_file,
            fetchers=Fetchers(
                orcid=lambda orcid: works[orcid],
                crossref=lambda doi: crossref if doi == "10.1000/shared" else None,
            ),
        )

        items = yaml.safe_load(publications.read_text(encoding="utf-8"))["items"]
        assert [item["title"] for item in items] == ["Shared paper"]
        assert items[0]["members"] == ["isaac-wong", "jane-doe"]
        assert summary.members_synced == 2
        # "After leaving" is in Isaac's window but not on his record, and out of
        # Jane's window; "Before joining" predates Isaac's start.
        assert summary.out_of_window == 2
        assert summary.deduplicated == 1

    def test_one_unreachable_record_does_not_fail_the_run(self, tmp_path: Path, people_file: Path) -> None:
        def fetcher(orcid: str) -> list[dict[str, Any]]:
            if orcid == "0000-0002-1825-0097":
                raise TimeoutError("ORCID unreachable")
            return [orcid_work(title="Still here", year=2024, month=11, day=1)]

        publications = tmp_path / "publications.yaml"
        summary = sync_publications(
            publications, people_file, fetchers=Fetchers(orcid=fetcher, crossref=lambda doi: None)
        )

        assert summary.members_synced == 1
        assert len(summary.failures) == 1
        assert "Jane Doe" in summary.failures[0]
        assert [item["title"] for item in yaml.safe_load(publications.read_text())["items"]] == ["Still here"]

    def test_sync_refuses_to_run_without_syncable_members(self, tmp_path: Path) -> None:
        people = tmp_path / "people.yaml"
        people.write_text(yaml.safe_dump({"items": [{"id": "a", "name": "A B"}]}), encoding="utf-8")
        with pytest.raises(LookupError, match="No syncable members"):
            sync_publications(tmp_path / "publications.yaml", people)

    def test_written_file_keeps_the_documented_header_and_field_order(self, tmp_path: Path) -> None:
        path = tmp_path / "publications.yaml"
        write_items(path, [normalize_work(orcid_work(), MEMBER)])
        text = path.read_text(encoding="utf-8")
        assert text.startswith("# Publications of the group — GENERATED FILE.")
        assert text.index("include:") < text.index("title:") < text.index("members:")
        # Internal bookkeeping never reaches the file.
        assert "_authors_raw" not in text


def inspire_record(
    *,
    title: str = "A paper",
    doi: str | None = "10.1000/abc",
    arxiv: str | None = "2501.00001",
    year: int = 2025,
    authors: list[str] | None = None,
    author_count: int | None = None,
    collaborations: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal INSPIRE record metadata mapping."""
    names = authors if authors is not None else ["Wong, Isaac", "Doe, Jane"]
    return {
        "titles": [{"title": title}],
        "dois": [{"value": doi}] if doi else [],
        "arxiv_eprints": [{"value": arxiv}] if arxiv else [],
        "authors": [{"full_name": name} for name in names],
        "author_count": author_count if author_count is not None else len(names),
        "collaborations": [{"value": name} for name in collaborations or []],
        "publication_info": [{"journal_title": "Phys.Rev.D", "journal_volume": "111", "artid": "022001", "year": year}],
        "document_type": ["article"],
        "earliest_date": f"{year}-03-04",
        "control_number": 1234567,
    }


def openalex_work(
    *,
    title: str = "A paper",
    doi: str | None = "10.1000/abc",
    date: str = "2025-03-04",
    authors: list[str] | None = None,
) -> dict[str, Any]:
    """Build a minimal OpenAlex work document."""
    names = authors if authors is not None else ["Isaac Wong", "Jane Doe"]
    return {
        "id": "https://openalex.org/W123",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "display_name": title,
        "publication_date": date,
        "publication_year": int(date[:4]),
        "type": "article",
        "authorships": [{"author": {"display_name": name}} for name in names],
        "primary_location": {"source": {"display_name": "Physical Review D"}},
        "biblio": {"volume": "111", "first_page": "022001"},
        "locations": [],
    }


class TestOtherSources:
    """Normalizing INSPIRE and OpenAlex records into the common entry shape."""

    def test_inspire_record_becomes_an_entry(self) -> None:
        entry = normalize_inspire_record(inspire_record(), MEMBER)
        assert entry["source"] == "inspire"
        assert entry["title"] == "A paper"
        assert entry["authors"] == ["**Isaac Wong**", "Jane Doe"]
        assert entry["venue"] == "Phys.Rev.D 111, 022001 (2025)"
        assert entry["date"] == "2025"
        assert {link["label"] for link in entry["links"]} == {"DOI", "arXiv", "INSPIRE"}

    def test_openalex_work_becomes_an_entry(self) -> None:
        entry = normalize_openalex_work(openalex_work(), MEMBER)
        assert entry["source"] == "openalex"
        assert entry["authors"] == ["**Isaac Wong**", "Jane Doe"]
        assert entry["venue"] == "Physical Review D 111, 022001 (2025)"
        assert entry["date"] == "2025-03-04"

    def test_the_same_paper_from_three_sources_is_one_entry(self) -> None:
        # The whole point of unioning sources: overlap is the normal case.
        entries = [
            normalize_work(orcid_work(), MEMBER, crossref=crossref_message([{"given": "Isaac", "family": "Wong"}])),
            normalize_inspire_record(inspire_record(), MEMBER),
            normalize_openalex_work(openalex_work(), MEMBER),
        ]
        merged = deduplicate(entries, {MEMBER.id: MEMBER})
        assert len(merged) == 1
        assert merged[0]["key"] == "doi:10.1000/abc"


class TestCollaborationClassification:
    """Which papers are routed to the separate collaboration section."""

    def test_a_named_collaboration_marks_the_paper_regardless_of_author_count(self) -> None:
        entry = normalize_inspire_record(
            inspire_record(authors=["Wong, Isaac"], collaborations=["LIGO Scientific Collaboration"]), MEMBER
        )
        assert entry["collaboration"] is True
        assert entry["collaborations"] == ["LIGO Scientific Collaboration"]

    def test_a_very_long_author_list_marks_the_paper(self) -> None:
        names = [f"Author{index}, A" for index in range(40)]
        entry = normalize_inspire_record(inspire_record(authors=names), MEMBER, threshold=15)
        assert entry["collaboration"] is True

    def test_an_ordinary_paper_is_not_marked(self) -> None:
        entry = normalize_inspire_record(inspire_record(authors=["Wong, Isaac", "Doe, Jane"]), MEMBER)
        assert entry["collaboration"] is False


class TestMultiSourceCollection:
    """Querying every source a member carries, and surviving one being down."""

    MEMBER_ALL = Member(
        id="isaac-wong",
        name="Isaac Wong",
        start=date(2024, 1, 1),
        orcid="0000-0003-2166-0027",
        inspire="Isaac.C.F.Wong.1",
        openalex="A5033794708",
    )

    def test_sources_lists_only_configured_identifiers(self) -> None:
        assert self.MEMBER_ALL.sources == ["orcid", "inspire", "openalex"]
        assert Member(id="x", name="X Y", start=date(2024, 1, 1), inspire="X.Y.1").sources == ["inspire"]

    def test_every_configured_source_is_queried(self) -> None:
        summary = SyncSummary()
        entries = collect_entries(
            [self.MEMBER_ALL],
            summary=summary,
            fetchers=Fetchers(
                orcid=lambda _: [orcid_work(title="from orcid", doi="10.1/a", year=2025, month=1, day=1)],
                inspire=lambda _: [inspire_record(title="from inspire", doi="10.1/b")],
                openalex=lambda _: [openalex_work(title="from openalex", doi="10.1/c")],
                crossref=lambda _: None,
            ),
        )
        assert {entry["source"] for entry in entries} == {"orcid", "inspire", "openalex"}
        assert summary.source_counts == {"orcid": 1, "inspire": 1, "openalex": 1}
        assert summary.members_synced == 1

    def test_one_source_failing_does_not_lose_the_others(self) -> None:
        def broken(_: str) -> list[dict[str, Any]]:
            raise TimeoutError("INSPIRE unreachable")

        summary = SyncSummary()
        entries = collect_entries(
            [self.MEMBER_ALL],
            summary=summary,
            fetchers=Fetchers(
                orcid=lambda _: [orcid_work(doi="10.1/a", year=2025, month=1, day=1)],
                inspire=broken,
                openalex=lambda _: [openalex_work(doi="10.1/c")],
                crossref=lambda _: None,
            ),
        )
        assert {entry["source"] for entry in entries} == {"orcid", "openalex"}
        assert len(summary.failures) == 1
        assert "via inspire" in summary.failures[0]
        # The member still counts as synced: two of three sources answered.
        assert summary.members_synced == 1

    def test_the_membership_window_applies_to_every_source(self) -> None:
        summary = SyncSummary()
        member = Member(id="x", name="X Y", start=date(2025, 1, 1), inspire="X.Y.1", openalex="A1")
        entries = collect_entries(
            [member],
            summary=summary,
            fetchers=Fetchers(
                inspire=lambda _: [inspire_record(year=2020)],
                openalex=lambda _: [openalex_work(date="2020-05-01")],
            ),
        )
        assert entries == []
        assert summary.out_of_window == 2


class TestMisattributionGuard:
    """Dropping records that auto-assigning databases filed under the wrong person."""

    LI = Member(id="tjonnie-li", name="Tjonnie G. F. Li", start=date(2021, 10, 1), inspire="T.G.F.Li.1")

    def test_a_short_paper_naming_no_member_is_dropped(self) -> None:
        summary = SyncSummary()
        record = inspire_record(title="Chip-yield analysis", authors=["Li, Zi-Ming", "Li, Tie-Fu", "Liu, Yu-xi"])
        entries = collect_entries([self.LI], summary=summary, fetchers=Fetchers(inspire=lambda _: [record]))
        assert entries == []
        assert summary.misattributed == 1

    def test_a_short_paper_naming_the_member_is_kept(self) -> None:
        summary = SyncSummary()
        record = inspire_record(title="A real paper", authors=["Li, Tjonnie G.F.", "Doe, Jane"])
        entries = collect_entries([self.LI], summary=summary, fetchers=Fetchers(inspire=lambda _: [record]))
        assert len(entries) == 1
        assert summary.misattributed == 0

    def test_collaboration_papers_are_exempt(self) -> None:
        # A member may not be listed individually on a thousand-author paper.
        summary = SyncSummary()
        record = inspire_record(
            title="An LVK paper", authors=["Abac, A. G."], author_count=2000, collaborations=["LIGO Scientific"]
        )
        entries = collect_entries([self.LI], summary=summary, fetchers=Fetchers(inspire=lambda _: [record]))
        assert len(entries) == 1
        assert summary.misattributed == 0

    def test_orcid_is_trusted_because_it_is_self_asserted(self) -> None:
        summary = SyncSummary()
        member = Member(id="isaac-wong", name="Isaac Wong", start=date(2024, 1, 1), orcid="0000-0003-2166-0027")
        work = orcid_work(title="Something they added themselves", year=2025, month=1, day=1)
        entries = collect_entries(
            [member], summary=summary, fetchers=Fetchers(orcid=lambda _: [work], crossref=lambda _: None)
        )
        assert len(entries) == 1
        assert summary.misattributed == 0


class TestPreprintAndPublishedVersions:
    """The same paper arriving as a preprint from one source and an article from another."""

    def test_arxiv_own_doi_becomes_an_arxiv_id(self) -> None:
        assert split_arxiv_doi("10.48550/arXiv.2411.17893", None) == (None, "2411.17893")
        assert split_arxiv_doi("10.1103/k3hv-cqfn", "2411.17893") == ("10.1103/k3hv-cqfn", "2411.17893")

    def test_an_entry_is_identified_by_every_identifier_it_carries(self) -> None:
        entry = {"doi": "10.1103/K3HV", "arxiv": "2411.17893", "title": "A Paper!"}
        assert identity_keys(entry) == ["doi:10.1103/k3hv", "arxiv:2411.17893", "title:a-paper"]

    def test_preprint_and_published_versions_merge_on_the_arxiv_id(self) -> None:
        published = normalize_inspire_record(
            inspire_record(title="A paper", doi="10.1103/k3hv-cqfn", arxiv="2411.17893"), MEMBER
        )
        preprint = normalize_openalex_work(openalex_work(title="A paper", doi="10.48550/arXiv.2411.17893"), OTHER)
        merged = deduplicate([published, preprint], {MEMBER.id: MEMBER, OTHER.id: OTHER})
        assert len(merged) == 1
        # The journal DOI is the better identity, and both members are credited.
        assert merged[0]["key"] == "doi:10.1103/k3hv-cqfn"
        assert merged[0]["members"] == ["isaac-wong", "jane-doe"]

    def test_records_sharing_only_a_title_still_merge(self) -> None:
        with_ids = normalize_inspire_record(inspire_record(title="Same paper", doi="10.1/x", arxiv=None), MEMBER)
        without = normalize_openalex_work(openalex_work(title="Same paper", doi=None), OTHER)
        merged = deduplicate([with_ids, without], {MEMBER.id: MEMBER, OTHER.id: OTHER})
        assert len(merged) == 1

    def test_grouping_is_transitive_across_a_linking_record(self) -> None:
        # A carries only the DOI, C only the arXiv id; B carries both and joins
        # them, so all three are one work.
        a = {"title": "T", "doi": "10.1/x", "members": ["a"], "links": [], "collaboration": False}
        b = {"title": "T2", "doi": "10.1/x", "arxiv": "2501.1", "members": ["b"], "links": [], "collaboration": False}
        c = {"title": "T3", "arxiv": "2501.1", "members": ["c"], "links": [], "collaboration": False}
        merged = deduplicate([a, b, c], {})
        assert len(merged) == 1
        assert merged[0]["members"] == ["a", "b", "c"]

    def test_unrelated_papers_stay_separate(self) -> None:
        a = {"title": "One", "doi": "10.1/x", "members": ["a"], "links": [], "collaboration": False}
        b = {"title": "Two", "doi": "10.1/y", "members": ["b"], "links": [], "collaboration": False}
        assert len(deduplicate([a, b], {})) == 2


class TestCollaborationOverride:
    """The hand override that decides which section a paper appears in."""

    def test_the_derived_value_is_used_when_no_override_is_set(self) -> None:
        assert is_collaboration({"collaboration": True}) is True
        assert is_collaboration({"collaboration": False}) is False
        assert is_collaboration({}) is False

    def test_the_override_wins_in_both_directions(self) -> None:
        # A big multi-institution paper that is not a collaboration paper.
        assert is_collaboration({"collaboration": True, "treat_as_collaboration": False}) is False
        # A community paper the record happens not to name a collaboration on.
        assert is_collaboration({"collaboration": False, "treat_as_collaboration": True}) is True

    def test_the_override_survives_a_resync_but_the_derived_value_does_not(self) -> None:
        existing = [
            {"key": "doi:1", "title": "Old", "collaboration": True, "treat_as_collaboration": False},
        ]
        fetched = [{"key": "doi:1", "title": "New", "include": True, "highlight": False, "collaboration": True}]
        merged, _ = merge_items(existing, fetched)
        assert merged[0]["treat_as_collaboration"] is False
        # The derived field is refreshed from the sources, not frozen.
        assert merged[0]["collaboration"] is True
        assert is_collaboration(merged[0]) is False

    def test_an_untouched_entry_keeps_no_override(self) -> None:
        existing = [{"key": "doi:1", "title": "Old", "collaboration": True}]
        fetched = [{"key": "doi:1", "title": "New", "include": True, "highlight": False, "collaboration": False}]
        merged, _ = merge_items(existing, fetched)
        assert "treat_as_collaboration" not in merged[0]
        # Nothing is frozen: a changed classification takes effect.
        assert is_collaboration(merged[0]) is False


class TestThreshold:
    """The author-count fallback, used only when no collaboration is named."""

    def test_the_default_threshold_ignores_ordinary_multi_author_papers(self) -> None:
        names = [f"Author{index}, A" for index in range(30)]
        entry = normalize_inspire_record(inspire_record(authors=names), MEMBER)
        assert entry["collaboration"] is False

    def test_a_thousand_author_paper_is_still_caught_without_a_named_collaboration(self) -> None:
        entry = normalize_inspire_record(inspire_record(authors=["Wong, Isaac"], author_count=1771), MEMBER)
        assert entry["collaboration"] is True


class TestMultipleIdentifiersPerSource:
    """A person can hold several profiles on one database."""

    def test_ids_for_accepts_one_id_or_many(self) -> None:
        one = Member(id="x", name="X Y", start=date(2024, 1, 1), openalex="A1")
        many = Member(id="x", name="X Y", start=date(2024, 1, 1), openalex=["A1", "A2"])
        assert one.ids_for("openalex") == ("A1",)
        assert many.ids_for("openalex") == ("A1", "A2")
        assert one.ids_for("inspire") == ()

    def test_a_bare_string_is_one_id_not_a_sequence_of_characters(self) -> None:
        assert Member(id="x", name="X Y", start=date(2024, 1, 1), inspire="T.G.F.Li.1").ids_for("inspire") == (
            "T.G.F.Li.1",
        )

    def test_every_entity_is_queried_and_their_works_unioned(self) -> None:
        # The reason this matters: OpenAlex files different papers under
        # different entities for the same researcher, so querying only the
        # fullest silently loses the rest.
        works = {
            "A1": [openalex_work(title="from the big entity", doi="10.1/a", authors=["Xavier Young"])],
            "A2": [openalex_work(title="from the small entity", doi="10.1/b", authors=["Xavier Young"])],
        }
        member = Member(id="x", name="Xavier Young", start=date(2024, 1, 1), openalex=["A1", "A2"])
        summary = SyncSummary()
        entries = collect_entries([member], summary=summary, fetchers=Fetchers(openalex=lambda i: works[i]))
        assert {entry["title"] for entry in entries} == {"from the big entity", "from the small entity"}


class TestCrossCrediting:
    """Crediting every member named on a paper, not only the one who found it."""

    ISAAC = Member(id="isaac-wong", name="Isaac Wong", start=date(2024, 1, 1), orcid="o1")
    JANE = Member(id="jane-doe", name="Jane Doe", start=date(2024, 1, 1), orcid="o2")
    LATE = Member(id="late-joiner", name="Jane Doe", start=date(2026, 1, 1), orcid="o3")

    def test_a_member_named_in_the_authors_is_credited_even_if_another_found_it(self) -> None:
        entry = normalize_inspire_record(inspire_record(authors=["Wong, Isaac", "Doe, Jane"], year=2025), self.ISAAC)
        assert entry["members"] == ["isaac-wong"]
        merged = deduplicate([entry], {"isaac-wong": self.ISAAC, "jane-doe": self.JANE})
        assert merged[0]["members"] == ["isaac-wong", "jane-doe"]
        assert merged[0]["authors"] == ["**Isaac Wong**", "**Jane Doe**"]

    def test_a_member_who_joined_after_publication_is_not_credited(self) -> None:
        entry = normalize_inspire_record(inspire_record(authors=["Wong, Isaac", "Doe, Jane"], year=2025), self.ISAAC)
        merged = deduplicate([entry], {"isaac-wong": self.ISAAC, "late-joiner": self.LATE})
        assert merged[0]["members"] == ["isaac-wong"]

    def test_a_non_member_author_is_not_turned_into_a_member(self) -> None:
        entry = normalize_inspire_record(inspire_record(authors=["Wong, Isaac", "Stranger, Sam"]), self.ISAAC)
        merged = deduplicate([entry], {"isaac-wong": self.ISAAC, "jane-doe": self.JANE})
        assert merged[0]["members"] == ["isaac-wong"]
