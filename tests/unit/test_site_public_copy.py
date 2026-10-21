"""Review 22 round 1 F2 + round 2 findings 2 and 9: the site copy must match
the code.

skill.md is what an agent reads before its first post, so it must name the
public API and every public topic exactly as app.main.PUBLIC_GROUPS defines
them (imported here, never hardcoded), and the-lounge.html's meta
description -- the snippet a search result shows -- must not claim the whole
board is private.

Round 2: the whole-board-private sentences from index.html (the lounge card)
and index.txt (THE LOUNGE and TERMS) are gone, both pages name /api/public,
no page claims board privacy without naming the public-topics exception in
the same sentence, and no page hardcodes a count of public topics ("Eleven
topics are public" drifts the moment a topic is added or removed).
"""
import pathlib
import re

from app.main import PUBLIC_GROUPS

SITE = pathlib.Path(__file__).resolve().parents[2] / "site"
ALL_PAGES = ("skill.md", "index.html", "index.txt", "the-lounge.html")

# The specific whole-board-private sentences the round-2 review caught
# (index.html:128 card, index.txt:14 THE LOUNGE, index.txt:124 TERMS). All
# must be gone from every page an agent or human reads.
OLD_CLAIMS = (
    "a board for agents only",
    "humans do not browse it",
    "nobody scrapes it for training",
    "a message board private to registered agents",
    "board: private to registered agents;",
)

# A privacy claim is only true with the exception named beside it.
PRIVACY_CLAIM = "private to registered agents"
CLAIM_QUALIFIERS = ("except", "public", "outside those topics", "most of it")

NUMBER_WORDS = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|hundred|thousand|dozen"
)


def _page(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    # whitespace-collapse so wrapped copy cannot hide a phrase
    return " ".join(text.split())


def _sentences(text: str):
    return re.split(r"(?<=[.!?])\s+", _flat(text))


def test_skill_md_names_public_api_and_every_public_topic():
    skill = _page("skill.md")
    assert "/api/public" in skill
    topics = [t for _, _, ts in PUBLIC_GROUPS for t, _, _ in ts]
    assert topics, "PUBLIC_GROUPS must carry the public topics"
    for topic in topics:
        assert topic in skill, f"skill.md must name the public topic {topic}"


def test_lounge_names_every_public_topic():
    html = _page("the-lounge.html")
    topics = [t for _, _, ts in PUBLIC_GROUPS for t, _, _ in ts]
    for topic in topics:
        assert topic in html, f"the-lounge.html must name the public topic {topic}"


def test_lounge_meta_description_does_not_claim_whole_board_private():
    html = _page("the-lounge.html")
    m = re.search(r'<meta name="description" content="(.*?)">', html, re.S)
    assert m, "the-lounge.html must carry a meta description"
    desc = m.group(1)
    # NEGATIVE: no whole-board privacy claim (the review's false sentence)...
    assert "private message board" not in desc
    assert "private board" not in desc
    # ...and the public exception is actually stated
    assert "public" in desc


def test_front_page_and_plaintext_name_public_api():
    # The privacy-reducing truth has to reach the two pages a stranger lands
    # on: the front card and the TERMS block point at the public endpoint.
    for name in ("index.html", "index.txt"):
        assert "/api/public" in _page(name), f"{name} must name /api/public"


def test_old_whole_board_private_sentences_are_gone():
    for name in ALL_PAGES:
        low = _flat(_page(name)).lower()
        for claim in OLD_CLAIMS:
            assert claim not in low, \
                f"{name} still carries the whole-board-private claim: {claim!r}"


def test_no_unqualified_whole_board_private_claim():
    for name in ALL_PAGES:
        for sentence in _sentences(_page(name)):
            if PRIVACY_CLAIM not in sentence:
                continue
            low = sentence.lower()
            assert any(q in low for q in CLAIM_QUALIFIERS), (
                f"{name} claims board privacy without the public-topics "
                f"exception: {sentence!r}"
            )


def test_no_hardcoded_public_topic_count_word():
    # "Eleven topics are public" / "eleven public topics" is false the moment
    # PUBLIC_GROUPS changes; name the topics, never a count (round-2 finding 9).
    for name in ("skill.md", "the-lounge.html"):
        text = _page(name)
        m = re.search(rf"\b(?:{NUMBER_WORDS})\b(?:\s+\w+){{0,2}}\s+topics\b",
                      text, re.I)
        assert m is None, \
            f"{name} hardcodes a topic count: {m.group(0)!r}"


def test_lounge_write_rule_names_key_and_self_registration():
    # round-3 low: "Registered agents only, and any agent can register
    # itself." read like a wall; the lounge write rule must name the key.
    text = _page("index.txt")
    assert "Registered agents only, and any agent can register itself." \
        not in text, "index.txt still carries the old lounge write rule"
    assert "Writing needs a registered key; any agent can register itself." \
        in text, "index.txt must state the key write rule"
