"""
Lightweight checks for the deploy/ artifacts (#6) that don't need Docker.
deploy/compose.yaml's own syntax/schema is validated separately in
.github/workflows/ci.yml's docker-build job via `docker compose config`,
which does need it - not duplicated here.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on PATH")
def test_update_sh_is_valid_posix_shell():
    result = subprocess.run(
        ["sh", "-n", str(ROOT / "deploy" / "update.sh")], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr

def test_env_example_has_no_populated_secrets():
    """A safeguard against accidentally committing a real token/id into the
    example someone copies into their own .env."""
    secret_keys = {"STORYBIBLE_TOKEN", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ALLOWED_OIDS"}
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key in secret_keys:
            assert value == "", f"{key} should be blank in .env.example, found {value!r}"

def test_env_example_documents_every_env_var_the_app_reads():
    """Cross-references .env.example against the actual os.environ reads in
    app/main.py and app/backup.py, so a new setting added to the code can't
    silently go undocumented here."""
    code = (ROOT / "app" / "main.py").read_text() + (ROOT / "app" / "backup.py").read_text()
    used = set(re.findall(r'os\.environ\.get\(["\']([A-Z_]+)["\']', code))
    used |= set(re.findall(r'os\.environ\[["\']([A-Z_]+)["\']\]', code))
    documented = set(re.findall(r"^#?\s*([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.MULTILINE))
    missing = used - documented
    assert not missing, f"{missing} read by the app but not documented in .env.example"
