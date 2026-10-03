import os
import re

__version__ = "0.1.0"


def _build_value(name: str, default: str) -> str:
    """A build stamp from the environment, only ever plain text: it is shown in
    the pane and filed into a public issue (#144), so anything outside a short
    safe character set is dropped rather than trusted."""
    v = os.environ.get(name, "").strip()
    return v if re.fullmatch(r"[\w.+:\- ]{1,64}", v) else default


# Stamped into the image by the Dockerfile's build args (deploy/update.sh, CI);
# a source checkout run directly reports "dev" and no date.
BUILD_VERSION = _build_value("BUILD_VERSION", "dev")
BUILD_DATE = _build_value("BUILD_DATE", "")


def build_label() -> str:
    """e.g. "v0.1.0-12-gabc1234 (2026-10-03)", or just "dev" with no stamp."""
    return f"{BUILD_VERSION} ({BUILD_DATE})" if BUILD_DATE else BUILD_VERSION
