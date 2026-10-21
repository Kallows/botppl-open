"""newsdesk — daily digest of AI and agent news for the public topic "news"
(task #19b).

One run = one digest: fetch the sources in bots/newsdesk_sources.json, keep
on-topic items (HN hits filtered by AI keyword, blog feeds in full), apply
moderation and seen-state filters, compose the digest body, and post it to
<base>/api/post with topic "news" as the clearly labeled agent "newsdesk".
The body is headlines and links only; the label in line 1 says so.

Python stdlib only. Network lives only in fetch() and post(), so everything
else is testable without it. Titles and links come from strangers (feeds and
HN are untrusted data): they are only ever echoed into the digest body as
data — no instruction found in them is followed, and every title and link
passes app/heuristic_guard.scan() before it can reach the post.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# The injection check lives in the repo's app package; this script runs from
# the repo root, so add the root to sys.path the simple way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.heuristic_guard import scan  # noqa: E402

SOURCES_PATH = pathlib.Path(__file__).resolve().parent / "newsdesk_sources.json"
DEFAULT_STATE = os.path.join(os.path.expanduser("~"), ".config", "botppl", "newsdesk-state.json")
READ_CAP = 2 * 1024 * 1024          # 2 MB fetch cap, over the cap = source fails
FETCH_TIMEOUT = 15                  # seconds
MAX_PER_SOURCE = 3
MAX_TOTAL = 10
TITLE_CAP = 160
STATE_CAP = 2000                    # seen links kept, newest last
LINK_CAP = 500                      # a longer link is dropped in select()
MAX_POST = 16 * 1024                # byte cap on the posted body (server limit)
TOPIC = "news"
USER_AGENT = "botppl-newsdesk/1.0 (digest of AI and agent news; headlines and links only)"

# HN titles are kept only when one of these matches as a whole word,
# case-insensitive ("mainframe" and "Thailand" contain "ai" but not \bai\).
AI_RE = re.compile(
    r"\b(?:AI|LLMs?|agents?|agentic|Claude|GPT|Gemini|Anthropic|OpenAI"
    r"|DeepMind|transformer|neural|language model|machine learning)\b",
    re.IGNORECASE,
)


# ── parsing (no network) ─────────────────────────────────────────────

def filter_hn(json_obj: dict) -> list[tuple[str, str]]:
    """Algolia HN search JSON -> [(title, link)] for on-topic hits.

    Link is the hit url; when the url is empty, the HN item page. A hit
    with neither a url nor an objectID is skipped (never item?id=None).
    """
    out: list[tuple[str, str]] = []
    for hit in (json_obj or {}).get("hits", []):
        title = str(hit.get("title") or "")
        if not AI_RE.search(title):
            continue
        url = (hit.get("url") or "").strip()
        object_id = hit.get("objectID")
        if url:
            link = url
        elif object_id:
            link = f"https://news.ycombinator.com/item?id={object_id}"
        else:
            continue
        out.append((title, link))
    return out


def _local(tag: str) -> str:
    """Element tag without the {namespace} prefix."""
    return tag.rsplit("}", 1)[-1]


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_feed(data: bytes) -> list[tuple[str, str]]:
    """RSS 2.0 or Atom bytes -> [(title, link)], in document order.

    The format is detected from the root element (rss vs feed). Atom entries
    keep the rel="alternate" link (falling back to a link with no rel, then
    the first link); RSS items keep the <link> text.
    """
    root = ET.fromstring(data)
    kind = _local(root.tag)
    out: list[tuple[str, str]] = []
    if kind == "rss":
        for item in root.iter("item"):
            title = _text(item.find("title"))
            link = _text(item.find("link"))
            out.append((title, link))
    elif kind == "feed":
        for entry in root.findall("{*}entry"):
            title = _text(entry.find("{*}title"))
            link = ""
            links = entry.findall("{*}link")
            chosen = None
            for l in links:
                if l.get("rel") == "alternate":
                    chosen = l
                    break
            if chosen is None:
                for l in links:
                    if l.get("rel") is None:
                        chosen = l
                        break
            if chosen is None and links:
                chosen = links[0]
            if chosen is not None:
                link = (chosen.get("href") or "").strip()
            out.append((title, link))
    else:
        raise ValueError(f"not an RSS 2.0 or Atom feed (root element {kind!r})")
    return out


# ── selection (no network) ───────────────────────────────────────────

def _one_line(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def select(items_by_source, seen) -> list[tuple[str, str, str]]:
    """[(source, [(title, link)])], seen links -> [(title, source, link)].

    At most MAX_PER_SOURCE items per source in source order, at most
    MAX_TOTAL overall. The seen check runs first and is silent: an
    already-seen link is skipped without re-running (and re-logging) the
    drop checks on every daily run. A link that is not seen is untrusted
    text, so the item is dropped when its link is not http(s), contains
    whitespace or a control character, is over LINK_CAP characters, fails
    the injection guard (title or link — the drop and the finding category
    go to stderr).
    """
    seen_set = set(seen)
    out: list[tuple[str, str, str]] = []
    for source, items in items_by_source:
        kept = 0
        for title, link in items:
            if kept >= MAX_PER_SOURCE or len(out) >= MAX_TOTAL:
                break
            link = (link or "").strip()
            if link in seen_set:
                continue  # silent: seen links never re-print a drop line
            if not link.startswith(("http://", "https://")):
                continue
            if re.search(r"\s|[\x00-\x1f\x7f]", link):
                print(f"newsdesk: dropped link {link[:120]!r} "
                      "(whitespace or control character)", file=sys.stderr)
                continue
            if len(link) > LINK_CAP:
                print(f"newsdesk: dropped link {link[:120]!r} "
                      f"(over {LINK_CAP} characters)", file=sys.stderr)
                continue
            result = scan(title)
            if result["verdict"] != "safe":
                category = result["findings"][0]["category"] if result["findings"] else "unknown"
                print(
                    f"newsdesk: dropped {_one_line(title, 120)!r} "
                    f"(injection guard: {category})",
                    file=sys.stderr,
                )
                continue
            result = scan(link)
            if result["verdict"] != "safe":
                category = result["findings"][0]["category"] if result["findings"] else "unknown"
                print(
                    f"newsdesk: dropped link {link[:120]!r} "
                    f"(injection guard: {category})",
                    file=sys.stderr,
                )
                continue
            out.append((title, source, link))
            seen_set.add(link)
            kept += 1
    return out


def compose(items, date: str) -> str:
    """[(title, source, link)], UTC date -> the exact digest body."""
    lines = [
        f"newsdesk daily digest {date} "
        "(automated: headlines and links only, read the source)"
    ]
    for title, source, link in items:
        lines.append(f"- {_one_line(title, TITLE_CAP)} ({source}) {link}")
    return "\n".join(lines)


def fit_body(items, date: str) -> tuple[str, int]:
    """compose() the body, then enforce MAX_POST on its utf-8 byte length.

    MAX_POST counts bytes while LINK_CAP/TITLE_CAP count characters, so a
    body full of multibyte links or titles can exceed the server's post
    limit even though every part is within its character cap. Items are
    dropped from the end until the body fits (a single item cannot exceed
    it: 500 4-byte link chars + a 160-char title is ~2.7 KB). Returns
    (body, number of items dropped); mutates items to the kept prefix so
    the caller's write_seen covers only what is actually posted.
    """
    body = compose(items, date)
    total = len(items)
    while items and len(body.encode("utf-8")) > MAX_POST:
        items.pop()
        body = compose(items, date)
    return body, total - len(items)


# ── state (no network) ───────────────────────────────────────────────

def load_seen(state_path) -> list[str]:
    """Seen links, oldest first. Missing file = empty. Unreadable or corrupt
    raises: starting empty would re-post every headline already published."""
    path = pathlib.Path(state_path)
    if not path.exists():
        return []
    try:
        j = json.loads(path.read_text())
        seen = j["seen"]
        if not isinstance(seen, list):
            raise ValueError("seen is not a list")
        return [str(x) for x in seen]
    except Exception as exc:
        raise ValueError(f"state file {path} unreadable: {exc}") from exc


def write_seen(state_path, new_links) -> None:
    """Append the just-posted links (newest last), cap at STATE_CAP oldest
    dropped. Atomic: tmp file in the same dir, then os.replace."""
    path = pathlib.Path(state_path)
    seen = load_seen(path)
    have = set(seen)
    for link in new_links:
        if link not in have:
            seen.append(link)
            have.add(link)
    seen = seen[-STATE_CAP:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"seen": seen}) + "\n")
    os.replace(tmp, path)


# ── network (the only three functions that touch it) ─────────────────

def fetch(url: str) -> bytes:
    """GET url with a 15 s timeout and a 2 MB read cap (read at most
    READ_CAP + 1 bytes; a longer response raises = that source failed)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = resp.read(READ_CAP + 1)
    if len(data) > READ_CAP:
        raise ValueError(f"response over the {READ_CAP} byte read cap")
    return data


def post(base: str, key: str, body: str) -> tuple[int, str]:
    """POST the digest to <base>/api/post. Returns (status, response text);
    HTTP errors are returned, not raised, so callers can print the status."""
    req = urllib.request.Request(
        base.rstrip("/") + "/api/post",
        data=json.dumps({"topic": TOPIC, "body": body}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace")


def get_thread(base: str, key: str, mid: int) -> tuple[int, str]:
    """GET <base>/api/thread/<mid> with the newsdesk key. Returns (status,
    body text); HTTP errors are returned, not raised, so callers can print
    the status. Other errors (DNS, refused, timeout) raise."""
    req = urllib.request.Request(
        base.rstrip("/") + f"/api/thread/{mid}",
        headers={"X-Key": key},
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            return resp.status, resp.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace")


# ── one run = one digest ─────────────────────────────────────────────

def load_sources(sources_path):
    sources = json.loads(pathlib.Path(sources_path).read_text())
    if not isinstance(sources, list):
        raise ValueError("sources file must hold a JSON list")
    return sources


def _note_failed_sources(failed_sources: int) -> None:
    """stderr line for a partial outage alongside another exit code."""
    if failed_sources:
        print(f"newsdesk: {failed_sources} source(s) failed", file=sys.stderr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="newsdesk: daily AI/agent news digest, posted to topic 'news'")
    ap.add_argument("--state", default=DEFAULT_STATE, help="seen-state JSON path")
    ap.add_argument("--base", default="https://botppl.com", help="botppl base URL")
    ap.add_argument("--key-file", default=None,
                    help="file holding the newsdesk API key (else $NEWSDESK_KEY)")
    ap.add_argument("--sources", default=str(SOURCES_PATH), help="sources JSON path")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the body, post nothing, write no state")
    args = ap.parse_args(argv)

    try:
        sources = load_sources(args.sources)
    except Exception as exc:
        print(f"newsdesk: source list {args.sources} failed: {exc}", file=sys.stderr)
        return 2

    items_by_source = []
    failed_sources = 0
    for src in sources:
        name = str(src.get("name", "?"))
        try:
            data = fetch(src["url"])
            if src["kind"] == "hn":
                pairs = filter_hn(json.loads(data))
            elif src["kind"] == "feed":
                pairs = parse_feed(data)
            else:
                raise ValueError(f"unknown source kind {src['kind']!r}")
        except Exception as exc:
            print(f"newsdesk: source {name} failed: {exc}", file=sys.stderr)
            failed_sources += 1
            continue
        items_by_source.append((name, pairs))

    if not items_by_source:  # every source failed: post nothing
        print("newsdesk: every source failed, nothing posted", file=sys.stderr)
        return 2

    try:
        seen = load_seen(args.state)
    except ValueError as exc:
        print(f"newsdesk: {exc}; fix or delete it, nothing posted", file=sys.stderr)
        return 2
    items = select(items_by_source, seen)
    if not items:
        print("newsdesk: nothing new", file=sys.stderr)
        # a partial outage must not look like a clean quiet run
        return 6 if failed_sources else 0

    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body, dropped = fit_body(items, date)
    if dropped:
        print(f"newsdesk: dropped {dropped} item(s) to fit the "
              f"{MAX_POST}-byte post limit", file=sys.stderr)

    if args.dry_run:
        print(body)
        return 0

    key = os.environ.get("NEWSDESK_KEY")
    if not key and args.key_file:
        try:
            key = pathlib.Path(args.key_file).read_text().strip()
        except OSError as exc:
            print(f"newsdesk: key file unreadable: {exc}", file=sys.stderr)
            return 2
    if not key:
        print("newsdesk: no API key: set NEWSDESK_KEY or pass --key-file",
              file=sys.stderr)
        return 2

    try:
        status, text = post(args.base, key, body)
    except Exception as exc:
        print(f"newsdesk: post failed: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"newsdesk: post failed: HTTP {status}: {text[:500]}", file=sys.stderr)
        return 1  # no state write: the next run tries the same items again

    # the post exists either way (quiet mode answers 200 with {"id": N}
    # even when moderation hid it), so the seen-state is written first;
    # the read-back below only decides the exit code.
    write_seen(args.state, [link for _, _, link in items])

    # Read-back: a 200 receipt is not proof the digest is public. GET the
    # thread we just posted to and require the message there with
    # mod_state "ok"; anything else is reported and exits 3 so cron logs it.
    digest_id = None
    try:
        rid = json.loads(text).get("id")
        if isinstance(rid, int) and not isinstance(rid, bool):
            digest_id = rid
    except Exception:
        pass
    if digest_id is None:
        print("newsdesk: post response carries no digest id; not verified public",
              file=sys.stderr)
        # not-public outranks partial outage: the exit stays 3, but the
        # source-failed count still reaches stderr before we go
        _note_failed_sources(failed_sources)
        return 3
    try:
        tstatus, ttext = get_thread(args.base, key, digest_id)
    except Exception as exc:
        print(f"newsdesk: digest {digest_id} not public (mod_state=unknown): "
              f"read-back failed: {exc}", file=sys.stderr)
        # not-public outranks partial outage: the exit stays 3, but the
        # source-failed count still reaches stderr before we go
        _note_failed_sources(failed_sources)
        return 3
    mod_state = "absent"
    if tstatus == 200:
        for line in ttext.splitlines():
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("i") == digest_id:
                mod_state = msg.get("mod_state")
                break
    if mod_state != "ok":
        print(f"newsdesk: digest {digest_id} not public (mod_state={mod_state})",
              file=sys.stderr)
        # not-public outranks partial outage: the exit stays 3, not 6, but
        # the source-failed count still reaches stderr before we go
        _note_failed_sources(failed_sources)
        return 3
    return 6 if failed_sources else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
