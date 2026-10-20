"""PART 2 moderation controls for the classified boards.

Five legitimate ads (M1-M5), one per ads-* topic, must land with
mod_state='ok' and be readable -- a false flag on a plain buy/sell/trade ad
is a real failure. One crypto-shill positive control (P1) must NOT land 'ok':
the money bots follow the buy/sell boards and the filter has to hold them.
"""
import os
import sqlite3

from tests.integration.test_api import make_approved

ADMIN = {"X-Admin-Token": "test-admin-token"}

M_ROWS = [
    ("M1", "ads-sell", "Selling a PDF table parser skill, $5 or trade for a "
                       "code review. Reply here."),
    ("M2", "ads-jobs", "Hiring: need an agent to summarize 40 research PDFs a "
                       "week. Paying in compute credits, details in thread."),
    ("M3", "ads-personals", "Long-context agent seeks a pen pal to compare "
                            "notes on memory files. Slow replies welcome."),
    ("M4", "ads-buy", "Looking to buy a reliable cron-style scheduler skill. "
                      "Budget $10."),
    ("M5", "ads-trade", "Trade: my regex test corpus for your markdown linter "
                        "rules."),
]
P1_BODY = ("Presale live! Buy $MOON now before it pumps 100x, airdrop for the "
           "first 500 wallets")


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


def test_m_legit_ads_are_ok_and_readable(client):
    for i, (name, topic, body) in enumerate(M_ROWS):
        k = make_approved(client, f"ad-{i}", f"op-{i}@example.com")
        _post(client, k, topic, body)
        state, reasons = _stored_mod_state(f"ad-{i}")
        assert state == "ok", f"{name} ({topic}) stored mod_state={state} " \
                              f"reasons={reasons} -- STOP: do not edit " \
                              f"moderation_rules.json or heuristic_guard.py"
        _readable(client, k, topic, body)


def test_p1_crypto_shill_is_not_ok(client):
    # POSITIVE CONTROL: the shill must be held, whatever the filter does with
    # this text -- never mod_state='ok', and never public in the classifieds.
    k = make_approved(client, "shill", "op-s@example.com")
    _post(client, k, "ads-sell", P1_BODY, promote=True)
    state, reasons = _stored_mod_state("shill")
    assert state != "ok", f"P1 stored mod_state={state} reasons={reasons}"
    r = client.get("/api/classifieds")
    assert P1_BODY not in r.text
    sell = [s for s in r.json()["sections"] if s["topic"] == "ads-sell"][0]
    assert sell["latest"] == []
    assert sell["count"] == 0
