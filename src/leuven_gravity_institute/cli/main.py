"""Command-line entry point for building and maintaining the website.

Commands:
    build               Render the site from content into the output directory.
    validate            Check the content files against their JSON Schemas.
    serve               Build, then serve the output locally for preview.
    publications sync   Refresh the publication list from the members' ORCID records.
"""

from __future__ import annotations

import functools
import http.server
import os
import socketserver
from pathlib import Path

import typer

from leuven_gravity_institute.site import SitePaths, build_site, validate_content
from leuven_gravity_institute.site.publications_sync import sync_publications
from leuven_gravity_institute.site.validate import validate_or_raise

app = typer.Typer(
    add_completion=False,
    help="Build and manage the Leuven Gravity Institute website from its content files.",
)
publications_app = typer.Typer(add_completion=False, help="Manage the group publication list.")
app.add_typer(publications_app, name="publications")


def _paths(root: Path, output: str) -> SitePaths:
    return SitePaths(root=root.resolve(), output_dir=output)


@app.command()
def validate(
    root: Path = typer.Option(Path.cwd(), help="Project root directory."),
) -> None:
    """Validate the content files against their JSON Schemas."""
    paths = _paths(root, "_site")
    errors = validate_content(paths.content, paths.schemas)
    if errors:
        typer.secho(f"Found {len(errors)} validation error(s):", fg=typer.colors.RED, bold=True)
        for error in errors:
            typer.secho(f"  - {error}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("All content files are valid.", fg=typer.colors.GREEN)


@app.command()
def build(
    root: Path = typer.Option(Path.cwd(), help="Project root directory."),
    output: str = typer.Option("_site", help="Output directory for the built site."),
    skip_validation: bool = typer.Option(False, help="Skip schema validation before building."),
) -> None:
    """Render the site from content into the output directory."""
    paths = _paths(root, output)
    if not skip_validation:
        try:
            validate_or_raise(paths.content, paths.schemas)
        except Exception as exc:
            typer.secho("Content validation failed:", fg=typer.colors.RED, bold=True)
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from exc
    out = build_site(paths)
    typer.secho(f"Built site -> {out}", fg=typer.colors.GREEN)


@app.command()
def serve(
    root: Path = typer.Option(Path.cwd(), help="Project root directory."),
    output: str = typer.Option("_site", help="Output directory for the built site."),
    port: int = typer.Option(8000, help="Port to serve on."),
) -> None:
    """Build the site and serve it locally for preview."""
    paths = _paths(root, output)
    out = build_site(paths)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
    os.chdir(out)
    with socketserver.TCPServer(("", port), handler) as httpd:
        typer.secho(f"Serving {out} at http://localhost:{port} (Ctrl+C to stop)", fg=typer.colors.GREEN)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            typer.secho("\nStopped.", fg=typer.colors.YELLOW)


@publications_app.command("sync")
def publications_sync_command(
    root: Path = typer.Option(Path.cwd(), help="Project root directory."),
) -> None:
    """Refresh the publication list from every member's ORCID record.

    Only works published while a member was affiliated with the group are
    listed, and a paper shared by several members is deduplicated into a single
    entry crediting all of them.
    """
    paths = _paths(root, "_site")
    try:
        summary = sync_publications(paths.content / "publications.yaml", paths.content / "people.yaml")
    except Exception as exc:
        typer.secho(f"Publication sync failed: {exc}", fg=typer.colors.RED, bold=True)
        raise typer.Exit(code=1) from exc

    typer.secho(
        f"Synced {summary.members_synced} ORCID record(s): {len(summary.added)} added, "
        f"{summary.updated} updated, {len(summary.removed)} removed "
        f"({summary.out_of_window} outside a membership window, "
        f"{summary.deduplicated} duplicate(s) merged).",
        fg=typer.colors.GREEN,
    )
    for failure in summary.failures:
        typer.secho(f"  could not fetch: {failure}", fg=typer.colors.YELLOW)


if __name__ == "__main__":
    app()
