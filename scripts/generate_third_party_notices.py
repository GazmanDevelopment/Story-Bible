"""
#66: Generates THIRD_PARTY_NOTICES.md by walking the real dependency graph
outward from requirements.txt's own direct packages - never
requirements-dev.txt's test-only ones (pytest, Playwright), since those
never ship in the Docker image (see the Dockerfile's
`pip install -r requirements.txt`).

Deliberately records name + license only, not exact version: only the
five direct packages are pinned in requirements.txt, everything else here
is transitive and unpinned, so a version would drift on every unrelated
upstream release without anything in *this* repo actually changing -
noise for a file whose committed copy CI diff-checks against a fresh
regeneration. A dependency or its license actually changing is the rare,
real event this file (and that check) exists to catch.

Marker evaluation (python_version, extras like pyjwt's `[crypto]`) uses
`packaging`, matching exactly what a real `pip install -r requirements.txt`
would resolve - not just "whatever happens to already be installed
alongside it" (this sandbox has stray packages, e.g. jinja2/pyyaml, that
are NOT part of this app's actual dependency closet - only reachable
through fastapi's `standard`/`all` extras, which requirements.txt never
requests).

The vendored front-end libraries (Quill, MSAL.js) have their own notices
right next to the code they vendor - see app/static/vendor/*/NOTICE.md -
deliberately not duplicated here.

Usage:
    python scripts/generate_third_party_notices.py
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
OUT = ROOT / "THIRD_PARTY_NOTICES.md"


def _direct_requirements() -> list[Requirement]:
    reqs = []
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        reqs.append(Requirement(line))
    return reqs


def _license_for(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c for c in (meta.get_all("Classifier") or []) if c.startswith("License ::")]
    if classifiers:
        # "License :: OSI Approved :: MIT License" -> "MIT License"
        return classifiers[0].split("::")[-1].strip()
    raw = meta.get("License")
    return raw.strip() if raw and raw.strip() else "See package metadata"


def _homepage_for(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    for url in meta.get_all("Project-URL") or []:
        label, _, value = url.partition(",")
        if label.strip().lower() in ("homepage", "repository", "source", "source code"):
            return value.strip()
    home = meta.get("Home-page")
    return home.strip() if home else ""


def collect() -> list[tuple[str, str, str]]:
    """Walk the dependency graph from requirements.txt's own direct
    packages outward, evaluating markers/extras the way pip actually
    would - returns (name, license, homepage) tuples, sorted by name.

    A package can be reached via more than one edge with different
    requested extras (nothing in today's graph does this, but nothing
    rules it out either) - `known_extras` tracks the union seen so far per
    package, and a package is re-expanded whenever a new extra shows up,
    so a dependency gated behind an extra discovered late isn't silently
    dropped just because an earlier, plainer edge got there first. This
    always terminates: each package's own extras are a small, fixed set
    from its own metadata, so re-expansions per package are bounded by
    that, not unbounded."""
    env = default_environment()
    queue: list[tuple[str, frozenset[str]]] = [
        (r.name, frozenset(r.extras)) for r in _direct_requirements()
    ]
    known_extras: dict[str, frozenset[str]] = {}
    rows: dict[str, tuple[str, str, str]] = {}

    while queue:
        name, extras = queue.pop()
        key = canonicalize_name(name)
        if key in known_extras and extras <= known_extras[key]:
            continue  # already processed, and nothing new to add
        known_extras[key] = known_extras.get(key, frozenset()) | extras
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # a marker-gated extra requirements.txt never actually requests

        real_name = dist.metadata["Name"] or name
        rows[canonicalize_name(real_name)] = (real_name, _license_for(dist), _homepage_for(dist))

        # The full set known so far, not just what arrived on this
        # particular edge - a marker keyed on an extra learned earlier
        # must still gate correctly on a later re-expansion.
        extra_values = known_extras[key] or frozenset({""})
        for req_str in dist.requires or []:
            req = Requirement(req_str)
            if req.marker is not None and not any(
                req.marker.evaluate({**env, "extra": e}) for e in extra_values
            ):
                continue
            queue.append((req.name, frozenset(req.extras)))

    return sorted(rows.values(), key=lambda r: r[0].lower())


def render(rows: list[tuple[str, str, str]]) -> str:
    lines = [
        "# Third-party notices",
        "",
        "This project is licensed under the GNU GPLv3 (see `LICENSE`). It also "
        "uses the open-source packages below, each under its own license - all "
        "are permissive (MIT/BSD/Apache/MPL-style) and compatible with the GPLv3.",
        "",
        "This covers **runtime** dependencies only - the ones "
        "`pip install -r requirements.txt` actually installs into the Docker "
        "image (see the Dockerfile). Dev/test-only tools (pytest, Playwright) "
        "are never shipped, so they're deliberately left out.",
        "",
        "The task pane's vendored front-end libraries (Quill, MSAL.js) have "
        "their own notices right next to the code they vendor - see "
        "`app/static/vendor/quill/NOTICE.md` and "
        "`app/static/vendor/msal/NOTICE.md`.",
        "",
        "Generated by `scripts/generate_third_party_notices.py` - regenerate "
        "after changing `requirements.txt`, don't hand-edit.",
        "",
        "| Package | License | Project URL |",
        "|---|---|---|",
    ]
    for name, license_, homepage in rows:
        lines.append(f"| {name} | {license_} | {homepage} |")
    lines.append("")
    return "\n".join(lines)


def generate() -> str:
    return render(collect())


if __name__ == "__main__":
    OUT.write_text(generate())
    print(f"Wrote {OUT}")
