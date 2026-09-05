"""Thin client for the public INSPIRE-HEP API.

INSPIRE is the literature database of the high-energy and gravitational-wave
community. It matters here because it *assigns papers to author profiles by
itself*: a member's record stays current whether or not they maintain it, which
an ORCID record does not. It also carries reliable author lists, collaboration
names, and arXiv identifiers.

Its limitation is the mirror image: it only covers high-energy and
gravitational-wave literature, so members working in signal processing or
optimisation need one of the other sources.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from leuven_gravity_institute.site.orcid import get_json

INSPIRE_LITERATURE_API = "https://inspirehep.net/api/literature"
INSPIRE_AUTHORS_API = "https://inspirehep.net/api/authors"

_FIELDS = (
    "titles,authors.full_name,authors.ids,author_count,collaborations,dois,"
    "arxiv_eprints,publication_info,document_type,earliest_date,preprint_date,control_number"
)
_PAGE_SIZE = 250
_TIMEOUT = 30.0


def resolve_bai(orcid: str, *, timeout: float = _TIMEOUT) -> str | None:
    """Resolve an ORCID iD to an INSPIRE author identifier (BAI).

    Only works when the author has linked their ORCID on INSPIRE, which many
    have not. Pin the BAI explicitly in ``people.yaml`` when this returns
    nothing.

    Args:
        orcid: The ORCID iD to resolve.
        timeout: Request timeout in seconds.

    Returns:
        The BAI (e.g. ``"T.G.F.Li.1"``), or ``None`` if no profile links it.

    """
    query = urllib.parse.urlencode({"q": f"ids.value:{orcid}", "fields": "ids", "size": 1})
    payload = get_json(f"{INSPIRE_AUTHORS_API}?{query}", timeout=timeout)
    for hit in payload.get("hits", {}).get("hits", []):
        for identifier in hit.get("metadata", {}).get("ids", []):
            if identifier.get("schema") == "INSPIRE BAI":
                return identifier.get("value")
    return None


def fetch_records(bai: str, *, timeout: float = _TIMEOUT) -> list[dict[str, Any]]:
    """Page through every INSPIRE literature record for an author BAI.

    Args:
        bai: The INSPIRE author identifier (e.g. ``"T.G.F.Li.1"``).
        timeout: Per-request timeout in seconds.

    Returns:
        A list of record ``metadata`` mappings.

    """
    query = urllib.parse.urlencode({"q": f"a {bai}", "fields": _FIELDS, "size": _PAGE_SIZE, "sort": "mostrecent"})
    url: str | None = f"{INSPIRE_LITERATURE_API}?{query}"
    records: list[dict[str, Any]] = []
    while url:
        payload = get_json(url, timeout=timeout)
        for hit in payload.get("hits", {}).get("hits", []):
            if "metadata" in hit:
                records.append(hit["metadata"])
        url = payload.get("links", {}).get("next")
    return records
