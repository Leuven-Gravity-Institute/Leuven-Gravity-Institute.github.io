"""Rendering of the static site from content + templates + assets.

The build is deliberately small and explicit:

1. Load the structured content (``content/*.yaml``).
2. Derive the sorted and grouped views the templates render (people by
   category, publications by year, upcoming vs. past events, ...).
3. Render one page per ``site.pages`` entry to ``<slug>/index.html``.
4. Render one profile page per person, carrying that person's own publication
   list, to ``people/<slug>/index.html``.
5. Write an Atom feed of recent updates to ``feed.xml``.
6. Copy ``assets/`` into the output and add a ``.nojekyll`` marker.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from datetime import UTC, date, datetime
from itertools import groupby
from pathlib import Path
from typing import Any

import markdown as markdown_lib
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from leuven_gravity_institute.site.content import load_content
from leuven_gravity_institute.site.paths import SitePaths
from leuven_gravity_institute.site.publications_sync import is_collaboration

_MD_EXTENSIONS = ["extra", "sane_lists", "smarty"]
_FEED_MAX_ENTRIES = 30


def _render_markdown(text: str | None) -> Markup:
    """Render a block of Markdown to safe HTML."""
    if not text:
        return Markup("")
    # Content is authored in-repo and therefore trusted; rendering it as HTML is intentional.
    return Markup(markdown_lib.markdown(str(text), extensions=_MD_EXTENSIONS))  # noqa: S704


def _render_markdown_inline(text: str | None) -> Markup:
    """Render Markdown without the wrapping ``<p>`` tag (for inline snippets)."""
    rendered = str(_render_markdown(text)).strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        rendered = rendered[len("<p>") : -len("</p>")]
    return Markup(rendered)  # noqa: S704


def _make_environment(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        undefined=ChainableUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["markdown"] = _render_markdown
    env.filters["markdown_inline"] = _render_markdown_inline
    return env


def _items(content: dict[str, Any], key: str) -> list[dict[str, Any]]:
    section = content.get(key) or {}
    return list(section.get("items") or [])


def _group_by(items: list[dict[str, Any]], key: str, label: str = "category") -> list[dict[str, Any]]:
    """Group items by a field, preserving first-appearance order.

    Returns a list of ``{<label>: value, "items": [...]}`` so a template can
    render each group under its own heading without knowing the categories in
    advance.
    """
    groups: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for item in items:
        value = item.get(key, "")
        group = index.get(value)
        if group is None:
            group = {label: value, "items": []}
            index[value] = group
            groups.append(group)
        group["items"].append(item)
    return groups


def _group_people(people: list[dict[str, Any]], groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Arrange people by research group, and within a group by category.

    Groups appear in the order they are declared under ``site.groups``; anyone
    whose ``group`` is unset or unrecognised falls into a trailing section with
    no group of its own. Templates hide the group heading while a single group
    is configured, so a one-group site reads exactly as it would without this
    dimension — but a second group can be added later as pure data.
    """
    buckets: dict[str | None, list[dict[str, Any]]] = {}
    for person in people:
        buckets.setdefault(person.get("group"), []).append(person)

    sections: list[dict[str, Any]] = []
    for group in groups:
        members = buckets.pop(group["id"], [])
        if members:
            sections.append({"group": group, "categories": _group_by(members, "category")})
    for members in buckets.values():
        sections.append({"group": None, "categories": _group_by(members, "category")})
    return sections


def _person_url(person: dict[str, Any]) -> str:
    """Return the clean URL of a person's profile page."""
    return f"/people/{person['slug']}/"


def _prepare_people(
    people: list[dict[str, Any]],
    publications: list[dict[str, Any]],
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach each person's own publications, profile URL, and group name.

    A publication lists every credited group member under ``members``; a
    person's page shows the subset naming them, newest first. Deduplication has
    already happened during the sync, so a paper shared by two members is one
    record appearing on both of their pages.
    """
    groups_by_id = {group["id"]: group for group in groups}
    for person in people:
        person["url"] = _person_url(person)
        person["group_name"] = (groups_by_id.get(person.get("group")) or {}).get("name", "")
        own = [pub for pub in publications if person["id"] in (pub.get("members") or [])]
        person["publications"] = own
        person["group_publications"] = [pub for pub in own if not is_collaboration(pub)]
        person["collaboration_publications"] = [pub for pub in own if is_collaboration(pub)]
    return people


def _by_year(publications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group publications into ``{"year": Y, "items": [...]}`` blocks, newest first."""
    return [
        {"year": year, "items": list(group)} for year, group in groupby(publications, key=lambda item: item.get("year"))
    ]


def _split_events(events: list[dict[str, Any]], today: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split events into upcoming (soonest first) and past (most recent first)."""
    upcoming = [item for item in events if str(item.get("end") or item.get("date") or "") >= today.isoformat()]
    past = [item for item in events if item not in upcoming]
    upcoming.sort(key=lambda item: str(item.get("date") or ""))
    past.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    return upcoming, past


def _normalize_pages(site: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand the ``site.pages`` config into a uniform structure for templates.

    Each section entry becomes ``{"name": str, "preview": int | None}`` whether
    it was authored as a bare string or as a mapping.
    """
    pages: list[dict[str, Any]] = []
    for raw in site.get("pages", []):
        sections = []
        for entry in raw.get("sections", []):
            if isinstance(entry, str):
                sections.append({"name": entry, "preview": None})
            else:
                sections.append({"name": entry["name"], "preview": entry.get("preview")})
        label = raw.get("label", "")
        pages.append(
            {
                "slug": raw.get("slug", ""),
                "label": label,
                "title": raw.get("title") or label,
                "lead": raw.get("lead", ""),
                "nav": raw.get("nav", True),
                "sections": sections,
            }
        )
    return pages


def _page_path(output: Path, slug: str) -> Path:
    """Resolve the output file for a page slug (clean ``/slug/`` URLs)."""
    return output / "index.html" if not slug else output / slug / "index.html"


def _build_context(content: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """Assemble the template context, including sorted and grouped views."""
    today = today or datetime.now(tz=UTC).date()
    site_doc = content.get("site") or {}

    all_publications = _items(content, "publications")
    included = [pub for pub in all_publications if pub.get("include", True) is not False]
    publications = sorted(
        included,
        key=lambda pub: (str(pub.get("date") or pub.get("year") or ""), pub.get("title", "")),
        reverse=True,
    )

    groups = list(site_doc.get("site", {}).get("groups") or [])
    people = _items(content, "people")
    people = _prepare_people(people, publications, groups)
    current = [person for person in people if person.get("status", "current") != "alumni"]
    alumni = [person for person in people if person.get("status") == "alumni"]

    news = sorted(_items(content, "news"), key=lambda item: str(item.get("date", "")), reverse=True)
    upcoming_events, past_events = _split_events(_items(content, "events"), today)

    # Large-collaboration papers are separated out: on a list of this shape they
    # would otherwise bury the group's own work, since one can carry thousands
    # of authors.
    group_led = [pub for pub in publications if not is_collaboration(pub)]
    collaboration = [pub for pub in publications if is_collaboration(pub)]

    return {
        "site": site_doc.get("site", {}),
        "org": site_doc.get("org", {}),
        "groups": groups,
        # A single group needs no heading of its own; several do.
        "show_group_headings": len(groups) > 1,
        "people": people,
        "people_by_id": {person["id"]: person for person in people},
        "people_sections": _group_people(current, groups),
        "alumni": alumni,
        "news": news,
        "events": upcoming_events + past_events,
        "upcoming_events": upcoming_events,
        "past_events": past_events,
        "research": _items(content, "research"),
        "publications": publications,
        "group_publications": group_led,
        "publications_by_year": _by_year(group_led),
        "collaboration_publications": collaboration,
        "collaboration_by_year": _by_year(collaboration),
        "selected_publications": [pub for pub in group_led if pub.get("highlight")],
        "software_groups": _group_by(_items(content, "software"), "category"),
        "teaching": _items(content, "teaching"),
        "openings": _items(content, "join"),
        "join": content.get("join") or {},
        "contact": content.get("contact") or {},
    }


def _rfc3339(value: str) -> str | None:
    """Convert a ``YYYY-MM-DD`` date string to an RFC 3339 UTC timestamp."""
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed_token(*parts: str) -> str:
    """Return a short, stable id token derived from content (not used for security)."""
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()[:12]


def _plain_title(markdown_text: str, limit: int = 100) -> str:
    """Derive a one-line plain-text title from a Markdown body."""
    text = re.sub(r"<[^>]+>", "", str(_render_markdown(markdown_text)))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "Update"


def _feed_entries(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a unified, newest-first list of feed entries from the content views."""
    base = str(context.get("site", {}).get("url", "")).rstrip("/")
    entries: list[dict[str, Any]] = []

    for item in context.get("news", []):
        updated = _rfc3339(str(item.get("date", "")))
        body = item.get("body", "")
        if updated is None:
            continue
        entries.append(
            {
                "title": item.get("title") or _plain_title(body),
                "id": f"{base}/news/#{_feed_token('news', str(item.get('date', '')), body)}",
                "link": item.get("url") or f"{base}/news/",
                "updated": updated,
                "category": "news",
                "content_html": str(_render_markdown(body)),
            }
        )

    for item in context.get("events", []):
        updated = _rfc3339(str(item.get("date", "")))
        if updated is None:
            continue
        title = item.get("title", "")
        meta = " · ".join(part for part in (item.get("kind", ""), item.get("location", "")) if part)
        entries.append(
            {
                "title": title,
                "id": f"{base}/news/#{_feed_token('event', str(item.get('date', '')), title)}",
                "link": item.get("url") or f"{base}/news/",
                "updated": updated,
                "category": "event",
                "content_html": meta,
            }
        )

    for item in context.get("publications", []):
        year = item.get("year")
        if not isinstance(year, int):
            continue
        authors = ", ".join(author.replace("*", "") for author in item.get("authors", []))
        summary = " — ".join(part for part in (authors, item.get("venue", "")) if part)
        entries.append(
            {
                "title": item.get("title", ""),
                "id": f"{base}/publications/#{_feed_token('publication', item.get('key') or item.get('title', ''))}",
                "link": item["links"][0]["url"] if item.get("links") else f"{base}/publications/",
                "updated": _rfc3339(str(item.get("date") or "")) or f"{year}-01-01T00:00:00Z",
                "category": "publication",
                "content_html": summary,
            }
        )

    entries.sort(key=lambda entry: entry["updated"], reverse=True)
    return entries[:_FEED_MAX_ENTRIES]


def _render_feed(env: Environment, context: dict[str, Any]) -> str:
    """Render the Atom feed XML for the site's recent updates."""
    base = str(context.get("site", {}).get("url", "")).rstrip("/")
    entries = _feed_entries(context)
    feed_updated = entries[0]["updated"] if entries else f"{datetime.now(tz=UTC).date().isoformat()}T00:00:00Z"
    return env.get_template("feed.xml").render(
        site=context.get("site", {}),
        entries=entries,
        feed_id=f"{base}/feed.xml",
        feed_self=f"{base}/feed.xml",
        feed_alternate=f"{base}/",
        feed_updated=feed_updated,
    )


def build_site(paths: SitePaths) -> Path:
    """Build the site and return the path to the output directory.

    Args:
        paths: Resolved input/output locations for the build.

    Returns:
        The output directory containing the rendered site.

    """
    content = load_content(paths.content)
    context = _build_context(content)

    pages = _normalize_pages(context["site"])
    nav_pages = [page for page in pages if page["nav"]]

    env = _make_environment(paths.templates)
    page_template = env.get_template("page.html")
    person_template = env.get_template("person.html")

    output = paths.output
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    for page in pages:
        html = page_template.render(page=page, nav_pages=nav_pages, **context)
        destination = _page_path(output, page["slug"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(html, encoding="utf-8")

    # One profile page per person, each with that person's own publication list.
    # The navigation keeps "People" highlighted while a profile is open.
    for person in context["people"]:
        page = {"slug": f"people/{person['slug']}", "label": "People", "title": person["name"], "sections": []}
        html = person_template.render(page=page, nav_pages=nav_pages, person=person, **context)
        destination = _page_path(output, page["slug"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(html, encoding="utf-8")

    (output / "feed.xml").write_text(_render_feed(env, context), encoding="utf-8")

    if paths.assets.is_dir():
        shutil.copytree(paths.assets, output / paths.assets_dir, dirs_exist_ok=True)

    # Disable GitHub Pages' default Jekyll processing of the pre-built output.
    (output / ".nojekyll").write_text("", encoding="utf-8")

    return output
