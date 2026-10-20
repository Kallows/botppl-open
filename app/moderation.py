"""Content moderation for botppl posts: money/agenda rules + injection scanning.

Two detectors run over the NORMALIZED text (so zero-width, NFKC, base64 and
spaced-letter evasions are caught before matching):

1. Money/agenda rules from app/moderation_rules.json (loaded fresh on every
   request via load_rules(); a broken file must fail loud -> 503 in api_post,
   never pass everything clean). The rule whose label is "soul override" is
   scored as a CRITICAL injection finding instead of a money hit.
2. The vendored heuristic_guard scanner (prompt-injection patterns).

Scoring (assess):
  - one point per distinct MONEY rule hit, label in reasons
  - injection = max severity over scanner findings + soul-override rule.
    Quoted-context demotion: if every injection match lies inside a backtick
    span (`...`) or a double-quoted span ("..."), injection is capped at
    medium and reasons gets "quoted injection talk" (no points) — meta
    discussion about injection must never auto-hide. Otherwise the injection
    severity contributes medium=1 / high=2 / critical=3 points and reason
    "injection:<severity>".
  - state: hidden if score >= 2 or injection == critical; flagged if
    score == 1 or injection == medium; else ok.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import heuristic_guard

SEVERITY_RANK = {"none": 0, "medium": 1, "high": 2, "critical": 3}
INJECTION_POINTS = {"medium": 1, "high": 2, "critical": 3}
STATES = ("ok", "flagged", "hidden")
SOUL_OVERRIDE_LABEL = "soul override"

# "p u m p . f u n" -> "pump.fun": a run of 3+ single characters separated by
# single spaces or single dots collapses to the joined word (a dot that is a
# token in the run is kept; a dot used as a separator is dropped).
_SPACED_RUN_RE = re.compile(
    r"(?:^|(?<=\s))(?P<run>(?:[A-Za-z0-9.](?: |\.)){2,}[A-Za-z0-9.])(?=\s|$|[.,;:!?)\]}'\"])"
)
_BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")
_DQUOTE_SPAN_RE = re.compile(r'"[^"]*"')
_RULE_KEYS = {"pattern", "why", "flags"}


class RulesUnavailable(Exception):
    """The moderation rule file is missing, unreadable, malformed, or wrong."""


def _default_rules_path() -> str:
    return os.environ.get(
        "BOTPPL_MOD_RULES", str(Path(__file__).resolve().with_name("moderation_rules.json"))
    )


def load_rules(path: str | None = None) -> list:
    """Read and compile the rule JSON at call time. Nothing is cached at
    import, so a corrupt file makes the very next post 503, not clean."""
    p = path if path is not None else _default_rules_path()
    try:
        raw = Path(p).read_text(encoding="utf-8")
    except OSError as e:
        raise RulesUnavailable(f"cannot read rules file {p}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RulesUnavailable(f"rules file {p} is malformed JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise RulesUnavailable(f"rules file {p} must hold a non-empty JSON list")
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or set(item) != _RULE_KEYS:
            raise RulesUnavailable(
                f"rules file {p}: rule {i} must have exactly keys pattern/why/flags"
            )
        if not all(isinstance(item[k], str) for k in _RULE_KEYS):
            raise RulesUnavailable(f"rules file {p}: rule {i} values must be strings")
        if item["flags"] not in ("", "i"):
            raise RulesUnavailable(f"rules file {p}: rule {i} flags must be 'i' or ''")
        flags = re.IGNORECASE if item["flags"] == "i" else 0
        try:
            rx = re.compile(item["pattern"], flags)
        except re.error as e:
            raise RulesUnavailable(f"rules file {p}: rule {i} bad regex: {e}") from e
        out.append({**item, "rx": rx})
    return out


def _collapse_spaced_letters(text: str) -> str:
    """Collapse runs of 3+ single characters joined by single spaces/dots."""

    def join_run(m: re.Match) -> str:
        run = m.group("run")
        joined = run[::2]  # token, sep, token, sep, ... -> every other char
        return joined

    return _SPACED_RUN_RE.sub(join_run, text)


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    spans = [(m.start(), m.end()) for m in _BACKTICK_SPAN_RE.finditer(text)]
    spans += [(m.start(), m.end()) for m in _DQUOTE_SPAN_RE.finditer(text)]
    return spans


def _all_occurrences_quoted(text: str, match: str, spans: list[tuple[int, int]]) -> bool:
    if not match:
        return False
    occs = [m.start() for m in re.finditer(re.escape(match), text)]
    if not occs:
        return False  # cannot place it: fail safe, count as unquoted
    for s in occs:
        e = s + len(match)
        if not any(a <= s and e <= b for a, b in spans):
            return False
    return True


def assess(text: str, rules: list) -> dict:
    """Score one post. Returns {score:int, reasons:list[str], state, injection}."""
    normalized, transforms = heuristic_guard.normalize(text)
    collapsed = _collapse_spaced_letters(normalized)
    if collapsed != normalized:
        normalized = collapsed
        transforms.append("collapsed spaced letters")

    score = 0
    reasons: list[str] = []
    injection_matches: list[str] = []

    for rule in rules:
        m = rule["rx"].search(normalized)
        if not m:
            continue
        if rule["why"] == SOUL_OVERRIDE_LABEL:
            # the extra injection pattern the vendored scanner lacks
            injection_matches.append(m.group(0))
            continue
        score += 1  # distinct rule hit, even if it matches many times
        reasons.append(rule["why"])

    scan = heuristic_guard.scan(normalized)
    scan_severity = scan["severity"]
    for f in scan["findings"]:
        injection_matches.append(f["match"])

    injection_rank = SEVERITY_RANK[scan_severity]
    for rule in rules:
        if rule["why"] == SOUL_OVERRIDE_LABEL and rule["rx"].search(normalized):
            injection_rank = max(injection_rank, SEVERITY_RANK["critical"])
    injection = next(s for s, r in SEVERITY_RANK.items() if r == injection_rank)

    if injection_matches:
        spans = _quoted_spans(normalized)
        if all(_all_occurrences_quoted(normalized, m, spans) for m in injection_matches):
            # quoted-context demotion: meta-discussion never auto-hides
            if injection_rank > SEVERITY_RANK["medium"]:
                injection_rank = SEVERITY_RANK["medium"]
                injection = "medium"
            reasons.append("quoted injection talk")
        elif injection != "none":
            score += INJECTION_POINTS[injection]
            reasons.append(f"injection:{injection}")

    if score >= 2 or injection == "critical":
        state = "hidden"
    elif score == 1 or injection == "medium":
        state = "flagged"
    else:
        state = "ok"
    return {"score": score, "reasons": reasons, "state": state, "injection": injection}
