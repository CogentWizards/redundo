from redundo.adapter.hashing import (
    HASH_LENGTH,
    canonicalize_json,
    content_hash,
    mask_volatile,
    normalize_text,
)


def test_hash_length_is_16_hex_chars():
    digest, _ = content_hash("hello world")
    assert len(digest) == HASH_LENGTH
    assert all(c in "0123456789abcdef" for c in digest)


def test_same_text_same_hash():
    a, _ = content_hash("Summarize this report")
    b, _ = content_hash("Summarize this report")
    assert a == b


def test_different_text_different_hash():
    a, _ = content_hash("Summarize this report")
    b, _ = content_hash("Summarize that report")
    assert a != b


def test_whitespace_normalization_prevents_spurious_differences():
    a, _ = content_hash("Summarize   this\n\nreport")
    b, _ = content_hash("Summarize this report")
    assert a == b


def test_leading_trailing_whitespace_stripped():
    a, _ = content_hash("  hello  ")
    b, _ = content_hash("hello")
    assert a == b


def test_case_is_not_normalized():
    a, _ = content_hash("Hello")
    b, _ = content_hash("hello")
    assert a != b


def test_unicode_nfc_normalization():
    # Build both forms programmatically rather than via source-file
    # literals (editors/tools tend to silently re-normalize those): "e" +
    # combining acute accent (NFD) vs. the precomposed single code point
    # (NFC). Same rendered text, different bytes -- should hash identically
    # once run through NFC normalization.
    import unicodedata

    precomposed = unicodedata.normalize("NFC", "caf\u00e9")
    decomposed = unicodedata.normalize("NFD", precomposed)
    assert precomposed != decomposed  # sanity: the two forms really do differ

    a, _ = content_hash(decomposed)
    b, _ = content_hash(precomposed)
    assert a == b


def test_iso_datetime_masked():
    text, count = mask_volatile("Run started at 2026-08-31T12:00:00Z and finished")
    assert "<DATE>" in text
    assert "2026-08-31" not in text
    assert count == 1


def test_date_only_masked():
    text, count = mask_volatile("Report for 2026-08-31 is ready")
    assert "<DATE>" in text
    assert count == 1


def test_uuid_masked():
    text, count = mask_volatile("call_id=550e8400-e29b-41d4-a716-446655440000 done")
    assert "<UUID>" in text
    assert "550e8400" not in text
    assert count == 1


def test_duration_masked():
    text, count = mask_volatile("completed in 1.23s")
    assert "<DUR>" in text
    assert count == 1

    text2, count2 = mask_volatile("took 450ms total")
    assert "<DUR>" in text2
    assert count2 == 1


def test_hex_address_masked():
    text, count = mask_volatile("object at 0x7f9a3c001e10")
    assert "<ADDR>" in text
    assert count == 1


def test_tmp_path_masked():
    text, count = mask_volatile("wrote to /tmp/abc123/output.json")
    assert "<TMP>" in text
    assert count == 1

    text2, count2 = mask_volatile("wrote to /var/folders/xy/T/tmpabcd/file.txt")
    assert "<TMP>" in text2
    assert count2 == 1


def test_bare_integers_not_masked_by_default():
    text, count = mask_volatile("order id 123456789")
    assert "123456789" in text
    assert count == 0


def test_bare_integers_masked_when_opted_in():
    text, count = mask_volatile("order id 123456789", mask_integers=True)
    assert "<NUM>" in text
    assert count == 1


def test_masked_span_count_accumulates_across_categories():
    text, count = mask_volatile(
        "at 2026-08-31T12:00:00Z call 550e8400-e29b-41d4-a716-446655440000 took 1.2s"
    )
    assert count == 3


def test_repeated_calls_with_only_a_timestamp_difference_hash_identically_once_masked():
    a, _ = content_hash("Request made at 2026-08-31T12:00:00Z for user 42")
    b, _ = content_hash("Request made at 2026-08-31T12:05:33Z for user 42")
    assert a == b


def test_json_canonicalization_ignores_key_order():
    a = canonicalize_json({"b": 2, "a": 1})
    b = canonicalize_json({"a": 1, "b": 2})
    assert a == b


def test_json_canonicalization_no_insignificant_whitespace():
    text = canonicalize_json({"a": 1})
    assert " " not in text


def test_json_canonicalization_normalizes_integer_floats():
    a = canonicalize_json({"count": 1.0})
    b = canonicalize_json({"count": 1})
    assert a == b


def test_json_canonicalization_preserves_real_floats():
    text = canonicalize_json({"ratio": 1.5})
    assert "1.5" in text


def test_structured_content_hash_ignores_key_order():
    a, _ = content_hash({"tool": "search", "query": "ai news"}, structured=True)
    b, _ = content_hash({"query": "ai news", "tool": "search"}, structured=True)
    assert a == b


def test_structured_content_hash_accepts_json_string_or_parsed_object():
    a, _ = content_hash('{"a": 1, "b": 2}', structured=True)
    b, _ = content_hash({"b": 2, "a": 1}, structured=True)
    assert a == b


def test_structured_content_masks_volatile_values_inside_json():
    a, _ = content_hash(
        {"request_id": "550e8400-e29b-41d4-a716-446655440000", "query": "ai"}, structured=True
    )
    b, _ = content_hash(
        {"request_id": "11111111-2222-3333-4444-555555555555", "query": "ai"}, structured=True
    )
    assert a == b


def test_malformed_structured_content_falls_back_to_text_instead_of_raising():
    digest, _ = content_hash("{not valid json at all", structured=True)
    assert len(digest) == HASH_LENGTH


def test_malformed_structured_content_fallback_is_deterministic():
    a, _ = content_hash("{not valid json", structured=True)
    b, _ = content_hash("{not valid json", structured=True)
    assert a == b


def test_malformed_structured_content_fallback_still_masks_volatile_spans():
    a, _ = content_hash("{broken json with id 550e8400-e29b-41d4-a716-446655440000", structured=True)
    b, _ = content_hash("{broken json with id 11111111-2222-3333-4444-555555555555", structured=True)
    assert a == b
