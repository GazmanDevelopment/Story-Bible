"""
Generates manifest.dev.xml / manifest.prod.xml from manifest.template.xml
(#8), so the two environments can't drift the way a single hand-edited
manifest.xml did (eight-plus places hardcoding a URL).

Each environment has its own fixed <Id>. Word ties trust and settings to
that GUID, so it must stay stable across regenerations of the SAME
environment - regenerating manifest.prod.xml a second time must produce
the same Id, or Word would treat it as a brand new add-in. Only a genuinely
new environment should ever get a freshly generated one.

Usage:
    python scripts/generate_manifest.py            # both dev and prod
    python scripts/generate_manifest.py dev
    python scripts/generate_manifest.py prod
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "manifest.template.xml"

# Fixed once, per environment - see module docstring. Generated with
# uuid.uuid4() when this system was set up (2026-09-26); never regenerate.
ENVIRONMENTS = {
    "dev": {
        "id": "210b7dd9-450e-4512-90ed-3c6006922404",
        "base_url": "https://localhost:3000",
        "app_domains": ["https://localhost:3000"],
    },
    "prod": {
        "id": "46fa4c7d-1d06-435a-a4f8-d10ee398022c",
        "base_url": "https://storybible.huscroft.com.au",
        "app_domains": [
            "https://storybible.huscroft.com.au",
            # The Office dialog-API sign-in fallback for Word without
            # nested app auth support (#13) - added now so this file
            # doesn't need touching again once that lands.
            "https://login.microsoftonline.com",
        ],
    },
}


def _version() -> str:
    # Deliberately not ROOT: tests monkeypatch that to redirect generate()'s
    # *output* elsewhere, which must not also change where this looks for
    # the `app` package to import - the two are unrelated concerns that
    # happen to share this module, not the same path.
    project_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(project_root))
    from app import __version__  # just the version string - app/__init__.py has no other side effects
    # Office manifest <Version> must be exactly 4 dot-separated integers,
    # AND office-addin-manifest's validator rejects a major version of 0
    # ("Manifest Version Too Low") - confirmed by actually running it, not
    # assumed. app.__version__ is still pre-1.0 (0.x while the app itself
    # is early), so floor the major component at 1 for the manifest only;
    # this doesn't touch app.__version__ itself, and stops being a no-op
    # shim the moment the app reaches a real 1.x.
    major, minor, patch = (int(p) for p in __version__.split("."))
    return f"{max(major, 1)}.{minor}.{patch}.0"


def generate(env: str) -> Path:
    cfg = ENVIRONMENTS[env]
    xml = TEMPLATE.read_text()
    xml = xml.replace("{{ID}}", cfg["id"])
    xml = xml.replace("{{VERSION}}", _version())
    xml = xml.replace("{{BASE_URL}}", cfg["base_url"])
    domains = "\n".join(f"    <AppDomain>{d}</AppDomain>" for d in cfg["app_domains"])
    xml = xml.replace("{{APP_DOMAINS}}", domains)
    out = ROOT / f"manifest.{env}.xml"
    out.write_text(xml)
    if env == "prod":
        # #61: also publish prod's manifest via GitHub Pages, so it's a
        # stable download for sideloading onto a new laptop (Word -> Insert
        # -> Add-ins -> More Add-ins -> My Add-ins -> Upload My Add-in)
        # without a network-share Trusted Add-in Catalog each time - see
        # docs/index.html for the actual steps. dev's manifest points at
        # localhost, so it's never published here.
        pages_copy = ROOT / "docs" / "manifest.xml"
        pages_copy.parent.mkdir(parents=True, exist_ok=True)
        pages_copy.write_text(xml)
    return out


if __name__ == "__main__":
    envs = sys.argv[1:] or list(ENVIRONMENTS)
    for env in envs:
        if env not in ENVIRONMENTS:
            sys.exit(f"Unknown environment '{env}' - choose from {list(ENVIRONMENTS)}")
    for env in envs:
        path = generate(env)
        print(f"Wrote {path}")
