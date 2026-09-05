# Gravitational Wave Group

[![Python CI](https://github.com/Leuven-Gravity-Institute/Leuven-Gravity-Institute.github.io/actions/workflows/ci.yml/badge.svg)](https://github.com/Leuven-Gravity-Institute/Leuven-Gravity-Institute.github.io/actions/workflows/ci.yml)
[![Website](https://github.com/Leuven-Gravity-Institute/Leuven-Gravity-Institute.github.io/actions/workflows/deploy.yml/badge.svg)](https://leuven-gravity-institute.github.io/)
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/Leuven-Gravity-Institute/Leuven-Gravity-Institute.github.io/main.svg)](https://results.pre-commit.ci/latest/github/Leuven-Gravity-Institute/Leuven-Gravity-Institute.github.io/main)
[![License](https://img.shields.io/badge/License-BSD_3--Clause-blue.svg)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

The website of the Gravitational Wave Group at the Leuven Gravity Institute, KU
Leuven. All information lives in plain, structured files; the site is rendered
from them. To update the site, you edit data — not HTML.

## Scope

The site covers **one research group**, not the whole institute, and says so on
the home page. It is published at the institute's URL because the repository
lives in the institute's GitHub organisation.

Groups are nonetheless a first-class dimension of the content. `site.groups` in
`content/site.yaml` registers each group, and every person carries a `group:`
tag pointing at one. With a single group registered the group headings stay
hidden and the People page reads exactly as it would without them — but if
another Leuven Gravity Institute group later joins, you add it to `site.groups`,
tag its members, and the People page grows a heading per group on its own. No
existing entry has to be rewritten, and nothing else has to change.

Note that in-page links and asset paths are **root-absolute** (`/assets/…`,
`/people/<slug>/`). The site therefore has to be served from the domain root; a
subpath deployment would need a base-URL prefix threaded through the builder and
templates first.

## Who needs a GitHub account

Nobody needs one to appear on the site. `content/people.yaml` is data that a
maintainer writes, so anyone can have a profile page, a biography, and an
automatically synced publication list without touching GitHub. The only
identifier the automation needs is an **ORCID iD**.

A GitHub account is needed only to _edit_ the site — and only a couple of
maintainers need write access. Everyone else can send changes to a maintainer,
be added as an outside collaborator on the repository without joining the
organisation, or open a pull request through GitHub's web editor without
installing anything.

## How it is organised

| Path                            | What it holds                                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------------------------- |
| `content/`                      | The single source of truth. One YAML file per kind of information.                             |
| `schemas/`                      | A JSON Schema for each content file, defining its allowed structure.                           |
| `templates/`                    | Jinja2 templates: the shared layout, the page and profile shells, and one partial per section. |
| `assets/`                       | CSS, JavaScript, and images served as-is.                                                      |
| `src/leuven_gravity_institute/` | The generator, the ORCID publication sync, and the command-line interface.                     |
| `_site/`                        | The rendered output (generated; not committed).                                                |

The rule of thumb: **facts go in `content/`, appearance goes in `templates/` and
`assets/`.** You can update everything on the site by editing `content/` alone.

## Pages

The site is split across separate pages, each at its own clean URL (`/`,
`/people/`, `/publications/`, …). Pages are defined as data under `site.pages`
in `content/site.yaml`: every entry has a `slug` (the URL), a `label` (its name
in the navigation), and a `sections` list naming the content blocks shown on
that page, top to bottom. A block may be written as `{name: news, preview: 3}`
to show only the most recent items with a link through to its full page.

To add, remove, reorder, or regroup pages — or to move a section from one page
to another — edit `site.pages`. No template changes are needed; a block name
maps to `templates/partials/section_<name>.html`.

In addition, **one profile page is generated per person** at `/people/<slug>/`,
carrying that person's biography and their own publication list. These are not
configured in `site.pages`; they follow from `content/people.yaml`.

## Content files

Each file under `content/` is a YAML document validated against the matching
`schemas/<name>.schema.json`. Every file begins with comments explaining its
fields.

| File                        | Contents                                                                                      |
| --------------------------- | --------------------------------------------------------------------------------------------- |
| `content/site.yaml`         | Site-wide settings (URL, title, accent, navigation, pages, groups) and the group description. |
| `content/people.yaml`       | Members: role, category, group, biography, ORCID iD, and the dates they joined and left.      |
| `content/research.yaml`     | Research themes.                                                                              |
| `content/publications.yaml` | Publications. **Generated** — see [Publication sync](#publication-sync).                      |
| `content/software.yaml`     | Software releases and public datasets.                                                        |
| `content/news.yaml`         | Dated announcements.                                                                          |
| `content/events.yaml`       | Seminars, workshops, and visits; split into upcoming and past by date.                        |
| `content/teaching.yaml`     | Courses, lecture series, and student projects.                                                |
| `content/join.yaml`         | Open positions and the text around them.                                                      |
| `content/contact.yaml`      | Address, contact details, and directions.                                                     |

### Editing rules

- Keep the existing keys and nesting; change the values.
- Markdown is supported in the longer prose fields (`org.intro`, `org.about`,
  `people[].bio`, `news[].body`, `join.intro`, …) and in author names (wrap a
  group member's name in `**double asterisks**`).
- Dates are ISO format (`YYYY-MM-DD`); a person's `start`/`end` may be shortened
  to `YYYY-MM` or `YYYY`.
- A person's `id` is referenced from `publications.yaml` and from the `people`
  lists in `research.yaml`, `software.yaml`, and `teaching.yaml`. Do not change
  an `id` once publications have been synced against it.
- A person's `group` must match an `id` under `site.groups`. The test suite
  checks both of these, so a broken reference fails CI rather than the page.
- After any edit, run `validate` (below) to confirm the structure is still
  correct.

## Publication sync

`content/publications.yaml` is generated from several bibliographic databases:

```bash
uv run lgi publications sync
```

### Why more than one source

No single database is reliable for a whole group, and the failure modes are
opposites of each other:

| Source          | Strength                                                               | Weakness                                                                 |
| --------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| **ORCID**       | Self-asserted, so what is there is authoritative.                      | Only as current as its owner keeps it; one member's stops in 2018.       |
| **INSPIRE-HEP** | Assigns papers to profiles itself, so it stays current with no upkeep. | High-energy and gravitational-wave literature only; mis-files homonyms.  |
| **OpenAlex**    | Widest subject coverage, reaching work the other two never list.       | Splits one researcher across many author entities; indexes software too. |

Each member therefore carries whichever of `orcid`, `inspire`, and `openalex`
apply, in `content/people.yaml`. Every configured source is queried and the
results are unioned. A source that cannot be reached is reported and skipped;
one outage never fails the run or empties the list.

**Identifiers are pinned, never looked up by name.** Both lookups fail in ways
that quietly corrupt a list: one member here is split across nine OpenAlex
author entities holding between 1 and 264 works, and a name search readily
returns a different researcher who shares the name. To find the right OpenAlex
id, `find_authors_by_orcid` lists every entity for an ORCID, fullest first.

### Only work produced here

Each person carries `start` — the date they joined — and, once they leave,
`end`. The sync keeps only works published **inside that window**, so a new
member's earlier career does not appear under the group's name, and a departed
member's later work stops being attributed to us.

`start` must be the date they joined _this group_, which is not always the date
on their ORCID employment record: for someone who did their PhD here and stayed
on, that record points at the PhD. Where the date is unknown, `people.yaml`
keeps the identifier as a comment rather than a field, so the person still
appears on the site but nothing is attributed to them by guesswork.

Partial dates are treated as the period they name: a work dated `2024` counts if
any part of 2024 falls in the window, rather than being dropped for want of a
day. A work with no date at all is excluded, since there is no way to place it.

### One entry per paper

A paper reaches the sync repeatedly — once per member who wrote it, and once per
source that indexes it. Records are grouped **transitively**: two are the same
work when they share _any_ of DOI, arXiv id, or normalized title. Transitivity
is what catches the common case where a preprint carries only an arXiv id and
the published article only a journal DOI, and neither matches the other directly
but both match a third record carrying the two together. arXiv's own
`10.48550/arXiv.*` DOI is rewritten to a plain arXiv id first, so a preprint is
not mistaken for a separate publication.

The surviving entry lists every contributing member under `members`, which is
what the per-member pages filter on: one paper, appearing on each of its
authors' pages and exactly once in the group list.

### What is filtered out

- **Software and datasets.** Zenodo mints a fresh DOI for every GitHub release,
  and OpenAlex indexes each as a work; a first run pulled in several hundred.
  These belong on the Software & data page, from `software.yaml`.
- **Likely mis-assignments.** INSPIRE and OpenAlex assign papers automatically,
  which is what keeps them current but also files work by same-surname
  researchers under the wrong profile. A record from those sources with a short
  author list is dropped unless the member is actually named among its authors.
  Large-collaboration papers are exempt, since a member may not be listed
  individually. ORCID is trusted as it stands, being self-asserted.

Author names are matched on family name plus given name, where a bare initial
matches the name it abbreviates but two spelled-out names must agree. Matching
on the initial alone is not enough — it makes "Tie-Fu Li" indistinguishable from
"Tjonnie G. F. Li", which is exactly how another researcher's papers reached
this group's page during development.

### Collaboration publications

A paper counts as a collaboration paper when the record names a collaboration or
when it has more than fifteen authors. Those are listed in their own section, so
thousand-author papers do not bury the group's own work, and their author lists
are abbreviated to the lead author plus the group members credited.

### Curation

The merge preserves human decisions:

- Entries are matched by their stable `key`. For a match, the metadata is
  refreshed while `include`, `highlight`, and `note` are kept.
- Hand-authored entries (those without a `key`) are never touched or removed.
- Set `include: false` to hide an entry; set `highlight: true` to mark selected
  work, rendered with a star.
- A managed entry the sync no longer returns is dropped — unless the fetch came
  back empty, which is treated as an outage rather than a mass deletion.

`.github/workflows/sync-publications.yml` runs this **weekly** (Mondays, and on
demand) and opens a pull request when anything changes, so new papers are
reviewed before they go live.

## Working locally

This project uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync                  # install dependencies

uv run lgi validate      # check content against the schemas
uv run lgi build         # render the site into _site/
uv run lgi serve         # build and preview at http://localhost:8000
```

`build` validates the content first and fails fast if anything is malformed.

## Deployment

Pushing to `main` triggers `.github/workflows/deploy.yml`, which builds the site
and publishes `_site/` to GitHub Pages. Enable Pages once under **Settings →
Pages → Build and deployment → Source: GitHub Actions**.
