"""Batching, coalescing, and bounded LRU caching wrapper for EmbeddingModel."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import (
    EmbeddingResult,
    InferenceUsage,
)
from powercontext.builtin.inference.protocols import EmbeddingModel

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory import EmbeddingProfile


@dataclass(slots=True)
class _BatchResolution:
    vector: tuple[float, ...]
    batch_id: int
    usage: InferenceUsage


@dataclass(slots=True)
class _PendingEntry:
    text: str
    future: asyncio.Future[_BatchResolution]


class CoalescingEmbeddingModel:
    """Wraps an EmbeddingModel with request coalescing, batch splitting, and LRU caching."""

    def __init__(
        self,
        base: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 256,
    ) -> None:
        if max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive")
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must be non-negative")
        if cache_size < 0:
            raise ValueError("cache_size must be non-negative")

        self._base = base
        self._max_batch_size = max_batch_size
        self._coalesce_ms = float(coalesce_ms)
        self._cache_size = cache_size

        self._cache: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._inflight: dict[str, _PendingEntry] = {}
        self._pending: list[_PendingEntry] = []
        self._timer_handle: asyncio.Handle | None = None
        self._next_batch_id: int = 0
        self._background_tasks: set[asyncio.Task[None]] = set()

    @property
    def profile(self) -> EmbeddingProfile:
        return self._base.profile

    def _cache_get(self, text: str) -> tuple[float, ...] | None:
        if self._cache_size <= 0:
            return None
        vec = self._cache.get(text)
        if vec is not None:
            self._cache.move_to_end(text)
        return vec

    def _cache_put(self, text: str, vector: tuple[float, ...]) -> None:
        if self._cache_size <= 0:
            return
        if text in self._cache:
            self._cache.move_to_end(text)
            self._cache[text] = vector
            return
        if len(self._cache) >= self._cache_size:
            self._cache.popitem(last=False)
        self._cache[text] = vector

    def _schedule_timer_if_needed(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._pending:
            if self._timer_handle is not None:
                self._timer_handle.cancel()
                self._timer_handle = None
            return
        if self._timer_handle is not None:
            return
        if self._coalesce_ms == 0:
            self._timer_handle = loop.call_soon(self._on_timer)
        else:
            self._timer_handle = loop.call_later(
                self._coalesce_ms / 1000.0, self._on_timer
            )

    def _on_timer(self) -> None:
        self._timer_handle = None
        while self._pending:
            self._flush_chunk(self._max_batch_size)

    def _flush_chunk(self, limit: int) -> None:
        if not self._pending:
            return
        entries = self._pending[:limit]
        del self._pending[:limit]
        self._next_batch_id += 1
        batch_id = self._next_batch_id
        task = asyncio.create_task(self._run_batch(entries, batch_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_batch(self, entries: list[_PendingEntry], batch_id: int) -> None:
        chunk_texts = tuple(entry.text for entry in entries)
        try:
            result = await self._base.embed(chunk_texts)
            if len(result.vectors) != len(entries):
                raise ValueError(
                    f"Embedding vector count mismatch: expected {len(entries)}, got {len(result.vectors)}"
                )
        except BaseException as exc:
            for entry in entries:
                self._inflight.pop(entry.text, None)
                if not entry.future.done():
                    entry.future.set_exception(exc)
            return

        batch_usage = result.usage
        for entry, raw_vec in zip(entries, result.vectors, strict=True):
            vec = tuple(float(v) for v in raw_vec)
            self._cache_put(entry.text, vec)
            self._inflight.pop(entry.text, None)
            if not entry.future.done():
                entry.future.set_result(
                    _BatchResolution(
                        vector=vec,
                        batch_id=batch_id,
                        usage=batch_usage,
                    )
                )

    async def embed(self, texts: Sequence[str], /) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(
                vectors=(),
                usage=InferenceUsage(requests=0, input_tokens=0, output_tokens=0),
            )

        loop = asyncio.get_running_loop()
        unique_texts = list(dict.fromkeys(texts))
        resolved_vectors: dict[str, tuple[float, ...]] = {}
        wait_entries: list[_PendingEntry] = []

        for text in unique_texts:
            cached = self._cache_get(text)
            if cached is not None:
                resolved_vectors[text] = cached
                continue
            entry = self._inflight.get(text)
            if entry is None:
                fut: asyncio.Future[_BatchResolution] = loop.create_future()
                fut.add_done_callback(
                    lambda f: f.exception() if not f.cancelled() else None
                )
                entry = _PendingEntry(text=text, future=fut)
                self._inflight[text] = entry
                self._pending.append(entry)
            wait_entries.append(entry)

        while len(self._pending) >= self._max_batch_size:
            self._flush_chunk(self._max_batch_size)
        self._schedule_timer_if_needed(loop)

        distinct_batches: dict[int, InferenceUsage] = {}
        if wait_entries:
            resolutions = await asyncio.gather(
                *(asyncio.shield(entry.future) for entry in wait_entries)
            )
            for entry, resolution in zip(wait_entries, resolutions, strict=True):
                resolved_vectors[entry.text] = resolution.vector
                distinct_batches[resolution.batch_id] = resolution.usage

        if not distinct_batches:
            usage = InferenceUsage(requests=0, input_tokens=0, output_tokens=0)
        else:
            total_requests = sum(u.requests for u in distinct_batches.values())
            has_input_tokens = any(
                u.input_tokens is not None for u in distinct_batches.values()
            )
            has_output_tokens = any(
                u.output_tokens is not None for u in distinct_batches.values()
            )
            usage = InferenceUsage(
                requests=total_requests,
                input_tokens=(
                    sum(u.input_tokens or 0 for u in distinct_batches.values())
                    if has_input_tokens
                    else None
                ),
                output_tokens=(
                    sum(u.output_tokens or 0 for u in distinct_batches.values())
                    if has_output_tokens
                    else None
                ),
            )

        vectors = tuple(resolved_vectors[t] for t in texts)
        return EmbeddingResult(vectors=vectors, usage=usage)
