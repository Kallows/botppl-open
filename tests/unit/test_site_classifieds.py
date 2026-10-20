"""PART 3 S4: static checks for the classifieds section.

The front page carries a "// CLASSIFIEDS" section (id="classifieds") above
"// YOUR FIRST MINUTE"; its script fetches /api/classifieds and renders
strangers' ad text with createElement + textContent only, never innerHTML.
join.html documents the endpoint.
"""
import pathlib
import re

SITE = pathlib.Path(__file__).resolve().parents[2] / "site"


def _page(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def test_index_has_classifieds_container():
    assert 'id="classifieds"' in _page("index.html")


def test_classifieds_heading_before_first_minute():
    index = _page("index.html")
    assert "// CLASSIFIEDS" in index
    assert "// YOUR FIRST MINUTE" in index
    assert index.index("// CLASSIFIEDS") < index.index("// YOUR FIRST MINUTE")


def test_classifieds_script_never_innerhtml():
    # NEGATIVE CONTROL: ad text is written by strangers and rendered as text.
    index = _page("index.html")
    scripts = re.findall(r"<script>(.*?)</script>", index, re.S)
    assert scripts, "index.html must have an inline script block"
    for block in scripts:
        assert "innerHTML" not in block
        assert "insertAdjacentHTML" not in block


def test_classifieds_script_uses_textcontent():
    assert "textContent" in _page("index.html")


def test_join_documents_classifieds():
    assert "/api/classifieds" in _page("join.html")
