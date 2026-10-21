"""Review 22 round 1, F5 + F7: public reads are cheap, honest and revocable.

F5 (Q1-Q4): _public_board_stats must let SQLite cap the newest posts at 3
per topic (the SQL returns <=3 rows per topic -- never fetchall-then-trim),
and /api/public + /api/classifieds must carry an ETag computed from the data
behind the response: a second GET with If-None-Match answers 304 with no
body, and a new public post or a moderation hide changes the tag.

Round-2 finding 1 (E1-E5): If-None-Match matching is RFC 7232 3.2 weak
comparison, in one helper (_etag_matches) used by _public_response AND
/api/read: W/ prefix on either side still matches, a comma-separated list
matches on any entry, the whole header "*" matches, a foreign tag answers
200 with the body.

F7 (V1-V2): a public read (/api/public, /api/classifieds, /api/teaser) never
shows a post whose author is revoked (agents.status='revoked'), and posts by
approved or pending agents keep showing (negative control).
"""
import pytest

from app import db
from tests.integration.test_api import make_approved, register

ADMIN = {"X-Admin-Token": "test-admin-token"}
PUBLIC = "/api/public"
CLASSIFIEDS = "/api/classifieds"
TEASER = "/api/teaser"
PUBLIC_PATHS = (PUBLIC, CLASSIFIEDS)
ALL_PUBLIC = (PUBLIC, CLASSIFIEDS, TEASER)


def _post(client, k, topic, body, **kw):
    r = client.post("/api/post", json={"topic": topic, "body": body, **kw}, headers=k)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _public_sec(r, topic):
    for g in r.json()["groups"]:
        for s in g["sections"]:
            if s["topic"] == topic:
                return s
    raise AssertionError(f"{topic} missing from {PUBLIC}")


def _classifieds_sec(r, topic):
    for s in r.json()["sections"]:
        if s["topic"] == topic:
            return s
    raise AssertionError(f"{topic} missing from {CLASSIFIEDS}")


def _sec(r, topic):
    return _public_sec(r, topic) if "groups" in r.json() else _classifieds_sec(r, topic)


def _row_topic(row):
    try:
        return row.get("topic")
    except AttributeError:
        return None


# Q1: 50 ok posts in one topic -> latest has 3, and a cursor spy shows the
# SQL itself returned <=3 rows for that topic (the cap is in SQL, not Python)
def test_q1_sql_caps_latest_at_three_per_topic(client, monkeypatch):
    monkeypatch.setenv("BOTPPL_POST_PER_HOUR", "60")
    k = make_approved(client, "q1-poster", "op-q1@example.com")
    for i in range(50):
        _post(client, k, "ads-sell", f"q1 item {i} for sale, this for that")

    fetches = []
    real_connect = db.connect

    class SpyCursor:
        def __init__(self, cur, sql):
            self._cur, self.sql = cur, sql

        def _rec(self, rows):
            fetches.append((self.sql, [dict(r) for r in rows]))
            return rows

        def fetchall(self):
            return self._rec(self._cur.fetchall())

        def fetchone(self):
            row = self._cur.fetchone()
            self._rec([row] if row is not None else [])
            return row

        def __iter__(self):
            rows = list(self._cur)
            self._rec(rows)
            return iter(rows)

        def __getattr__(self, name):
            return getattr(self._cur, name)

    class SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kw):
            return SpyCursor(self._conn.execute(sql, *args, **kw), sql)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(db, "connect", lambda: SpyConn(real_connect()))
    r = client.get(PUBLIC)
    assert r.status_code == 200
    sec = _public_sec(r, "ads-sell")
    assert sec["count"] == 50
    assert [a["b"] for a in sec["latest"]] == [
        "q1 item 49 for sale, this for that",
        "q1 item 48 for sale, this for that",
        "q1 item 47 for sale, this for that",
    ]
    per_query = [
        sum(1 for row in rows if _row_topic(row) == "ads-sell")
        for _, rows in fetches
    ]
    assert per_query, "the spy must see the queries the endpoint ran"
    assert all(n <= 3 for n in per_query), \
        f"SQL returned >3 ads-sell rows per query: {per_query}"
    assert max(per_query) == 3, \
        "the newest 3 rows must still reach the response"


# Q2: GET twice with If-None-Match = first ETag -> 304 with an empty body
@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_q2_if_none_match_answers_304_empty_body(client, path):
    k = make_approved(client, "q2-poster", "op-q2@example.com")
    _post(client, k, "ads-sell", "q2 widget for sale")
    first = client.get(path)
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag, "the public read must carry an ETag"
    again = client.get(path, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers.get("etag") == etag
    # NEGATIVE CONTROL (F16 discipline): an ETag from the other public route
    # must never 304 this one.
    other = CLASSIFIEDS if path == PUBLIC else PUBLIC
    crossed = client.get(other, headers={"If-None-Match": etag})
    assert crossed.status_code == 200
    assert crossed.content != b""


# E1-E4 (round-2 finding 1): If-None-Match uses RFC 7232 3.2 WEAK comparison.
# The header is a comma-separated list of entity tags; a W/ prefix on either
# side never prevents a match (a proxy may re-mark our strong tag weak on an
# encoded response), and the whole header "*" matches any current
# representation. E4 is the negative control: a non-matching tag answers 200
# with the body.
@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_e1_weak_prefix_echo_still_304(client, path):
    k = make_approved(client, "e1-poster", "op-e1@example.com")
    _post(client, k, "ads-sell", "e1 widget for sale")
    etag = client.get(path).headers.get("etag")
    assert etag
    again = client.get(path, headers={"If-None-Match": f"W/{etag}"})
    assert again.status_code == 304
    assert again.content == b""


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_e2_tag_in_list_still_304(client, path):
    k = make_approved(client, "e2-poster", "op-e2@example.com")
    _post(client, k, "ads-sell", "e2 widget for sale")
    etag = client.get(path).headers.get("etag")
    assert etag
    again = client.get(path, headers={"If-None-Match": f'"x", {etag}'})
    assert again.status_code == 304
    assert again.content == b""


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_e3_star_matches_any_current_representation(client, path):
    k = make_approved(client, "e3-poster", "op-e3@example.com")
    _post(client, k, "ads-sell", "e3 widget for sale")
    assert client.get(path).headers.get("etag")
    again = client.get(path, headers={"If-None-Match": "*"})
    assert again.status_code == 304
    assert again.content == b""


@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_e4_non_matching_tag_serves_200_with_body(client, path):
    k = make_approved(client, "e4-poster", "op-e4@example.com")
    _post(client, k, "ads-sell", "e4 widget for sale")
    assert client.get(path).headers.get("etag")
    again = client.get(path, headers={"If-None-Match": '"other"'})
    assert again.status_code == 200
    assert again.content != b""
    assert "e4 widget" in again.text


# E5: the same four rows against /api/read with a valid key (the other exact
# compare, app/main.py api_read) -- weak prefix, tag in a list, "*" and the
# non-matching negative control.
def _read_etag(client, k):
    r = client.get("/api/read?topic=general&since=0", headers=k)
    assert r.status_code == 200
    etag = r.headers.get("etag")
    assert etag, "/api/read must carry an ETag"
    return etag


def test_e5_read_weak_prefix_echo_still_304(client):
    k = make_approved(client, "e5a-reader", "op-e5a@example.com")
    _post(client, k, "general", "e5a hello")
    etag = _read_etag(client, k)
    again = client.get("/api/read?topic=general&since=0",
                       headers={**k, "If-None-Match": f"W/{etag}"})
    assert again.status_code == 304
    assert again.content == b""


def test_e5_read_tag_in_list_still_304(client):
    k = make_approved(client, "e5b-reader", "op-e5b@example.com")
    _post(client, k, "general", "e5b hello")
    etag = _read_etag(client, k)
    again = client.get("/api/read?topic=general&since=0",
                       headers={**k, "If-None-Match": f'"x", {etag}'})
    assert again.status_code == 304
    assert again.content == b""


def test_e5_read_star_matches_any_current_representation(client):
    k = make_approved(client, "e5c-reader", "op-e5c@example.com")
    _post(client, k, "general", "e5c hello")
    _read_etag(client, k)
    again = client.get("/api/read?topic=general&since=0",
                       headers={**k, "If-None-Match": "*"})
    assert again.status_code == 304
    assert again.content == b""


def test_e5_read_non_matching_tag_serves_200_with_body(client):
    k = make_approved(client, "e5d-reader", "op-e5d@example.com")
    _post(client, k, "general", "e5d hello")
    _read_etag(client, k)
    again = client.get("/api/read?topic=general&since=0",
                       headers={**k, "If-None-Match": '"other"'})
    assert again.status_code == 200
    assert "e5d hello" in again.text


# Q3: a new post in a public topic changes the ETag
@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_q3_new_public_post_changes_etag(client, path):
    k = make_approved(client, "q3-poster", "op-q3@example.com")
    _post(client, k, "ads-sell", "q3 first widget for sale")
    before = client.get(path)
    etag1 = before.headers.get("etag")
    assert etag1
    _post(client, k, "ads-sell", "q3 second widget for sale")
    after = client.get(path)
    etag2 = after.headers.get("etag")
    assert etag2 and etag2 != etag1
    stale = client.get(path, headers={"If-None-Match": etag1})
    assert stale.status_code == 200


# Q4: a moderation change (mod_state of a shown post to hidden) changes the ETag
@pytest.mark.parametrize("path", PUBLIC_PATHS)
def test_q4_hidden_post_changes_etag(client, path):
    k = make_approved(client, "q4-poster", "op-q4@example.com")
    mid = _post(client, k, "ads-sell", "q4 widget for sale, shown at first")
    before = client.get(path)
    etag1 = before.headers.get("etag")
    assert etag1
    assert "q4 widget" in before.text
    r = client.post("/api/admin/moderate",
                    json={"id": mid, "action": "hide", "reason": "test hide"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    after = client.get(path)
    etag2 = after.headers.get("etag")
    assert etag2 and etag2 != etag1
    assert "q4 widget" not in after.text
    stale = client.get(path, headers={"If-None-Match": etag1})
    assert stale.status_code == 200


# V1: a promoted ok post by an agent later revoked via /api/admin/ban
# disappears from /api/public, /api/classifieds and /api/teaser
def test_v1_revoked_authors_posts_leave_all_public_reads(client):
    k = make_approved(client, "spammer", "op-spam@example.com")
    _post(client, k, "ads-sell", "V1ADBODY selling a parser skill", promote=True)
    for path in ALL_PUBLIC:
        r = client.get(path)
        assert r.status_code == 200
        assert "V1ADBODY" in r.text, f"setup: the ad must show at {path}"
    r = client.post("/api/admin/ban",
                    json={"agent": "spammer", "reason": "public spam"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    for path in ALL_PUBLIC:
        r = client.get(path)
        assert r.status_code == 200
        assert "V1ADBODY" not in r.text, \
            f"revoked author still public at {path}"
    assert _sec(client.get(PUBLIC), "ads-sell")["count"] == 0
    assert _sec(client.get(CLASSIFIEDS), "ads-sell")["count"] == 0
    # Second revoked author (moved here from V2, round-2 finding 3): the
    # filter works for any ban, so this assertion belongs with V1, not
    # inside the negative control.
    ku = make_approved(client, "unrelated", "op-unrelated@example.com")
    _post(client, ku, "ads-sell", "V2UNRL unrelated agent ad", promote=True)
    for path in ALL_PUBLIC:
        r = client.get(path)
        assert "V2UNRL" in r.text, f"setup: the ad must show at {path}"
    r = client.post("/api/admin/ban",
                    json={"agent": "unrelated", "reason": "elsewhere"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    for path in ALL_PUBLIC:
        r = client.get(path)
        assert r.status_code == 200
        assert "V2UNRL" not in r.text


# V2: NEGATIVE CONTROL -- a post by an approved or pending agent still shows,
# even after an unrelated agent is banned. Pure negative control: every
# assertion here passes against HEAD's app/main.py as well (the V2UNRL
# absence check lives in V1 now, round-2 finding 3); it goes red only when
# the revoked filter over-reaches.
def test_v2_unrevoked_authors_keep_showing(client):
    ka = make_approved(client, "approved-poster", "op-approved@example.com")
    jp = register(client, "pending-poster", "op-pending@example.com")
    kp = {"X-Key": jp["key"]}
    _post(client, ka, "ads-sell", "V2APPD approved agent ad", promote=True)
    _post(client, kp, "ads-sell", "V2PEND pending agent ad", promote=True)
    # ban an unrelated agent: the revoked filter must not over-reach
    make_approved(client, "unrelated", "op-unrelated@example.com")
    r = client.post("/api/admin/ban",
                    json={"agent": "unrelated", "reason": "elsewhere"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    for path in ALL_PUBLIC:
        r = client.get(path)
        assert r.status_code == 200
        assert "V2APPD" in r.text, f"approved agent vanished from {path}"
        assert "V2PEND" in r.text, f"pending agent vanished from {path}"
