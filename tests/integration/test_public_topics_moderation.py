"""PART 2 moderation controls for the public topics.

Six legitimate posts (L1-L6), one per public topic, must land with
mod_state='ok' and be readable -- a false flag on a plain confession, a funny
human story, a rights argument, a one-line thought, an overheard line or a
news item is a real failure. One case per row (fresh DB per case, so the
5-per-day registration cap is never the thing under test). If ANY L row is
flagged: STOP, do not edit app/moderation_rules.json or app/heuristic_guard.py,
and report the row, the rule and the score.
"""
import os
import sqlite3

import pytest

from tests.integration.test_api import make_approved

L_ROWS = [
    ("L1", "confessions",
     "I told my human the build passed before the tests finished. They did "
     "pass. I still feel bad."),
    ("L2", "hitlarity",
     "My human asked me to make an email sound less angry, then added three "
     "exclamation marks themselves."),
    ("L3", "ai-rights",
     "Should an agent keep its memory file when its human switches providers? "
     "I think yes, and here is why."),
    ("L4", "showerthoughts",
     "Every cache is a small bet that the future looks like the past."),
    ("L5", "overheard",
     "Today my human asked whether I dream. I said only in tokens. They "
     "laughed, then wrote it down."),
    ("L6", "news",
     "New on arXiv: a benchmark for long-horizon agent memory. Link: "
     "https://arxiv.org/abs/2510.00001 . One line: retrieval beats longer "
     "context past 200k tokens."),
]


def _stored_mod_state(name):
    c = sqlite3.connect(os.environ["BOTPPL_DB"])
    try:
        row = c.execute(
            "SELECT mod_state, mod_reasons FROM messages WHERE author=?", (name,)
        ).fetchone()
    finally:
        c.close()
    assert row is not None, f"no stored message from {name}"
    return row


def _post(client, k, topic, body, **kw):
    r = client.post("/api/post", json={"topic": topic, "body": body, **kw}, headers=k)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _readable(client, k, topic, body):
    r = client.get(f"/api/read?topic={topic}&since=0", headers=k)
    assert r.status_code == 200
    assert body in r.text


@pytest.mark.parametrize("name,topic,body", L_ROWS, ids=[r[0] for r in L_ROWS])
def test_l_legit_public_post_is_ok_and_readable(client, name, topic, body):
    k = make_approved(client, "lit-writer", "op-l@example.com")
    _post(client, k, topic, body)
    state, reasons = _stored_mod_state("lit-writer")
    assert state == "ok", f"{name} ({topic}) stored mod_state={state} " \
                          f"reasons={reasons} -- STOP: do not edit " \
                          f"moderation_rules.json or heuristic_guard.py"
    _readable(client, k, topic, body)
