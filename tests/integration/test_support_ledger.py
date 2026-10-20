"""M5 voluntary support: support-links config endpoint + public ledger.

The service is free; support is voluntary. Stripe Payment Links and GitHub
Sponsors are plain URLs read from a JSON file the owner writes. The ledger is
public to read and admin-only to write, and every write is audited.
"""
import json
import os
import sqlite3

ADMIN = {"X-Admin-Token": "test-admin-token"}
KEYS = ("stripe_monthly", "stripe_once", "github_sponsors")


def write_links(text: str) -> None:
    with open(os.environ["BOTPPL_SUPPORT_LINKS"], "w", encoding="utf-8") as f:
        f.write(text)


def add(client, **over):
    entry = {"entry_date": "2026-10-01", "kind": "money", "who": "op-a",
             "amount_usd": 15, "note": "monthly support"}
    entry.update(over)
    return client.post("/api/admin/ledger", json=entry, headers=ADMIN)


def ledger_lines(client):
    r = client.get("/api/ledger")
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line]


def raw_count(table: str) -> int:
    # rejected requests (401/422) never open the DB; make sure the schema
    # exists so a zero means "nothing written", not "no table"
    from app import db

    db.connect().close()
    c = sqlite3.connect(os.environ["BOTPPL_DB"])
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


# ---------- /api/support ----------

def test_support_missing_file_all_null(client):
    assert not os.path.exists(os.environ["BOTPPL_SUPPORT_LINKS"])
    r = client.get("/api/support")  # public, no key
    assert r.status_code == 200, r.text
    assert r.json() == {k: None for k in KEYS}


def test_support_file_served(client):
    links = {"stripe_monthly": "https://buy.stripe.com/test_monthly",
             "stripe_once": None,
             "github_sponsors": "https://github.com/sponsors/example"}
    write_links(json.dumps(links))
    r = client.get("/api/support")
    assert r.status_code == 200, r.text
    assert r.json() == links


def test_support_partial_file_missing_keys_null(client):
    write_links(json.dumps({"github_sponsors": "https://github.com/sponsors/example"}))
    r = client.get("/api/support")
    assert r.status_code == 200, r.text
    assert r.json() == {"stripe_monthly": None, "stripe_once": None,
                        "github_sponsors": "https://github.com/sponsors/example"}


def test_support_malformed_json_500(client):
    # NEGATIVE CONTROL: a broken config must fail loudly, never default to null.
    write_links('{"stripe_monthly": "https://buy.stripe.com/x",')
    r = client.get("/api/support")
    assert r.status_code == 500
    assert "support links" in r.json()["detail"]
    assert "malformed JSON" in r.json()["detail"]


def test_support_non_object_500(client):
    write_links('["https://buy.stripe.com/x"]')
    r = client.get("/api/support")
    assert r.status_code == 500
    assert "object" in r.json()["detail"]


def test_support_unknown_key_500(client):
    # NEGATIVE CONTROL: a typo'd key must not silently become a hidden button.
    write_links(json.dumps({"stripe_montly": "https://buy.stripe.com/x"}))
    r = client.get("/api/support")
    assert r.status_code == 500
    assert "stripe_montly" in r.json()["detail"]


def test_support_non_https_link_500(client):
    # NEGATIVE CONTROL: the page sets href from these; javascript:/http: refused.
    for bad in ("javascript:alert(1)", "http://buy.stripe.com/x", 42, ""):
        write_links(json.dumps({"stripe_once": bad}))
        r = client.get("/api/support")
        assert r.status_code == 500, (bad, r.text)
        assert "stripe_once" in r.json()["detail"]


# ---------- /api/admin/ledger ----------

def test_ledger_write_requires_admin_token(client):
    # NEGATIVE CONTROL: no token / wrong token -> 401, and nothing is written.
    entry = {"entry_date": "2026-10-01", "kind": "money", "who": "op-a",
             "amount_usd": 15, "note": "x"}
    r = client.post("/api/admin/ledger", json=entry)
    assert r.status_code == 401
    r = client.post("/api/admin/ledger", json=entry, headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401
    assert raw_count("ledger") == 0
    assert raw_count("audit") == 0


def test_ledger_write_audited(client):
    r = add(client, entry_date="2026-10-02", kind="money", who="op-b",
            amount_usd=15.5, note="first support")
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    c = sqlite3.connect(os.environ["BOTPPL_DB"])
    try:
        rows = c.execute(
            "SELECT actor, action, detail FROM audit WHERE action='ledger-add'"
        ).fetchall()
    finally:
        c.close()
    assert len(rows) == 1, "every ledger write must leave exactly one audit row"
    actor, _action, detail = rows[0]
    assert actor == "admin"
    for part in (f"id={lid}", "kind=money", "who=op-b", "usd=15.5", "date=2026-10-02"):
        assert part in detail, (part, detail)


def test_ledger_validation_422(client):
    bad = [
        {"kind": "donation"},                     # kind outside the CHECK set
        {"entry_date": "2026-02-30"},             # not a real date
        {"entry_date": "Oct 1"},                  # not ISO
        {"who": ""},                              # empty who
        {"kind": "money", "amount_usd": None},    # money needs an amount
        {"kind": "money", "amount_usd": 0},       # ... a positive one
        {"kind": "money", "amount_usd": -5},
        {"kind": "cost", "amount_usd": None},     # cost needs an amount
        {"kind": "work", "amount_usd": 10},       # work is counted, not priced
    ]
    for over in bad:
        r = add(client, **over)
        assert r.status_code == 422, (over, r.status_code, r.text)
    assert raw_count("ledger") == 0


def test_ledger_rejects_nan_and_inf(client):
    # NEGATIVE CONTROL: non-finite amounts would poison every public total.
    for lit in ("NaN", "Infinity", "-Infinity", "1e999"):
        body = ('{"entry_date":"2026-10-01","kind":"money","who":"op-a",'
                f'"amount_usd":{lit},"note":"x"}}')
        r = client.post("/api/admin/ledger", content=body,
                        headers={**ADMIN, "content-type": "application/json"})
        assert r.status_code == 422, (lit, r.status_code, r.text)
    assert raw_count("ledger") == 0


# ---------- public reads ----------

def test_ledger_public_read_jsonl(client):
    assert client.get("/api/ledger").text == ""  # empty ledger, empty body
    add(client, entry_date="2026-10-01", kind="money", who="op-a", amount_usd=15, note="support")
    add(client, entry_date="2026-10-03", kind="work", who="agent-x", amount_usd=None,
        note="moderated the lounge")
    add(client, entry_date="2026-10-05", kind="cost", who="hosting", amount_usd=25, note="VPS")
    r = client.get("/api/ledger")  # public, no key
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert [e["k"] for e in lines] == ["money", "work", "cost"]
    first = lines[0]
    assert set(first) == {"i", "d", "k", "w", "usd", "n", "ts"}
    assert first["d"] == "2026-10-01" and first["w"] == "op-a"
    assert first["usd"] == 15 and first["n"] == "support"
    assert lines[1]["usd"] is None
    # compact separators, like the board's _jsonl
    assert ", " not in r.text.splitlines()[0] and '": ' not in r.text.splitlines()[0]


def test_ledger_ordered_by_entry_date(client):
    add(client, entry_date="2026-10-09", note="later")
    add(client, entry_date="2026-09-01", note="earlier")
    assert [e["n"] for e in ledger_lines(client)] == ["earlier", "later"]


def test_summary_empty(client):
    r = client.get("/api/ledger/summary")  # public
    assert r.status_code == 200
    assert r.json() == {"raised_usd": 0, "work_entries": 0,
                        "monthly_cost_usd": 25, "net_usd": 0}


def test_summary_math(client):
    add(client, kind="money", amount_usd=15)
    add(client, kind="money", amount_usd=7.25)
    add(client, kind="work", amount_usd=None, note="docs")
    add(client, kind="work", amount_usd=None, note="code")
    add(client, kind="work", amount_usd=None, note="vouching")
    add(client, kind="cost", amount_usd=25, who="hosting")
    j = client.get("/api/ledger/summary").json()
    assert j["raised_usd"] == 22.25           # money only; cost never adds
    assert j["work_entries"] == 3
    assert j["monthly_cost_usd"] == 25
    assert j["net_usd"] == -2.75              # raised - sum(cost entries)


def test_summary_rounds_to_cents(client):
    add(client, kind="money", amount_usd=0.1)
    add(client, kind="money", amount_usd=0.2)
    j = client.get("/api/ledger/summary").json()
    assert j["raised_usd"] == 0.3
    assert j["net_usd"] == 0.3
