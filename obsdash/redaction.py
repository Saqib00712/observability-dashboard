"""
Phase 10: privacy helpers -- text redaction and label bounding.

Two different jobs:

1. `redact_text()` -- scrub secrets / personal data that leaked into a STRING
   (an exception message, a log line, a span attribute, a request path):
   bearer tokens, provider API keys, JWTs, cloud access keys,
   `password=...` style pairs, e-mail addresses, and payment-card numbers
   (digits that pass the Luhn checksum, so ordinary numbers survive).

   Honest limits: this is pattern-based best effort, not a guarantee.
   Names, street addresses, phone numbers and free-form secrets with no
   recognisable shape are NOT detected. The strongest protection is still
   not recording content at all (the AI wrapper has no prompt/response
   setters; headers and bodies are never stored). Redaction is the
   second line of defence for what slips through by accident.

2. `sanitize_label()` / `LabelLimiter` -- keep metric labels bounded:
   strip control characters, cap length, and collapse everything past N
   distinct values into one "__other__" bucket so a caller (or an
   attacker) cannot create unlimited time series.
"""

import re
import threading

REDACTED = "[redacted]"
OTHER = "__other__"
_MAX_SCAN_CHARS = 4000  # bound regex work on huge strings

_KEY_VALUE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|authorization|access[_-]?key|client[_-]?secret)"
    r"([\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_PROVIDER_KEY = re.compile(
    r"\b(?:(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}|(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD_CANDIDATE = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def luhn_valid(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card_sub(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[redacted-card]" if 13 <= len(digits) <= 19 and luhn_valid(digits) else m.group(0)


def redact_text(text: str, max_len: int | None = None) -> str:
    """Return `text` with recognisable secrets/PII replaced. Optionally
    truncated to `max_len` characters (after redaction)."""
    if not isinstance(text, str):
        return text
    s = text[:_MAX_SCAN_CHARS]
    # Bearer/Basic first, so "Authorization: Bearer <token>" loses the token
    # itself (the key=value rule alone would only swallow the word "Bearer").
    s = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", s)
    s = _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", s)
    s = _JWT.sub(REDACTED, s)
    s = _PROVIDER_KEY.sub(REDACTED, s)
    s = _EMAIL.sub("[redacted-email]", s)
    s = _CARD_CANDIDATE.sub(_card_sub, s)
    return s[:max_len] if max_len else s


def sanitize_label(value, max_len: int = 64) -> str:
    s = _CONTROL.sub("", redact_text(str(value))).strip()[:max_len]
    return s or "unknown"


class LabelLimiter:
    """Allows at most `max_values` distinct keys; the rest map to OTHER.
    Keys can be any hashable (e.g. a (provider, target) tuple)."""

    def __init__(self, max_values: int = 100) -> None:
        self._max = max_values
        self._seen: set = set()
        self._lock = threading.Lock()
        self.overflowed = 0

    def admit(self, key):
        with self._lock:
            if key in self._seen:
                return key
            if len(self._seen) < self._max:
                self._seen.add(key)
                return key
            self.overflowed += 1
            return None  # caller substitutes OTHER
