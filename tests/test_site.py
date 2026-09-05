"""Tests for content validation and the site build.

These run against the repository's own content, so a schema drift or a broken
template is caught here rather than in the deploy workflow.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, ClassVar
from xml.etree import ElementTree

import pytest

from leuven_gravity_institute.site import SitePaths, build_site, load_content, validate_content
from leuven_gravity_institute.site.builder import _build_context, _group_people, _split_events

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def paths() -> SitePaths:
    """Point the build at the repository, writing to a throwaway output dir."""
    return SitePaths(root=ROOT, output_dir="_test_site")


@pytest.fixture(scope="module")
def built(paths: SitePaths) -> Iterator[Path]:
    """Build the site once per module, and clean the output up afterwards."""
    output = build_site(paths)
    yield output
    shutil.rmtree(output, ignore_errors=True)


class TestContent:
    """The repository's own content must satisfy its schemas."""

    def test_content_is_valid(self, paths: SitePaths) -> None:
        assert validate_content(paths.content, paths.schemas) == []

    def test_every_content_file_has_a_schema(self, paths: SitePaths) -> None:
        missing = [name for name in load_content(paths.content) if not (paths.schemas / f"{name}.schema.json").exists()]
        assert missing == []

    def test_person_group_ids_are_registered_in_site_yaml(self, paths: SitePaths) -> None:
        content = load_content(paths.content)
        known = {group["id"] for group in content["site"]["site"]["groups"]}
        for person in content["people"]["items"]:
            assert person.get("group") in known, f"{person['id']} has an unregistered group"

    def test_group_leads_name_real_people(self, paths: SitePaths) -> None:
        content = load_content(paths.content)
        known = {person["id"] for person in content["people"]["items"]}
        for group in content["site"]["site"]["groups"]:
            if group.get("lead"):
                assert group["lead"] in known

    def test_person_ids_referenced_elsewhere_exist(self, paths: SitePaths) -> None:
        content = load_content(paths.content)
        known = {person["id"] for person in content["people"]["items"]}
        for name in ("research", "software", "teaching", "publications"):
            for item in content[name]["items"]:
                unknown = set(item.get("people") or item.get("members") or []) - known
                assert unknown == set(), f"{name}.yaml references unknown person ids: {sorted(unknown)}"


class TestBuild:
    """The rendered output."""

    def test_every_configured_page_is_written(self, built: Path, paths: SitePaths) -> None:
        content = load_content(paths.content)
        for page in content["site"]["site"]["pages"]:
            slug = page["slug"]
            expected = built / "index.html" if not slug else built / slug / "index.html"
            assert expected.is_file(), f"missing page for slug {slug!r}"

    def test_a_profile_page_is_written_for_every_person(self, built: Path, paths: SitePaths) -> None:
        for person in load_content(paths.content)["people"]["items"]:
            assert (built / "people" / person["slug"] / "index.html").is_file()

    def test_a_profile_page_lists_only_that_persons_publications(self, built: Path, paths: SitePaths) -> None:
        content = load_content(paths.content)
        publications = content["publications"]["items"]
        person = next(p for p in content["people"]["items"] if p["id"] == "isaac-wong")
        html = (built / "people" / person["slug"] / "index.html").read_text(encoding="utf-8")
        for pub in publications:
            if person["id"] in (pub.get("members") or []):
                assert pub["title"] in html
            else:
                assert pub["title"] not in html

    def test_assets_and_nojekyll_are_copied(self, built: Path) -> None:
        assert (built / "assets" / "css" / "style.css").is_file()
        assert (built / ".nojekyll").is_file()

    def test_feed_is_valid_xml_with_entries(self, built: Path) -> None:
        tree = ElementTree.parse(built / "feed.xml")
        entries = tree.getroot().findall("{http://www.w3.org/2005/Atom}entry")
        assert entries, "the feed should contain at least one entry"

    def test_navigation_links_every_nav_page(self, built: Path, paths: SitePaths) -> None:
        html = (built / "index.html").read_text(encoding="utf-8")
        for page in load_content(paths.content)["site"]["site"]["pages"]:
            if page.get("nav", True):
                href = "/" if not page["slug"] else f"/{page['slug']}/"
                assert f'href="{href}"' in html


class TestContext:
    """Derived views the templates depend on."""

    def test_publications_are_attached_to_the_people_who_wrote_them(self, paths: SitePaths) -> None:
        context = _build_context(load_content(paths.content))
        for person in context["people"]:
            for pub in person["publications"]:
                assert person["id"] in pub["members"]

    def test_alumni_are_separated_from_current_members(self, paths: SitePaths) -> None:
        context = _build_context(load_content(paths.content))
        listed = [
            person
            for block in context["people_sections"]
            for category in block["categories"]
            for person in category["items"]
        ]
        assert all(person.get("status") != "alumni" for person in listed)
        assert all(person["status"] == "alumni" for person in context["alumni"])

    def test_group_headings_stay_hidden_while_one_group_is_configured(self, paths: SitePaths) -> None:
        context = _build_context(load_content(paths.content))
        assert len(context["groups"]) == 1
        assert context["show_group_headings"] is False

    def test_each_person_carries_their_group_name(self, paths: SitePaths) -> None:
        context = _build_context(load_content(paths.content))
        names = {group["id"]: group["name"] for group in context["groups"]}
        for person in context["people"]:
            assert person["group_name"] == names[person["group"]]

    def test_excluded_publications_are_not_rendered(self, paths: SitePaths) -> None:
        content = load_content(paths.content)
        content["publications"]["items"] = [
            {"title": "Shown", "include": True, "year": 2025, "members": []},
            {"title": "Hidden", "include": False, "year": 2025, "members": []},
        ]
        titles = [pub["title"] for pub in _build_context(content)["publications"]]
        assert titles == ["Shown"]

    def test_events_split_into_upcoming_and_past(self) -> None:
        events = [
            {"date": "2026-01-01", "title": "past"},
            {"date": "2026-12-01", "title": "future"},
            {"date": "2026-05-01", "end": "2026-07-01", "title": "ongoing"},
        ]
        upcoming, past = _split_events(events, date(2026, 6, 1))
        assert [item["title"] for item in upcoming] == ["ongoing", "future"]
        assert [item["title"] for item in past] == ["past"]


class TestPeopleGrouping:
    """Arranging people by group, then category."""

    GROUPS: ClassVar[list[dict[str, Any]]] = [
        {"id": "group-one", "name": "Group One"},
        {"id": "group-two", "name": "Group Two"},
    ]

    def test_groups_follow_the_order_declared_in_site_yaml(self) -> None:
        people = [
            {"name": "B", "group": "group-two", "category": "Faculty"},
            {"name": "A", "group": "group-one", "category": "Faculty"},
        ]
        sections = _group_people(people, self.GROUPS)
        assert [block["group"]["id"] for block in sections] == ["group-one", "group-two"]

    def test_categories_are_nested_inside_each_group(self) -> None:
        people = [
            {"name": "A", "group": "group-one", "category": "Faculty"},
            {"name": "B", "group": "group-one", "category": "Students"},
            {"name": "C", "group": "group-one", "category": "Faculty"},
        ]
        sections = _group_people(people, self.GROUPS)
        assert len(sections) == 1
        categories = sections[0]["categories"]
        assert [category["category"] for category in categories] == ["Faculty", "Students"]
        assert [person["name"] for person in categories[0]["items"]] == ["A", "C"]

    def test_a_group_with_no_members_is_omitted(self) -> None:
        sections = _group_people([{"name": "A", "group": "group-one", "category": "Faculty"}], self.GROUPS)
        assert [block["group"]["id"] for block in sections] == ["group-one"]

    def test_untagged_people_still_appear_in_a_trailing_section(self) -> None:
        people = [
            {"name": "A", "group": "group-one", "category": "Faculty"},
            {"name": "B", "category": "Faculty"},
        ]
        sections = _group_people(people, self.GROUPS)
        assert sections[-1]["group"] is None
        assert [person["name"] for person in sections[-1]["categories"][0]["items"]] == ["B"]
