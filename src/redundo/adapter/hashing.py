"""Content hashing: the precise procedure, implemented once so it can be
copied correctly. See docs/hashing.md for the written contract this implements and
the reasoning behind each choice. Do not change this file's behavior
without bumping HASH_SPEC (exact-match hashing) or SIMILARITY_SPEC
(fingerprinting) as appropriate -- comparisons across corpora depend on
both sides having run identical code.

The one invariant that matters most: nothing downstream of this module
ever sees raw content, only hashes. That's what makes it safe to run this
adapter against production traces without a security review of the
analyzer. Don't let raw prompt/argument text leak into logs, exceptions,
or the `name`/`workflow` fields this module doesn't touch.

`similarity_fingerprint()` is a deliberate, narrower exception to that
invariant's strength, not a silent one: unlike `content_hash` (a real
cryptographic hash -- no information about closeness leaks from it at
all), a SimHash fingerprint is designed so that similar inputs produce
comparable outputs. That is what makes near-duplicate detection possible,
and it is also a real reduction in the one-wayness this module otherwise
guarantees -- see docs/hashing.md for the precise tradeoff before using
it anywhere the stronger guarantee is assumed.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

HASH_SPEC = "v1"
HASH_LENGTH = 16  # hex chars = 64 bits. See docs/hashing.md for the birthday-bound math.

SIMILARITY_SPEC = "v1"
SIMHASH_BITS = 64
SIMHASH_HEX_LENGTH = SIMHASH_BITS // 4  # kept as its own constant -- see docs/hashing.md
_SHINGLE_SIZE = 4  # characters, not words -- see similarity_fingerprint's docstring

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


def _masked_text(raw: Any, *, structured: bool, mask_integers: bool) -> tuple[str, int]:
    """normalize (text) or canonicalize (structured), then mask_volatile --
    the exact procedure `content_hash` and `similarity_fingerprint` both
    hash from, so a shared volatile span (a UUID, a timestamp) can't make
    two identical calls hash differently under `content_hash`, or -- the
    inverse and arguably more important failure for a *similarity* score
    -- make two genuinely unrelated calls look artificially similar under
    `similarity_fingerprint` just because they happen to share an
    unmasked timestamp or temp path.

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
    return mask_volatile(text, mask_integers=mask_integers)


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
    """
    masked_text, span_count = _masked_text(raw, structured=structured, mask_integers=mask_integers)
    digest = hashlib.sha256(masked_text.encode("utf-8")).hexdigest()[:HASH_LENGTH]
    return digest, span_count


def _shingles(text: str) -> list[str]:
    """Character n-grams, not word n-grams -- this project's actual
    content shapes (canonicalized JSON, URLs) have no whitespace token
    boundaries to split on (`{"query":"ai news"}` is one "word" under
    word-splitting). A string shorter than the shingle size is treated as
    one shingle of its own rather than producing none at all, which would
    leave `simhash64` with nothing to vote on.
    """
    if not text:
        return []
    if len(text) <= _SHINGLE_SIZE:
        return [text]
    return [text[i : i + _SHINGLE_SIZE] for i in range(len(text) - _SHINGLE_SIZE + 1)]


def _stable_shingle_hash(shingle: str) -> int:
    """A 64-bit hash stable across processes -- deliberately not Python's
    builtin hash(), which is randomized per-process for str
    (PYTHONHASHSEED) and would silently break every fingerprint
    comparison made across two separate `redundo adapt` runs.
    """
    digest = hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash64(text: str) -> int:
    """TF-weighted SimHash over character shingles. Term-frequency
    weighted, not TF-IDF -- there is no corpus-level document-frequency
    signal available here; this function sees one record's content at a
    time, by the same trust-boundary design as `content_hash` (raw
    content never accumulates across records inside this module). That
    means a JSON payload's repeated boilerplate keys/punctuation get
    outsized influence over the resulting bits for short structured
    content -- a known, inherent limitation of TF-only weighting on this
    kind of content, not a bug to fix later. See docs/hashing.md.

    A bit position whose vote sums to exactly zero resolves to 0,
    deterministically -- not a coin flip, so identical input always
    produces an identical fingerprint.
    """
    votes = [0] * SIMHASH_BITS
    for shingle in _shingles(text):
        shingle_hash = _stable_shingle_hash(shingle)
        for bit in range(SIMHASH_BITS):
            if (shingle_hash >> bit) & 1:
                votes[bit] += 1
            else:
                votes[bit] -= 1
    fingerprint = 0
    for bit in range(SIMHASH_BITS):
        if votes[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def similarity_fingerprint(
    raw: Any,
    *,
    structured: bool = False,
    mask_integers: bool = False,
) -> str:
    """A SimHash fingerprint over the same masked/normalized text
    `content_hash` hashes from -- hex-formatted, `SIMHASH_HEX_LENGTH`
    characters wide.

    This is NOT a drop-in replacement for `content_hash` and does not
    carry the same one-wayness guarantee: it is specifically designed so
    that similar inputs produce comparable outputs (that is what makes
    near-duplicate detection possible at all), which is a real, deliberate
    reduction in the module's usual "nothing about the content leaks"
    invariant -- see the module docstring and docs/hashing.md before using
    this anywhere that stronger guarantee is assumed to hold.
    """
    masked_text, _ = _masked_text(raw, structured=structured, mask_integers=mask_integers)
    fingerprint = simhash64(masked_text)
    return f"{fingerprint:0{SIMHASH_HEX_LENGTH}x}"


def hamming_distance(a: str, b: str) -> int:
    """Number of differing bits between two hex-formatted fingerprints
    from `similarity_fingerprint`. Smaller means more similar; 0 means an
    identical fingerprint, not necessarily identical content -- SimHash is
    lossy by construction (see `simhash64`).
    """
    return bin(int(a, 16) ^ int(b, 16)).count("1")
