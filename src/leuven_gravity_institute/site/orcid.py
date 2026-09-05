"""Thin clients for the public ORCID and Crossref APIs.

Only the small slice of each API that the publication sync needs is
implemented here, so the sync module can stay focused on merge logic and can
be tested by injecting fakes in place of these functions.

Both services are queried anonymously over their public endpoints; no
credentials are required or used.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

ORCID_API = "https://pub.orcid.org/v3.0"
CROSSREF_API = "https://api.crossref.org/works"

USER_AGENT = "Leuven-Gravity-Institute.github.io publication sync (+https://leuven-gravity-institute.github.io)"

# ORCID's bulk works endpoint accepts a limited number of put-codes per call.
_BULK_CHUNK = 50
_TIMEOUT = 30.0


def get_json(url: str, *, timeout: float = _TIMEOUT, mailto: str | None = None) -> dict[str, Any]:
    """Fetch and parse a JSON document over HTTPS.

    Args:
        url: The absolute ``https://`` URL to fetch.
        timeout: Request timeout in seconds.
        mailto: Contact address appended to the User-Agent, which puts Crossref
            requests in its faster "polite" pool.

    Returns:
        The parsed JSON document.

    Raises:
        ValueError: If ``url`` is not an HTTPS URL.

    """
    if not url.startswith("https://"):
        raise ValueError(f"Refusing to fetch non-HTTPS URL: {url}")
    agent = f"{USER_AGENT} mailto:{mailto}" if mailto else USER_AGENT
    request = urllib.request.Request(url, headers={"User-Agent": agent, "Accept": "application/json"})  # noqa: S310 - scheme checked above
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - scheme checked above
        return json.load(response)


def fetch_orcid_works(orcid: str, *, timeout: float = _TIMEOUT) -> list[dict[str, Any]]:
    """Fetch every work on an ORCID record, as full work documents.

    ORCID returns works grouped: one group per distinct work, holding the
    summaries contributed by each source (the researcher, a publisher, a
    repository). The put-code of every summary in a group points at the same
    work, so one representative per group is fetched in bulk to obtain the
    fuller record (contributors, journal title, external identifiers).

    Args:
        orcid: The ORCID iD, e.g. ``"0000-0003-2166-0027"``.
        timeout: Per-request timeout in seconds.

    Returns:
        A list of ORCID ``work`` documents, one per group.

    """
    summary = get_json(f"{ORCID_API}/{orcid}/works", timeout=timeout)
    put_codes = [code for group in summary.get("group") or [] if (code := _group_put_code(group)) is not None]
    works: list[dict[str, Any]] = []
    for start in range(0, len(put_codes), _BULK_CHUNK):
        chunk = put_codes[start : start + _BULK_CHUNK]
        joined = ",".join(str(code) for code in chunk)
        payload = get_json(f"{ORCID_API}/{orcid}/works/{joined}", timeout=timeout)
        for element in payload.get("bulk") or []:
            work = element.get("work")
            if work:
                works.append(work)
    return works


def _group_put_code(group: dict[str, Any]) -> int | None:
    """Pick the put-code of the most informative summary in an ORCID group.

    Summaries carrying a DOI are preferred, since the fuller record behind them
    is the one worth fetching; ties fall back to the most recently modified.
    """
    summaries = group.get("work-summary") or []
    if not summaries:
        return None

    def rank(summary: dict[str, Any]) -> tuple[int, int]:
        ids = (summary.get("external-ids") or {}).get("external-id") or []
        has_doi = any((identifier.get("external-id-type") or "").lower() == "doi" for identifier in ids)
        modified = ((summary.get("last-modified-date") or {}) or {}).get("value") or 0
        return (1 if has_doi else 0, int(modified))

    return max(summaries, key=rank).get("put-code")


def fetch_crossref_work(doi: str, *, timeout: float = _TIMEOUT, mailto: str | None = None) -> dict[str, Any] | None:
    """Fetch a Crossref work record for a DOI.

    Crossref is used for the author list and journal details, which ORCID
    records carry only sporadically.

    Args:
        doi: The DOI, without the ``https://doi.org/`` prefix.
        timeout: Request timeout in seconds.
        mailto: Contact address for Crossref's polite pool.

    Returns:
        The Crossref ``message`` mapping, or ``None`` if the DOI is unknown or
        Crossref is unreachable. A failure here is never fatal: the sync falls
        back to the metadata ORCID provided.

    """
    url = f"{CROSSREF_API}/{urllib.parse.quote(doi, safe='')}"
    try:
        payload = get_json(url, timeout=timeout, mailto=mailto)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError):
        return None
    message = payload.get("message")
    return message if isinstance(message, dict) else None
