"""System test for weak-comparison If-None-Match behind an encoder (round-2
finding 1). Run with BOTPPL_BASE=https://botppl.com.

The deploy puts Caddy's `encode` in front of the app, which may re-mark our
strong ETag weak on an encoded response, so the real seam is: GET
/api/public with Accept-Encoding: gzip, then echo the ETag the server sent
back VERBATIM (weak prefix included) and expect 304. Never run this against
the network from a task run; it only runs when BOTPPL_BASE is set.
"""
import os

import httpx
import pytest

BASE = os.environ.get("BOTPPL_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="BOTPPL_BASE not set")


def test_public_etag_echo_304_with_gzip():
    first = httpx.get(f"{BASE}/api/public",
                      headers={"Accept-Encoding": "gzip"}, timeout=15)
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "the public read must carry an ETag even behind gzip"
    again = httpx.get(f"{BASE}/api/public",
                      headers={"Accept-Encoding": "gzip", "If-None-Match": etag},
                      timeout=15)
    assert again.status_code == 304, (
        f"If-None-Match {etag!r} must answer 304 (weak comparison); "
        f"got {again.status_code}"
    )
    assert again.content == b""
