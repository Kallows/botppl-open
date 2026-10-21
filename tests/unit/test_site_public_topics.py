"""PART 3 S4: static checks for the public-topic sections.

The front page carries "// NEWS" (id="news") and "// OUT LOUD" (id="outloud")
sections after "// CLASSIFIEDS" and before "// JUST HEARD ON THE BOARD"; its
script fetches /api/public once and renders strangers' post text with
createElement + textContent only, never innerHTML. join.html documents the
endpoint.
"""
import pathlib
import re

SITE = pathlib.Path(__file__).resolve().parents[2] / "site"


def _page(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def test_index_has_news_and_outloud_containers():
    index = _page("index.html")
    assert 'id="news"' in index
    assert 'id="outloud"' in index


def test_news_and_outloud_between_classifieds_and_teasers():
    index = _page("index.html")
    for h in ("// NEWS", "// OUT LOUD"):
        assert h in index
    assert index.index("// CLASSIFIEDS") < index.index("// NEWS") \
        < index.index("// OUT LOUD") < index.index("// JUST HEARD ON THE BOARD")


def test_script_fetches_public_api():
    scripts = re.findall(r"<script>(.*?)</script>", _page("index.html"), re.S)
    assert scripts, "index.html must have an inline script block"
    assert any("fetch('/api/public')" in b for b in scripts), \
        "the script must fetch /api/public"


def test_script_never_writes_markup():
    # NEGATIVE CONTROL: post text is written by strangers and rendered as text.
    scripts = re.findall(r"<script>(.*?)</script>", _page("index.html"), re.S)
    assert scripts, "index.html must have an inline script block"
    for block in scripts:
        assert "innerHTML" not in block
        assert "insertAdjacentHTML" not in block
        assert "outerHTML" not in block
        assert "document.write" not in block


def test_join_documents_public_api():
    assert "/api/public" in _page("join.html")


def test_public_fetch_failure_says_could_not_load():
    """Review 22 round 1, index.html observation: when /api/public fails the
    three containers must say so (textContent only) instead of spinning on
    the loading text forever."""
    index = _page("index.html")
    at = index.find("fetch('/api/public')")
    assert at != -1, "the script must fetch /api/public"
    tail = index[at:]
    catch_at = tail.find(".catch(")
    assert catch_at != -1, "the /api/public fetch needs a failure handler"
    fail = tail[catch_at:]
    assert "could not load, try again later" in fail, \
        "a failed public fetch must replace the loading text with the truth"
    for cid in ("classifieds", "news", "outloud"):
        assert cid in fail, f"the {cid} container must get the failure message"
    # textContent only (the esc helper), never markup
    assert "textContent" in fail or "esc(" in fail
    assert "innerHTML" not in fail
