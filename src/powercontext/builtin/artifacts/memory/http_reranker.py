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

"""HTTP cross-encoder reranker adapter with batching and global tie-breaking."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

if TYPE_CHECKING:
    import httpx

    from powercontext.builtin.artifacts.memory.models import MemoryHit


class HTTPMemoryReranker:
    """Rerank coarse Memory candidates via a batched HTTP scoring endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = "http-cross-encoder-v1",
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(policy_id, str) or not policy_id.strip():
            raise ValueError("policy_id must be a non-empty string")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score candidates in bounded HTTP batches and return top global ranks."""

        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        candidate_count = len(candidates)
        if candidate_count < 1:
            raise ValueError("rerank requires at least one candidate")
        if type(limit) is not int or limit < 1 or limit > candidate_count:
            raise ValueError("rerank limit must be between one and the candidate count")

        indexed = [(rank, candidate.text) for rank, candidate in enumerate(candidates, start=1)]
        batches = [
            indexed[offset : offset + self._batch_size]
            for offset in range(0, candidate_count, self._batch_size)
        ]

        global_scores: dict[int, float] = {}
        request_count = 0

        for batch in batches:
            payload = {
                "query": query,
                "candidates": [{"rank": rank, "text": text} for rank, text in batch],
            }
            response = await self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
            request_count += 1

            try:
                body = response.json()
            except Exception as exc:
                raise ValueError("Invalid JSON in HTTP reranker response") from exc

            if not isinstance(body, dict) or set(body.keys()) != {"scores"}:
                raise ValueError("HTTP reranker response must contain only 'scores'")

            scores_list = body["scores"]
            if not isinstance(scores_list, list) or len(scores_list) != len(batch):
                raise ValueError("HTTP reranker 'scores' must be a list matching batch size")

            expected_ranks = {rank for rank, _ in batch}
            seen_ranks: set[int] = set()

            for item in scores_list:
                if not isinstance(item, dict) or set(item.keys()) != {"rank", "score"}:
                    raise ValueError("Each score entry must contain only 'rank' and 'score'")
                rank = item["rank"]
                score = item["score"]
                if type(rank) is not int or rank not in expected_ranks or rank in seen_ranks:
                    raise ValueError(f"Invalid or duplicate rank in batch response: {rank!r}")
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError(f"Score must be a finite number, got {score!r}")
                seen_ranks.add(rank)
                global_scores[rank] = float(score)

            if seen_ranks != expected_ranks:
                raise ValueError("Batch response did not cover all expected ranks")

        ordered_ranks = sorted(
            range(1, candidate_count + 1),
            key=lambda r: (-global_scores[r], r),
        )
        selected_ranks = tuple(ordered_ranks[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected_ranks,
            usage=InferenceUsage(requests=request_count),
        )


__all__ = ["HTTPMemoryReranker"]
