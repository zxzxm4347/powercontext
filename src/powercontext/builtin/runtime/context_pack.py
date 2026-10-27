# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Incremental Context Pack wire format and delta reconstruction."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ContextPackError(ValueError):
    """Raised when a ContextPack or ContextPackDelta fails validation."""


def _validate_digest_format(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        raise ContextPackError(f"Invalid {field_name}: {value!r}")
    return value


def _validate_items_tuple(items: object) -> tuple[bytes, ...]:
    if isinstance(items, (bytes, bytearray, str)) or not isinstance(items, Iterable):
        raise ContextPackError("items must be an iterable of bytes")
    result: list[bytes] = []
    for item in items:
        if type(item) is not bytes:
            raise ContextPackError("each item must be bytes")
        result.append(item)
    return tuple(result)


def _encode_b64_list(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_b64_list(raw_list: object, field_name: str) -> tuple[bytes, ...]:
    if not isinstance(raw_list, list):
        raise ContextPackError(f"{field_name} must be a list of base64 strings")
    decoded: list[bytes] = []
    for entry in raw_list:
        if not isinstance(entry, str):
            raise ContextPackError(f"{field_name} elements must be strings")
        try:
            raw_bytes = base64.b64decode(entry.encode("ascii"), validate=True)
        except Exception as exc:
            raise ContextPackError(f"Invalid base64 in {field_name}") from exc
        if base64.b64encode(raw_bytes).decode("ascii") != entry:
            raise ContextPackError(f"Non-canonical base64 in {field_name}")
        decoded.append(raw_bytes)
    return tuple(decoded)


def _compute_pack_digest_and_bytes(items: tuple[bytes, ...]) -> tuple[str, int, bytes]:
    unsigned = {
        "schema": PACK_SCHEMA,
        "items": _encode_b64_list(items),
    }
    canonical_unsigned = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = "sha256:" + hashlib.sha256(canonical_unsigned).hexdigest()
    byte_count = 0
    serialized = b""
    for _ in range(5):
        payload = {
            **unsigned,
            "digest": digest,
            "byte_count": byte_count,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(serialized) == byte_count:
            break
        byte_count = len(serialized)
    return digest, byte_count, serialized


_BOUNDARY_RE = re.compile(
    r'(?=\},\{"citation":|\]\}\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1|\n### |\nEND_POWERCONTEXT_PREPARED_TEXT_V1)'
)


def _split_structural_segments(text: str) -> list[str]:
    segments: list[str] = []
    for part in _BOUNDARY_RE.split(text):
        if not part:
            continue
        rev_marker = '"revision":'
        if not segments and rev_marker in part:
            idx = part.index(rev_marker) + len(rev_marker)
            segments.append(part[:idx])
            if part[idx:]:
                segments.append(part[idx:])
        else:
            segments.append(part)
    return segments


def _chunk_utf8_text(text: str, chunk_bytes: int) -> tuple[bytes, ...]:
    chunks: list[bytes] = []
    for segment in _split_structural_segments(text):
        encoded_seg = segment.encode("utf-8")
        if len(encoded_seg) <= chunk_bytes:
            chunks.append(encoded_seg)
            continue
        current = bytearray()
        for ch in segment:
            ch_bytes = ch.encode("utf-8")
            if len(ch_bytes) > chunk_bytes:
                raise ContextPackError(
                    f"UTF-8 character requires {len(ch_bytes)} bytes, exceeding chunk_bytes={chunk_bytes}"
                )
            if current and len(current) + len(ch_bytes) > chunk_bytes:
                chunks.append(bytes(current))
                current.clear()
            current.extend(ch_bytes)
        if current:
            chunks.append(bytes(current))
    return tuple(chunks)


@dataclass(frozen=True, slots=True)
class ContextPack:
    """Serialized, chunked representation of a PreparedContext payload."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    def __post_init__(self) -> None:
        validated_items = _validate_items_tuple(self.items)
        object.__setattr__(self, "items", validated_items)
        _validate_digest_format(self.digest, "digest")
        if type(self.byte_count) is not int or self.byte_count <= 0:
            raise ContextPackError("byte_count must be a positive integer")
        expected_digest, expected_byte_count, _ = _compute_pack_digest_and_bytes(validated_items)
        if self.digest != expected_digest:
            raise ContextPackError("ContextPack digest does not match items")
        if self.byte_count != expected_byte_count:
            raise ContextPackError("ContextPack byte_count does not match serialized length")

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        validated_items = _validate_items_tuple(items)
        digest, byte_count, _ = _compute_pack_digest_and_bytes(validated_items)
        return cls(items=validated_items, digest=digest, byte_count=byte_count)

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = 512) -> ContextPack:
        if type(chunk_bytes) is not int or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if content is None or content == "":
            return cls.build(())
        if not isinstance(content, str):
            raise ContextPackError("content must be a str or None")
        try:
            content.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ContextPackError("content must be valid UTF-8") from exc
        chunks = _chunk_utf8_text(content, chunk_bytes)
        return cls.build(chunks)

    def to_bytes(self) -> bytes:
        _, _, serialized = _compute_pack_digest_and_bytes(self.items)
        return serialized

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPack:
        if not isinstance(raw, (bytes, bytearray)):
            raise ContextPackError("raw must be bytes")
        try:
            payload = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except Exception as exc:
            raise ContextPackError("Invalid UTF-8 or JSON in ContextPack") from exc
        if not isinstance(payload, dict) or set(payload.keys()) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("ContextPack JSON must contain exact schema, items, digest, and byte_count")
        if payload["schema"] != PACK_SCHEMA:
            raise ContextPackError(f"Unsupported ContextPack schema: {payload['schema']!r}")
        items = _decode_b64_list(payload["items"], "items")
        digest = _validate_digest_format(payload["digest"], "digest")
        byte_count = payload["byte_count"]
        if type(byte_count) is not int or byte_count <= 0:
            raise ContextPackError("byte_count must be a positive integer")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Prefix-reuse delta transforming a base ContextPack into a target ContextPack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest_format(self.base_digest, "base_digest")
        _validate_digest_format(self.target_digest, "target_digest")
        if type(self.prefix_items) is not int or self.prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        validated_appended = _validate_items_tuple(self.appended)
        object.__setattr__(self, "appended", validated_appended)

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("base and target must be ContextPack instances")
        prefix_len = 0
        for b_item, t_item in zip(base.items, target.items, strict=False):
            if b_item != t_item:
                break
            prefix_len += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix_len,
            appended=target.items[prefix_len:],
        )

    def to_bytes(self) -> bytes:
        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_b64_list(self.appended),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> ContextPackDelta:
        if not isinstance(raw, (bytes, bytearray)):
            raise ContextPackError("raw must be bytes")
        try:
            payload = json.loads(bytes(raw).decode("utf-8", errors="strict"))
        except Exception as exc:
            raise ContextPackError("Invalid UTF-8 or JSON in ContextPackDelta") from exc
        expected_keys = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        if not isinstance(payload, dict) or set(payload.keys()) != expected_keys:
            raise ContextPackError("ContextPackDelta JSON has missing or extra fields")
        if payload["schema"] != DELTA_SCHEMA:
            raise ContextPackError(f"Unsupported ContextPackDelta schema: {payload['schema']!r}")
        base_digest = _validate_digest_format(payload["base_digest"], "base_digest")
        target_digest = _validate_digest_format(payload["target_digest"], "target_digest")
        prefix_items = payload["prefix_items"]
        if type(prefix_items) is not int or prefix_items < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        appended = _decode_b64_list(payload["appended"], "appended")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate and apply a ContextPackDelta to a base ContextPack."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("base must be a ContextPack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta must be a ContextPackDelta")
    if delta.base_digest != base.digest:
        raise ContextPackError("Delta base_digest does not match base ContextPack digest")
    if type(delta.prefix_items) is not int or delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("Delta prefix_items out of bounds for base ContextPack")
    rebuilt_items = base.items[: delta.prefix_items] + delta.appended
    rebuilt = ContextPack.build(rebuilt_items)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("Reconstructed ContextPack digest does not match delta target_digest")
    return rebuilt


__all__ = [
    "DELTA_SCHEMA",
    "PACK_SCHEMA",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
