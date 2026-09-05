"""Content hashing: the precise procedure, implemented once so it can be
copied correctly. See docs/hashing.md for the written contract this implements and
the reasoning behind each choice. Do not change this file's behavior
without bumping HASH_SPEC -- comparisons across corpora depend on both
sides having run identical code.

The one invariant that matters most: nothing downstream of this module
ever sees raw content, only hashes. That's what makes it safe to run this
adapter against production traces without a security review of the
analyzer. Don't let raw prompt/argument text leak into logs, exceptions,
or the `name`/`workflow` fields this module doesn't touch.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

HASH_SPEC = "v1"
HASH_LENGTH = 16  # hex chars = 64 bits. See docs/hashing.md for the birthday-bound math.

# Order matters: broader/more specific patterns first so a later pattern
# can't partially match inside a token an earlier pass already replaced.
_ISO_DATETIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_ISO_DATE_ONLY_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_RFC_DATE_RE = re.compile(
    r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\d{1,2}\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4}\s+"
    r"\d{2}:\d{2}:\d{2}\s+(?:GMT|UTC|[+-]\d{4})\b"
)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?(?:ms|us|µs|ns|s|m|h)\b")
_HEX_ADDR_RE = re.compile(r"\b0x[0-9a-fA-F]{4,}\b")
_TMP_PATH_RE = re.compile(r"(?:/tmp/|/var/folders/)\S*")
_BARE_INTEGER_RE = re.compile(r"\b\d{5,}\b")  # opt-in only; see mask_volatile

# (replacement token, pattern) in application order.
_MASKS: list[tuple[str, re.Pattern[str]]] = [
    ("<DATE>", _ISO_DATETIME_RE),
    ("<DATE>", _ISO_DATE_ONLY_RE),
    ("<DATE>", _RFC_DATE_RE),
    ("<UUID>", _UUID_RE),
    ("<DUR>", _DURATION_RE),
    ("<ADDR>", _HEX_ADDR_RE),
    ("<TMP>", _TMP_PATH_RE),
]


def normalize_text(text: str) -> str:
    """Unicode NFC, collapse whitespace runs, strip ends. For plain-text
    content (prompts, messages) -- not for structured content, which goes
    through canonicalize_json instead.
    """
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_numbers(value: Any) -> Any:
    """Floats with no fractional part collapse to int, so 1 and 1.0
    canonicalize identically. This is the one deliberate departure from
    "serialize exactly what json.loads gave you" -- documented in docs/hashing.md.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_numbers(v) for v in value]
    return value


def canonicalize_json(value: Any) -> str:
    """Sorted keys, no insignificant whitespace, normalized numbers.
    `value` may already be a parsed object, or a JSON string -- both are
    accepted so callers don't need to know which they have.
    """
    if isinstance(value, str):
        value = json.loads(value)
    normalized = _normalize_numbers(value)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def mask_volatile(text: str, *, mask_integers: bool = False) -> tuple[str, int]:
    """Replace volatile spans with stable placeholder tokens. Returns
    (masked_text, spans_masked) -- the count is the diagnostic signal:
    surface it in metadata so "zero repeats" and "zero repeats because
    something volatile leaked through" don't look identical.

    mask_integers is opt-in and off by default: a bare integer could be an
    epoch timestamp or an order ID, and masking order IDs would collapse
    genuinely different calls into one hash -- a false positive in the
    headline metric, which is worse than a false negative here.
    """
    total = 0
    for token, pattern in _MASKS:
        text, n = pattern.subn(token, text)
        total += n
    if mask_integers:
        text, n = _BARE_INTEGER_RE.subn("<NUM>", text)
        total += n
    return text, total


def content_hash(
    raw: Any,
    *,
    structured: bool = False,
    mask_integers: bool = False,
) -> tuple[str, int]:
    """The full procedure: normalize (text) or canonicalize (structured),
    mask volatile spans, hash. Returns (hash_hex, masked_span_count).

    `structured=True` for tool call arguments / JSON payloads.
    `structured=False` (default) for free-text prompts and messages.

    A `structured=True` value that isn't actually valid JSON (a mime_type
    attribute claiming application/json on malformed content, for
    instance) falls back to plain-text normalization instead of raising.
    This adapter runs inside the boundary that's supposed to keep raw
    content contained; letting one malformed record's parse error
    propagate as an uncaught exception is the wrong failure mode here --
    it aborts the whole conversion, and an exception object risks
    carrying a fragment of that content into a log or traceback somewhere
    outside this function's control. Degrading to text-mode hashing
    keeps every record processed and keeps content from ever needing to
    leave this function in the first place.
    """
    if structured:
        try:
            text = canonicalize_json(raw)
        except (TypeError, ValueError):
            text = normalize_text(str(raw))
    else:
        text = normalize_text(str(raw))
    masked_text, span_count = mask_volatile(text, mask_integers=mask_integers)
    digest = hashlib.sha256(masked_text.encode("utf-8")).hexdigest()[:HASH_LENGTH]
    return digest, span_count
