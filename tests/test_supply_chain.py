"""
#74 / #76: supply-chain and deployment hardening. These are guard rails on
config files - the point is that a later edit can't quietly undo them.
"""
import fnmatch
import os
import re
import stat
from pathlib import Path

import tempfile

if "STORYBIBLE_DB" not in os.environ:   # same pattern as every other test module: app.main reads this at import
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ------------------------------------------------ actions pinned (F23 / #76)
def _uses_lines():
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*(?:-\s*)?uses:\s*(\S+)(.*)$", line)
            if m and not m.group(1).startswith("./"):
                yield wf.name, n, m.group(1), m.group(2)


def test_there_are_workflows_and_actions_to_check():
    assert {wf.name for wf in WORKFLOWS} >= {"ci.yml", "security-audit.yml"}
    assert len(list(_uses_lines())) >= 6


@pytest.mark.parametrize("wf,n,ref,comment", list(_uses_lines()), ids=lambda v: str(v)[:40])
def test_every_action_is_pinned_to_a_full_commit_sha_with_a_version_comment(wf, n, ref, comment):
    """A tag can be moved to different code; a commit SHA cannot."""
    assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), f"{wf}:{n} {ref} is not pinned to a SHA"
    assert re.search(r"#\s*v\d+", comment), f"{wf}:{n} {ref} has no '# vN' comment saying which release the SHA is"


def test_actions_cache_is_no_longer_the_node20_release():
    """#76: actions/cache v4 runs on Node 20, which GitHub is retiring; v5+ run on Node 24.
    (Checks 'v5 or later', not a particular SHA, so a Dependabot bump doesn't fail it.)"""
    ci = _text(".github/workflows/ci.yml")
    m = re.search(r"uses: actions/cache@[0-9a-f]{40} # v(\d+)", ci)
    assert m and int(m.group(1)) >= 5, "actions/cache should be pinned at v5 or later"


def test_the_manifest_validator_is_pinned_to_a_version():
    ci = _text(".github/workflows/ci.yml")
    calls = re.findall(r"npx --yes (office-addin-manifest\S*) validate", ci)
    assert calls and all(re.fullmatch(r"office-addin-manifest@\d+\.\d+\.\d+", c) for c in calls), calls


# -------------------------------------------------------- Dependabot + audits
def test_dependabot_covers_python_docker_and_actions_weekly():
    blocks = {}
    for block in _text(".github/dependabot.yml").split("- package-ecosystem:")[1:]:
        blocks[block.split()[0]] = block
    assert {"pip", "docker", "github-actions"} <= set(blocks)
    for name in ("pip", "docker", "github-actions"):
        assert re.search(r"interval:\s*weekly", blocks[name]), f"{name} isn't checked weekly"


def test_the_audit_workflow_scans_python_and_vendored_js_on_a_schedule():
    wf = _text(".github/workflows/security-audit.yml")
    assert re.search(r"schedule:\s*\n\s*-\s*cron:", wf), "no weekly schedule"
    assert "workflow_dispatch" in wf
    assert "pip-audit -r requirements.txt" in wf and "pip-audit -r requirements-dev.txt" in wf
    assert re.search(r"pip-audit==\d+\.\d+\.\d+", wf), "pip-audit itself should be pinned"
    assert "python scripts/check_vendored_js.py --audit" in wf
    assert re.search(r"permissions:\s*\n\s*contents: read", wf)


def test_the_audit_is_kept_out_of_the_main_ci_so_a_new_advisory_cannot_block_unrelated_prs():
    assert "pip-audit" not in _text(".github/workflows/ci.yml")


# ---------------------------------------------------------------- Dockerfile
def test_base_image_is_pinned_by_digest_and_pip_by_version():
    dockerfile = _text("Dockerfile")
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", dockerfile, re.MULTILINE)
    assert re.search(r"pip install --no-cache-dir pip==\d+\.\d+(\.\d+)?", dockerfile)
    assert "--upgrade pip" not in dockerfile


def test_bytecode_is_precompiled_because_the_root_filesystem_is_read_only():
    assert "compileall" in _text("Dockerfile")


# ----------------------------------------------------- container hardening (F22)
HARDENING = ("read_only: true", "no-new-privileges:true", "pids_limit: 256")


def test_compose_template_is_hardened():
    compose = _text("deploy/compose.yaml.example")
    live = "\n".join(l for l in compose.splitlines() if not l.lstrip().startswith("#"))
    for setting in HARDENING:
        assert setting in live, f"{setting} missing from deploy/compose.yaml.example"
    assert re.search(r"cap_drop:\s*\n\s*-\s*ALL", live)
    assert re.search(r"tmpfs:\s*\n\s*-\s*/tmp:size=\d+m", live)   # an explicit size, not Docker's default
    assert 'user: "568:568"' in live


def test_ci_runs_the_container_with_the_same_hardening_as_compose():
    """If the app ever needed a writable root fs or extra privileges, CI - not the NAS - should find out."""
    ci = _text(".github/workflows/ci.yml")
    run = ci[ci.index("docker run -d"):ci.index("story-bible:ci\n", ci.index("docker run -d"))]
    for flag in ("--read-only", "--tmpfs /tmp:size=128m", "--cap-drop ALL", "--security-opt no-new-privileges:true",
                 "--pids-limit 256", "--user 568:568"):
        assert flag in run, f"CI's container run lacks {flag}"


# ------------------------------------------------- network exposure (F21) docs
def test_the_exposed_plain_http_port_is_explained_where_it_is_published():
    compose = _text("deploy/compose.yaml.example")
    assert "PLAIN HTTP" in compose and 'Network exposure' in compose
    readme = _text("deploy/README.md")
    assert "## Network exposure" in readme
    assert "curl -m 5 http://<nas-ip>:2285/api/health" in readme      # how to verify it worked
    assert "2285" in readme and "Synology" in readme


def test_accepted_risks_are_recorded_in_security_md():
    sec = _text("SECURITY.md")
    for phrase in ("Subresource Integrity", "localStorage", "plaintext", "CVE-2025-15056", "office.js"):
        assert phrase in sec, f"SECURITY.md doesn't mention {phrase}"


def test_backup_docs_say_snapshots_do_not_survive_losing_the_pool():
    doc = " ".join(_text("docs/BACKUP.md").split())   # normalise the line wrapping
    assert "does not survive losing the pool" in doc and "off the box" in doc


# ---------------------------------------------------- ignore-file case fix
@pytest.mark.parametrize("ignore_file", [".gitignore", ".dockerignore"])
def test_entra_notes_file_is_ignored_in_either_spelling(ignore_file):
    """Ignore patterns are case-sensitive on Linux (CI, Docker) though not on
    Windows: the file is EntraIDs.txt, and one spelling isn't enough."""
    patterns = [l.strip() for l in _text(ignore_file).splitlines() if l.strip() and not l.startswith("#")]
    for name in ("EntraIDs.txt", "EntraIds.txt"):
        assert any(fnmatch.fnmatchcase(name, p) for p in patterns), f"{ignore_file} doesn't ignore {name}"


# ------------------------------------------------- backup file modes (F24)
@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_backups_are_created_owner_only(tmp_path, monkeypatch):
    from app import backup, main
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    old_umask = os.umask(0o022)   # a permissive umask, to prove the app doesn't rely on the environment's
    try:
        result = backup.run_backup()
    finally:
        os.umask(old_umask)
    bdir = tmp_path / "bk"
    assert stat.S_IMODE(bdir.stat().st_mode) == 0o700
    assert stat.S_IMODE(result["db_backup"].stat().st_mode) == 0o600
    assert stat.S_IMODE(result["json_dir"].stat().st_mode) == 0o700
    assert stat.S_IMODE((bdir / "json").stat().st_mode) == 0o700
    assert stat.S_IMODE((bdir / ".last_success").stat().st_mode) == 0o600
    for f in result["json_dir"].glob("*.json"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_backup_asks_for_owner_only_modes_on_every_artifact(tmp_path, monkeypatch):
    """Platform-independent twin of the test above: records the chmod calls
    rather than checking the resulting file modes, so it also runs where
    chmod is a no-op (Windows)."""
    from app import backup, main
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    with main.db() as con:   # make sure at least one series exists, so a JSON export file is produced
        con.execute("INSERT OR IGNORE INTO series (id, data, updated, owner_oid) VALUES ('modes1', '{\"name\": \"m\"}', 0, '')")
    calls = []
    real = os.chmod
    monkeypatch.setattr(backup.os, "chmod", lambda p, m: (calls.append((Path(p), m)), real(p, m))[1])
    result = backup.run_backup()
    asked = dict(calls)
    bk = tmp_path / "bk"
    assert asked[bk] == 0o700
    assert asked[bk / "json"] == 0o700
    assert asked[result["json_dir"]] == 0o700
    assert asked[result["db_backup"]] == 0o600
    assert asked[bk / ".last_success"] == 0o600
    exports = list(result["json_dir"].glob("*.json"))
    assert exports and all(asked[f] == 0o600 for f in exports)


def test_backup_folders_are_created_owner_only_not_tightened_afterwards(tmp_path, monkeypatch):
    """The window between 'file written' and 'chmod' must not exist for a folder
    that is private from the moment it appears - so folders are mkdir'd 0700."""
    from app import backup
    made = []
    real_mkdir = Path.mkdir
    monkeypatch.setattr(Path, "mkdir", lambda self, mode=0o777, parents=False, exist_ok=False:
                        (made.append((self, mode)), real_mkdir(self, mode, parents, exist_ok))[1])
    backup._private_dir(tmp_path / "a" / "b" / "c")
    assert [m for _, m in made] == [0o700, 0o700, 0o700] and [p.name for p, _ in made] == ["a", "b", "c"]


def test_a_failed_chmod_is_logged_not_silent(tmp_path, monkeypatch):
    import io
    import logging

    from app import backup, main
    monkeypatch.setattr(backup.os, "chmod", lambda p, m: (_ for _ in ()).throw(PermissionError("no")))
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    main.logger.addHandler(handler)
    try:
        backup._private(tmp_path, 0o700)     # must not raise...
    finally:
        main.logger.removeHandler(handler)
    assert "couldn't restrict" in buf.getvalue()   # ...but must not be silent either
