# Content hashing

Shared by every source in `redundo.adapter.sources` -- implemented
once in `src/redundo/adapter/hashing.py`, not per-source, so hashes
stay comparable across sources. Originally written for the OpenInference
source specifically; the procedure itself has no source-specific logic
at all.

The analyzer never sees content, only hashes -- that's what makes it safe
to run against production traces without a security review of the
analyzer itself. That means normalization has to happen here, once,
precisely, so it's implemented the same way everywhere.

**Algorithm**: SHA-256, truncated to 16 hex characters (64 bits). Not 8:
32 bits puts the birthday-bound 50% collision probability around 77,000
items, uncomfortably close to a real trace corpus. 64 bits is beyond any
realistic run.

**Plain text** (prompts, messages): Unicode NFC normalize, collapse
whitespace runs to a single space, strip leading/trailing. Cheap, safe,
prevents spurious differences from re-rendering. Do not lowercase --
case differences in prompts are usually meaningful and lowercasing buys
nothing for repeat detection.

**Structured content** (tool call arguments, JSON payloads): canonical
JSON -- sorted keys, no insignificant whitespace, floats with no
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
| Bare integers (5+ digits) | `<NUM>` -- **opt-in only, off by default** |

Volatile content, not whitespace, is what actually breaks the metric: if
any prompt contains a timestamp, a UUID, or a per-call ID, no two prompts
ever hash the same and the non-productive-cycle signal silently returns
zero -- not an error, just an empty result that looks like good news.
CrewAI injects the current date into agent context and generates a fresh
`call_id` per call via `llm_call_context()`; every framework has its own
equivalent. Mask before you trust a zero.

Numeric masking is opt-in because a bare integer is ambiguous -- it could
be an epoch timestamp or an order ID, and masking order IDs collapses
genuinely different lookups into one hash, which is a false positive in
the headline metric. A false positive there is worse than a false
negative from an unmasked timestamp slipping through.

No stemming, no semantic normalization, no embedding similarity. This is
exact-repeat detection with volatile fields masked. Fuzzy matching turns a
crisp signal into a threshold someone has to defend.

### The diagnostic that makes this trustworthy

Every record's `metadata` carries `masked_spans`: the count of masks
applied to that record's content before hashing. At the corpus level,
report what fraction of records had at least one mask applied.

Two readings that need to stay distinguishable:

- Zero masks and zero repeats found -> probably genuinely no cycles.
- Zero repeats found but heavy masking -> suspicious. Something volatile
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
meaningless -- differing hashes could mean genuinely different content, or
just differently-normalized identical content. **Refuse to compare
mismatched specs rather than producing a confident wrong answer.**

### Reference implementation (~15 lines)

Copy this. That's the point -- the more people copy this exact procedure
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
