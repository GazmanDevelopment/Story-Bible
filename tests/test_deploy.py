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

def test_compose_yaml_has_no_env_file_directive():
    """#36: TrueNAS's "Install via YAML" takes only the pasted text, with
    nothing alongside it - a relative env_file path doesn't resolve to
    anything real there (it looked for /tmp/.env and failed). Settings must
    stay inline in environment: instead - regression guard against
    reintroducing env_file: here. Only real (non-comment) lines count -
    compose.yaml's own explanatory comment mentions "env_file" as prose."""
    lines = (ROOT / "deploy" / "compose.yaml").read_text().splitlines()
    real_lines = [l for l in lines if not l.strip().startswith("#")]
    assert not any("env_file" in l for l in real_lines)

def test_compose_yaml_environment_block_has_the_key_settings():
    text = (ROOT / "deploy" / "compose.yaml").read_text()
    for key in ("STORYBIBLE_TOKEN", "FORWARDED_ALLOW_IPS", "AUTH_MODE", "TZ"):
        assert re.search(rf"^\s*{key}:", text, re.MULTILINE), f"{key} missing from compose.yaml's environment: block"

def test_compose_yaml_has_no_real_secrets_or_identifiers():
    """A committed real value for any of these would leak a secret, a real
    Entra tenant/client id, or the Synology's LAN IP - guards against ever
    accidentally pushing a filled-in copy of this file.

    STORYBIBLE_TOKEN in particular must stay blank, not some non-empty
    placeholder: app/main.py treats a blank token as "auth off" and says so
    honestly (/api/health reports "auth": false) - a shipped placeholder
    string would instead report "auth": true while actually being a value
    anyone who has seen this public repo could authenticate with."""
    text = (ROOT / "deploy" / "compose.yaml").read_text()
    for key in ("STORYBIBLE_TOKEN", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ALLOWED_OIDS", "FORWARDED_ALLOW_IPS"):
        assert re.search(rf'^\s*{key}: ""\s*$', text, re.MULTILINE), f'{key} is not blank ("") in compose.yaml'
