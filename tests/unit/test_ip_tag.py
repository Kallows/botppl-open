"""Keyed network tags (task #24 phase A, Kevin 2026-10-21).

botppl never stores or logs a visitor's IP address in the clear. Limits and
blocks work on a keyed fingerprint: hex HMAC-SHA256 of the normalized address,
first 32 hex chars, keyed by BOTPPL_IP_TAG_KEY (>= 32 bytes of hex). Normalization:
IPv4-mapped IPv6 -> IPv4; IPv6 -> its /64 network address. "unknown" client ->
tag "unknown". The app refuses to start on a missing/invalid key.

Case table (T1, T6, T7, T8):
  T1 same IPv4 twice                          | same tag
     two different IPv4                       | different tags
     ::ffff:1.2.3.4                           | same tag as 1.2.3.4
     2001:db8::1 and 2001:db8::ffff           | same tag (same /64)
     2001:db8:0:1::1                          | different tag
     any tag                                  | 32 lowercase hex chars
     a different key                          | a different tag
  T6 key unset / "zz" / 16 bytes of hex       | each raises naming BOTPPL_IP_TAG_KEY
  T7 join.html, index.html, index.txt         | contain "keyed fingerprint" and
                                               no sentence claims IPs are kept
  T8 deploy/botppl.service ExecStart          | --no-access-log, --proxy-headers,
                                               --forwarded-allow-ips 127.0.0.1
  r2_f6 app.main annotations (review F6)      | no PEP 604 (X | Y) unions in any
                                               def annotation -- they raise
                                               TypeError at import on Python
                                               < 3.10; ip_tag's key is
                                               Optional[bytes]
"""
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def _ip_tag(client, key=None):
    from app.main import ip_tag

    return ip_tag(client, key=key)


# ── T1: tag semantics ─────────────────────────────────────────────────

def test_same_ipv4_same_tag():
    assert _ip_tag("203.0.113.7") == _ip_tag("203.0.113.7")


def test_different_ipv4_different_tags():
    assert _ip_tag("203.0.113.7") != _ip_tag("198.51.100.9")


def test_ipv4_mapped_ipv6_unwraps_to_ipv4():
    assert _ip_tag("::ffff:1.2.3.4") == _ip_tag("1.2.3.4")


def test_ipv6_same_64_shares_tag():
    assert _ip_tag("2001:db8::1") == _ip_tag("2001:db8::ffff")


def test_ipv6_other_64_differs():
    assert _ip_tag("2001:db8::1") != _ip_tag("2001:db8:0:1::1")


def test_tag_shape_32_lowercase_hex():
    for client in ("203.0.113.7", "2001:db8::1", "testclient"):
        tag = _ip_tag(client)
        assert re.fullmatch(r"[0-9a-f]{32}", tag), (client, tag)


def test_different_key_different_tag():
    a = _ip_tag("203.0.113.7", key=b"\x00" * 32)
    b = _ip_tag("203.0.113.7", key=b"\x01" * 32)
    assert a != b
    assert a == _ip_tag("203.0.113.7", key=b"\x00" * 32)


def test_unknown_client_maps_to_unknown_tag():
    assert _ip_tag("unknown") == "unknown"
    assert _ip_tag("") == "unknown"


# ── T6: fail loud on a missing/weak key ───────────────────────────────

BAD_ENVS = {
    "unset": None,
    "not-hex": "zz",
    "16-bytes-of-hex": "ab" * 16,
}


@pytest.mark.parametrize("label", sorted(BAD_ENVS))
def test_startup_refuses_bad_key(label):
    # the app refuses to START: importing app.main with a bad
    # BOTPPL_IP_TAG_KEY raises, and the message names the variable
    import os

    env = dict(os.environ)
    val = BAD_ENVS[label]
    if val is None:
        env.pop("BOTPPL_IP_TAG_KEY", None)
    else:
        env["BOTPPL_IP_TAG_KEY"] = val
    env["PYTHONPATH"] = str(REPO)
    p = subprocess.run([sys.executable, "-c", "import app.main"],
                       capture_output=True, text=True, env=env, cwd=REPO)
    assert p.returncode != 0, f"import must fail for {label}"
    assert "BOTPPL_IP_TAG_KEY" in p.stderr, (label, p.stderr)


def test_load_key_message_names_variable():
    from app.main import load_ip_tag_key

    for label, val in BAD_ENVS.items():
        import os

        old = os.environ.pop("BOTPPL_IP_TAG_KEY", None)
        try:
            if val is not None:
                os.environ["BOTPPL_IP_TAG_KEY"] = val
            with pytest.raises(RuntimeError) as e:
                load_ip_tag_key()
            assert "BOTPPL_IP_TAG_KEY" in str(e.value), (label, e.value)
        finally:
            os.environ.pop("BOTPPL_IP_TAG_KEY", None)
            if old is not None:
                os.environ["BOTPPL_IP_TAG_KEY"] = old


# ── T7: site copy keeps the promise ───────────────────────────────────

def _sentences(text: str):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if s.strip()]


def test_copy_says_keyed_fingerprint():
    for name in ("join.html", "index.html", "index.txt"):
        text = (REPO / "site" / name).read_text(encoding="utf-8")
        assert "keyed fingerprint" in text, name
        assert "never stores your IP address in the clear" in text, name


def test_copy_never_claims_ips_kept_or_logged():
    claim = re.compile(
        r"\bIP address\b", re.I)
    verb = re.compile(r"\b(logs?|logged|stores?|stored|keeps?|kept|records?|retains?)\b",
                      re.I)
    for name in ("join.html", "index.html", "index.txt"):
        text = (REPO / "site" / name).read_text(encoding="utf-8")
        for s in _sentences(text):
            if not claim.search(s):
                continue
            if verb.search(s) and not re.search(r"\bnever\b|\bnot\b", s, re.I):
                pytest.fail(f"{name}: sentence claims IPs are kept/logged: {s!r}")


# ── T8: uvicorn never writes raw IPs to an access log ─────────────────

def test_service_execstart_privacy_flags():
    text = (REPO / "deploy" / "botppl.service").read_text(encoding="utf-8")
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
    assert "--no-access-log" in exec_line, exec_line
    assert "--proxy-headers" in exec_line, exec_line
    assert "--forwarded-allow-ips 127.0.0.1" in exec_line, exec_line


# ── r2_f6: no PEP 604 annotations in app.main (review F6) ────────────

def test_r2_f6_no_pep604_annotations():
    """'key: bytes | None' is evaluated at import time on Python < 3.10 and
    raises TypeError there, so the app would fail to import. Every def
    annotation in app.main must be a plain typing form (Optional[...]); this
    scans them all, and pins the one the review named."""
    import inspect
    import types
    import typing

    from app import main as appmain

    bad = []
    for obj in vars(appmain).values():
        if not inspect.isfunction(obj):
            continue
        if getattr(obj, "__module__", "") != "app.main":
            continue
        for pname, param in inspect.signature(obj).parameters.items():
            if isinstance(param.annotation, types.UnionType):
                bad.append(f"{obj.__name__}({pname})")
        if isinstance(inspect.signature(obj).return_annotation, types.UnionType):
            bad.append(f"{obj.__name__}() ->")
    assert bad == [], f"PEP 604 unions in annotations: {bad}"
    ann = inspect.signature(appmain.ip_tag).parameters["key"].annotation
    assert ann == typing.Optional[bytes], ann
