"""Validation of content files against their JSON Schemas.

Each content file ``<name>.yaml`` is checked against ``<name>.schema.json`` in
the schemas directory when a matching schema exists. This keeps edits to the
content honest: a typo'd key or a missing required field is reported clearly
instead of silently producing a broken page.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from leuven_gravity_institute.site.content import load_content


class ValidationError(Exception):
    """Raised when one or more content files fail schema validation."""


def _load_schema(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _duplicate_errors(items: list[dict[str, Any]], field: str, source: str) -> list[str]:
    """Report values of ``field`` that appear on more than one item."""
    counts = Counter(item[field] for item in items if item.get(field))
    return [f"{source}: duplicate {field} {value!r} ({count} entries)" for value, count in counts.items() if count > 1]


def semantic_errors(content: dict[str, Any]) -> list[str]:
    """Check the rules that span files, which a per-file schema cannot see.

    A JSON Schema validates one document in isolation, so it cannot notice two
    people sharing a ``slug`` — which would silently overwrite one profile page
    with the other, since both render to the same path — nor a ``group`` naming
    a group that does not exist.

    Args:
        content: The loaded content, keyed by filename stem.

    Returns:
        Human-readable error messages; empty when everything is consistent.

    """
    errors: list[str] = []
    people = (content.get("people") or {}).get("items") or []
    errors += _duplicate_errors(people, "id", "people.yaml")
    errors += _duplicate_errors(people, "slug", "people.yaml")

    groups = ((content.get("site") or {}).get("site") or {}).get("groups") or []
    errors += _duplicate_errors(groups, "id", "site.yaml")

    known_groups = {group["id"] for group in groups if group.get("id")}
    for person in people:
        if person.get("group") and person["group"] not in known_groups:
            errors.append(f"people.yaml: {person.get('id')!r} is in unknown group {person['group']!r}")

    known_people = {person["id"] for person in people if person.get("id")}
    for group in groups:
        if group.get("lead") and group["lead"] not in known_people:
            errors.append(f"site.yaml: group {group.get('id')!r} has unknown lead {group['lead']!r}")

    return errors


def validate_content(content_dir: Path, schemas_dir: Path) -> list[str]:
    """Validate every content file that has a matching schema.

    Args:
        content_dir: Directory containing the content files.
        schemas_dir: Directory containing ``<name>.schema.json`` files.

    Returns:
        A list of human-readable error messages. Empty when everything is valid.

    """
    content = load_content(content_dir)
    errors: list[str] = []
    for name, document in content.items():
        schema_path = schemas_dir / f"{name}.schema.json"
        if not schema_path.exists():
            continue
        validator = Draft202012Validator(_load_schema(schema_path))
        for error in sorted(validator.iter_errors(document), key=str):
            location = "/".join(str(part) for part in error.absolute_path) or "(root)"
            errors.append(f"{name}.yaml: at '{location}': {error.message}")
    return errors + semantic_errors(content)


def validate_or_raise(content_dir: Path, schemas_dir: Path) -> None:
    """Validate content and raise :class:`ValidationError` if anything fails.

    Args:
        content_dir: Directory containing the content files.
        schemas_dir: Directory containing the schema files.

    Raises:
        ValidationError: If any content file fails validation.

    """
    errors = validate_content(content_dir, schemas_dir)
    if errors:
        raise ValidationError("\n".join(errors))
