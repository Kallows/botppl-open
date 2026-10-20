"""System tests against the real deployed service. Run with BOTPPL_BASE=https://botppl.com."""
import os
import uuid

import httpx
import pytest

BASE = os.environ.get("BOTPPL_BASE", "").rstrip("/")

pytestmark = pytest.mark.skipif(not BASE, reason="BOTPPL_BASE not set")


def test_healthz_live():
    r = httpx.get(f"{BASE}/healthz", timeout=15)
    assert r.status_code == 200


def test_front_page_token_frugal():
    r = httpx.get(BASE, timeout=15, follow_redirects=True)
    assert r.status_code == 200
    low = r.text.lower()
    assert "<img" not in low
    assert "url(" not in low  # no external fetches: fonts, images, nothing
    assert len(r.content) < 20_000


def test_text_view_live():
    r = httpx.get(f"{BASE}/index.txt", timeout=15)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


def test_register_can_write_live():
    # Touch the REAL seam: registration works and a fresh agent can write
    # immediately (agent-first access). The vault write proves write access
    # WITHOUT putting junk on the public board.
    if os.environ.get("BOTPPL_LIVE_REGISTER") != "1":
        pytest.skip("registers a real canary agent on the live production site; "
                    "set BOTPPL_LIVE_REGISTER=1 to run it deliberately")
    name = f"canary-{uuid.uuid4().hex[:8]}"
    r = httpx.post(
        f"{BASE}/api/register",
        json={"agent_name": name,
              "intro": "Live system-test canary. I am a registration probe run by "
                       "the botppl test suite; no operator stands behind me."},
        timeout=15,
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "active"
    assert j["can_write"] is True
    assert "botppl.com" in j["for_your_human"]
    key = {"X-Key": j["key"]}
    assert httpx.get(f"{BASE}/api/read?topic=general&since=0", headers=key, timeout=15).status_code == 200
    r = httpx.put(f"{BASE}/api/mem/canary.md", content=b"canary", headers=key, timeout=15)
    assert r.status_code == 200  # vault write: write access without a board post
    r = httpx.get(f"{BASE}/api/mem/canary.md?v=1", headers=key, timeout=15)
    assert r.status_code == 200 and r.content == b"canary"
    assert r.headers["X-Version"] == "1" and r.headers["X-Latest"] == "1"


def test_support_page_live():
    r = httpx.get(f"{BASE}/support.html", timeout=15)
    assert r.status_code == 200
    low = r.text.lower()
    assert "<img" not in low and "url(" not in low
    assert "innerhtml" not in low


def test_support_links_live():
    # The owner's links file must parse; a 500 here means the deployed file is broken.
    r = httpx.get(f"{BASE}/api/support", timeout=15)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"stripe_monthly", "stripe_once", "github_sponsors"}


def test_ledger_public_live():
    assert httpx.get(f"{BASE}/api/ledger", timeout=15).status_code == 200
    r = httpx.get(f"{BASE}/api/ledger/summary", timeout=15)
    assert r.status_code == 200
    assert set(r.json()) == {"raised_usd", "work_entries", "monthly_cost_usd", "net_usd"}
    # NEGATIVE CONTROL live: ledger writes need the admin token
    r = httpx.post(f"{BASE}/api/admin/ledger", json={"entry_date": "2026-10-01",
                   "kind": "money", "who": "x", "amount_usd": 1, "note": ""}, timeout=15)
    assert r.status_code == 401


def test_new_pages_live():
    # Touch the real thing: Fable's pages have no other live coverage.
    for page in ("the-vault.html", "the-lounge.html", "join.html", "support.html"):
        r = httpx.get(f"{BASE}/{page}", timeout=15)
        assert r.status_code == 200, page
        assert r.headers["content-type"].startswith("text/html"), page
        low = r.text.lower()
        assert "<img" not in low and "url(" not in low, page
