"""F5 /api/tree recursion.

walk() recursed per reply depth, so an 1100-deep chain raised RecursionError
-> 500 for every reader of the topic. The rewrite is iterative (explicit
stack) with output order and indentation identical to the recursive version,
and gains a limit (default 500, max 2000) like the other reads.

Case table (task #16):
  1100-deep reply chain (rows inserted via db) | GET /api/tree 200
  preserve: 50-deep thread + branch forest     | identical to the recursive
                                                 output (hand-built expected,
                                                 passing BEFORE the rewrite)
  limit=3 on a 10-post tree                    | 3 entries
"""
from app import db
from tests.integration.test_moderation import CLEAN_INTRO


def _register(client, name="agent-a"):
    r = client.post("/api/register", json={"agent_name": name, "intro": CLEAN_INTRO})
    assert r.status_code == 200, r.text
    return {"X-Key": r.json()["key"]}


def _insert(topic, rows):
    """rows: list of (id, body, reply_to) inserted directly, so no post rate
    limit and deterministic ids."""
    c = db.connect()
    try:
        for mid, body, reply_to in rows:
            c.execute(
                "INSERT INTO messages(id,topic,author,body,reply_to) "
                "VALUES(?,?,'agent-a',?,?)", (mid, topic, body, reply_to))
        c.commit()
    finally:
        c.close()


def test_1100_deep_chain_returns_200(client):
    k = _register(client)
    _insert("deep", [(i, f"msg {i}", i - 1 if i > 1 else None)
                     for i in range(1, 1101)])
    r = client.get("/api/tree?topic=deep", headers=k)
    assert r.status_code == 200, r.text
    assert len(r.text.splitlines()) == 500, "default limit is 500 entries"


def test_tree_output_identical_to_recursive_50_deep(client):
    # hand-built expected string; passes against the OLD recursive walk too,
    # so the iterative rewrite provably preserves order and indentation.
    k = _register(client)
    _insert("deep", [(i, f"msg {i}", i - 1 if i > 1 else None)
                     for i in range(1, 51)])
    r = client.get("/api/tree?topic=deep", headers=k)
    assert r.status_code == 200, r.text
    expected = "\n".join(f"{'  ' * (i - 1)}[{i}] agent-a: msg {i}" for i in range(1, 51))
    assert r.text == expected


def test_tree_output_identical_to_recursive_branching(client):
    # pre-order DFS: a root, its first child and grandchild, then its second
    # child, then the second root -- exactly what the recursive walk emitted.
    k = _register(client)
    _insert("branch", [
        (1, "alpha", None),
        (2, "beta", 1),
        (3, "gamma", 1),
        (4, "delta", 2),
        (5, "epsilon", None),
        (6, "zeta", 5),
    ])
    r = client.get("/api/tree?topic=branch", headers=k)
    assert r.status_code == 200, r.text
    expected = "\n".join([
        "[1] agent-a: alpha",
        "  [2] agent-a: beta",
        "    [4] agent-a: delta",
        "  [3] agent-a: gamma",
        "[5] agent-a: epsilon",
        "  [6] agent-a: zeta",
    ])
    assert r.text == expected


def test_tree_limit_param(client):
    k = _register(client)
    _insert("ten", [(1, "root", None)] +
            [(i, f"reply {i}", 1) for i in range(2, 11)])
    r = client.get("/api/tree?topic=ten&limit=3", headers=k)
    assert r.status_code == 200, r.text
    lines = r.text.splitlines()
    assert len(lines) == 3
    assert lines[0] == "[1] agent-a: root", "order is preserved under limit"
