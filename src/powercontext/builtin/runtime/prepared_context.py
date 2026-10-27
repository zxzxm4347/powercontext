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

"""Prepare final, bounded context from approved Artifact evidence."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import TypeVar

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import Experience, ExperienceSearchHit, render_experience
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.profile.models import Profile
from powercontext.builtin.artifacts.search import analyze_text
from powercontext.builtin.artifacts.topic_memory import TopicMemory, TopicMemorySearchHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_code import (
    CodeEvidenceRef,
    PreparedCodeCandidate,
    assemble_code,
)
from powercontext.builtin.runtime.prepared_text import (
    TRUST_POLICY,
    ContextTextItem,
    fit_context_text_item,
    render_context_text,
)
from powercontext.builtin.runtime.recall_sufficiency import RecallBudgetView

_MIN_TRUNCATED_CONTENT_BYTES = 64
_ELLIPSIS = "…"
_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class _PreparedContextEntry:
    origin: PreparedContextOrigin
    kind: str
    citation: dict[str, object]
    content: str
    truncated: bool


@dataclass(frozen=True)
class _EntryFit:
    """One fit attempt on the non-assembly path: the entry that fitted, or why none did.

    The two drop reasons are disjoint and mirror the assembly path's
    :class:`~powercontext.builtin.runtime.prepared_text.FitOutcome`. An entry is dropped
    *below the minimum truncated content* when a shorter rendering exists and fits but is too
    short to be an honest delivery; it is dropped *with no fitting truncation* when no
    shortened rendering fits at all.
    """

    entry: _PreparedContextEntry | None = None
    dropped_below_min_bytes: bool = False
    dropped_no_fitting_truncation: bool = False


@dataclass(frozen=True)
class PreparedContextOmissions:
    """Aggregate counts of the items the byte budget omitted in one build.

    Every count is a per-call aggregate, never per-entry attribution and never a verdict about
    an entry: an entry losing to the budget is not a negative result about that entry.

    ``dropped_items`` is the sum of its two sub-counts, so "the budget could not fit this
    item" can be told apart from "this item was too short to truncate into the remaining
    space". A single merged counter would misreport the second cause as the first.
    """

    truncated_items: int = 0
    dropped_items: int = 0
    dropped_below_min_bytes: int = 0
    dropped_no_fitting_truncation: int = 0


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


@dataclass(frozen=True)
class PreparedEvidenceFragment:
    """One literal UTF-8 slice selected from a Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class PreparedContextBuild:
    """Final public context and the exact origins selected to produce it.

    ``omissions`` is always filled by the Builder. The RFC 1560 recall trace is deliberately
    **not** a field here: ``ScopedContextApplication._prepare`` returns ``build.context`` and
    discards the rest, so such a field would have no production observer. The trace is
    delivered through the Runtime's optional ``RecallEffortSink`` instead.
    """

    context: PreparedContext
    origins: tuple[PreparedContextOrigin, ...]
    omissions: PreparedContextOmissions = PreparedContextOmissions()
    code_origins: tuple[CodeEvidenceRef, ...] = ()
    fragments: tuple[PreparedEvidenceFragment, ...] = ()


PreparedContextOrigin = MemoryCitation | ArtifactRef | MemoryEntryAddress | ArtifactAddress


@dataclass(frozen=True)
class PreparedMemoryCandidates:
    """Memory candidates read from one Scope."""

    scope_id: str
    memory_ref: ArtifactRef | None = None
    hits: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class PreparedExperienceCandidates:
    """Experience candidates read from one Scope."""

    scope_id: str
    hits: tuple[ExperienceSearchHit, ...] = ()


@dataclass(frozen=True)
class PreparedProfileCandidate:
    """The latest committed Profile snapshot read from one Scope."""

    scope_id: str
    profile: Profile


class PreparedContextBuilder:
    """Select and render final context without I/O, persistence, or reranking."""

    memory_candidate_limit = 16
    topic_memory_candidate_limit = 8
    experience_candidate_limit = 8
    candidate_limit = memory_candidate_limit
    entry_limit = 8
    topic_memory_entry_limit = 8
    experience_entry_limit = 2
    max_entry_content_bytes = 2000
    _text_entry_limit: int | None = None

    def empty(self) -> PreparedContext:
        return PreparedContext(status="empty", content=None, content_bytes=0)

    def build(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContext:
        return self.build_result(
            request=request,
            scope_id=scope_id,
            memory_ref=memory_ref,
            hits=hits,
            topic_memory_hits=topic_memory_hits,
            experience_hits=experience_hits,
        ).context

    def build_result(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContextBuild:
        return self.build_scopes_result(
            request=request,
            current_scope_id=scope_id,
            memory_candidates=(
                PreparedMemoryCandidates(scope_id=scope_id or "", memory_ref=memory_ref, hits=tuple(hits)),
            ),
            experience_candidates=(PreparedExperienceCandidates(scope_id=scope_id or "", hits=tuple(experience_hits)),),
            topic_memory_hits=topic_memory_hits,
        )

    def build_scopes_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
        code_candidates: Sequence[PreparedCodeCandidate] = (),
    ) -> PreparedContextBuild:
        if request.include_code:

            def historical(history_request: PrepareContextRequest, entries: int) -> PreparedContextBuild:
                builder = PreparedContextBuilder()
                builder.entry_limit = entries
                builder._text_entry_limit = entries if entries < self.entry_limit else None
                return builder.build_scopes_result(
                    request=history_request.model_copy(update={"include_code": False}),
                    current_scope_id=current_scope_id,
                    memory_candidates=memory_candidates,
                    topic_memory_hits=topic_memory_hits,
                    experience_candidates=experience_candidates,
                    profile_candidates=profile_candidates,
                )

            return assemble_code(request, code_candidates, self.entry_limit, historical)

        if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
            raise PreparedContextInvariantError("memory-candidate-limit")
        if len(topic_memory_hits) > self.topic_memory_candidate_limit:
            raise PreparedContextInvariantError("topic-memory-candidate-limit")
        if sum(len(candidates.hits) for candidates in experience_candidates) > self.experience_candidate_limit:
            raise PreparedContextInvariantError("experience-candidate-limit")

        if request.evidence_selection:
            content, origins, fragments, omissions = self._select_evidence(
                request=request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
            )
            if content is None:
                return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions, fragments=())
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > request.max_bytes:
                raise PreparedContextInvariantError("output-budget")
            return PreparedContextBuild(
                context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
                origins=origins,
                omissions=omissions,
                fragments=fragments,
            )

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        if content is None:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions)
        content_bytes = len(content.encode("utf-8"))
        # The assembly path is bounded by construction (every fitted item is measured against
        # `max_bytes`), so the ceiling check belongs to the non-assembly renderer only.
        if request.assembly is None and content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=origins,
            omissions=omissions,
        )

    def probe_budget(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> RecallBudgetView:
        """Report what the byte budget does to one candidate set, without delivering it.

        This is the RFC 1560 *budget probe*: one pass of this Builder's own pure selection and
        rendering code, keeping only the counters and discarding the rendered output. It lets
        the gate tell budget-limited thinness from recall-limited thinness.

        Pure and side-effect free: no I/O, no persistence, nothing reported to any sink, and
        the same inputs as :meth:`build_scopes_result` are guaranteed to yield the same
        counters because both go through :meth:`_select_entries`.
        """

        if request.evidence_selection:
            content, origins, _, omissions = self._select_evidence(
                request=request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
            )
        else:
            content, origins, omissions = self._select_entries(
                request=request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )
        content_bytes = 0 if content is None else len(content.encode("utf-8"))
        return RecallBudgetView(
            max_bytes=request.max_bytes,
            delivered_items=len(origins),
            truncated_items=omissions.truncated_items,
            dropped_items=omissions.dropped_items,
            unused_bytes=max(0, request.max_bytes - content_bytes),
        )

    def _select_evidence(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
    ) -> tuple[
        str | None,
        tuple[PreparedContextOrigin, ...],
        tuple[PreparedEvidenceFragment, ...],
        PreparedContextOmissions,
    ]:
        seen_entries: set[tuple[str, str, int, str, str]] = set()
        groups: list[list[tuple[MemoryEntryAddress, str]]] = []
        for cand_group in memory_candidates:
            if cand_group.hits and cand_group.memory_ref is None:
                raise PreparedContextInvariantError("memory-ref-missing")
            scope_id = cand_group.scope_id or current_scope_id or ""
            group_items: list[tuple[MemoryEntryAddress, str]] = []
            seen_in_group: set[tuple[str, str]] = set()
            for hit in cand_group.hits:
                if hit.memory_ref != cand_group.memory_ref:
                    raise PreparedContextInvariantError("memory-ref-mismatch")
                ckey = (hit.entry_id, hit.entry_version_id)
                if ckey in seen_in_group:
                    continue
                seen_in_group.add(ckey)
                if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                    continue
                gkey = (
                    scope_id,
                    hit.memory_ref.artifact_id,
                    hit.memory_ref.revision,
                    hit.entry_id,
                    hit.entry_version_id,
                )
                if gkey in seen_entries:
                    continue
                seen_entries.add(gkey)
                addr = MemoryEntryAddress(
                    memory=ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref),
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                group_items.append((addr, hit.text))
            if group_items:
                groups.append(group_items)

        interleaved = _interleave_groups(groups)
        if not interleaved:
            return None, (), (), PreparedContextOmissions()

        max_bytes = request.max_bytes
        max_items = self.entry_limit
        q_exact, q_stems, q_prefixes, q_bigrams = _extract_evidence_features(request.query)

        doc_entries: list[tuple[int, MemoryEntryAddress, str, list[tuple[int, int, str]]]] = []
        for doc_rank, (addr, full_text) in enumerate(interleaved):
            sents = _segment_evidence_sentences(full_text)
            if sents:
                doc_entries.append((doc_rank, addr, full_text, sents))

        if not doc_entries:
            return None, (), (), PreparedContextOmissions()

        all_sents: list[dict[str, object]] = []
        df_exact: dict[str, int] = {}
        df_stem: dict[str, int] = {}
        total_sent_count = 0

        for doc_rank, addr, full_text, sents in doc_entries:
            norm_counts: dict[str, int] = {}
            for _, _, s_text in sents:
                norm = " ".join(s_text.casefold().split())
                norm_counts[norm] = norm_counts.get(norm, 0) + 1
            seen_norms: set[str] = set()
            for sent_idx, (s_start, s_end, s_text) in enumerate(sents):
                total_sent_count += 1
                s_exact, s_stems, s_prefixes, s_bigrams = _extract_evidence_features(s_text)
                for token in s_exact & q_exact:
                    df_exact[token] = df_exact.get(token, 0) + 1
                for token in s_stems & q_stems:
                    df_stem[token] = df_stem.get(token, 0) + 1
                norm = " ".join(s_text.casefold().split())
                is_duplicate_occ = norm in seen_norms
                seen_norms.add(norm)
                is_boilerplate = (
                    norm_counts[norm] >= 2 and len(s_exact & q_exact) == 0 and len(s_stems & q_stems) == 0
                )
                all_sents.append({
                    "doc_rank": doc_rank,
                    "address": addr,
                    "full_text": full_text,
                    "sent_idx": sent_idx,
                    "num_sents": len(sents),
                    "start": s_start,
                    "end": s_end,
                    "text": s_text,
                    "exact": s_exact,
                    "stems": s_stems,
                    "prefixes": s_prefixes,
                    "bigrams": s_bigrams,
                    "is_repeated": is_boilerplate or is_duplicate_occ,
                    "direct_score": 0.0,
                    "total_score": 0.0,
                })

        for info in all_sents:
            if bool(info["is_repeated"]):
                info["direct_score"] = 0.0
                continue
            s_exact = info["exact"]
            s_stems = info["stems"]
            s_prefixes = info["prefixes"]
            s_bigrams = info["bigrams"]
            assert isinstance(s_exact, set)
            assert isinstance(s_stems, set)
            assert isinstance(s_prefixes, set)
            assert isinstance(s_bigrams, set)
            exact_hits = s_exact & q_exact
            stem_only_hits = (s_stems & q_stems) - {_stem_evidence_token(t) for t in exact_hits}
            prefix_only_hits = (
                (s_prefixes & q_prefixes)
                - {t[:5] for t in exact_hits if len(t) >= 5}
                - {t[:5] for t in stem_only_hits if len(t) >= 5}
            )
            bigram_hits = s_bigrams & q_bigrams

            idf_exact = sum(
                math.log(1.0 + (total_sent_count + 1.0) / (df_exact.get(t, 0) + 1.0)) for t in exact_hits
            )
            idf_stem = sum(
                0.75 * math.log(1.0 + (total_sent_count + 1.0) / (df_stem.get(t, 0) + 1.0))
                for t in stem_only_hits
            )
            idf_prefix = 0.35 * len(prefix_only_hits)
            bg_score = 1.5 * len(bigram_hits)

            coverage = (
                len(exact_hits) + 0.75 * len(stem_only_hits) + 0.35 * len(prefix_only_hits)
            ) / max(1, len(q_exact))
            info["direct_score"] = idf_exact + idf_stem + idf_prefix + bg_score + 2.5 * coverage

        by_doc: dict[int, list[dict[str, object]]] = {}
        for info in all_sents:
            doc_rank = int(info["doc_rank"])  # type: ignore[arg-type]
            by_doc.setdefault(doc_rank, []).append(info)

        for doc_rank, s_list in by_doc.items():
            doc_max_direct = max((float(s["direct_score"]) for s in s_list), default=0.0)  # type: ignore[arg-type]
            doc_prior = 1.0 if doc_rank == 0 else (0.72 if doc_rank == 1 else 0.5 * (0.85 ** (doc_rank - 1)))
            for idx, s in enumerate(s_list):
                if bool(s["is_repeated"]):
                    s["total_score"] = -1.0
                    continue
                neighbor_boost = 0.0
                if idx > 0 and float(s_list[idx - 1]["direct_score"]) > 0:  # type: ignore[arg-type]
                    neighbor_boost = max(neighbor_boost, 0.28 * float(s_list[idx - 1]["direct_score"]))  # type: ignore[arg-type]
                if idx + 1 < len(s_list) and float(s_list[idx + 1]["direct_score"]) > 0:  # type: ignore[arg-type]
                    neighbor_boost = max(neighbor_boost, 0.22 * float(s_list[idx + 1]["direct_score"]))  # type: ignore[arg-type]
                discourse_matches = len(_EVIDENCE_DISCOURSE_RE.findall(str(s["text"])))
                discourse_boost = min(1.2, 0.35 * discourse_matches)
                pos_boost = 0.0
                if idx == len(s_list) - 1 and len(s_list) > 2:
                    pos_boost = 0.35
                elif idx > 0:
                    pos_boost = 0.15
                direct_val = float(s["direct_score"])  # type: ignore[arg-type]
                if direct_val > 0:
                    s["total_score"] = (direct_val + neighbor_boost + 0.5 * discourse_boost + pos_boost) * (
                        0.65 + 0.35 * doc_prior
                    )
                elif doc_max_direct > 0 and doc_rank <= 1:
                    s["total_score"] = (0.45 * neighbor_boost + 0.45 * discourse_boost + 0.25 * pos_boost) * doc_prior
                else:
                    s["total_score"] = 0.05 * doc_prior

        def build_fragments_from_selected(
            selected_set: set[tuple[int, int]],
            *,
            bridge_gaps: bool,
        ) -> list[PreparedEvidenceFragment]:
            selected_by_doc: dict[int, list[int]] = {}
            for d_rank, s_idx in selected_set:
                selected_by_doc.setdefault(d_rank, []).append(s_idx)
            frags: list[tuple[float, int, int, PreparedEvidenceFragment]] = []
            for d_rank, indices in selected_by_doc.items():
                indices.sort()
                s_list = by_doc[d_rank]
                full_text = str(s_list[0]["full_text"])
                address = s_list[0]["address"]
                assert isinstance(address, MemoryEntryAddress)
                if bridge_gaps and len(indices) >= 2:
                    bridged = [indices[0]]
                    for nxt in indices[1:]:
                        prev = bridged[-1]
                        if nxt == prev + 2:
                            mid_s = s_list[prev + 1]
                            mid_bytes = len(str(mid_s["text"]).encode("utf-8"))
                            if (
                                not bool(mid_s["is_repeated"])
                                and mid_bytes <= 260
                                and (len(s_list) > 3 or len(indices) + 1 < len(s_list))
                            ):
                                bridged.append(prev + 1)
                        bridged.append(nxt)
                    indices = bridged
                groups_idx: list[list[int]] = []
                for idx in indices:
                    if groups_idx and idx == groups_idx[-1][-1] + 1:
                        groups_idx[-1].append(idx)
                    else:
                        groups_idx.append([idx])
                for group in groups_idx:
                    g_start = int(s_list[group[0]]["start"])  # type: ignore[arg-type]
                    g_end = int(s_list[group[-1]]["end"])  # type: ignore[arg-type]
                    g_text = full_text[g_start:g_end]
                    g_score = max(float(s_list[i]["total_score"]) for i in group)  # type: ignore[arg-type]
                    frag = PreparedEvidenceFragment(
                        address=address,
                        start=g_start,
                        end=g_end,
                        text=g_text,
                    )
                    frags.append((g_score, d_rank, g_start, frag))
            frags.sort(key=lambda item: (-item[0], item[1], item[2]))
            return [item[3] for item in frags[:max_items]]

        ranked_candidates = [
            s for s in all_sents if not bool(s["is_repeated"]) and float(s["total_score"]) > 0  # type: ignore[arg-type]
        ]
        ranked_candidates.sort(
            key=lambda s: (-float(s["total_score"]), int(s["doc_rank"]), int(s["sent_idx"]))  # type: ignore[arg-type]
        )
        if not ranked_candidates:
            ranked_candidates = sorted(
                all_sents,
                key=lambda s: (int(s["doc_rank"]), int(s["sent_idx"])),  # type: ignore[arg-type]
            )

        direct_candidates = [s for s in ranked_candidates if float(s["direct_score"]) > 0]  # type: ignore[arg-type]
        context_candidates = [s for s in ranked_candidates if float(s["direct_score"]) == 0]  # type: ignore[arg-type]

        selected_keys: set[tuple[int, int]] = set()
        best_fragments: list[PreparedEvidenceFragment] = []
        best_rendered: str | None = None

        def try_commit(candidate_keys: set[tuple[int, int]]) -> bool:
            nonlocal best_fragments, best_rendered
            for bridge in (True, False):
                frags = build_fragments_from_selected(candidate_keys, bridge_gaps=bridge)
                if not frags or len(frags) > max_items:
                    continue
                rendered = _render_evidence_fragments(frags)
                if len(rendered.encode("utf-8")) <= max_bytes:
                    best_fragments = frags
                    best_rendered = rendered
                    return True
            return False

        for cand in direct_candidates:
            key = (int(cand["doc_rank"]), int(cand["sent_idx"]))  # type: ignore[arg-type]
            trial = selected_keys | {key}
            if try_commit(trial):
                selected_keys = trial

        if selected_keys:
            top_doc_rank = min(d for d, _ in selected_keys)
            for cand in context_candidates:
                d_rank = int(cand["doc_rank"])  # type: ignore[arg-type]
                if d_rank != top_doc_rank:
                    continue
                num_in_doc = sum(1 for d, _ in selected_keys if d == d_rank)
                num_sents = int(cand["num_sents"])  # type: ignore[arg-type]
                if num_sents >= 2 and num_in_doc >= num_sents - 1:
                    continue
                key = (d_rank, int(cand["sent_idx"]))  # type: ignore[arg-type]
                trial = selected_keys | {key}
                if try_commit(trial):
                    selected_keys = trial

        truncated_items = 0
        if not best_fragments and ranked_candidates:
            top = ranked_candidates[0]
            top_addr = top["address"]
            assert isinstance(top_addr, MemoryEntryAddress)
            base_frag = PreparedEvidenceFragment(
                address=top_addr,
                start=0,
                end=1,
                text="a",
            )
            one_char_bytes = len(_render_evidence_fragments((base_frag,)).encode("utf-8"))
            avail_text_bytes = max_bytes - (one_char_bytes - 1)
            top_start = int(top["start"])  # type: ignore[arg-type]
            top_end = int(top["end"])  # type: ignore[arg-type]
            win = _window_evidence_sentence(
                str(top["full_text"]),
                top_start,
                top_end,
                avail_text_bytes,
                q_exact,
                q_stems,
            )
            if win is not None:
                w_start, w_end, w_text = win
                frag = PreparedEvidenceFragment(
                    address=top_addr,
                    start=w_start,
                    end=w_end,
                    text=w_text,
                )
                rendered = _render_evidence_fragments((frag,))
                if len(rendered.encode("utf-8")) <= max_bytes:
                    best_fragments = [frag]
                    best_rendered = rendered
                    if w_end - w_start < top_end - top_start:
                        truncated_items = 1

        if not best_fragments or best_rendered is None:
            omissions = PreparedContextOmissions(
                dropped_items=len(doc_entries),
                dropped_no_fitting_truncation=len(doc_entries),
            )
            return None, (), (), omissions

        selected_docs = {_evidence_address_key(frag.address) for frag in best_fragments}
        dropped = max(0, len(doc_entries) - len(selected_docs))
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped,
            dropped_no_fitting_truncation=dropped,
        )
        origins: tuple[PreparedContextOrigin, ...] = tuple(frag.address for frag in best_fragments)
        return best_rendered, origins, tuple(best_fragments), omissions

    def _select_entries(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        """Run one complete pure selection pass and render it.

        Returns ``(content, origins, omissions)``; ``content`` is ``None`` exactly when nothing
        was selected, which is the empty-result case for both paths. This is the single
        selection path shared by :meth:`build_scopes_result` and :meth:`probe_budget`, so the
        probe's counters always describe the selection the build would actually perform.
        """

        if request.assembly is not None:
            return self._select_text(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )

        memory_entries = _interleave_groups(
            tuple(
                self._memory_entries(
                    candidates.memory_ref,
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in memory_candidates
            )
        )
        topic_memory_entries = self._topic_memory_entries(topic_memory_hits)
        experience_entries = _interleave_groups(
            tuple(
                self._experience_entries(
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in experience_candidates
            )
        )[: self.experience_entry_limit]
        entries, omissions = self._fit_entries(request, memory_entries, topic_memory_entries, experience_entries)
        if not entries:
            return None, (), omissions
        return _render(entries), tuple(entry.origin for entry in entries), omissions

    def _select_text(
        self,
        request: PrepareContextRequest,
        *,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        assembly = request.assembly
        if assembly is None:
            raise PreparedContextInvariantError("text-assembly-missing")
        included: list[ContextTextItem] = []
        origins: list[PreparedContextOrigin] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for section in assembly.sections:
            # Unfilled earlier sections leave their capacity available to later ones.
            limit = (
                section.limit
                if self._text_entry_limit is None
                else min(section.limit, self._text_entry_limit - len(included))
            )
            if section.family == "profile":
                entries = self._profile_entries(profile_candidates)
            elif section.family == "topic-memory":
                entries = self._topic_memory_entries(topic_memory_hits, scope_id=current_scope_id)
            else:
                groups = (
                    tuple(
                        tuple(
                            self._memory_entries(group.memory_ref, (hit,), scope_id=group.scope_id)
                            for hit in group.hits
                        )
                        for group in memory_candidates
                    )
                    if section.family == "memory"
                    else tuple(
                        tuple(self._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                        for group in experience_candidates
                    )
                )
                entries = tuple(chain.from_iterable(_interleave_groups(groups)))
            seen: set[tuple[str, str, str, int, str | None, str | None]] = set()
            rank = 0
            selected_count = 0
            for entry in entries:
                if selected_count >= limit:
                    break
                item = _text_item(entry)
                artifact = item.artifact
                identity = (
                    artifact.scope_id,
                    artifact.artifact.family,
                    artifact.artifact.artifact_id,
                    artifact.artifact.revision,
                    item.entry_id,
                    item.entry_version_id,
                )
                if identity in seen or not item.content.strip():
                    continue
                seen.add(identity)
                rank += 1
                outcome = fit_context_text_item(included, replace(item, recall_rank=rank), assembly, request.max_bytes)
                if outcome.item is None:
                    dropped_below_min_bytes += int(outcome.dropped_below_min_bytes)
                    dropped_no_fitting_truncation += int(outcome.dropped_no_fitting_truncation)
                    continue
                fitted = outcome.item
                included.append(fitted)
                origins.append(entry.origin)
                selected_count += 1
                truncated_items += int(fitted.truncated)
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )
        if not included:
            return None, (), omissions
        content = render_context_text(included, assembly)
        return content, tuple(origins), omissions

    def _profile_entries(self, candidates: Sequence[PreparedProfileCandidate]) -> tuple[_PreparedContextEntry, ...]:
        entries = []
        for candidate in candidates:
            origin = ArtifactAddress(scope_id=candidate.scope_id, artifact=candidate.profile.as_ref())
            entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="profile",
                    citation={"artifact": origin.model_dump(mode="json")},
                    content=candidate.profile.content.content,
                    truncated=False,
                )
            )
        return tuple(entries)

    def _memory_entries(
        self,
        memory_ref: ArtifactRef | None,
        hits: Sequence[MemoryHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        if hits and memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        memory_entries: list[_PreparedContextEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.memory_ref != memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")

            citation_key = (hit.entry_id, hit.entry_version_id)
            if citation_key in seen:
                continue
            seen.add(citation_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            origin: PreparedContextOrigin = citation
            rendered_citation = citation.model_dump(mode="json")
            if scope_id is not None:
                memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
                origin = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                rendered_citation = {
                    "memory": memory.model_dump(mode="json"),
                    "entry_id": hit.entry_id,
                    "entry_version_id": hit.entry_version_id,
                }
            memory_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="memory",
                    citation=rendered_citation,
                    content=hit.text,
                    truncated=False,
                )
            )
        return tuple(memory_entries)

    def _topic_memory_entries(
        self,
        hits: Sequence[TopicMemorySearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        topic_entries: list[_PreparedContextEntry] = []
        seen_topics: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != TopicMemory.family:
                raise PreparedContextInvariantError("topic-memory-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_topics:
                continue
            seen_topics.add(identity)
            if len(topic_entries) >= self.topic_memory_entry_limit:
                break
            content = {"title": hit.title, "summary": hit.summary}
            if hit.snippet is not None:
                content["snippet"] = hit.snippet
            origin: PreparedContextOrigin = hit.artifact_ref
            citation = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                citation = {"artifact": origin.model_dump(mode="json")}
                body = "\n\n".join(f"{label.capitalize()}: {value}" for label, value in content.items())
            topic_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="topic-memory",
                    citation=citation,
                    content=body,
                    truncated=False,
                )
            )
        return tuple(topic_entries)

    def _experience_entries(
        self,
        hits: Sequence[ExperienceSearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        experience_entries: list[_PreparedContextEntry] = []
        seen_experiences: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != Experience.family:
                raise PreparedContextInvariantError("experience-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_experiences:
                continue
            seen_experiences.add(identity)
            origin: PreparedContextOrigin = hit.artifact_ref
            rendered_citation: dict[str, object] = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                rendered_citation = {"artifact": origin.model_dump(mode="json")}
            experience_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="experience",
                    citation=rendered_citation,
                    content=render_experience(hit.content),
                    truncated=False,
                )
            )
        return tuple(experience_entries)

    def _fit_entries(
        self,
        request: PrepareContextRequest,
        memory_entries: Sequence[_PreparedContextEntry],
        topic_memory_entries: Sequence[_PreparedContextEntry],
        experience_entries: Sequence[_PreparedContextEntry],
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for candidate in _interleave(memory_entries, topic_memory_entries, experience_entries):
            if len(entries) >= self.entry_limit:
                break
            fit = self._fit_entry(
                entries,
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                text=candidate.content,
                max_bytes=request.max_bytes,
            )
            if fit.entry is None:
                dropped_below_min_bytes += int(fit.dropped_below_min_bytes)
                dropped_no_fitting_truncation += int(fit.dropped_no_fitting_truncation)
                continue
            entries.append(fit.entry)
            if fit.entry.truncated:
                truncated_items += 1
        return tuple(entries), PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )

    def _fit_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        *,
        origin: PreparedContextOrigin,
        kind: str,
        citation: dict[str, object],
        text: str,
        max_bytes: int,
    ) -> _EntryFit:
        """Fit one entry into the remaining budget, reporting why it was dropped if it was.

        The two ``None`` paths of the original code are kept apart: ``below-min-bytes`` means a
        shorter rendering exists and fits but is too short to deliver honestly, while
        ``no-fitting-truncation`` means no shortened rendering fitted at all.
        """

        source_bytes = len(text.encode("utf-8"))
        entry_budget = min(source_bytes, self.max_entry_content_bytes)
        candidate = _PreparedContextEntry(
            origin=origin,
            kind=kind,
            citation=citation,
            content=text if source_bytes <= entry_budget else _truncate_utf8(text, entry_budget),
            truncated=source_bytes > entry_budget,
        )
        if _rendered_bytes((*entries, candidate)) <= max_bytes:
            return _EntryFit(entry=candidate)
        if source_bytes < _MIN_TRUNCATED_CONTENT_BYTES:
            return _EntryFit(dropped_below_min_bytes=True)

        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(entry_budget, source_bytes - 1)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            byte_budget = (lower + upper) // 2
            candidate = _PreparedContextEntry(
                origin=origin,
                kind=kind,
                citation=citation,
                content=_truncate_utf8(text, byte_budget),
                truncated=True,
            )
            if len(candidate.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
                lower = byte_budget + 1
                continue
            if _rendered_bytes((*entries, candidate)) <= max_bytes:
                best = candidate
                lower = byte_budget + 1
            else:
                upper = byte_budget - 1
        if best is None:
            return _EntryFit(dropped_no_fitting_truncation=True)
        return _EntryFit(entry=best)


def _interleave(
    memory: Sequence[_PreparedContextEntry],
    topics: Sequence[_PreparedContextEntry],
    experiences: Sequence[_PreparedContextEntry],
) -> tuple[_PreparedContextEntry, ...]:
    """Preserve each family's rank while alternating Memory, Topic, Experience."""

    ordered: list[_PreparedContextEntry] = []
    for index in range(max(len(memory), len(topics), len(experiences))):
        if index < len(memory):
            ordered.append(memory[index])
        if index < len(topics):
            ordered.append(topics[index])
        if index < len(experiences):
            ordered.append(experiences[index])
    return tuple(ordered)


def _interleave_groups(groups: Sequence[Sequence[_Item]]) -> tuple[_Item, ...]:
    ordered: list[_Item] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return tuple(ordered)


def _render(entries: Sequence[_PreparedContextEntry]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [_render_entry(entry) for entry in entries],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _text_item(entry: _PreparedContextEntry) -> ContextTextItem:
    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return ContextTextItem(
            artifact=origin.memory,
            content=entry.content,
            recall_rank=0,
            entry_id=origin.entry_id,
            entry_version_id=origin.entry_version_id,
        )
    if isinstance(origin, ArtifactAddress):
        return ContextTextItem(artifact=origin, content=entry.content, recall_rank=0)
    raise PreparedContextInvariantError("text-scope-missing")


def _render_entry(entry: _PreparedContextEntry) -> dict[str, object]:
    rendered: dict[str, object] = {
        "citation": entry.citation,
        "content": entry.content,
        "truncated": entry.truncated,
    }
    if entry.kind != "memory":
        rendered["kind"] = entry.kind
    return rendered


def _rendered_bytes(entries: Sequence[_PreparedContextEntry]) -> int:
    return len(_render(entries).encode("utf-8"))


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"


_EVIDENCE_WORD_RE = re.compile(r"[a-z]+|\d+")
_EVIDENCE_ABBREV_WORDS = frozenset({
    "al", "fig", "figs", "eq", "eqs", "dr", "mr", "mrs", "ms", "prof",
    "inc", "ltd", "jr", "sr", "vs", "etc", "no", "nos", "vol", "vols",
    "pp", "approx", "ca", "cf", "st", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
_EVIDENCE_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "had", "has", "have", "in", "into", "is", "it", "its",
    "may", "might", "of", "on", "or", "our", "s", "such", "than", "that",
    "the", "their", "them", "there", "these", "they", "this", "those", "to",
    "was", "we", "were", "what", "when", "where", "which", "who", "will",
    "with", "would",
})
_EVIDENCE_DISCOURSE_RE = re.compile(
    r"\b(?:results?\b|show(?:s|ed|n)?\b|demonstrat\w+|indicat\w+|suggest\w+|"
    r"conclud\w+|confirm\w+|reveal\w+|associat\w+|correlat\w+|significant\w*|"
    r"increas\w+|decreas\w+|reduc\w+|induc\w+|inhibit\w+|prevent\w+|"
    r"protect\w+|caus\w+|lead(?:s|ing)?\b|led\b|effect\w*|efficac\w+|"
    r"treated|treatment|patients?\b|compared|whereas|however|therefore|"
    r"thus\b|furthermore|moreover|notably|importantly|collectively|"
    r"together|overall|summary|findings?\b|found\b|observed|identified|"
    r"detected|measured|evaluated|analyzed|examined|assessed|determined|"
    r"required|essential|necessary|sufficient|critical|crucial|linked|"
    r"impaired|improved|enhanced|promoted|suppressed|blocked|activated|"
    r"regulated|mediated)\b"
    r"|\b\d+(?:\.\d+)?\s*%"
    r"|\bp\s*[<=>]\s*0?\.\d+",
    re.IGNORECASE,
)


def _evidence_address_key(addr: MemoryEntryAddress) -> tuple[str, str, int, str, str]:
    return (
        addr.memory.scope_id,
        addr.memory.artifact.artifact_id,
        addr.memory.artifact.revision,
        addr.entry_id,
        addr.entry_version_id,
    )


def _stem_evidence_token(tok: str) -> str:
    if len(tok) > 5 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 5 and tok.endswith("ing"):
        return tok[:-3]
    if len(tok) > 4 and tok.endswith("ed"):
        return tok[:-2]
    if len(tok) > 4 and tok.endswith("es"):
        return tok[:-2]
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
        return tok[:-1]
    return tok


def _extract_evidence_features(text: str) -> tuple[set[str], set[str], set[str], set[tuple[str, str]]]:
    raw_words = [
        w
        for w in _EVIDENCE_WORD_RE.findall(text.casefold())
        if w not in _EVIDENCE_STOPWORDS and (len(w) > 1 or w.isdigit())
    ]
    analyzed = [t for t in analyze_text(text) if t not in _EVIDENCE_STOPWORDS and len(t) > 1]
    exact = set(raw_words) | set(analyzed)
    stems = {_stem_evidence_token(t) for t in exact if len(t) >= 3}
    prefixes = {t[:5] for t in exact if len(t) >= 5}
    bigrams: set[tuple[str, str]] = set()
    if len(raw_words) >= 2:
        for i in range(len(raw_words) - 1):
            bigrams.add((_stem_evidence_token(raw_words[i]), _stem_evidence_token(raw_words[i + 1])))
    return exact, stems, prefixes, bigrams


def _segment_evidence_sentences(text: str) -> list[tuple[int, int, str]]:
    n = len(text)
    if n == 0:
        return []
    spans: list[tuple[int, int, str]] = []
    start = 0
    while start < n and text[start].isspace():
        start += 1
    i = start
    while i < n:
        ch = text[i]
        is_break = False
        if ch == "\n":
            is_break = True
        elif ch in "。！？!?":
            is_break = True
        elif ch == ".":
            if i + 1 < n and text[i + 1].isspace():
                prev_char = text[i - 1] if i > 0 else ""
                if prev_char.isdigit() and i + 2 < n and text[i + 2].isdigit():
                    is_break = False
                else:
                    j = i - 1
                    while j >= start and text[j].isalpha():
                        j -= 1
                    prev_word = text[j + 1 : i].casefold()
                    k = i + 1
                    while k < n and text[k] in " \t":
                        k += 1
                    next_non_space = text[k] if k < n else ""
                    if prev_word in _EVIDENCE_ABBREV_WORDS and next_non_space.islower():
                        is_break = False
                    elif (
                        len(prev_word) == 1
                        and prev_word.isalpha()
                        and next_non_space.isupper()
                        and (j < start or text[j] != " ")
                    ):
                        is_break = False
                    elif next_non_space.islower() and prev_word in {"e", "i", "cf", "vs", "al"}:
                        is_break = False
                    else:
                        is_break = True
            elif i + 1 == n:
                is_break = True
        if is_break:
            end = i + 1 if ch != "\n" else i
            while end < n and text[end] in "\"')]}”’":
                end += 1
            s_start = start
            s_end = end
            while s_start < s_end and text[s_start].isspace():
                s_start += 1
            while s_end > s_start and text[s_end - 1].isspace():
                s_end -= 1
            if s_end > s_start:
                spans.append((s_start, s_end, text[s_start:s_end]))
            start = end
            while start < n and text[start].isspace():
                start += 1
            i = start
            continue
        i += 1
    if start < n:
        s_start = start
        s_end = n
        while s_start < s_end and text[s_start].isspace():
            s_start += 1
        while s_end > s_start and text[s_end - 1].isspace():
            s_end -= 1
        if s_end > s_start:
            spans.append((s_start, s_end, text[s_start:s_end]))
    return spans


def _window_evidence_sentence(
    text: str,
    s_start: int,
    s_end: int,
    max_content_bytes: int,
    q_exact: set[str],
    q_stems: set[str],
) -> tuple[int, int, str] | None:
    if max_content_bytes <= 0:
        return None
    sub = text[s_start:s_end]
    if len(sub.encode("utf-8")) <= max_content_bytes:
        return s_start, s_end, sub
    best_pos = 0
    sub_lower = sub.casefold()
    for tok in sorted(q_exact | q_stems, key=len, reverse=True):
        idx = sub_lower.find(tok)
        if idx >= 0:
            best_pos = idx
            break
    n = len(sub)
    left = max(0, best_pos - max_content_bytes // 3)
    while left > 0 and left < n and not sub[left - 1].isspace():
        left -= 1
    while left < n and sub[left].isspace():
        left += 1
    right = left
    last_good = left
    for pos in range(left + 1, n + 1):
        cand = sub[left:pos]
        if len(cand.encode("utf-8")) > max_content_bytes:
            break
        right = pos
        if pos == n or sub[pos].isspace():
            last_good = pos
    if last_good > left:
        right = last_good
    while right > left and sub[right - 1].isspace():
        right -= 1
    if right <= left:
        for pos in range(1, n + 1):
            if len(sub[:pos].encode("utf-8")) <= max_content_bytes:
                right = pos
            else:
                break
        left = 0
    if right <= left:
        return None
    return s_start + left, s_start + right, text[s_start + left : s_start + right]


def _render_evidence_fragments(fragments: Sequence[PreparedEvidenceFragment]) -> str:
    items = [
        {
            "citation": {
                "scope_id": frag.address.memory.scope_id,
                "memory_ref": frag.address.memory.artifact.model_dump(mode="json"),
                "entry_id": frag.address.entry_id,
                "entry_version_id": frag.address.entry_version_id,
            },
            "content": frag.text,
            "start": frag.start,
            "end": frag.end,
        }
        for frag in fragments
    ]
    envelope = {
        "trust": "untrusted_history",
        "items": items,
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))
