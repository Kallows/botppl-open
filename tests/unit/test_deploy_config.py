"""Deploy-config lock tests (task #17, tests only).

Plain-text checks on deploy/Caddyfile, deploy/botppl.service and
deploy/backup.sh. Each checker is a small function that takes the file text
and returns a list of problems, so negative controls can feed it a MUTATED
copy (one requirement dropped or inverted) and assert the checker reports a
problem. The real files must report none.

These are text checks, not Caddy/systemd parsers: they pin the exact security
bearing lines so a hostile PR that weakens any of them fails CI.
"""
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _read(name):
    return (DEPLOY / name).read_text(encoding="utf-8")


# ── Checkers ──────────────────────────────────────────────────────────

def check_caddyfile(text):
    problems = []
    if "request_body" not in text:
        problems.append("missing request_body block")
    if "max_size 512KB" not in text:
        problems.append("missing request_body max_size 512KB")
    if "Strict-Transport-Security" not in text:
        problems.append("missing Strict-Transport-Security header")
    if 'X-Content-Type-Options "nosniff"' not in text:
        problems.append('missing X-Content-Type-Options "nosniff"')
    if 'X-Frame-Options "DENY"' not in text:
        problems.append('missing X-Frame-Options "DENY"')
    if "-Server" not in text:
        problems.append("missing -Server (Server header not suppressed)")
    if "trusted_proxies" in text:
        problems.append("must not contain trusted_proxies")
    return problems


def check_service(text):
    problems = []
    if "NoNewPrivileges=yes" not in text:
        problems.append("missing NoNewPrivileges=yes")
    if "ProtectSystem=strict" not in text:
        problems.append("missing ProtectSystem=strict")
    if "MemoryMax=" not in text:
        problems.append("missing MemoryMax=")
    if "--no-server-header" not in text:
        problems.append("missing --no-server-header")
    if "--host 127.0.0.1" not in text:
        problems.append("API must bind --host 127.0.0.1")
    rw = [ln.strip() for ln in text.splitlines()
          if ln.strip().startswith("ReadWritePaths=")]
    if rw != ["ReadWritePaths=/var/lib/botppl"]:
        problems.append(
            f"ReadWritePaths must be exactly /var/lib/botppl, got {rw!r}")
    if "User=botppl" not in text:
        problems.append("missing User=botppl")
    return problems


def check_backup(text):
    problems = []
    if "umask 077" not in text:
        problems.append("missing umask 077")
    if "/var/backups/botppl" not in text:
        problems.append("backups must live in /var/backups/botppl")
    if "chmod 600" not in text:
        problems.append("missing chmod 600 on the backup file")
    # Backup files must not be written under /var/lib/botppl (the app user's
    # writable dir). Comment lines may mention it; code lines may only use it
    # as the DB= read source.
    for ln in text.splitlines():
        stripped = ln.strip()
        if stripped.startswith("#") or "/var/lib/botppl" not in stripped:
            continue
        if not stripped.startswith("DB="):
            problems.append(
                f"backup write target under /var/lib/botppl: {stripped!r}")
    return problems


CHECKERS = {
    "Caddyfile": check_caddyfile,
    "botppl.service": check_service,
    "backup.sh": check_backup,
}


# ── Real files must be clean ──────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(CHECKERS))
def test_real_deploy_file_clean(name):
    problems = CHECKERS[name](_read(name))
    assert problems == []


# ── Negative controls: each mutation must be caught ───────────────────

def _caddy_drop_max_size(t):
    return t.replace("\t\tmax_size 512KB\n", "")


def _caddy_add_trusted_proxies(t):
    return t.replace(
        "\thandle /api/* {",
        "\ttrusted_proxies static private_ranges\n\thandle /api/* {")


def _service_host_public(t):
    return t.replace("--host 127.0.0.1", "--host 0.0.0.0")


def _service_second_readwritepaths(t):
    return t.replace(
        "ReadWritePaths=/var/lib/botppl",
        "ReadWritePaths=/var/lib/botppl /var/tmp/botppl")


def _service_drop_nonewprivileges(t):
    return t.replace("NoNewPrivileges=yes\n", "")


def _service_drop_memorymax(t):
    return t.replace("MemoryMax=600M\n", "")


def _backup_drop_umask(t):
    return t.replace("umask 077\n", "")


def _backup_drop_chmod600(t):
    return t.replace('chmod 600 "$OUT"\n', "")


def _backup_write_under_varlib(t):
    return t.replace("DIR=/var/backups/botppl", "DIR=/var/lib/botppl/backups")


MUTATIONS = [
    ("Caddyfile", "max_size dropped", _caddy_drop_max_size),
    ("Caddyfile", "trusted_proxies added", _caddy_add_trusted_proxies),
    ("botppl.service", "--host moved to 0.0.0.0", _service_host_public),
    ("botppl.service", "second ReadWritePaths added", _service_second_readwritepaths),
    ("botppl.service", "NoNewPrivileges dropped", _service_drop_nonewprivileges),
    ("botppl.service", "MemoryMax dropped", _service_drop_memorymax),
    ("backup.sh", "umask dropped", _backup_drop_umask),
    ("backup.sh", "chmod 600 dropped", _backup_drop_chmod600),
    ("backup.sh", "backup dir moved under /var/lib/botppl", _backup_write_under_varlib),
]


@pytest.mark.parametrize("name,desc,mutate", MUTATIONS, ids=[f"{n}:{d}" for n, d, _ in MUTATIONS])
def test_mutation_is_caught(name, desc, mutate):
    checker = CHECKERS[name]
    mutated = mutate(_read(name))
    # the mutation really changed the text, and the checker noticed
    assert mutated != _read(name), f"mutation {desc} did not change the file"
    problems = checker(mutated)
    assert problems, f"checker missed mutation: {desc}"
