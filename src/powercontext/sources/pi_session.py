"""Pi session history branch parser, source definition, and incremental connector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import SourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

_CONTROL_EVENT_TYPES = frozenset({
    "model_change",
    "thinking_level_change",
    "custom",
    "label",
    "session_info",
    "system",
})


def _validate_non_empty_trimmed(name: str, value: object) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not have leading or trailing whitespace")
    return value


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build a collision-free, length-bounded source identity for one Pi event."""

    _validate_non_empty_trimmed("session_id", session_id)
    _validate_non_empty_trimmed("event_id", event_id)
    enc_session = quote(session_id, safe="")
    enc_event = quote(event_id, safe="")
    candidate = f"pi-event:{enc_session}:{enc_event}"
    if len(candidate) <= MAX_SOURCE_ID_LENGTH:
        return candidate
    digest = hashlib.sha256(f"{session_id}\x00{event_id}".encode("utf-8")).hexdigest()
    return f"pi-event:sha256:{digest}"


def _canonical_record_digest(record: dict[str, JsonValue]) -> str:
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PiSessionInput(BaseModel):
    """Offline Pi session JSONL byte payload and optional target branch leaf."""

    model_config = ConfigDict(frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """Normalized visible event extracted from a Pi session branch."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_event(self) -> PiEventInput:
        _validate_non_empty_trimmed("session_id", self.session_id)
        _validate_non_empty_trimmed("event_id", self.event_id)
        if self.parent_id is not None:
            _validate_non_empty_trimmed("parent_id", self.parent_id)
        _validate_non_empty_trimmed("event_type", self.event_type)
        _validate_non_empty_trimmed("role", self.role)
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must be non-empty")
        if not isinstance(self.record, dict):
            raise ValueError("record must be a dict")
        pi_event_source_id(self.session_id, self.event_id)
        return self


class PiEventSource(Source):
    """Captured Source representing one visible Pi session event."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]

    @model_validator(mode="after")
    def _validate_source(self) -> PiEventSource:
        expected_name = pi_event_source_id(self.session_id, self.event_id)
        if self.name != expected_name:
            raise ValueError("PiEventSource name must match pi_event_source_id")
        if self.materialization != SourceMaterialization.CAPTURED:
            raise ValueError("PiEventSource materialization must be CAPTURED")
        return self


def _extract_content_blocks(
    raw_content: object,
    *,
    allow_tool_calls: bool,
) -> str:
    if isinstance(raw_content, str):
        return raw_content if raw_content.strip() else ""
    if not isinstance(raw_content, list):
        raise ValueError("content must be a string or list of content blocks")
    parts: list[str] = []
    for block in raw_content:
        if not isinstance(block, dict):
            raise ValueError("content block must be a JSON object")
        btype = block.get("type")
        if btype == "text":
            text_val = block.get("text")
            if not isinstance(text_val, str):
                raise ValueError("text block must contain string 'text'")
            if text_val.strip():
                parts.append(text_val)
        elif btype == "toolCall" and allow_tool_calls:
            tool_name = _validate_non_empty_trimmed("toolCall.name", block.get("name"))
            call_id = _validate_non_empty_trimmed("toolCall.id", block.get("id"))
            arguments = block.get("arguments")
            if not isinstance(arguments, dict):
                raise ValueError("toolCall block must contain object 'arguments'")
            args_json = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            parts.append(f"[tool_call {tool_name} {call_id}] {args_json}")
        elif btype in {"thinking", "image"}:
            continue
        else:
            raise ValueError(f"Unsupported content block type: {btype!r}")
    return "\n".join(parts)


def _normalize_node_event(
    session_id: str,
    record: dict[str, JsonValue],
) -> PiEventInput | None:
    event_type = _validate_non_empty_trimmed("type", record.get("type"))
    event_id = _validate_non_empty_trimmed("id", record.get("id"))
    raw_parent = record.get("parentId")
    parent_id = None if raw_parent is None else _validate_non_empty_trimmed("parentId", raw_parent)

    if event_type == "message":
        msg = record.get("message")
        if not isinstance(msg, dict):
            raise ValueError("message record must contain object 'message'")
        role = _validate_non_empty_trimmed("message.role", msg.get("role"))
        if role == "system":
            return None
        if role == "bashExecution":
            command = _validate_non_empty_trimmed("message.command", msg.get("command"))
            output = msg.get("output")
            if not isinstance(output, str):
                raise ValueError("bashExecution message must contain string 'output'")
            body = f"$ {command}\n{output}"
        elif role == "toolResult":
            tool_name = _validate_non_empty_trimmed("message.toolName", msg.get("toolName"))
            tool_call_id = _validate_non_empty_trimmed("message.toolCallId", msg.get("toolCallId"))
            text_body = _extract_content_blocks(msg.get("content", ""), allow_tool_calls=False)
            if not text_body.strip():
                return None
            body = f"[tool_result {tool_name} {tool_call_id}] {text_body}"
        else:
            body = _extract_content_blocks(msg.get("content"), allow_tool_calls=True)
            if not body.strip():
                return None
        return PiEventInput(
            session_id=session_id,
            event_id=event_id,
            parent_id=parent_id,
            event_type=event_type,
            role=role,
            content=f"{role}: {body}",
            record=record,
        )

    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str):
            raise ValueError(f"{event_type} record must contain string 'summary'")
        if not summary.strip():
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=event_id,
            parent_id=parent_id,
            event_type=event_type,
            role=event_type,
            content=f"{event_type}: {summary}",
            record=record,
        )

    if event_type == "custom_message":
        body = _extract_content_blocks(record.get("content"), allow_tool_calls=False)
        if not body.strip():
            return None
        return PiEventInput(
            session_id=session_id,
            event_id=event_id,
            parent_id=parent_id,
            event_type=event_type,
            role="custom",
            content=f"custom: {body}",
            record=record,
        )

    if event_type in _CONTROL_EVENT_TYPES:
        return None

    raise ValueError(f"Unsupported Pi session record type: {event_type!r}")


def _parse_pi_session_internal(
    content: bytes,
    leaf_id: str | None,
) -> tuple[str, dict[str, dict[str, JsonValue]], list[str], tuple[PiEventInput, ...]]:
    if not isinstance(content, bytes) or len(content) == 0:
        raise ValueError("Pi session content must be non-empty bytes")
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Pi session content must be valid UTF-8") from exc

    lines = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
    if not lines:
        raise ValueError("Pi session content must not be empty")

    records: list[dict[str, JsonValue]] = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON line in Pi session: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Every Pi session JSONL line must be a JSON object")
        records.append(parsed)

    header = records[0]
    if header.get("type") != "session":
        raise ValueError("First Pi session record must have type 'session'")
    version = header.get("version")
    if type(version) is not int or version != 3:
        raise ValueError("Pi session header version must be integer 3")
    session_id = _validate_non_empty_trimmed("session.id", header.get("id"))

    by_id: dict[str, dict[str, JsonValue]] = {}
    normalized_by_id: dict[str, PiEventInput | None] = {}
    ordered_ids: list[str] = []

    for rec in records[1:]:
        rec_type = rec.get("type")
        if rec_type == "session":
            raise ValueError("Multiple session headers are not allowed")
        node_id = _validate_non_empty_trimmed("record.id", rec.get("id"))
        if node_id in by_id:
            raise ValueError(f"Duplicate Pi session record id: {node_id!r}")
        if "parentId" not in rec:
            raise ValueError(f"Record {node_id!r} is missing 'parentId'")
        parent_id = rec["parentId"]
        if parent_id is not None:
            parent_str = _validate_non_empty_trimmed("record.parentId", parent_id)
            if parent_str == node_id:
                raise ValueError(f"Record {node_id!r} cannot be its own parent")
        by_id[node_id] = rec
        normalized_by_id[node_id] = _normalize_node_event(session_id, rec)
        ordered_ids.append(node_id)

    for node_id in ordered_ids:
        seen_chain: set[str] = set()
        curr: str | None = node_id
        while curr is not None:
            if curr in seen_chain:
                raise ValueError(f"Cycle detected in Pi session graph at {curr!r}")
            seen_chain.add(curr)
            rec = by_id.get(curr)
            if rec is None:
                raise ValueError(f"Missing parent record {curr!r} in Pi session graph")
            parent_val = rec["parentId"]
            curr = None if parent_val is None else str(parent_val)

    if not ordered_ids:
        if leaf_id is not None:
            raise ValueError(f"Requested leaf_id {leaf_id!r} not found in empty session")
        return session_id, by_id, [], ()

    target_leaf = ordered_ids[-1] if leaf_id is None else _validate_non_empty_trimmed("leaf_id", leaf_id)
    if target_leaf not in by_id:
        raise ValueError(f"Requested leaf_id {target_leaf!r} does not exist in Pi session")

    branch_node_ids: list[str] = []
    curr_id: str | None = target_leaf
    while curr_id is not None:
        branch_node_ids.append(curr_id)
        parent_val = by_id[curr_id]["parentId"]
        curr_id = None if parent_val is None else str(parent_val)
    branch_node_ids.reverse()

    events = tuple(
        normalized_by_id[nid]
        for nid in branch_node_ids
        if normalized_by_id[nid] is not None
    )
    return session_id, by_id, branch_node_ids, events  # type: ignore[return-value]


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Atomically parse a Pi v3 JSONL archive and return root-to-leaf visible events."""

    if not isinstance(value, PiSessionInput):
        raise ValueError("value must be a PiSessionInput")
    _, _, _, events = _parse_pi_session_internal(value.content, value.leaf_id)
    return events


class _PiEventTextEvidenceProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type="pi-event",
            source_id=source.name,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class _PiEventSourceDefinition:
    name = "pi-event"
    version = "1"
    input_class = PiEventInput
    source_class = PiEventSource
    projections = (_PiEventTextEvidenceProjection(),)

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        if not isinstance(value, PiEventInput):
            raise ValueError("value must be PiEventInput")
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        if not isinstance(source, PiEventSource):
            raise ValueError("source must be PiEventSource")
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = (
    _PiEventSourceDefinition()
)


class PiSessionConnector:
    """Incremental connector for local Pi v3 session JSONL files."""

    name = "pi-session-history"
    version = "1"
    source_definitions = frozenset({"pi-event"})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        try:
            raw_bytes = self._path.read_bytes()
        except OSError as exc:
            raise ValueError(f"Unable to read Pi session file: {exc}") from exc

        session_id, by_id, branch_node_ids, branch_events = _parse_pi_session_internal(
            raw_bytes, self._leaf_id
        )
        current_digests = {nid: _canonical_record_digest(rec) for nid, rec in by_id.items()}

        prev_checkpoint = session.checkpoint
        submitted_events: list[str] = []
        recorded_digests: dict[str, str] = {}

        if prev_checkpoint is not None:
            if not isinstance(prev_checkpoint, dict) or prev_checkpoint.get("schema_version") != "1":
                raise ValueError("Invalid PiSessionConnector checkpoint format")
            if prev_checkpoint.get("session_id") != session_id:
                raise ValueError("Pi session_id changed across incremental runs")
            raw_submitted = prev_checkpoint.get("submitted_events")
            raw_digests = prev_checkpoint.get("node_digests")
            if not isinstance(raw_submitted, list) or not isinstance(raw_digests, dict):
                raise ValueError("Corrupt PiSessionConnector checkpoint state")
            for nid, expected_digest in raw_digests.items():
                if not isinstance(nid, str) or not isinstance(expected_digest, str):
                    raise ValueError("Corrupt node digest entry in checkpoint")
                actual_digest = current_digests.get(nid)
                if actual_digest != expected_digest:
                    raise ValueError(f"Previously recorded Pi session node {nid!r} was mutated or removed")
                recorded_digests[nid] = expected_digest
            for eid in raw_submitted:
                if not isinstance(eid, str):
                    raise ValueError("Corrupt submitted_events entry in checkpoint")
                submitted_events.append(eid)

        submitted_set = set(submitted_events)
        new_submissions = 0
        for event in branch_events:
            if event.event_id in submitted_set:
                continue
            await session.submit(event.event_id, "pi-event", event)
            submitted_events.append(event.event_id)
            submitted_set.add(event.event_id)
            new_submissions += 1

        for nid in branch_node_ids:
            recorded_digests[nid] = current_digests[nid]

        next_checkpoint: dict[str, JsonValue] = {
            "schema_version": "1",
            "session_id": session_id,
            "submitted_events": list(submitted_events),
            "node_digests": dict(sorted(recorded_digests.items())),
        }
        if prev_checkpoint is not None and new_submissions == 0 and next_checkpoint == prev_checkpoint:
            return ConnectorRunCompletion(
                status=ConnectorRunStatus.COMPLETE,
                checkpoint=prev_checkpoint,
            )
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint=next_checkpoint,
        )


__all__ = [
    "PI_EVENT_SOURCE_DEFINITION",
    "PiEventInput",
    "PiEventSource",
    "PiSessionConnector",
    "PiSessionInput",
    " parse_pi_session".strip(),
    "pi_event_source_id",
]
