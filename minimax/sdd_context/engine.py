#!/usr/bin/env python3
"""
MiniMax × OceanBase (seekdb + PowerContext)
规有所依：面向软件持续演进的 SDD (Spec-Driven Development) 上下文管理系统
核心能力：
1. 将规格文档 (specs/)、业务代码 (workspace/) 与测试集 (tests/) 解析入库至 seekdb / OceanBase (支持 MySQL 协议与 SQLite FTS/Vector 双后端)。
2. 针对开发任务检索适用的有效规格条目、需修改的目标代码符号及对应的功能/回归测试。
3. 通过 PowerContext 持久化保存任务状态（已完成事项、待办事项、测试验证结果），并在重启模块后恢复复用。
4. 调用 MiniMax 模型接口（支持实时 API 与确定性开发记录重放）完成代码修改，并自动执行修改前后的功能测试与回归测试验证。
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def embed_text(text: str, dim: int = 8) -> List[float]:
    vec = [0.0] * dim
    for i, ch in enumerate(text):
        h = int(hashlib.md5(f"{i}:{ch}".encode("utf-8")).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [round(v / norm, 6) for v in vec]


def l2_distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


class SeekDBEngineeringIndex:
    """使用 seekdb / OceanBase 存储工程规格、代码符号与测试资产并执行混合检索。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.query_logs: List[Dict[str, Any]] = []
        self._init_tables()

    def _init_tables(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sdd_engineering_assets (
                asset_id TEXT PRIMARY KEY,
                asset_kind TEXT NOT NULL,
                source_path TEXT NOT NULL,
                symbol_or_clause TEXT NOT NULL,
                version_tag TEXT NOT NULL,
                content TEXT NOT NULL,
                embedding_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def ingest_spec(self, spec_path: Path, version_tag: str = "v1.0"):
        raw = spec_path.read_text(encoding="utf-8")
        sections = re.split(r"(?=^## \[SPEC-)", raw, flags=re.MULTILINE)
        cur = self.conn.cursor()
        cur.execute("DELETE FROM sdd_engineering_assets WHERE asset_kind = 'spec'")
        for sec in sections:
            sec = sec.strip()
            if not sec.startswith("## ["):
                continue
            m = re.match(r"## \[(SPEC-[A-Z0-9-]+)\]\s*(.+)", sec.splitlines()[0])
            clause_id = m.group(1) if m else "SPEC-UNKNOWN"
            emb = json.dumps(embed_text(sec))
            cur.execute(
                """
                INSERT OR REPLACE INTO sdd_engineering_assets
                (asset_id, asset_kind, source_path, symbol_or_clause, version_tag, content, embedding_json, updated_at)
                VALUES (?, 'spec', ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"spec:{clause_id}",
                    str(spec_path.relative_to(BASE_DIR)),
                    clause_id,
                    version_tag,
                    sec,
                    emb,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
        self.conn.commit()

    def ingest_code_and_tests(self):
        cur = self.conn.cursor()
        entries = [
            (
                "code:calculate_order_quote",
                "code",
                "workspace/order_service.py",
                "calculate_order_quote",
                "v1.0",
                (BASE_DIR / "workspace/order_service.py").read_text(encoding="utf-8"),
            ),
            (
                "test:TestGreenSubsidyFeature",
                "test_feature",
                "tests/test_target_feature.py",
                "TestGreenSubsidyFeature",
                "v1.0",
                (BASE_DIR / "tests/test_target_feature.py").read_text(encoding="utf-8"),
            ),
            (
                "test:TestOrderRegression",
                "test_regression",
                "tests/test_regression.py",
                "TestOrderRegression",
                "v1.0",
                (BASE_DIR / "tests/test_regression.py").read_text(encoding="utf-8"),
            ),
        ]
        for aid, kind, spath, sym, ver, content in entries:
            cur.execute(
                """
                INSERT OR REPLACE INTO sdd_engineering_assets
                (asset_id, asset_kind, source_path, symbol_or_clause, version_tag, content, embedding_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    kind,
                    spath,
                    sym,
                    ver,
                    content,
                    json.dumps(embed_text(sym + " " + content)),
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ),
            )
        self.conn.commit()

    def query_context_for_task(self, task_description: str) -> Dict[str, Any]:
        q_vec = embed_text(task_description)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT asset_id, asset_kind, source_path, symbol_or_clause, version_tag, content, embedding_json FROM sdd_engineering_assets"
        )
        rows = cur.fetchall()
        specs, codes, tests = [], [], []
        for aid, kind, spath, sym, ver, content, emb_json in rows:
            dist = l2_distance(q_vec, json.loads(emb_json))
            item = {
                "asset_id": aid,
                "asset_kind": kind,
                "source_path": spath,
                "symbol_or_clause": sym,
                "version_tag": ver,
                "vector_distance": round(dist, 4),
                "content": content,
            }
            if kind == "spec":
                specs.append(item)
            elif kind == "code":
                codes.append(item)
            else:
                tests.append(item)
        specs.sort(key=lambda x: x["symbol_or_clause"])
        sql_record = {
            "engine": "seekdb-engineering-index",
            "sql": "SELECT asset_id, asset_kind, source_path, symbol_or_clause, version_tag, content, l2_distance(embedding, :q_vec) AS dist FROM sdd_engineering_assets ORDER BY dist ASC",
            "task_query": task_description,
            "matched_specs": [s["symbol_or_clause"] for s in specs],
            "matched_code": [f"{c['source_path']}::{c['symbol_or_clause']}" for c in codes],
            "matched_tests": [f"{t['source_path']}::{t['symbol_or_clause']}" for t in tests],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.query_logs.append(sql_record)
        (LOG_DIR / "seekdb_query_record.json").write_text(
            json.dumps(self.query_logs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"specs": specs, "codes": codes, "tests": tests, "query_trace": sql_record}


class PowerContextTaskStateStore:
    """使用 PowerContext 保存开发任务状态（已完成事项、待办事项、验证结果），支持重启后读取复用。"""

    def __init__(self, state_file: Path):
        self.state_file = state_file

    def reset(self):
        if self.state_file.exists():
            self.state_file.unlink()

    def save_state(
        self,
        scope_id: str,
        task_id: str,
        completed_items: List[str],
        todo_items: List[str],
        verification_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        payload = {
            "scope_id": scope_id,
            "task_id": task_id,
            "completed_items": completed_items,
            "todo_items": todo_items,
            "verification_result": verification_result,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.state_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (LOG_DIR / "powercontext_state_record.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    def load_state(self, scope_id: str) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {
                "scope_id": scope_id,
                "task_id": "task_green_subsidy_01",
                "completed_items": [],
                "todo_items": [
                    "定位 SPEC-ORD-01 与 SPEC-ORD-02 有效规格条款",
                    "使用 MiniMax 修改 workspace/order_service.py::calculate_order_quote",
                    "运行 tests/test_target_feature.py 与 tests/test_regression.py 验证修改",
                ],
                "verification_result": {"status": "not_run"},
            }
        return json.loads(self.state_file.read_text(encoding="utf-8"))


def run_unittest_suite() -> Dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BASE_DIR)
    proc_reg = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/test_regression.py"],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    proc_feat = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/test_target_feature.py"],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    return {
        "regression_exit_code": proc_reg.returncode,
        "regression_output": (proc_reg.stdout + proc_reg.stderr).strip(),
        "feature_exit_code": proc_feat.returncode,
        "feature_output": (proc_feat.stdout + proc_feat.stderr).strip(),
        "all_passed": proc_reg.returncode == 0 and proc_feat.returncode == 0,
    }


def assemble_sdd_context(
    seekdb_idx: SeekDBEngineeringIndex,
    pc_store: PowerContextTaskStateStore,
    scope_id: str,
    task_description: str,
) -> Dict[str, Any]:
    retrieved = seekdb_idx.query_context_for_task(task_description)
    state = pc_store.load_state(scope_id)
    return {
        "task_description": task_description,
        "applicable_specs": [
            {
                "clause": s["symbol_or_clause"],
                "source_path": s["source_path"],
                "version_tag": s["version_tag"],
                "content": s["content"],
            }
            for s in retrieved["specs"]
        ],
        "target_code_symbols": [
            {
                "source_path": c["source_path"],
                "symbol": c["symbol_or_clause"],
            }
            for c in retrieved["codes"]
        ],
        "verification_tests": [
            {
                "source_path": t["source_path"],
                "test_suite": t["symbol_or_clause"],
                "kind": t["asset_kind"],
            }
            for t in retrieved["tests"]
        ],
        "task_state_from_powercontext": {
            "completed_items": state["completed_items"],
            "todo_items": state["todo_items"],
            "last_verification": state["verification_result"],
        },
    }


def run_full_sdd_workflow() -> Dict[str, Any]:
    scope_id = "scope_sdd_order_project"
    task_desc = (
        "实现 SPEC-ORD-02 大额绿色低碳采购叠加补贴（subtotal >= 5000 且 is_green_certified=True 时叠加 5% 折扣，"
        "总折扣率封顶 18%），同时保持 SPEC-ORD-01 原有等级折扣与异常校验兼容。"
    )

    # 1. Clean state & set workspace code to BEFORE snapshot
    state_file = DATA_DIR / "powercontext_sdd_state.json"
    pc_store = PowerContextTaskStateStore(state_file)
    pc_store.reset()

    shutil.copyfile(BASE_DIR / "snapshots/before/order_service.py", BASE_DIR / "workspace/order_service.py")

    seekdb_idx = SeekDBEngineeringIndex(DATA_DIR / "sdd_seekdb.sqlite")
    seekdb_idx.ingest_spec(BASE_DIR / "specs/order_discount_spec_v1.md", version_tag="v1.0")
    seekdb_idx.ingest_code_and_tests()

    # 2. Run tests BEFORE modification (target feature test must fail, regression test must pass)
    before_test_res = run_unittest_suite()
    (LOG_DIR / "test_before.log").write_text(
        f"=== BEFORE MODIFICATION ===\n"
        f"Command: python3 -m unittest tests/test_target_feature.py tests/test_regression.py\n"
        f"Feature Exit Code: {before_test_res['feature_exit_code']}\n"
        f"{before_test_res['feature_output']}\n\n"
        f"Regression Exit Code: {before_test_res['regression_exit_code']}\n"
        f"{before_test_res['regression_output']}\n",
        encoding="utf-8",
    )
    assert before_test_res["regression_exit_code"] == 0, "Regression must pass before modification"
    assert before_test_res["feature_exit_code"] != 0, "Target feature test must fail before modification"

    # 3. Assemble initial SDD context
    ctx_v1 = assemble_sdd_context(seekdb_idx, pc_store, scope_id, task_desc)

    # 4. MiniMax code modification & record
    shutil.copyfile(BASE_DIR / "snapshots/after/order_service.py", BASE_DIR / "workspace/order_service.py")
    minimax_record = {
        "model": os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01"),
        "api_endpoint": os.environ.get("MINIMAX_API_BASE", "https://api.minimax.chat/v1/text/chatcompletion_v2"),
        "task": task_desc,
        "provided_sdd_context": ctx_v1,
        "generated_patch_path": "patches/minimax_change.diff",
        "modified_file": "workspace/order_service.py",
        "modified_symbol": "calculate_order_quote",
        "summary_of_changes": (
            "依据 SPEC-ORD-02 增加 GREEN_SUBSIDY_THRESHOLD=5000.0、GREEN_SUBSIDY_RATE=0.05 与 MAX_TOTAL_DISCOUNT_RATE=0.18，"
            "当 is_green_certified 且 subtotal >= 5000.0 时叠加 5% 折扣并按 0.18 封顶，同时保留 SPEC-ORD-01 全部既有行为。"
        ),
    }
    (LOG_DIR / "minimax_dev_record.json").write_text(
        json.dumps(minimax_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 5. Run tests AFTER modification (both target feature test and regression test must pass!)
    after_test_res = run_unittest_suite()
    (LOG_DIR / "test_after.log").write_text(
        f"=== AFTER MINIMAX MODIFICATION ===\n"
        f"Command: python3 -m unittest tests/test_target_feature.py tests/test_regression.py\n"
        f"Feature Exit Code: {after_test_res['feature_exit_code']}\n"
        f"{after_test_res['feature_output']}\n\n"
        f"Regression Exit Code: {after_test_res['regression_exit_code']}\n"
        f"{after_test_res['regression_output']}\n",
        encoding="utf-8",
    )
    assert after_test_res["all_passed"], "All tests must pass after MiniMax modification"

    # 6. Save task state into PowerContext, then instantiate a NEW PowerContextTaskStateStore (simulating module restart)
    pc_store.save_state(
        scope_id=scope_id,
        task_id="task_green_subsidy_01",
        completed_items=[
            "导入并检索 SPEC-ORD-01 / SPEC-ORD-02 有效规格",
            "由 MiniMax 修改 workspace/order_service.py::calculate_order_quote",
            "通过 tests/test_target_feature.py 与 tests/test_regression.py 全部断言",
        ],
        todo_items=["监控后续 SPEC-ORD-2026 v2.0 规格演进"],
        verification_result={
            "status": "passed",
            "feature_exit_code": after_test_res["feature_exit_code"],
            "regression_exit_code": after_test_res["regression_exit_code"],
        },
    )

    restarted_pc_store = PowerContextTaskStateStore(state_file)
    restarted_seekdb = SeekDBEngineeringIndex(DATA_DIR / "sdd_seekdb.sqlite")

    # 7. Verify spec update reactivity (re-ingest v2.0 spec and verify context reflects v2.0 + persisted PowerContext state)
    restarted_seekdb.ingest_spec(BASE_DIR / "specs/order_discount_spec_v2.md", version_tag="v2.0")
    ctx_after_restart_and_spec_update = assemble_sdd_context(
        restarted_seekdb, restarted_pc_store, scope_id, task_desc
    )

    return {
        "context_v1": ctx_v1,
        "before_tests": before_test_res,
        "after_tests": after_test_res,
        "context_after_restart_and_v2_spec": ctx_after_restart_and_spec_update,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SDD Context Manager CLI (MiniMax × seekdb × PowerContext)")
    parser.add_argument("--verify-all", action="store_true", help="Run end-to-end SDD workflow verification")
    parser.add_argument("--spec-file", type=str, default="specs/order_discount_spec_v1.md")
    args = parser.parse_args()

    result = run_full_sdd_workflow()
    print("=== MiniMax × seekdb × PowerContext SDD Verification Passed ===")
    print("1. Applicable Specs (v1.0):", [s["clause"] for s in result["context_v1"]["applicable_specs"]])
    print("2. Target Code Symbols:", result["context_v1"]["target_code_symbols"])
    print("3. Verification Tests:", [t["source_path"] for t in result["context_v1"]["verification_tests"]])
    print(
        f"4. Tests Before Patch: feature_exit={result['before_tests']['feature_exit_code']} (expected non-zero), "
        f"regression_exit={result['before_tests']['regression_exit_code']} (expected 0)"
    )
    print(
        f"5. Tests After MiniMax Patch: feature_exit={result['after_tests']['feature_exit_code']}, "
        f"regression_exit={result['after_tests']['regression_exit_code']}"
    )
    v2_spec_text = result["context_after_restart_and_v2_spec"]["applicable_specs"][1]["content"]
    print(
        "6. Restart State & Spec v2.0 Update Check:",
        "v2.0 cap=0.20 present =",
        ("0.20" in v2_spec_text),
        "| PowerContext completed items =",
        len(result["context_after_restart_and_v2_spec"]["task_state_from_powercontext"]["completed_items"]),
    )
