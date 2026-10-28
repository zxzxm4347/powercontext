#!/usr/bin/env python3
"""
ScholarPulse CLI & Verification Entrypoint (Qoder × OceanBase SeekDB × PowerContext)
OB-P:Z4X16gLee3rCjUZxvoUw0g
"""
import argparse
import json
import os
import uvicorn
from scholar_engine import ScholarPulseEngine
from server import app


def run_verify() -> dict:
    engine = ScholarPulseEngine()
    init_res = engine.init_and_seed_seekdb(force_reset=True)
    mem_res = engine.save_learner_memory(
        learner_id="scholar-01",
        weak_topics=["hybrid_rag"],
        mastered_doc_ids=["PAPER-VSAG-01"],
        preferred_depth="deep-derivation",
        review_note="重点强化 RRF 倒数排名融合公式推导",
    )
    reloaded = ScholarPulseEngine().get_learner_memory("scholar-01")
    brief_res = engine.build_research_brief_and_flashcards(
        research_question="SeekDB Delta HNSW 与 RRF 混合检索在字节预算下如何协同？",
        max_bytes=1400,
        learner_id="scholar-01",
    )
    ok = (
        init_res.get("status") == "online"
        and init_res.get("total_sections", 0) >= 4
        and mem_res.get("status") == "saved"
        and reloaded is not None
        and "hybrid_rag" in reloaded.get("weak_topics", [])
        and brief_res["prepared_context"].startswith("BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n")
        and brief_res["prepared_context_bytes"] <= 1400
        and brief_res["flashcards"][0]["priority"] == "high_weak_topic"
    )
    summary = {
        "overall_pass": ok,
        "team_token": "OB-P:Z4X16gLee3rCjUZxvoUw0g",
        "init": init_res,
        "memory_reloaded": reloaded,
        "prepared_context_bytes": brief_res["prepared_context_bytes"],
        "top_flashcard": brief_res["flashcards"][0],
        "handoff": brief_res["handoff"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"OVERALL_VERIFY_PASS={ok}")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ScholarPulse Qoder Server & Verifier")
    parser.add_argument("--verify", action="store_true", help="Run end-to-end SeekDB + PowerContext verification")
    parser.add_argument("--serve", action="store_true", help="Start FastAPI server")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8093")), help="Server port")
    args = parser.parse_args()
    if args.verify:
        res = run_verify()
        raise SystemExit(0 if res["overall_pass"] else 1)
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=args.port)
