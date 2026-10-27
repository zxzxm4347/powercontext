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

"""Source lifecycle DAG propagation for Memory and Topic Memory availability."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryCitation
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.sources import validate_scope_id
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.persistence.database import AsyncDatabase
    from powercontext.builtin.runtime.application import BuiltinRuntime


class SourceLifecycleError(ValueError):
    """Raised when a lifecycle registration, event, or resolution violates the contract."""


class SourceLifecycleEvent(BaseModel):
    """Immutable lifecycle availability event for a registered Source node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    revision: int = Field(gt=0, strict=True)
    sequence: int = Field(gt=0, strict=True)
    available: bool = Field(strict=True)
    reason: str

    @field_validator("node_id", "reason", mode="before")
    @classmethod
    def _validate_non_blank_str(cls, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must be a non-empty, non-whitespace string")
        return value


@dataclass
class _LifecycleNode:
    scope_id: str
    node_id: str
    kind: Literal["source", "memory", "topic"]
    ref_json: str
    revision: int | None
    parents: tuple[str, ...]
    policy: Literal["all", "any"] | None
    available: bool
    last_sequence: int | None
    last_reason: str | None
    ordinal: int

    def reference(self) -> SourceRef | MemoryCitation | ArtifactRef:
        data = json.loads(self.ref_json)
        if self.kind == "source":
            return SourceRef.model_validate(data)
        if self.kind == "memory":
            return MemoryCitation.model_validate(data)
        return ArtifactRef.model_validate(data)

    def target_identity(self) -> tuple[str, str]:
        ref = self.reference()
        if isinstance(ref, SourceRef):
            return ("source", f"{ref.source_type}:{ref.source_id}")
        if isinstance(ref, MemoryCitation):
            return ("memory", ref.entry_id)
        return ("topic", f"{ref.artifact_id}:{ref.revision}")


def _validate_node_id(node_id: object) -> str:
    if not isinstance(node_id, str) or not node_id.strip():
        raise SourceLifecycleError("node_id must be a non-empty, non-whitespace string")
    return node_id


def _validate_parents_and_policy(
    node_id: str,
    parents: object,
    policy: object,
    existing_nodes: dict[str, _LifecycleNode],
) -> tuple[tuple[str, ...], Literal["all", "any"]]:
    if policy not in ("all", "any"):
        raise SourceLifecycleError("policy must be 'all' or 'any'")
    if isinstance(parents, (str, bytes)) or not isinstance(parents, Sequence):
        raise SourceLifecycleError("parents must be a sequence of node_ids")
    parent_tuple = tuple(parents)
    if not parent_tuple:
        raise SourceLifecycleError("derived node must declare at least one parent")
    seen: set[str] = set()
    for parent in parent_tuple:
        _validate_node_id(parent)
        if parent == node_id:
            raise SourceLifecycleError("self-loop is not allowed")
        if parent in seen:
            raise SourceLifecycleError("duplicate parents are not allowed")
        if parent not in existing_nodes:
            raise SourceLifecycleError(f"unknown parent node: {parent}")
        seen.add(parent)
    return parent_tuple, policy


def _evaluate_derived_availability(
    parents: tuple[str, ...],
    policy: Literal["all", "any"],
    nodes: dict[str, _LifecycleNode],
) -> bool:
    parent_states = [nodes[parent].available for parent in parents]
    return all(parent_states) if policy == "all" else any(parent_states)


def _recompute_dag_availability(nodes: dict[str, _LifecycleNode]) -> None:
    ordered = sorted(nodes.values(), key=lambda item: item.ordinal)
    for node in ordered:
        if node.kind == "source":
            continue
        assert node.policy is not None
        node.available = _evaluate_derived_availability(node.parents, node.policy, nodes)


class ScopedSourceLifecycleApplication:
    """Manage source lifecycle DAG and propagate availability in one scope."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    def _database(self) -> AsyncDatabase | None:
        if self._runtime._record_service is not None and hasattr(self._runtime._record_service, "_database"):
            return self._runtime._record_service._database
        if hasattr(self._runtime._provider, "_database"):
            return self._runtime._provider._database
        return None

    async def _load_nodes(self) -> dict[str, _LifecycleNode]:
        database = self._database()
        if database is None:
            return dict(self._runtime.lifecycle._fallback_store.get(self.scope_id, {}))
        async with database.transaction() as connection:
            rows = (
                await connection.execute(
                    select(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id)
                    .order_by(SOURCE_LIFECYCLE_NODES_TABLE.c.ordinal.asc())
                )
            ).mappings().all()
        nodes: dict[str, _LifecycleNode] = {}
        for row in rows:
            node = _LifecycleNode(
                scope_id=row["scope_id"],
                node_id=row["node_id"],
                kind=row["kind"],
                ref_json=row["ref_json"],
                revision=row["revision"],
                parents=tuple(json.loads(row["parents_json"])),
                policy=row["policy"],
                available=bool(row["available"]),
                last_sequence=row["last_sequence"],
                last_reason=row["last_reason"],
                ordinal=int(row["ordinal"]),
            )
            nodes[node.node_id] = node
        return nodes

    async def _insert_node(self, node: _LifecycleNode) -> None:
        database = self._database()
        if database is None:
            self._runtime.lifecycle._fallback_store.setdefault(self.scope_id, {})[node.node_id] = node
            return
        async with database.transaction() as connection:
            await connection.execute(
                SOURCE_LIFECYCLE_NODES_TABLE.insert().values(
                    scope_id=node.scope_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    ref_json=node.ref_json,
                    revision=node.revision,
                    parents_json=json.dumps(list(node.parents)),
                    policy=node.policy,
                    available=node.available,
                    last_sequence=node.last_sequence,
                    last_reason=node.last_reason,
                    ordinal=node.ordinal,
                )
            )

    async def _save_nodes(self, nodes: dict[str, _LifecycleNode]) -> None:
        database = self._database()
        if database is None:
            self._runtime.lifecycle._fallback_store[self.scope_id] = dict(nodes)
            return
        async with database.transaction() as connection:
            for node in nodes.values():
                await connection.execute(
                    update(SOURCE_LIFECYCLE_NODES_TABLE)
                    .where(
                        SOURCE_LIFECYCLE_NODES_TABLE.c.scope_id == self.scope_id,
                        SOURCE_LIFECYCLE_NODES_TABLE.c.node_id == node.node_id,
                    )
                    .values(
                        available=node.available,
                        last_sequence=node.last_sequence,
                        last_reason=node.last_reason,
                    )
                )

    async def _reconcile_runtime_state(self, nodes: dict[str, _LifecycleNode], *, reason: str) -> None:
        memory_nodes = [node for node in nodes.values() if node.kind == "memory"]
        if not memory_nodes:
            return
        async with self._runtime._context(self.scope_id) as context:
            service = context.artifacts.memory
            try:
                current, entries = await service.head_entries(context.artifacts.memory_artifact_id)
            except RepositoryNotFoundError:
                return
            manifest_states = {item.entry_id: item.state for item in current.content.manifest.entries}
            entries_by_id = {entry.entry_id: entry for entry in entries}
            to_forget_ids: list[str] = []
            to_reactivate_ids: list[str] = []
            for node in sorted(memory_nodes, key=lambda item: item.ordinal):
                citation = node.reference()
                assert isinstance(citation, MemoryCitation)
                entry_id = citation.entry_id
                state = manifest_states.get(entry_id)
                if not node.available and state == "active":
                    to_forget_ids.append(entry_id)
                elif node.available and state == "inactive":
                    to_reactivate_ids.append(entry_id)
            if to_forget_ids:
                forget_entries = [entries_by_id[eid] for eid in to_forget_ids if eid in entries_by_id]
                if forget_entries:
                    current = await service.forget(current, entries=forget_entries, reason=reason)
            if to_reactivate_ids:
                if to_forget_ids:
                    current, entries = await service.head_entries(context.artifacts.memory_artifact_id)
                    entries_by_id = {entry.entry_id: entry for entry in entries}
                reactivate_entries = [entries_by_id[eid] for eid in to_reactivate_ids if eid in entries_by_id]
                if reactivate_entries:
                    await service.reactivate(current, entries=reactivate_entries, reason=reason)

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        *,
        revision: int = 1,
    ) -> None:
        _validate_node_id(node_id)
        if type(revision) is not int or revision <= 0:
            raise SourceLifecycleError("revision must be a positive integer")
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source_ref must be a SourceRef")
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type,
                source_ref.source_id,
            )
        except Exception as error:
            raise SourceLifecycleError("source_ref does not exist or cannot be read") from error

        ref_json = json.dumps(source_ref.model_dump(mode="json"), sort_keys=True)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load_nodes()
            existing = nodes.get(node_id)
            if existing is not None:
                if (
                    existing.kind == "source"
                    and existing.ref_json == ref_json
                    and existing.revision == revision
                ):
                    return
                raise SourceLifecycleError(f"conflicting registration for node {node_id}")
            target_id = ("source", f"{source_ref.source_type}:{source_ref.source_id}")
            if any(other.target_identity() == target_id for other in nodes.values()):
                raise SourceLifecycleError("source_ref is already bound to another node")
            node = _LifecycleNode(
                scope_id=self.scope_id,
                node_id=node_id,
                kind="source",
                ref_json=ref_json,
                revision=revision,
                parents=(),
                policy=None,
                available=True,
                last_sequence=None,
                last_reason=None,
                ordinal=len(nodes),
            )
            await self._insert_node(node)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        *,
        parents: Sequence[str],
        policy: Literal["all", "any"] = "all",
    ) -> None:
        _validate_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("citation must be a MemoryCitation")

        ref_json = json.dumps(citation.model_dump(mode="json"), sort_keys=True)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load_nodes()
            existing = nodes.get(node_id)
            parent_tuple, validated_policy = _validate_parents_and_policy(node_id, parents, policy, nodes)
            if existing is not None:
                if (
                    existing.kind == "memory"
                    and existing.ref_json == ref_json
                    and existing.parents == parent_tuple
                    and existing.policy == validated_policy
                ):
                    await self._reconcile_runtime_state(nodes, reason="idempotent memory registration")
                    return
                raise SourceLifecycleError(f"conflicting registration for node {node_id}")

        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:
            raise SourceLifecycleError("citation does not exist or cannot be read") from error

        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load_nodes()
            parent_tuple, validated_policy = _validate_parents_and_policy(node_id, parents, policy, nodes)
            target_id = ("memory", citation.entry_id)
            if any(other.target_identity() == target_id for other in nodes.values()):
                raise SourceLifecycleError("citation is already bound to another node")
            available = _evaluate_derived_availability(parent_tuple, validated_policy, nodes)
            node = _LifecycleNode(
                scope_id=self.scope_id,
                node_id=node_id,
                kind="memory",
                ref_json=ref_json,
                revision=None,
                parents=parent_tuple,
                policy=validated_policy,
                available=available,
                last_sequence=None,
                last_reason=None,
                ordinal=len(nodes),
            )
            nodes[node_id] = node
            await self._insert_node(node)
            await self._reconcile_runtime_state(nodes, reason="memory registration")

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        *,
        parents: Sequence[str],
        policy: Literal["all", "any"] = "all",
    ) -> None:
        _validate_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("artifact_ref must be a topic-memory ArtifactRef")

        ref_json = json.dumps(artifact_ref.model_dump(mode="json"), sort_keys=True)
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load_nodes()
            existing = nodes.get(node_id)
            parent_tuple, validated_policy = _validate_parents_and_policy(node_id, parents, policy, nodes)
            if existing is not None:
                if (
                    existing.kind == "topic"
                    and existing.ref_json == ref_json
                    and existing.parents == parent_tuple
                    and existing.policy == validated_policy
                ):
                    return
                raise SourceLifecycleError(f"conflicting registration for node {node_id}")

        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                "topic-memory",
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except Exception as error:
            raise SourceLifecycleError("topic artifact_ref does not exist or cannot be read") from error

        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load_nodes()
            parent_tuple, validated_policy = _validate_parents_and_policy(node_id, parents, policy, nodes)
            target_id = ("topic", f"{artifact_ref.artifact_id}:{artifact_ref.revision}")
            if any(other.target_identity() == target_id for other in nodes.values()):
                raise SourceLifecycleError("topic artifact_ref is already bound to another node")
            available = _evaluate_derived_availability(parent_tuple, validated_policy, nodes)
            node = _LifecycleNode(
                scope_id=self.scope_id,
                node_id=node_id,
                kind="topic",
                ref_json=ref_json,
                revision=None,
                parents=parent_tuple,
                policy=validated_policy,
                available=available,
                last_sequence=None,
                last_reason=None,
                ordinal=len(nodes),
            )
            nodes[node_id] = node
            await self._insert_node(node)

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event must be a SourceLifecycleEvent")
        async with self._runtime._scope_operation(self.scope_id), self._runtime._locked(self.scope_id):
            nodes = await self._load_nodes()
            target = nodes.get(event.node_id)
            if target is None or target.kind != "source":
                raise SourceLifecycleError(f"unknown source node: {event.node_id}")
            if target.revision != event.revision:
                raise SourceLifecycleError("event revision does not match registered source revision")
            if target.last_sequence is not None:
                if event.sequence < target.last_sequence:
                    raise SourceLifecycleError("stale event sequence")
                if event.sequence == target.last_sequence:
                    if target.available != event.available or target.last_reason != event.reason:
                        raise SourceLifecycleError("conflicting event for identical sequence")
                    await self._reconcile_runtime_state(nodes, reason=event.reason)
                    return sorted(node.node_id for node in nodes.values() if node.available)

            target.available = event.available
            target.last_sequence = event.sequence
            target.last_reason = event.reason
            _recompute_dag_availability(nodes)
            await self._save_nodes(nodes)
            await self._reconcile_runtime_state(nodes, reason=event.reason)
            return sorted(node.node_id for node in nodes.values() if node.available)

    async def resolve(
        self,
        node_id: str,
        *,
        purpose: Literal["generation", "audit"],
    ) -> SourceRef | MemoryCitation | ArtifactRef:
        _validate_node_id(node_id)
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError("purpose must be 'generation' or 'audit'")
        async with self._runtime._scope_operation(self.scope_id):
            nodes = await self._load_nodes()
            node = nodes.get(node_id)
            if node is None:
                raise SourceLifecycleError(f"unknown node: {node_id}")
            if purpose == "generation" and not node.available:
                raise SourceLifecycleError(f"node {node_id} is not available for generation")
            return node.reference()

    async def inactive_topic_artifact_ids(self) -> frozenset[str]:
        nodes = await self._load_nodes()
        inactive: set[str] = set()
        for node in nodes.values():
            if node.kind == "topic" and not node.available:
                ref = node.reference()
                assert isinstance(ref, ArtifactRef)
                inactive.add(ref.artifact_id)
        return frozenset(inactive)


class SourceLifecycleApplication:
    """Select the scoped Source lifecycle application service."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime
        self._fallback_store: dict[str, dict[str, _LifecycleNode]] = {}

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleApplication:
        return ScopedSourceLifecycleApplication(self._runtime, scope_id)
