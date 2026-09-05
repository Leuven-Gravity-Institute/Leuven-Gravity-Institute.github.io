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
    Member,
    build_authors,
    date_span,
    deduplicate,
    emphasise_members,
    entry_key,
    in_membership_window,
    members_from_people,
    merge_items,
    normalize_work,
    parse_date,
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

    def test_family_name_and_initial_come_from_the_display_name(self) -> None:
        assert MEMBER.family == "wong"
        assert MEMBER.initial == "i"


class TestAuthors:
    """Rendering author lists."""

    def test_members_are_emphasised_by_family_name_and_initial(self) -> None:
        rendered = emphasise_members(["Isaac Wong", "Someone Else"], [MEMBER])
        assert rendered == ["**Isaac Wong**", "Someone Else"]

    def test_a_different_person_with_the_same_surname_is_not_emphasised(self) -> None:
        rendered = emphasise_members(["Brian Wong"], [MEMBER])
        assert rendered == ["Brian Wong"]

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
            works_fetcher=lambda orcid: works[orcid],
            crossref_fetcher=lambda doi: crossref if doi == "10.1000/shared" else None,
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
        summary = sync_publications(publications, people_file, works_fetcher=fetcher, crossref_fetcher=lambda doi: None)

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
