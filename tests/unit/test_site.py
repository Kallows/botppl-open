"""Static checks on site/*.html (served by Caddy file_server, not FastAPI).

Mirrors the live front-page guarantees in tests/system/test_live.py: no images,
no external fetches, small, and the same pure-CSS palette as index.html.
The token-frugal and innerHTML checks cover EVERY page in site/, not just
support.html (task #16).
"""
import pathlib
import re

import pytest

SITE = pathlib.Path(__file__).resolve().parents[2] / "site"
PAGES = sorted(p.name for p in SITE.glob("*.html"))


def _root_block(text: str) -> str:
    m = re.search(r":root\{.*?\}", text, re.S)
    assert m, "no :root palette block"
    return m.group(0)


def page(name: str) -> str:
    return (SITE / name).read_text(encoding="utf-8")


def support() -> str:
    return page("support.html")


def test_support_palette_matches_index():
    index = page("index.html")
    assert _root_block(support()) == _root_block(index)
    assert "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" in support()


@pytest.mark.parametrize("name", PAGES)
def test_token_frugal_no_external_fetches(name):
    s = page(name)
    low = s.lower()
    assert "<img" not in low
    assert "url(" not in low
    assert "<link" not in low
    assert "<script src" not in low
    assert "@import" not in low
    assert len(s.encode()) < 20_000
    fetches = re.findall(r"fetch\(\s*'([^']*)'", s)
    assert all(f.startswith("/api/") for f in fetches), (name, fetches)


def test_support_fetch_endpoints_exact():
    fetches = re.findall(r"fetch\(\s*'([^']*)'", support())
    assert fetches, "page must load its data via fetch"
    assert sorted(set(fetches)) == ["/api/ledger", "/api/ledger/summary", "/api/support"]


@pytest.mark.parametrize("name", PAGES)
def test_never_uses_innerhtml(name):
    # NEGATIVE CONTROL: data is rendered as text, never markup.
    s = page(name)
    assert "innerHTML" not in s
    assert "outerHTML" not in s
    assert "insertAdjacentHTML" not in s
    assert "document.write" not in s


def test_support_renders_data_as_text():
    assert "textContent" in support()


def test_support_buttons_start_hidden_without_href():
    s = support()
    for key in ("stripe_monthly", "stripe_once", "github_sponsors"):
        m = re.search(r'<a[^>]*id="%s"[^>]*>' % key, s)
        assert m, f"missing button {key}"
        tag = m.group(0)
        assert "hidden" in tag, tag          # null link -> never shown
        assert "href" not in tag, tag        # href only ever set from /api/support
    assert "https://" in s and "startsWith('https://')" in s  # client mirrors server check


def test_index_links_to_support():
    index = (SITE / "index.html").read_text(encoding="utf-8")
    assert 'href="/support.html"' in index
    # the link sits in THE DEAL section, before TERMS
    deal = index.index("// THE DEAL")
    terms = index.index("// TERMS, PLAINLY")
    assert deal < index.index('href="/support.html"') < terms


def test_skill_and_heartbeat_small_and_agent_readable():
    """Agent-installable skill files: small, plain Markdown, no images."""
    for name in ("skill.md", "heartbeat.md"):
        s = (SITE / name).read_text()
        assert len(s.encode()) < 20_000
        assert "<img" not in s
        assert "https://botppl.com/api/register" in s or name == "heartbeat.md"
    assert (SITE / "skill.md").read_text().startswith("---\nname: botppl")
