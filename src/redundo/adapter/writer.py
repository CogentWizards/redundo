"""JSONL output -- one record per line, matching the redundo analyze
schema contract.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO


def write_jsonl(records: list[dict[str, Any]], stream: TextIO) -> None:
    for record in records:
        stream.write(json.dumps(record, ensure_ascii=False))
        stream.write("\n")


def write_jsonl_file(records: list[dict[str, Any]], path: str | Path) -> None:
    with Path(path).open("w", encoding="utf-8") as handle:
        write_jsonl(records, handle)
