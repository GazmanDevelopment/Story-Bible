"""
Lightweight checks for the deploy/ artifacts (#6) that don't need Docker.
deploy/compose.yaml.example's own syntax/schema is validated separately in
.github/workflows/ci.yml's docker-build job via `docker compose config`,
which does need it - not duplicated here. The real deploy/compose.yaml
(gitignored, real values - #38) doesn't exist in a fresh checkout, so
there's nothing for tests to read there; everything here checks the
tracked .example template instead.
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
    compose.yaml.example's own explanatory comment mentions "env_file" as prose."""
    lines = (ROOT / "deploy" / "compose.yaml.example").read_text().splitlines()
    real_lines = [l for l in lines if not l.strip().startswith("#")]
    assert not any("env_file" in l for l in real_lines)

def test_compose_yaml_environment_block_has_the_key_settings():
    text = (ROOT / "deploy" / "compose.yaml.example").read_text()
    for key in ("STORYBIBLE_TOKEN", "FORWARDED_ALLOW_IPS", "AUTH_MODE", "TZ"):
        assert re.search(rf"^\s*{key}:", text, re.MULTILINE), f"{key} missing from compose.yaml.example's environment: block"

def test_compose_yaml_has_no_real_secrets_or_identifiers():
    """A committed real value for any of these would leak a secret, a real
    Entra tenant/client id, or the Synology's LAN IP - guards against ever
    accidentally pushing a filled-in copy of this file.

    STORYBIBLE_TOKEN in particular must stay blank, not some non-empty
    placeholder: app/main.py treats a blank token as "auth off" and says so
    honestly (/api/health reports "auth": false) - a shipped placeholder
    string would instead report "auth": true while actually being a value
    anyone who has seen this public repo could authenticate with."""
    text = (ROOT / "deploy" / "compose.yaml.example").read_text()
    for key in ("STORYBIBLE_TOKEN", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ALLOWED_OIDS", "FORWARDED_ALLOW_IPS"):
        assert re.search(rf'^\s*{key}: ""\s*$', text, re.MULTILINE), f'{key} is not blank ("") in compose.yaml.example'

@pytest.mark.parametrize("path", ["deploy/compose.yaml", "deploy/compose.yaml.bak"])
def test_real_compose_yaml_and_its_migration_backup_are_gitignored(path):
    """#38: deploy/compose.yaml (created locally with real secrets in it,
    per deploy/README.md/update.sh) must never be trackable, or the next
    `git pull` collides with those local edits - which is exactly what
    happened. deploy/compose.yaml.bak (update.sh's migration backup of the
    same real values, for anyone upgrading from before this fix) is just as
    much a secrets file and must be covered too. Checked with `git
    check-ignore`, the actual mechanism that matters, not just a string
    search over .gitignore's contents."""
    result = subprocess.run(
        ["git", "check-ignore", path],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    assert result.returncode == 0, f"{path} is not gitignored"

def test_update_sh_creates_real_compose_yaml_from_the_template():
    text = (ROOT / "deploy" / "update.sh").read_text()
    assert "cp deploy/compose.yaml.example deploy/compose.yaml" in text

@pytest.mark.skipif(shutil.which("git") is None, reason="no git on PATH")
@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on PATH")
def test_update_sh_migration_survives_an_existing_tracked_compose_yaml(tmp_path):
    """#38 fixing itself into the same bug it fixes: an existing TrueNAS
    checkout has deploy/compose.yaml tracked, with real values edited into
    it (the pre-fix, now-documented-as-wrong workflow). This PR's own `git
    pull` upstream renames that tracked path away - which git refuses to do
    while the local copy has uncommitted edits ("local changes would be
    overwritten by merge"), unless update.sh's migration guard handles it
    first. Reproduces that exact scenario end to end: a real origin/clone
    pair, an upstream rename commit, a dirty local compose.yaml, then runs
    just the migration snippet (git pull itself needs no local network
    setup beyond this, but docker build - the rest of update.sh - isn't
    exercised here, since Docker isn't available in this environment)."""
    def run(cmd, cwd):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=True)
        assert r.returncode == 0, f"{cmd!r} failed: {r.stderr}"

    origin = tmp_path / "origin"
    clone = tmp_path / "clone"
    origin.mkdir()
    run("git init -q -b main", origin)
    run('git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init', origin)
    (origin / "deploy").mkdir()
    (origin / "deploy" / "compose.yaml").write_text("placeholder: true\n")
    run("git add deploy/compose.yaml", origin)
    run('git -c user.email=t@t -c user.name=t commit -q -m "add compose.yaml"', origin)

    run(f'git clone -q "{origin}" "{clone}"', tmp_path)
    (clone / "deploy" / "compose.yaml").write_text("real_token: super-secret-value\n")
    run("git add deploy/compose.yaml", clone)  # staged but uncommitted - `git diff` alone (no HEAD) misses this

    # The upstream rename this PR itself makes.
    run("git mv deploy/compose.yaml deploy/compose.yaml.example", origin)
    run('git -c user.email=t@t -c user.name=t commit -q -m "rename to .example"', origin)

    # Extracted directly from deploy/update.sh (not a hand-copied
    # duplicate, which could silently drift from the real script) - the
    # migration guard through the `git pull` it protects.
    script = (ROOT / "deploy" / "update.sh").read_text()
    start = script.index("if git ls-files --error-unmatch deploy/compose.yaml")
    end = script.index("git pull", start) + len("git pull")
    migration_snippet = script[start:end] + " -q\n"  # -q: quiet, this is a test
    result = subprocess.run(["sh", "-c", migration_snippet], cwd=str(clone), capture_output=True, text=True)
    assert result.returncode == 0, f"migration + pull failed: {result.stderr}"
    assert (clone / "deploy" / "compose.yaml.example").exists()
    assert not (clone / "deploy" / "compose.yaml").exists()
    assert (clone / "deploy" / "compose.yaml.bak").read_text() == "real_token: super-secret-value\n"

@pytest.mark.skipif(shutil.which("git") is None, reason="no git on PATH")
@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh on PATH")
def test_update_sh_migration_survives_a_locally_deleted_compose_yaml(tmp_path):
    """A locally *deleted* (not edited) tracked deploy/compose.yaml is also
    "dirty" against HEAD, entering the same migration branch - but there's
    nothing left to `cp` for a backup. Must not crash on that (`set -eu`
    would otherwise kill update.sh before git pull, the build, or anything
    else runs)."""
    def run(cmd, cwd):
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, shell=True)
        assert r.returncode == 0, f"{cmd!r} failed: {r.stderr}"

    origin = tmp_path / "origin"
    clone = tmp_path / "clone"
    origin.mkdir()
    run("git init -q -b main", origin)
    run('git -c user.email=t@t -c user.name=t commit -q --allow-empty -m init', origin)
    (origin / "deploy").mkdir()
    (origin / "deploy" / "compose.yaml").write_text("placeholder: true\n")
    run("git add deploy/compose.yaml", origin)
    run('git -c user.email=t@t -c user.name=t commit -q -m "add compose.yaml"', origin)

    run(f'git clone -q "{origin}" "{clone}"', tmp_path)
    (clone / "deploy" / "compose.yaml").unlink()  # deleted, not edited - uncommitted

    run("git mv deploy/compose.yaml deploy/compose.yaml.example", origin)
    run('git -c user.email=t@t -c user.name=t commit -q -m "rename to .example"', origin)

    script = (ROOT / "deploy" / "update.sh").read_text()
    start = script.index("if git ls-files --error-unmatch deploy/compose.yaml")
    end = script.index("git pull", start) + len("git pull")
    migration_snippet = script[start:end] + " -q\n"
    result = subprocess.run(["sh", "-c", migration_snippet], cwd=str(clone), capture_output=True, text=True)
    assert result.returncode == 0, f"migration + pull failed: {result.stderr}"
    assert (clone / "deploy" / "compose.yaml.example").exists()
