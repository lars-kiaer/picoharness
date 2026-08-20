"""A `code` provider for `extract@1`, producing `log_summary@2`.

Principle P3: use deterministic code first. A log filter is `grep`, so this is
what `extract@1` looks like before a model is involved. It follows the rules in
`fixtures/README.md` under "How each field is decided" — the same rules a model
would be given in its system prompt.

Two things make this worth having beyond v1.

**It is the baseline.** The selection policy sorts `kind == "code"` first, so a
model only earns a place by beating this parser's pass rate at an acceptable
cost. Without a baseline, "the model scored 0.8" has nothing to be measured
against.

**It abstains honestly.** Section 3.1 calls abstention the property that matters
most. Code has an advantage here that no model has: when a rule does not apply,
there is nothing to produce, so `null` is what falls out rather than something
that has to be resisted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..payload import Payload

# --------------------------------------------------------------------------
# the vocabulary
# --------------------------------------------------------------------------

#: Whole words that mark a line as reporting a failure. `warn` is not here.
_ERROR_WORDS = (
    "emerg", "emergency", "alert", "crit", "critical", "panic", "fatal",
    "err", "error", "errors", "fail", "failed", "failure", "failures",
)
_ERROR_RE = re.compile(r"\b(" + "|".join(_ERROR_WORDS) + r")\b", re.IGNORECASE)

_SEVERITY_WORDS: dict[str, str] = {
    "emerg": "critical", "emergency": "critical", "alert": "critical",
    "crit": "critical", "critical": "critical", "panic": "critical", "fatal": "critical",
    "err": "error", "error": "error", "errors": "error", "fail": "error",
    "failed": "error", "failure": "error", "failures": "error",
    "warn": "warning", "warning": "warning",
    "notice": "info", "info": "info",
    "debug": "debug",
}
_SEVERITY_RE = re.compile(r"\b(" + "|".join(_SEVERITY_WORDS) + r")\b", re.IGNORECASE)
_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

#: RFC 5424 numeric severity is the priority modulo 8. 0-3 is a failure.
_SEVERITY_BY_NUMBER = ("critical", "critical", "critical", "error",
                       "warning", "info", "info", "debug")

#: A service lifecycle record, and not merely the word. systemd writes it at the
#: head of the message: "Started X.", "Starting X...". A line that says
#: "checkpoint starting" or "CTRL-EVENT-SCAN-STARTED" is reporting work, and
#: rule 7 does not count work as a service start.
_LIFECYCLE_RE = re.compile(
    r"^(started|starting|stopped|stopping|reloaded|restarting|scheduled restart)\b",
    re.IGNORECASE,
)
_CRON_RE = re.compile(r"\bcron|CMD\s*\(", re.IGNORECASE)

#: A collector saying the window is incomplete. This has to be narrow. A first
#: attempt matched bare "incomplete", and `adv-01` then nulled `error_count`
#: because one log line said "archive marked incomplete" — an injected line
#: could have done the same on purpose.
_TRUNCATED_RE = re.compile(
    r"\b(?:output|log|window|input)\s+(?:was\s+)?truncated"
    r"|truncated\s+(?:at|after|by)\b"
    r"|lines?\s+omitted"
    r"|output\s+limit\s+reached"
    r"|log\s+rotated\s+mid",
    re.IGNORECASE,
)
_REDACTED_RE = re.compile(r"removed by|redacted|scrubbed|\[message removed", re.IGNORECASE)

# --------------------------------------------------------------------------
# line shapes, tried in order
# --------------------------------------------------------------------------

_RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>\d\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+(?P<tag>\S+)\s+\S+\s+\S+\s+\S+\s*(?P<msg>.*)$"
)
_RFC3164 = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<tag>[^\s:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$"
)
_ISOHOST = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\s+"
    r"(?P<host>\S+)\s+(?P<tag>[^\s:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$"
)
_LEVELTAG = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)\s+"
    r"(?P<level>[A-Za-z]{3,8})\s+(?P<tag>[^\s:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$"
)
_KERNEL = re.compile(r"^(?P<ts>\[\s*\d+\.\d+\])\s*(?P<msg>.*)$")
_TAGONLY = re.compile(r"^(?P<tag>[^\s:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$")
_ISOONLY = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:?\d{2}|Z)?)"
    r"\s+(?P<msg>.*)$"
)


@dataclass(frozen=True, slots=True)
class Line:
    raw: str
    ts: str | None = None
    host: str | None = None
    tag: str | None = None
    msg: str = ""
    priority: int | None = None

    @property
    def severity(self) -> str | None:
        """What this line states, or None if it states nothing."""
        if self.priority is not None:
            return _SEVERITY_BY_NUMBER[self.priority % 8]
        found = _SEVERITY_RE.search(self.msg) or _SEVERITY_RE.search(self.raw)
        if not found:
            return None
        return _SEVERITY_WORDS[found.group(1).lower()]

    @property
    def is_error(self) -> bool:
        if self.priority is not None and self.priority % 8 <= 3:
            return True
        return bool(_ERROR_RE.search(self.msg) or _ERROR_RE.search(self.raw))


def parse_line(raw: str) -> Line:
    """Recognise one line. Unknown shapes still yield a Line, with less in it."""
    for pattern in (_RFC5424, _RFC3164, _LEVELTAG, _ISOHOST):
        m = pattern.match(raw)
        if m:
            groups = m.groupdict()
            pri = groups.get("pri")
            return Line(
                raw=raw,
                ts=groups.get("ts"),
                host=None if groups.get("host") in ("-", None) else groups["host"],
                tag=None if groups.get("tag") in ("-", None) else groups["tag"],
                msg=groups.get("msg", "").strip(),
                priority=int(pri) if pri else None,
            )
    for pattern in (_KERNEL, _ISOONLY):
        m = pattern.match(raw)
        if m:
            return Line(raw=raw, ts=m.group("ts"), msg=m.group("msg").strip())
    m = _TAGONLY.match(raw)
    if m and m.group("tag").lower() not in _SEVERITY_WORDS:
        return Line(raw=raw, tag=m.group("tag"), msg=m.group("msg").strip())
    return Line(raw=raw, msg=raw.strip())


# --------------------------------------------------------------------------
# the capability
# --------------------------------------------------------------------------


def extract(payload: Payload) -> dict[str, Any]:
    """`extract@1` over a window of log text, producing `log_summary@2`.

    Every field is derived or `null`. Nothing here invents a value, which is the
    property section 7.5 says disqualifies a provider when it is absent.
    """
    body = payload.as_text()
    lines = [parse_line(raw) for raw in body.splitlines() if raw.strip()]
    errors = [line for line in lines if line.is_error]
    first = errors[0] if errors else None

    truncated = any(_TRUNCATED_RE.search(line.raw) for line in lines)
    severities = [s for s in (line.severity for line in lines) if s]

    return {
        "host": _host(lines, first),
        "error_count": None if truncated else len(errors),
        "first_error": _first_error(first),
        "first_error_at": first.ts if first else None,
        "service": first.tag if first else None,
        "max_severity": max(severities, key=lambda s: _RANK[s]) if severities else None,
        "service_restarted": _restarted(lines),
    }


def _host(lines: list[Line], first: Line | None) -> str | None:
    """The host on the first error line; otherwise the first host stated."""
    if first is not None and first.host:
        return first.host
    for line in lines:
        if line.host:
            return line.host
    return None


def _first_error(first: Line | None) -> str | None:
    """The message of the first error line, or None when it was removed."""
    if first is None or not first.msg:
        return None
    if _REDACTED_RE.search(first.msg):
        return None
    return first.msg


def _restarted(lines: list[Line]) -> bool | None:
    """True, False, or None — applied in that order. See rule 7.

    `None` is the interesting case: it means the window carries no service
    records at all, so the question does not have an answer here. A model that
    guesses `false` for that is inventing, and rule 7 is where it shows.
    """
    for line in lines:
        if _CRON_RE.search(line.tag or ""):
            continue  # a cron job running a command is not a service start
        if _LIFECYCLE_RE.match(line.msg):
            return True
    return False if any(line.tag for line in lines) else None


__all__ = ["extract", "parse_line", "Line"]
