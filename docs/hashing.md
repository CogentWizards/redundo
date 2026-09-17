# Content hashing

Shared by every source in `redundo.adapter.sources`. Implemented once in
`src/redundo/adapter/hashing.py`, not per-source, so hashes stay
comparable across sources. Originally written for the OpenInference
source specifically; the procedure itself has no source-specific logic
at all.

The analyzer never sees content, only hashes. That's what makes it safe
to run against production traces without a security review of the
analyzer itself. That means normalization has to happen here, once,
precisely, so it's implemented the same way everywhere.

**Algorithm**: SHA-256, truncated to 16 hex characters (64 bits). Not 8:
32 bits puts the birthday-bound 50% collision probability around 77,000
items, uncomfortably close to a real trace corpus. 64 bits is beyond any
realistic run.

**Plain text** (prompts, messages): Unicode NFC normalize, collapse
whitespace runs to a single space, strip leading/trailing. Cheap, safe,
prevents spurious differences from re-rendering. Case is preserved,
never lowercased: case differences in prompts are usually meaningful and
lowercasing buys nothing for repeat detection.

**Structured content** (tool call arguments, JSON payloads): canonical
JSON, sorted keys, no insignificant whitespace, floats with no
fractional part normalized to int (so `1` and `1.0` canonicalize
identically). Key ordering alone otherwise produces different hashes for
identical calls.

If content marked structured (e.g. by `input.mime_type`) turns out not to
be valid JSON, fall back to plain-text normalization rather than raising.
The adapter is the trust boundary that's supposed to keep raw content from
crossing into the analyzer; a parse error propagating as an uncaught
exception aborts the whole conversion over one bad record and risks that
content reaching a log or traceback outside this procedure's control.
Degrading to text-mode hashing keeps every record processed without ever
needing raw content to leave the hashing function.

**Masking, applied after normalization/canonicalization, before hashing**,
in this order:

| Pattern | Replacement |
|---|---|
| ISO 8601 datetimes and date-only, RFC-1123-style dates | `<DATE>` |
| UUIDs | `<UUID>` |
| Durations (`1.23s`, `450ms`) | `<DUR>` |
| Hex addresses (`0x7f...`) | `<ADDR>` |
| Temp paths (`/tmp/...`, `/var/folders/...`) | `<TMP>` |
| OpenClaw's `EXTERNAL_UNTRUSTED_CONTENT id="..."` wrapper id | `<ID>` |
| Bare integers (5+ digits) | `<NUM>`. Opt-in only, off by default |

Volatile content, not whitespace, is what actually breaks the metric. If
any prompt contains a timestamp, a UUID, or a per-call ID, no two prompts
ever hash the same and the non-productive-cycle signal silently returns
zero, not an error, just an empty result that looks like good news.
CrewAI injects the current date into agent context and generates a fresh
`call_id` per call via `llm_call_context()`; every framework has its own
equivalent. OpenClaw's own prompt-injection defense wraps every piece of
external tool content in `<<<EXTERNAL_UNTRUSTED_CONTENT id="...">>>`
markers with a fresh random id on every call, confirmed live: two
byte-identical `web_search` results seconds apart, served from cache,
still hashed differently before this mask existed. Mask before you trust
a zero.

Numeric masking is opt-in because a bare integer is ambiguous. It could
be an epoch timestamp or an order ID, and masking order IDs collapses
genuinely different lookups into one hash, which is a false positive in
the headline metric. A false positive there is worse than a false
negative from an unmasked timestamp slipping through.

No stemming, no semantic normalization, no embedding similarity. This is
exact-repeat detection with volatile fields masked. Fuzzy matching turns a
crisp signal into a threshold someone has to defend.

## Similarity fingerprinting

The paragraph above still describes `content_hash` completely and
correctly. It stays exact-match-only, unconditionally. A second, separate
function, `similarity_fingerprint()`, exists alongside it for one
specific, lower-confidence use case: detecting calls that are *similar
but not identical* (a retried search with slightly reworded terms, a
re-fetched URL with a different query parameter), something `content_hash`
cannot do by design, and was never meant to. Where `content_hash` is used
is unchanged by this; `similarity_fingerprint` is additive, opt-in per
source, and feeds a separate, explicitly lower-confidence bucket in the
waste analysis (see `redundo.analyzer.near_duplicates`), never the three
exact-match buckets.

**Algorithm**: SimHash over character 4-gram shingles of the same
masked/normalized text `content_hash` hashes from. Masking matters *more*
here than for exact hashing: an unmasked shared timestamp or UUID could
artificially inflate similarity between two genuinely unrelated calls,
not merely fail to match two identical ones. Each shingle is hashed with
`blake2b` (stable across processes, unlike Python's randomized builtin
`hash()`) to 64 bits; each of the 64 bit positions gets a
term-frequency-weighted vote across every shingle, and the final bit is
the sign of that vote. Two fingerprints are compared by Hamming distance
(bits differing, 0 to 64). Smaller means more similar, 0 means identical
fingerprint (not necessarily identical content, SimHash is lossy).

**This is not a stronger or weaker version of `content_hash`. It is a
different kind of guarantee, and a real reduction in the one that matters
most.** `content_hash` is a real cryptographic hash: given the digest
alone, nothing about the underlying content's *closeness* to any other
content leaks. A similarity-preserving fingerprint cannot make that
promise. Closeness leaking is the entire point, since that's what makes
near-duplicate detection possible at all. Concretely: given
`hamming_distance` oracle access (which any reader of this metadata
already has) and a set of guesses, an attacker can iteratively refine a
guess toward a short, low-entropy secret, a URL, a search query, a short
code, watching the distance shrink with each attempt, in a way
`content_hash` never permits (its only usable attack is an exact-match
dictionary guess, all-or-nothing, no partial credit). This is a real,
deliberate tradeoff accepted for the capability, not an oversight. It
does **not** change anything about `content_hash`'s own guarantee, which
is unaffected and unchanged.

**No IDF, only TF.** Unlike a corpus-aware similarity scheme, this
function sees one record's content at a time, the same one-record-at-a-time
trust boundary `content_hash` already has, never accumulating content
across records. That means there is no document-frequency signal to
down-weight boilerplate: a JSON payload's repeated keys and punctuation
get outsized influence on the resulting bits for short structured
content. A known, inherent limitation of this approach on this kind of
content, not a bug to fix later.

**The similarity threshold is not a validated constant.** The commonly
cited "around 3 bits out of 64" figure from web-page near-duplicate
detection literature does not transfer here. That number assumes long
documents with many shingles, where a single edit moves only a small
fraction of the vote. This project's actual content (short JSON tool
arguments, search queries, URLs) means a single differing token can flip
a much larger share of the 64 bits.
`redundo.analyzer.near_duplicates.DEFAULT_SIMILARITY_THRESHOLD` is a
documented starting point calibrated against real captured examples, not
a number to trust blindly. It's exposed as a constructor kwarg
specifically so it can be recalibrated per corpus.

**Fingerprints are only ever computed for call-side content** (an
`llm_call`'s prompt, a `tool_call`'s arguments), never for a `tool_result`
or an LLM response. "Was this call similar to an earlier call" is the
useful redundancy question; "was this result similar to an earlier
result" is a different, unscoped one `classify.py` doesn't ask either. A
record with only an opaque `content_hash` (no observable content at all)
never gets a fingerprint. A SimHash over a span's own arbitrary id would
be noise, not signal, and risks a coincidentally small Hamming distance
between two unrelated opaque records: a false near-duplicate.

### The masking diagnostic

Every record's `metadata` carries `masked_spans`: the count of masks
applied to that record's content before hashing. At the corpus level,
report what fraction of records had at least one mask applied.

Two readings that need to stay distinguishable:

- Zero masks and zero repeats found: probably genuinely no cycles.
- Zero repeats found but heavy masking: suspicious. Something volatile
  may still be getting through unmasked, or masking removed the very
  signal that would have shown a repeat. The metric may be blind here,
  not clean.

Without the masking-fraction number, those two cases produce identical
output ("no waste found") and look the same. With it, "no waste found"
and "measurement may have failed" are distinguishable, which is the whole
point of reporting a masking rate at all.

### Versioning

Every record's `metadata.hash_spec` is the string `"v1"` (this document's
version). If this normalization or masking procedure changes, that bumps.
Comparing two corpora hashed under different `hash_spec` values is
meaningless: differing hashes could mean genuinely different content, or
just differently-normalized identical content. **Refuse to compare
mismatched specs rather than producing a confident wrong answer.**

`metadata.similarity_spec` versions `similarity_fingerprint()` the same
way, independently of `HASH_SPEC`. A future change to the SimHash
procedure (shingle size, hash function, bit width) shouldn't silently
also invalidate `content_hash` comparisons, and vice versa. Same refusal
discipline applies: `redundo.analyzer.ingest.check_consistent_similarity_spec`
raises rather than comparing fingerprints computed under two different
procedures.

### Reference implementation

Copy this, about 15 lines. That's the point. The more people copy this exact procedure
rather than reimplementing it from the prose above, the more hashes stay
comparable across users, which is what makes any cross-corpus claim
possible later. The full version with the diagnostic count and JSON
number normalization lives in `src/redundo/adapter/hashing.py`.

```python
import hashlib, json, re, unicodedata

MASKS = [
    (r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b", "<DATE>"),
    (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<UUID>"),
    (r"\b\d+(?:\.\d+)?(?:ms|us|ns|s|m|h)\b", "<DUR>"),
    (r"\b0x[0-9a-fA-F]{4,}\b", "<ADDR>"),
    (r"(?:/tmp/|/var/folders/)\S*", "<TMP>"),
]

def content_hash(text: str, structured: bool = False) -> str:
    try:
        if not structured:
            raise ValueError
        text = json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    except ValueError:
        text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()
    for pattern, token in MASKS:
        text = re.sub(pattern, token, text)
    return hashlib.sha256(text.encode()).hexdigest()[:16]
```
