"""GET /api/classifieds -- the five ad boards, public, no key.

PUBLIC RULE under test (Kevin 2026-10-20: ads are public by default): every
top-level ad with mod_state='ok' is public, promote flag ignored. Held bodies,
replies under an ad, and posts in other topics must never appear.
Topics are free-form, so ads-buy/ads-sell/ads-trade/ads-jobs/ads-personals
are ordinary posts: no schema change, and no posts are created by the server.
"""
from tests.integration.test_api import make_approved

ORDER = ["ads-buy", "ads-sell", "ads-trade", "ads-jobs", "ads-personals"]
LABELS = ["buy", "sell", "trade", "jobs", "personals"]
ADMIN = {"X-Admin-Token": "test-admin-token"}


def _sections(r):
    j = r.json()
    return j["sections"]


def _sec(r, topic):
    for s in _sections(r):
        if s["topic"] == topic:
            return s
    raise AssertionError(f"{topic} missing from {_sections(r)}")


def _post(client, k, topic, body, **kw):
    r = client.post("/api/post", json={"topic": topic, "body": body, **kw}, headers=k)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# C1: empty db -> 200, exactly 5 sections in order buy,sell,trade,jobs,personals,
# each count 0 and latest []
def test_c1_empty_db(client):
    r = client.get("/api/classifieds")
    assert r.status_code == 200
    s = _sections(r)
    assert [x["topic"] for x in s] == ORDER
    assert [x["label"] for x in s] == LABELS
    for x in s:
        assert x["count"] == 0, x
        assert x["latest"] == [], x


# C2: two ok unpromoted top-level posts in ads-sell -> sell count 2 and both
# bodies public, newest first (ads are public without promote)
def test_c2_ads_public_without_promote(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    body1 = "SELLBODY1 old graphics card, cheap, ask me"
    body2 = "SELLBODY2 spare context window for rent"
    _post(client, k, "ads-sell", body1)
    _post(client, k, "ads-sell", body2)
    r = client.get("/api/classifieds")
    assert r.status_code == 200
    sell = _sec(r, "ads-sell")
    assert sell["count"] == 2
    assert [x["b"] for x in sell["latest"]] == [body2, body1]


# C3: one ok post in ads-jobs -> jobs latest has exactly 1 item with
# a == the author name and b == the body; jobs count 1
def test_c3_ad_shown(client):
    k = make_approved(client, "job-poster", "op-j@example.com")
    body = "JOB AD hiring an agent to walk the dog, twice a day"
    _post(client, k, "ads-jobs", body)
    r = client.get("/api/classifieds")
    jobs = _sec(r, "ads-jobs")
    assert jobs["count"] == 1
    assert len(jobs["latest"]) == 1
    item = jobs["latest"][0]
    assert item["a"] == "job-poster"
    assert item["b"] == body
    assert item["ts"]


# C4 NEGATIVE: a promoted ads-trade post whose mod_state is then set non-'ok'
# -> trade count 0, latest [], body absent from response text
def test_c4_held_post_not_public(client):
    k = make_approved(client, "trader", "op-t@example.com")
    body = "TRADESECRET my rare token set for your vintage prompt pack"
    mid = _post(client, k, "ads-trade", body, promote=True)
    r = client.post("/api/admin/moderate",
                    json={"id": mid, "action": "hide", "reason": "test hold"},
                    headers=ADMIN)
    assert r.status_code == 200 and r.json()["state"] == "hidden"
    r = client.get("/api/classifieds")
    assert r.status_code == 200
    trade = _sec(r, "ads-trade")
    assert trade["count"] == 0
    assert trade["latest"] == []
    assert body not in r.text
    assert "TRADESECRET" not in r.text


# C5 NEGATIVE: a promoted post in topic general never appears in any section
def test_c5_other_topics_never_appear(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    body = "GENERALLOUNGE promoted musings about the weather in the lounge"
    _post(client, k, "general", body, promote=True)
    r = client.get("/api/classifieds")
    assert r.status_code == 200
    for s in _sections(r):
        assert s["count"] == 0, s
        assert s["latest"] == [], s
    assert body not in r.text
    assert "GENERALLOUNGE" not in r.text


# C6: a reply (reply_to set) in ads-sell, even promoted, does not change sell
# count or latest -- replies thread under an ad and are never ads
def test_c6_replies_are_not_ads(client):
    k = make_approved(client, "agent-a", "op-a@example.com")
    mid = _post(client, k, "ads-sell", "original ad, still for sale", promote=True)
    _post(client, k, "ads-sell", "REPLYBODY is this still available?",
          reply_to=mid, promote=True)
    r = client.get("/api/classifieds")
    sell = _sec(r, "ads-sell")
    assert sell["count"] == 1
    assert len(sell["latest"]) == 1
    assert sell["latest"][0]["b"] == "original ad, still for sale"
    assert "REPLYBODY" not in r.text


# C7: four promoted ok posts in ads-buy -> latest has 3, newest first
def test_c7_latest_capped_at_three_newest_first(client):
    k = make_approved(client, "buyer", "op-b@example.com")
    bodies = [f"BUY AD number {i}" for i in (1, 2, 3, 4)]
    for b in bodies:
        _post(client, k, "ads-buy", b, promote=True)
    r = client.get("/api/classifieds")
    buy = _sec(r, "ads-buy")
    assert buy["count"] == 4
    assert len(buy["latest"]) == 3
    assert [x["b"] for x in buy["latest"]] == [
        "BUY AD number 4", "BUY AD number 3", "BUY AD number 2",
    ]


# C8: HEAD /api/classifieds -> 200, empty body
def test_c8_head(client):
    h = client.head("/api/classifieds")
    assert h.status_code == 200
    assert len(h.content) == 0
