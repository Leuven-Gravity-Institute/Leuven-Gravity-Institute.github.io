"""Thin client for the public OpenAlex API.

OpenAlex aggregates Crossref, PubMed, arXiv and others, so it reaches work that
neither ORCID nor INSPIRE lists — which is what members outside high-energy
physics need.

Its weakness is author disambiguation: one researcher is often split across
several author entities, and resolving by ORCID returns whichever the API
prefers, not necessarily the fullest. One of this group's members resolves that
way to an entity holding a single work while another entity, carrying the same
ORCID, holds hundreds. So the author id is pinned per person in ``people.yaml``
rather than looked up, and :func:`find_authors` exists to choose it.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from leuven_gravity_institute.site.orcid import get_json

OPENALEX_API = "https://api.openalex.org"

_SELECT = "id,doi,display_name,publication_date,publication_year,type,authorships,primary_location,biblio,locations"
_PAGE_SIZE = 200
_TIMEOUT = 30.0


def find_authors(query: str, *, timeout: float = _TIMEOUT, mailto: str | None = None) -> list[dict[str, Any]]:
    """Search OpenAlex author entities, to pick the right id by hand.

    Args:
        query: A name or ORCID iD to search for.
        timeout: Request timeout in seconds.
        mailto: Contact address for OpenAlex's polite pool.

    Returns:
        Author entities with their ``id``, ``display_name``, ``works_count``,
        and ``orcid`` — enough to tell fragmented entities apart.

    """
    url = f"{OPENALEX_API}/authors?{urllib.parse.urlencode({'search': query, 'per-page': 10})}"
    payload = get_json(url, timeout=timeout, mailto=mailto)
    return list(payload.get("results") or [])


def find_authors_by_orcid(orcid: str, *, timeout: float = _TIMEOUT, mailto: str | None = None) -> list[dict[str, Any]]:
    """Return every OpenAlex author entity carrying an ORCID iD, fullest first.

    One ORCID commonly maps to several entities — one of this group's members
    has nine, holding between 1 and 264 works. Resolving by ORCID through the
    ``/authors/{orcid}`` route returns just one of them, and not necessarily the
    fullest, so use this to see them all and pin the right id.

    Args:
        orcid: The ORCID iD to look up.
        timeout: Request timeout in seconds.
        mailto: Contact address for OpenAlex's polite pool.

    Returns:
        Author entities sorted by ``works_count``, descending.

    """
    query = urllib.parse.urlencode({"filter": f"orcid:https://orcid.org/{orcid}", "per-page": 25})
    payload = get_json(f"{OPENALEX_API}/authors?{query}", timeout=timeout, mailto=mailto)
    return sorted(payload.get("results") or [], key=lambda author: -author.get("works_count", 0))


def fetch_works(author_id: str, *, timeout: float = _TIMEOUT, mailto: str | None = None) -> list[dict[str, Any]]:
    """Page through every OpenAlex work for a pinned author id.

    Args:
        author_id: The OpenAlex author id (e.g. ``"A5047727655"``), with or
            without the ``https://openalex.org/`` prefix.
        timeout: Per-request timeout in seconds.
        mailto: Contact address for OpenAlex's polite pool.

    Returns:
        A list of OpenAlex work documents.

    """
    short_id = author_id.rstrip("/").rsplit("/", 1)[-1]
    works: list[dict[str, Any]] = []
    cursor = "*"
    while cursor:
        query = urllib.parse.urlencode(
            {"filter": f"author.id:{short_id}", "select": _SELECT, "per-page": _PAGE_SIZE, "cursor": cursor}
        )
        payload = get_json(f"{OPENALEX_API}/works?{query}", timeout=timeout, mailto=mailto)
        works.extend(payload.get("results") or [])
        cursor = (payload.get("meta") or {}).get("next_cursor")
    return works
