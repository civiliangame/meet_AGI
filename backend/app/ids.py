"""Prefixed ULID generation.

IDs are `<prefix>_<26-char Crockford base32 ULID>`. ULIDs sort lexicographically by
creation time, which means `ORDER BY id` is a valid chronological sort and cursor
pagination can use the id directly. Prefixes make IDs self-describing in logs and
in the frontend, where a stray `doc_` where a `prs_` belongs is otherwise invisible.
"""

from __future__ import annotations

import os
import time
from typing import Final

# Crockford base32: no I, L, O, or U (removed to avoid transcription ambiguity).
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIX_PERSON: Final = "prs"
PREFIX_DOCUMENT: Final = "doc"
PREFIX_CHUNK: Final = "chk"
PREFIX_MEETING: Final = "mtg"
PREFIX_SEGMENT: Final = "seg"
PREFIX_INTERJECTION: Final = "itj"
PREFIX_UTTERANCE: Final = "utt"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def new_ulid() -> str:
    """48-bit millisecond timestamp + 80 bits of randomness, base32-encoded."""
    timestamp = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(timestamp, 10) + _encode(randomness, 16)


def new_id(prefix: str) -> str:
    return f"{prefix}_{new_ulid()}"
