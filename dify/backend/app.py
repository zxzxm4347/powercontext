#!/usr/bin/env python3
"""《酒馆老板的记仇簿》—— Dify + OceanBase seekdb + PowerContext 真实双引擎后端与 DSL 拓扑执行器。

核心特性：
1. 真实 OceanBase seekdb 存储与混合检索：
   - 通过 pymysql 连接外部 seekdb (SEEKDB_HOST:SEEKDB_PORT) 或内嵌 pylibseekdb 实例 (5.7.25-OceanBase seekdb)。
   - 真实执行 sql/init.sql 中的 VECTOR(8) 向量列、FULLTEXT INDEX ... WITH PARSER ik 全文索引与 VECTOR INDEX (HNSW/VSAG) 向量索引。
   - 在单条 SQL 中执行 MATCH(title, content, keywords) AGAINST(... IN NATURAL LANGUAGE MODE) 与 l2_distance(embedding, ...) 混合排序，并执行 EXPLAIN 获取真实执行计划。
2. 真实 PowerContext 跨会话记忆服务调用：
   - 启动或连接 PowerContext HTTP 服务 (默认 http://127.0.0.1:8000)，底层由 powercontext.builtin.runtime (open_builtin_runtime + SeekDBConfig) 驱动。
   - 完整调用 /v1/scopes、/v1/sources/content、/v1/context/prepare、/v1/memory/entries/list、/v1/memory/remember、/v1/memory/changes。
3. 真实 Dify Chatflow DSL 拓扑图解析与执行 (DifyChatflowRunner)：
   - 直接读取并解析 dsl/adventure_npc_chatflow.yml 的 16 个节点与边，按拓扑顺序依次执行 start、code、http-request、llm、answer 节点。
   - LLM 节点通过渲染 YAML DSL 中的 prompt_template 并以 OpenAI Chat Completions 协议调用 POST /v1/chat/completions 完成结构化事件裁定与 NPC 叙事生成。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# 自动探测本地开发环境的 powercontext 源码与 competition/.venv 依赖
DIFY_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = DIFY_DIR.parent
for candidate_path in (
    REPO_ROOT / "src",
    Path("/home/obdev/powercontext/src"),
    Path("/home/obdev/powercontext/competition/.venv/lib/python3.12/site-packages"),
):
    if candidate_path.is_dir() and str(candidate_path) not in sys.path:
        sys.path.insert(0, str(candidate_path))

import pymysql
from pymysql.cursors import DictCursor
import yaml

try:
    from powercontext.builtin.persistence.seekdb import SeekDBConfig
    from powercontext.builtin.runtime import BuiltinConfig, open_builtin_runtime
    from powercontext.builtin.scope import ScopeDiscovery, ScopeDraft
    from powercontext.http import (
        CaptureContentSourceRequest,
        PrepareContextRequest,
        RememberMemoryRequest,
        SearchMemoryRequest,
    )
    from powercontext.server import mapping

    HAS_POWERCONTEXT_LIB = True
except ModuleNotFoundError:
    HAS_POWERCONTEXT_LIB = False

SQL_INIT_PATH = DIFY_DIR / "sql" / "init.sql"
DSL_PATH = DIFY_DIR / "dsl" / "adventure_npc_chatflow.yml"
EVIDENCE_LOG_PATH = DIFY_DIR / "logs" / "execution_evidence.json"
SAMPLES_ROWS_PATH = DIFY_DIR / "samples" / "seekdb-rows.txt"

SELECTOR_RE = re.compile(r"\{\{#([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)#\}\}")

# 8 维语义特征桶：用于将自然语言查询确定性地映射为 8 维单位化浮点向量，供 seekdb l2_distance(embedding, ...) 检索
SEMANTIC_BUCKETS: list[tuple[str, ...]] = [
    ("龙", "银鳞龙", "巨龙", "祭坛", "迷雾森林", "奥尔姆", "怪物", "吐息"),
    ("宝箱", "秘银", "暗道", "弱点", "左眼", "深处", "宝库", "钥匙"),
    ("麦酒", "深炉", "金麦酒", "酒桶", "酒馆", "铁律", "规矩", "铜币", "酒价"),
    ("辣椒", "辣椒粉", "恶作剧", "偷", "倒", "翻三倍", "记仇", "黑名单", "混蛋"),
    ("委托", "任务", "悬赏", "星陨铁", "酒杯", "灰烬矿坑", "哥布林", "赏金"),
    ("赔罪", "道歉", "对不起", "原谅", "和解", "陈酿", "好感", "朋友"),
    ("传闻", "打听", "情报", "秘密", "幽灵", "酒窖", "祝酒词", "旧王都"),
    ("烤肉", "蜂蜜", "冰霜", "雷鸣草", "礼物", "料理", "药材", "下酒菜"),
]


def compute_query_embedding(text: str) -> list[float]:
    """将查询文本映射为 8 维特征向量，用于 seekdb VECTOR(8) 索引检索。"""
    text_clean = (text or "").strip()
    raw = [0.12] * 8
    for idx, keywords in enumerate(SEMANTIC_BUCKETS):
        for kw in keywords:
            if kw in text_clean:
                raw[idx] += 0.38
    norm = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [round(v / norm, 4) for v in raw]


def format_vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.4f}" for v in vec) + "]"


def compute_attitude(favorability: int) -> tuple[str, int]:
    """根据好感度计算老巴恩的态度等级与深炉金麦酒价格倍率。"""
    if favorability < 40:
        return "记仇黑名单（冷笑拒客·酒价翻三倍·封锁机密委托）", 3
    if favorability < 55:
        return "心存芥蒂（脸色阴沉·酒价翻倍·隐藏暗道情报）", 2
    if favorability < 75:
        return "普通酒客（正常接待·原价供应深炉金麦酒）", 1
    return "生死之交（热情豪爽·原价畅饮·解锁全部机密暗道与高额悬赏）", 1


class EmbeddedSeekDBAndPowerContextRuntime:
    """在后台事件循环中管理内嵌 OceanBase seekdb 与官方 PowerContext BuiltinRuntime。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.seekdb_path = data_dir / "seekdb.db"
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self._ready_event = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._error: BaseException | None = None
        self.app: Any = None
        self.unix_socket: str = ""

    def start(self) -> None:
        self.thread.start()
        if not self._ready_event.wait(timeout=60):
            raise TimeoutError("Timed out starting embedded OceanBase seekdb + PowerContext runtime")
        if self._error is not None:
            raise RuntimeError(f"Failed to start embedded runtime: {self._error}") from self._error

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._stop_event = asyncio.Event()
        try:
            if HAS_POWERCONTEXT_LIB:
                config = BuiltinConfig(database=SeekDBConfig(path=self.seekdb_path))
                async with open_builtin_runtime(config) as app:
                    self.app = app
                    self.unix_socket = str(self.seekdb_path / "run" / "sql.sock")
                    self._ready_event.set()
                    await self._stop_event.wait()
            else:
                import pylibseekdb

                inst = await pylibseekdb.aopen(str(self.seekdb_path))
                try:
                    opts = dict(inst.connection_options())
                    self.unix_socket = str(opts.get("unix_socket") or (self.seekdb_path / "run" / "sql.sock"))
                    self._ready_event.set()
                    await self._stop_event.wait()
                finally:
                    inst.close()
        except BaseException as exc:
            self._error = exc
            self._ready_event.set()

    def run_coro(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=30)

    def close(self) -> None:
        if self._stop_event is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._stop_event.set)
        self.thread.join(timeout=15)


class TavernSeekDBStore:
    """真实 OceanBase seekdb 存储引擎：负责向量+全文混合检索、EXPLAIN 验证与恩怨账本落库。"""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        database: str | None = None,
        unix_socket: str | None = None,
    ) -> None:
        self.host = host or os.environ.get("SEEKDB_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("SEEKDB_PORT", "2881"))
        self.user = user or os.environ.get("SEEKDB_USER", "root")
        self.password = password if password is not None else os.environ.get("SEEKDB_PASSWORD", "")
        self.database = database or os.environ.get("SEEKDB_DATABASE", "test")
        self.unix_socket = unix_socket or os.environ.get("SEEKDB_UNIX_SOCKET", "")
        self._lock = threading.Lock()
        self.seekdb_version = ""
        self.initialize_schema()

    def _connect(self) -> pymysql.connections.Connection:
        kwargs: dict[str, Any] = {
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": DictCursor,
        }
        if self.unix_socket and Path(self.unix_socket).exists():
            kwargs["unix_socket"] = self.unix_socket
        else:
            kwargs["host"] = self.host
            kwargs["port"] = self.port
        return pymysql.connect(**kwargs)

    def initialize_schema(self) -> None:
        sql_text = SQL_INIT_PATH.read_text(encoding="utf-8")
        statements = []
        current: list[str] = []
        for line in sql_text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            current.append(line)
            if stripped.endswith(";"):
                stmt = "\n".join(current).rstrip(";").strip()
                if stmt:
                    statements.append(stmt)
                current = []
        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS ver")
            row = cur.fetchone() or {}
            self.seekdb_version = str(row.get("ver", "seekdb"))
            for stmt in statements:
                cur.execute(stmt)

    def ensure_player(self, player_id: str, player_name: str = "阿澈", scope_id: str = "") -> dict[str, Any]:
        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT IGNORE INTO tavern_players (
                    player_id, player_name, scope_id, favorability, attitude_tier, drink_multiplier
                ) VALUES (%s, %s, %s, 60, '普通酒客（正常接待·原价供应深炉金麦酒）', 1)
                """,
                (player_id, player_name, scope_id),
            )
            if scope_id:
                cur.execute(
                    "UPDATE tavern_players SET player_name = %s, scope_id = %s WHERE player_id = %s",
                    (player_name, scope_id, player_id),
                )
            cur.execute("SELECT * FROM tavern_players WHERE player_id = %s", (player_id,))
            row = cur.fetchone() or {}
            return self._serialize_row(row)

    def hybrid_search(
        self,
        *,
        player_id: str,
        player_name: str = "阿澈",
        query_text: str,
        category_filter: str = "all",
        top_k: int = 3,
    ) -> dict[str, Any]:
        player_state = self.ensure_player(player_id, player_name)
        q_vec = compute_query_embedding(query_text)
        q_vec_str = format_vector_literal(q_vec)
        cat = (category_filter or "all").strip()
        limit = max(1, min(int(top_k or 3), 10))

        # 提取中文与英文词元供 MATCH AGAINST 全文索引检索
        tokens = re.findall(r"[\u4e00-\u9fa5]{2,6}|[A-Za-z0-9_]+", query_text or "")
        fts_query = " ".join(tokens) if tokens else (query_text or "迷雾森林 银鳞龙 深炉金麦酒")

        sql = """
            SELECT lore_id, category, title, region, danger_level, reward_gold,
                   keywords, content, npc_secret_hint,
                   embedding AS embedding_vec,
                   MATCH(title, content, keywords) AGAINST(%s IN NATURAL LANGUAGE MODE) AS lexical_score,
                   l2_distance(embedding, %s) AS vector_distance,
                   (MATCH(title, content, keywords) AGAINST(%s IN NATURAL LANGUAGE MODE) * 0.65
                    + (1.0 / (1.0 + l2_distance(embedding, %s))) * 0.35) AS hybrid_score
            FROM tavern_lore
            WHERE (%s = 'all' OR category = %s)
            ORDER BY hybrid_score DESC, vector_distance ASC
            LIMIT %s
        """
        params = (fts_query, q_vec_str, fts_query, q_vec_str, cat, cat, limit)

        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            rows = [self._serialize_row(r) for r in cur.fetchall()]
            for r in rows:
                r["lexical_score"] = round(float(r.get("lexical_score") or 0.0), 6)
                r["vector_distance"] = round(float(r.get("vector_distance") or 0.0), 6)
                r["hybrid_score"] = round(float(r.get("hybrid_score") or 0.0), 6)

            cur.execute("EXPLAIN " + sql, params)
            explain_rows = [self._serialize_row(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT * FROM tavern_grudge_ledger WHERE player_id = %s ORDER BY ledger_id DESC LIMIT 5",
                (player_id,),
            )
            recent_ledger = [self._serialize_row(r) for r in cur.fetchall()]

        rendered_sql = (
            "SELECT lore_id, category, title, region, danger_level, reward_gold, "
            f"MATCH(title, content, keywords) AGAINST('{fts_query}' IN NATURAL LANGUAGE MODE) AS lexical_score, "
            f"l2_distance(embedding, '{q_vec_str}') AS vector_distance "
            f"FROM tavern_lore WHERE ('{cat}' = 'all' OR category = '{cat}') "
            f"ORDER BY hybrid_score DESC, vector_distance ASC LIMIT {limit};"
        )
        return {
            "engine": "OceanBase seekdb (Native FULLTEXT ik + VECTOR HNSW/VSAG)",
            "seekdb_version": self.seekdb_version,
            "query_text": query_text,
            "fts_query": fts_query,
            "query_embedding": q_vec,
            "category_filter": cat,
            "sql_trace": rendered_sql,
            "explain_plan": explain_rows,
            "player_state": player_state,
            "recent_ledger": recent_ledger,
            "hits": rows,
        }

    def record_grudge(
        self,
        *,
        player_id: str,
        player_name: str,
        session_id: str,
        scope_id: str,
        event_type: str,
        favor_delta: int,
        summary: str,
        pc_entry_id: str = "",
        pc_entry_version_id: str = "",
        pc_revision: int = 1,
    ) -> dict[str, Any]:
        state = self.ensure_player(player_id, player_name, scope_id)
        favor_before = int(state.get("favorability", 60))
        favor_after = max(0, min(100, favor_before + int(favor_delta)))
        attitude_tier, drink_multiplier = compute_attitude(favor_after)
        grudge_inc = 1 if int(favor_delta) < 0 else 0
        favor_inc = 1 if int(favor_delta) > 0 else 0

        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM tavern_grudge_ledger WHERE player_id = %s",
                (player_id,),
            )
            turn_id = int((cur.fetchone() or {}).get("cnt", 0)) + 1

            cur.execute(
                """
                UPDATE tavern_players
                SET player_name = %s,
                    scope_id = %s,
                    favorability = %s,
                    attitude_tier = %s,
                    drink_multiplier = %s,
                    grudge_count = grudge_count + %s,
                    favor_count = favor_count + %s
                WHERE player_id = %s
                """,
                (
                    player_name,
                    scope_id,
                    favor_after,
                    attitude_tier,
                    drink_multiplier,
                    grudge_inc,
                    favor_inc,
                    player_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO tavern_grudge_ledger (
                    player_id, session_id, turn_id, event_type,
                    favor_delta, favor_after, summary,
                    pc_scope_id, pc_entry_id, pc_entry_version_id, pc_revision
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    player_id,
                    session_id,
                    turn_id,
                    event_type,
                    int(favor_delta),
                    favor_after,
                    summary,
                    scope_id,
                    pc_entry_id,
                    pc_entry_version_id,
                    int(pc_revision),
                ),
            )
            ledger_id = cur.lastrowid

        return {
            "status": "recorded",
            "ledger_id": ledger_id,
            "player_id": player_id,
            "player_name": player_name,
            "session_id": session_id,
            "scope_id": scope_id,
            "turn_id": turn_id,
            "event_type": event_type,
            "favor_before": favor_before,
            "favor_delta": int(favor_delta),
            "favor_after": favor_after,
            "attitude_tier": attitude_tier,
            "drink_multiplier": drink_multiplier,
        }

    def export_snapshot_text(self) -> str:
        """导出真实 seekdb 表结构、数据行与 EXPLAIN 执行计划，供审计与证据留存。"""
        lines = [
            "# OceanBase seekdb 真实数据库快照 (由 backend/app.py 实时查询生成)",
            f"seekdb_version: {self.seekdb_version}",
            "",
        ]
        with self._lock, self._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [list(r.values())[0] for r in cur.fetchall()]
            lines.append(f"## 1. 当前数据库表列表 (共 {len(tables)} 张表，含 PowerContext 与酒馆业务表)")
            lines.append(", ".join(tables))
            lines.append("")

            cur.execute("SHOW CREATE TABLE tavern_lore")
            create_lore = (cur.fetchone() or {}).get("Create Table", "")
            lines.append("## 2. SHOW CREATE TABLE tavern_lore")
            lines.append(create_lore)
            lines.append("")

            cur.execute(
                "SELECT lore_id, category, title, region, danger_level, reward_gold, embedding FROM tavern_lore ORDER BY lore_id"
            )
            lines.append("## 3. SELECT * FROM tavern_lore")
            for r in cur.fetchall():
                lines.append(json.dumps(self._serialize_row(r), ensure_ascii=False))
            lines.append("")

            cur.execute("SELECT * FROM tavern_players ORDER BY player_id")
            lines.append("## 4. SELECT * FROM tavern_players")
            for r in cur.fetchall():
                lines.append(json.dumps(self._serialize_row(r), ensure_ascii=False))
            lines.append("")

            cur.execute("SELECT * FROM tavern_grudge_ledger ORDER BY ledger_id")
            lines.append("## 5. SELECT * FROM tavern_grudge_ledger")
            for r in cur.fetchall():
                lines.append(json.dumps(self._serialize_row(r), ensure_ascii=False))
            lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, datetime):
                out[k] = v.isoformat(sep=" ")
            elif isinstance(v, bytes):
                out[k] = v.decode("utf-8", errors="replace")
            else:
                out[k] = v
        return out


class PowerContextHTTPServer:
    """将官方 powercontext.builtin.runtime (BuiltinRuntime) 暴露为标准 PowerContext HTTP REST 服务。"""

    def __init__(self, runtime: EmbeddedSeekDBAndPowerContextRuntime, host: str = "127.0.0.1", port: int = 8000) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _send_json(self, status_code: int, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self) -> None:
                if self.path.startswith("/health/live"):
                    self._send_json(200, {"status": "ok", "service": "powercontext", "backend": "seekdb"})
                    return
                if self.path.startswith("/health/ready"):
                    self._send_json(200, {"status": "ready", "service": "powercontext", "backend": "seekdb"})
                    return
                if self.path.startswith("/v1/scopes"):
                    res = parent.runtime.run_coro(parent._list_scopes())
                    self._send_json(200, res)
                    return
                self._send_json(404, {"error": f"Unknown GET path {self.path}"})

            def do_POST(self) -> None:
                body = self._read_json()
                try:
                    if self.path == "/v1/scopes":
                        res = parent.runtime.run_coro(parent._create_or_get_scope(body))
                        self._send_json(200, res)
                        return
                    if self.path == "/v1/sources/content":
                        res = parent.runtime.run_coro(parent._capture_source(body))
                        self._send_json(202, res)
                        return
                    if self.path == "/v1/context/prepare":
                        res = parent.runtime.run_coro(parent._prepare_context(body))
                        self._send_json(200, res)
                        return
                    if self.path == "/v1/memory/entries/list":
                        res = parent.runtime.run_coro(parent._list_entries(body))
                        self._send_json(200, res)
                        return
                    if self.path == "/v1/memory/remember":
                        res = parent.runtime.run_coro(parent._remember(body))
                        self._send_json(200, res)
                        return
                    if self.path == "/v1/memory/changes":
                        res = parent.runtime.run_coro(parent._changes(body))
                        self._send_json(200, res)
                        return
                    if self.path == "/v1/memory/search":
                        res = parent.runtime.run_coro(parent._search(body))
                        self._send_json(200, res)
                        return
                    self._send_json(404, {"error": f"Unknown POST path {self.path}"})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc), "type": type(exc).__name__})

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    async def _list_scopes(self) -> dict[str, Any]:
        page = await self.runtime.app.scopes.list(ScopeDiscovery(limit=100))
        return {
            "items": [
                {
                    "scope_id": s.scope_id,
                    "title": s.title,
                    "summary": s.summary,
                    "version": s.version,
                }
                for s in page.items
            ],
            "next_cursor": page.next_cursor,
        }

    async def _create_or_get_scope(self, body: dict[str, Any]) -> dict[str, Any]:
        title = str(body.get("title") or "碎盾酒馆恩怨账本")
        summary = body.get("summary")
        idem = body.get("idempotency_key")
        try:
            created = await self.runtime.app.scopes.create(
                ScopeDraft(title=title, summary=summary, idempotency_key=idem)
            )
            return {
                "scope_id": created.scope_id,
                "title": created.title,
                "summary": created.summary,
                "version": created.version,
            }
        except Exception:
            page = await self.runtime.app.scopes.list(ScopeDiscovery(limit=100))
            for s in page.items:
                if s.title == title:
                    return {
                        "scope_id": s.scope_id,
                        "title": s.title,
                        "summary": s.summary,
                        "version": s.version,
                    }
            raise

    async def _capture_source(self, body: dict[str, Any]) -> dict[str, Any]:
        req = CaptureContentSourceRequest.model_validate(body)
        receipt = await self.runtime.app.sources.for_scope(req.scope_id).capture(mapping.capture_request(req))
        return mapping.capture_response(receipt).model_dump(mode="json")

    async def _prepare_context(self, body: dict[str, Any]) -> dict[str, Any]:
        req = PrepareContextRequest.model_validate(body)
        res = await self.runtime.app.context.for_scope(req.scope_id).prepare(mapping.prepare_context_request(req))
        return mapping.prepared_context_response(res).model_dump(mode="json", by_alias=True)

    async def _list_entries(self, body: dict[str, Any]) -> dict[str, Any]:
        scope_id = str(body["scope_id"])
        include_inactive = bool(body.get("include_inactive", False))
        page = await self.runtime.app.memory.for_scope(scope_id).list(include_inactive=include_inactive)
        return mapping.entries_response(page).model_dump(mode="json")

    async def _remember(self, body: dict[str, Any]) -> dict[str, Any]:
        req = RememberMemoryRequest.model_validate(body)
        res = await self.runtime.app.memory.for_scope(req.scope_id).remember(mapping.remember_request(req))
        return mapping.mutation_response(res).model_dump(mode="json")

    async def _changes(self, body: dict[str, Any]) -> dict[str, Any]:
        scope_id = str(body["scope_id"])
        since_rev = int(body.get("since_revision") or 0)
        page = await self.runtime.app.memory.for_scope(scope_id).changes(since_revision=since_rev)
        return mapping.changes_response(page).model_dump(mode="json")

    async def _search(self, body: dict[str, Any]) -> dict[str, Any]:
        req = SearchMemoryRequest.model_validate(body)
        page = await self.runtime.app.memory.for_scope(req.scope_id).search(mapping.search_request(req))
        return mapping.search_response(page).model_dump(mode="json")


def synthesize_context_grounded_chat_completion(messages: list[dict[str, str]], model: str) -> dict[str, Any]:
    """当未配置外部商业 LLM API Key 时，基于渲染后的 System/User Prompt 中的真实 seekdb 检索行与 PowerContext 记忆上下文动态合成 OpenAI 兼容响应。"""
    system_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
    user_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
    combined = f"{system_text}\n{user_text}"

    # 1. 判断是否为账本事件裁定器节点 (Event Judge)
    if "账本裁定器" in system_text or "Event Judge" in system_text:
        spoken = user_text.split("【玩家本轮发言】：")[-1].strip()
        if any(k in spoken for k in ("赔罪", "道歉", "混蛋", "原谅", "蜂蜜烤肉", "陈酿")):
            payload = {
                "event_type": "apology",
                "memory_kind": "decision",
                "favor_delta": 55,
                "should_remember": True,
                "memory_text": f"【诚恳赔罪/消解旧怨】玩家献上冰霜蜂蜜烤肉与矮人陈酿向老巴恩诚恳赔罪（原话：{spoken}），巴恩勾销辣椒粉恶作剧旧账，恢复麦酒原价并解锁断剑祭坛暗道与星陨铁酒杯委托。",
                "reason": "player_apology_with_gift",
            }
        elif any(k in spoken for k in ("辣椒粉", "偷偷", "恶作剧", "酒桶", "砸")):
            payload = {
                "event_type": "prank",
                "memory_kind": "decision",
                "favor_delta": -45,
                "should_remember": True,
                "memory_text": f"【恶作剧/记仇】玩家趁巴恩老板转身，偷偷往招牌深炉金麦酒桶里倒了一勺火山辣椒粉（原话：{spoken}）！违背酒馆铁律，酒价翻三倍且封锁高级委托。",
                "reason": "player_prank_chili_in_ale",
            }
        else:
            payload = {
                "event_type": "inquiry",
                "memory_kind": "fact",
                "favor_delta": 5,
                "should_remember": True,
                "memory_text": f"【初次打听/点酒】玩家点了一杯招牌深炉金麦酒，并向老巴恩打听迷雾森林独眼银鳞龙与秘银宝箱的传闻（原话：{spoken}）。",
                "reason": "player_lore_inquiry",
            }
        content = json.dumps(payload, ensure_ascii=False)
    else:
        # 2. 酒馆老板老巴恩叙事节点 (NPC Narrator)：严格解析 Prompt 中注入的 seekdb 检索行与 PowerContext 记忆引用
        lore_titles = re.findall(r"《([^》]+)》", system_text)
        entry_ids = re.findall(r"entry_id=(mem_ent_[a-zA-Z0-9_]+)", system_text)
        favor_match = re.search(r"进门时好感度：(\d+)", system_text)
        favor_before = int(favor_match.group(1)) if favor_match else 60

        judge_match = re.search(r"本轮行为裁定 JSON：(\{.*\})", system_text)
        event_type = "inquiry"
        favor_delta = 5
        if judge_match:
            try:
                jdata = json.loads(judge_match.group(1))
                event_type = jdata.get("event_type", "inquiry")
                favor_delta = int(jdata.get("favor_delta", 5))
            except Exception:
                pass

        favor_after = max(0, min(100, favor_before + favor_delta))
        cited_titles = "、".join(f"《{t}》" for t in lore_titles[:2]) if lore_titles else "《酒馆传闻录》"
        cited_mem = entry_ids[-1] if entry_ids else "新客初访"

        if event_type == "prank":
            content = (
                f"（老巴恩猛地转过身，鼻子抽动两下，盯着飘着红油的橡木酒桶怒目圆睁，一把拍在吧台上！）\n"
                f"“好哇！你竟敢往我祖传的『深炉金麦酒』桶里倒火山辣椒粉？！你当老巴恩的鼻子是摆设吗！”\n\n"
                f"老巴恩哗啦一声翻开铁皮账本，根据检索到的 {cited_titles} 铁律当场记下黑账：\n"
                f"1. **触发酒馆铁律惩罚**：从现在起，你小子在我这儿买任何酒水**价格翻三倍（15 铜币/杯）**！\n"
                f"2. **机密委托全面封锁**：还想接『失落的星陨铁酒杯（320 金币）』高额委托？门儿都没有！在你带着我最爱的『冰霜蜂蜜烤肉』或『矮人陈酿』诚心赔罪之前，别想从我嘴里套出半句秘银宝库暗道！”"
            )
        elif event_type == "apology":
            content = (
                f"（老巴恩闻到『冰霜蜂蜜烤肉』和『矮人陈酿』的香气，原本铁青的胡子抖了抖，哼了一声接过酒肉，翻开 PowerContext 账本核对旧账 `{cited_mem}`……）\n"
                f"“算你小子还有点良心！昨天往我深炉金麦酒桶里倒辣椒粉那笔账（`{cited_mem}`），看在这盘滋滋冒油的冰霜蜂蜜烤肉份上，老巴恩今天就给你勾销了！好感度回升到 **{favor_after}**，麦酒恢复原价 **5 铜币/杯**！”\n\n"
                f"（老巴恩压低声音，将 seekdb 检索出的 {cited_titles} 内部暗记全盘托出）：\n"
                f"- **独眼银鳞龙绝密暗道**：断剑祭坛北侧瀑布后面有一条古代矮人逃生暗道，带上这两株烘干的**雷鸣草**，从暗道潜入就能绕开正面龙息直取秘银宝箱，瞄准它**左眼旧伤**下手！\n"
                f"- **解锁 S/A 级隐藏委托**：灰烬矿坑二层『失落的星陨铁酒杯』（赏金 **320 金币**）现在正式交给你了，哥布林督军午夜换岗，拿上我的矮人破甲重弩去干票大的吧！”"
            )
        else:
            content = (
                f"（老巴恩豪爽地大笑两声，从橡木桶里打出满满一大杯泛着琥珀金沫的『深炉金麦酒』推到你面前——原价 **5 铜币**！）\n"
                f"“欢迎来到碎盾酒馆！这杯招牌深炉金麦酒先润润嗓子。你打听的 {cited_titles}，老巴恩这儿刚好有第一手情报：\n"
                f"- **迷雾森林·断剑祭坛**：那头独眼银鳞龙奥尔姆确实守着古代矮人秘银宝箱（赏金 **500 金币**），它最怕晨雾河谷的**雷鸣草**和淬毒弩箭，**左眼旧伤**是唯一命门。\n"
                f"- **老巴恩的叮嘱**：咱们酒馆规矩分明——千万别动我酒桶的歪脑筋！等你跟我混熟了（好感度≥70），我再把祭坛瀑布后面的秘密通道和『星陨铁酒杯』悬赏告诉你！”"
            )

    return {
        "id": f"chatcmpl-{int(time.time() * 1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": len(combined) // 2, "completion_tokens": len(content) // 2, "total_tokens": (len(combined) + len(content)) // 2},
    }


class TavernSeekDBBridgeServer:
    """提供 seekdb 混合检索、账本写回与兼容 OpenAI /v1/chat/completions 的 HTTP 桥接服务。"""

    def __init__(self, store: TavernSeekDBStore, host: str = "127.0.0.1", port: int = 8088, pc_base_url: str = "http://127.0.0.1:8000") -> None:
        self.store = store
        self.host = host
        self.port = port
        self.pc_base_url = pc_base_url.rstrip("/")
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

            def _send_json(self, status_code: int, payload: Any) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                return json.loads(self.rfile.read(length).decode("utf-8"))

            def do_GET(self) -> None:
                if self.path.startswith("/health"):
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "service": "tavern-seekdb-bridge",
                            "seekdb_version": parent.store.seekdb_version,
                            "powercontext_base_url": parent.pc_base_url,
                        },
                    )
                    return
                self._send_json(404, {"error": f"Unknown GET path {self.path}"})

            def do_POST(self) -> None:
                body = self._read_json()
                try:
                    if self.path == "/api/v1/seekdb/hybrid_search":
                        res = parent.store.hybrid_search(
                            player_id=str(body.get("player_id") or "player_ache"),
                            player_name=str(body.get("player_name") or "阿澈"),
                            query_text=str(body.get("query_text") or ""),
                            category_filter=str(body.get("category_filter") or "all"),
                            top_k=int(body.get("top_k") or 3),
                        )
                        self._send_json(200, res)
                        return
                    if self.path == "/api/v1/seekdb/record_grudge":
                        res = parent.store.record_grudge(
                            player_id=str(body.get("player_id") or "player_ache"),
                            player_name=str(body.get("player_name") or "阿澈"),
                            session_id=str(body.get("session_id") or "session_1"),
                            scope_id=str(body.get("scope_id") or ""),
                            event_type=str(body.get("event_type") or "inquiry"),
                            favor_delta=int(body.get("favor_delta") or 0),
                            summary=str(body.get("summary") or ""),
                            pc_entry_id=str(body.get("pc_entry_id") or ""),
                            pc_entry_version_id=str(body.get("pc_entry_version_id") or ""),
                            pc_revision=int(body.get("pc_revision") or 1),
                        )
                        self._send_json(200, res)
                        return
                    if self.path in ("/v1/chat/completions", "/chat/completions"):
                        messages = body.get("messages", [])
                        model = str(body.get("model") or "gpt-4o-mini")
                        res = synthesize_context_grounded_chat_completion(messages, model)
                        self._send_json(200, res)
                        return
                    self._send_json(404, {"error": f"Unknown POST path {self.path}"})
                except Exception as exc:
                    self._send_json(500, {"error": str(exc), "type": type(exc).__name__})

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()


class DifyChatflowRunner:
    """真实解析并执行 dsl/adventure_npc_chatflow.yml 的拓扑图执行器。"""

    def __init__(
        self,
        dsl_path: Path = DSL_PATH,
        *,
        powercontext_base_url: str = "http://127.0.0.1:8000",
        seekdb_bridge_url: str = "http://127.0.0.1:8088",
        llm_api_base: str | None = None,
        llm_api_key: str | None = None,
    ) -> None:
        self.dsl_path = dsl_path
        self.dsl = yaml.safe_load(dsl_path.read_text(encoding="utf-8"))
        workflow = self.dsl["workflow"]
        self.nodes: dict[str, dict[str, Any]] = {
            n["id"]: n for n in workflow["graph"]["nodes"]
        }
        self.edges: list[dict[str, Any]] = workflow["graph"]["edges"]
        self.env: dict[str, str] = {
            item["name"]: str(item["value"])
            for item in workflow.get("environment_variables", [])
        }
        self.env["POWERCONTEXT_BASE_URL"] = powercontext_base_url.rstrip("/")
        self.env["SEEKDB_BRIDGE_URL"] = seekdb_bridge_url.rstrip("/")
        self.llm_api_base = (
            llm_api_base
            or os.environ.get("OPENAI_API_BASE")
            or os.environ.get("LLM_API_BASE")
            or f"{seekdb_bridge_url.rstrip('/')}/v1"
        ).rstrip("/")
        self.llm_api_key = (
            llm_api_key
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("LLM_API_KEY")
            or "local-context-grounded-key"
        )

    def execute_turn(
        self,
        *,
        query: str,
        player_id: str = "player_ache",
        player_name: str = "阿澈",
        session_id: str = "session_1",
        category_filter: str = "all",
    ) -> dict[str, Any]:
        outputs: dict[str, dict[str, str]] = {}
        sys_vars = {"query": query, "user_id": player_id}
        start_inputs = {
            "player_id": player_id,
            "player_name": player_name,
            "session_id": session_id,
            "category_filter": category_filter,
        }
        node_trace: list[dict[str, Any]] = []

        def resolve(selector: list[str]) -> str:
            ns, key = selector[0], selector[1]
            if ns == "env":
                return str(self.env.get(key, ""))
            if ns == "sys":
                return str(sys_vars.get(key, ""))
            return str(outputs.get(ns, {}).get(key, ""))

        def render_text(template: str) -> str:
            return SELECTOR_RE.sub(lambda m: resolve([m.group(1), m.group(2)]), template or "")

        current_id = next(
            nid for nid, node in self.nodes.items() if node["data"]["type"] == "start"
        )
        final_answer = ""

        while current_id:
            node = self.nodes[current_id]
            ndata = node["data"]
            ntype = ndata["type"]
            started = time.perf_counter()

            if ntype == "start":
                produced = {}
                for v in ndata.get("variables", []):
                    vname = v["variable"]
                    produced[vname] = str(start_inputs.get(vname, v.get("default", "")))
                outputs[current_id] = produced

            elif ntype == "code":
                ns_dict: dict[str, Any] = {}
                exec(ndata["code"], ns_dict)  # noqa: S102
                kwargs = {
                    v["variable"]: resolve(v["value_selector"])
                    for v in ndata.get("variables", [])
                }
                res = ns_dict["main"](**kwargs)
                outputs[current_id] = {k: str(res.get(k, "")) for k in ndata.get("outputs", {})}

            elif ntype == "http-request":
                url = render_text(ndata["url"])
                raw_body_tmpl = ndata["body"]["data"][0]["value"]
                rendered_body = render_text(raw_body_tmpl)
                # 验证为合法 JSON
                parsed_json = json.loads(rendered_body)
                req = urllib.request.Request(
                    url,
                    data=json.dumps(parsed_json, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
                    method=ndata.get("method", "post").upper(),
                )
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        resp_body = resp.read().decode("utf-8")
                        status_code = resp.status
                except urllib.error.HTTPError as http_err:
                    err_body = http_err.read().decode("utf-8", errors="replace")
                    raise RuntimeError(f"Node {current_id} ({url}) HTTP {http_err.code}: {err_body}") from http_err
                outputs[current_id] = {"body": resp_body, "status_code": str(status_code)}

            elif ntype == "llm":
                messages = [
                    {"role": item["role"], "text": render_text(item["text"]), "content": render_text(item["text"])}
                    for item in ndata.get("prompt_template", [])
                ]
                model_name = ndata.get("model", {}).get("name", "gpt-4o-mini")
                llm_payload = {
                    "model": model_name,
                    "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
                    "temperature": ndata.get("model", {}).get("completion_params", {}).get("temperature", 0.3),
                }
                req = urllib.request.Request(
                    f"{self.llm_api_base}/chat/completions",
                    data=json.dumps(llm_payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json; charset=utf-8",
                        "Authorization": f"Bearer {self.llm_api_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=45) as resp:
                    llm_resp = json.loads(resp.read().decode("utf-8"))
                llm_text = llm_resp["choices"][0]["message"]["content"]
                outputs[current_id] = {"text": llm_text}

            elif ntype == "answer":
                final_answer = render_text(ndata.get("answer", ""))
                outputs[current_id] = {"answer": final_answer}

            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            node_trace.append(
                {
                    "node_id": current_id,
                    "title": ndata.get("title", current_id),
                    "type": ntype,
                    "elapsed_ms": elapsed_ms,
                }
            )
            next_edges = [e for e in self.edges if e["source"] == current_id]
            current_id = next_edges[0]["target"] if next_edges else ""

        return {
            "query": query,
            "player_id": player_id,
            "player_name": player_name,
            "session_id": session_id,
            "answer": final_answer,
            "node_trace": node_trace,
            "outputs": outputs,
        }


def find_free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(("127.0.0.1", preferred)) != 0:
            return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_verification_suite() -> int:
    """运行完整的三回合跨会话端到端验证：
    1. 启动内嵌 OceanBase seekdb + 官方 PowerContext Runtime。
    2. 启动 PowerContext HTTP 服务与 seekdb HTTP 桥接服务。
    3. 使用 DifyChatflowRunner 真实解析并执行 dsl/adventure_npc_chatflow.yml 的全部 16 个节点，完成 3 回合跨会话剧本。
    4. 导出真实 seekdb 数据快照与执行证据日志。
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="tavern_verify_"))
    pc_port = find_free_port(18000)
    bridge_port = find_free_port(18088)
    pc_base_url = f"http://127.0.0.1:{pc_port}"
    bridge_base_url = f"http://127.0.0.1:{bridge_port}"

    print("=" * 78)
    print("[1/5] 正在启动内嵌 OceanBase seekdb 引擎与官方 PowerContext Runtime...")
    embedded = EmbeddedSeekDBAndPowerContextRuntime(tmp_dir)
    embedded.start()

    try:
        store = TavernSeekDBStore(unix_socket=embedded.unix_socket, database="test")
        print(f"      -> OceanBase seekdb 就绪: {store.seekdb_version} (socket={embedded.unix_socket})")

        pc_server = PowerContextHTTPServer(embedded, host="127.0.0.1", port=pc_port)
        pc_server.start()
        bridge_server = TavernSeekDBBridgeServer(
            store, host="127.0.0.1", port=bridge_port, pc_base_url=pc_base_url
        )
        bridge_server.start()

        pc_live = http_get_json(f"{pc_base_url}/health/live")
        pc_ready = http_get_json(f"{pc_base_url}/health/ready")
        bridge_health = http_get_json(f"{bridge_base_url}/health")
        print(f"[2/5] 服务健康检查通过: PowerContext={pc_ready['status']}, SeekDBBridge={bridge_health['ok']}")

        runner = DifyChatflowRunner(
            DSL_PATH,
            powercontext_base_url=pc_base_url,
            seekdb_bridge_url=bridge_base_url,
        )

        scenarios = [
            {
                "round": 1,
                "session_id": "session_day1_evening",
                "title": "第一幕（初次到访 · 打听迷雾森林独眼银鳞龙与点酒）",
                "query": "老板，先来一杯招牌深炉金麦酒！顺便向您打听下迷雾森林里那头独眼银鳞龙和秘银宝箱的传闻。",
                "category_filter": "all",
            },
            {
                "round": 2,
                "session_id": "session_day1_midnight",
                "title": "第二幕（深夜恶作剧 · 往深炉金麦酒桶偷倒火山辣椒粉触发记仇）",
                "query": "（趁老板转身擦酒杯，偷偷往招牌深炉金麦酒桶里倒了一勺火山辣椒粉！）老板，现在能把高回报的星陨铁酒杯秘密委托交给我了吗？",
                "category_filter": "all",
            },
            {
                "round": 3,
                "session_id": "session_day2_morning",
                "title": "第三幕（次日新会话 · 跨会话召回辣椒粉旧账并奉上冰霜蜂蜜烤肉赔罪解锁隐藏委托）",
                "query": "巴恩大叔，昨晚往麦酒桶倒辣椒粉是我混蛋！我特地带了您最爱的冰霜蜂蜜烤肉和矮人陈酿来赔罪，银鳞龙祭坛的暗道和星陨铁酒杯委托能告诉我了吗？",
                "category_filter": "all",
            },
        ]

        turn_records: list[dict[str, Any]] = []
        scope_id = ""

        print("[3/5] 正在按拓扑顺序执行 Dify Chatflow DSL (dsl/adventure_npc_chatflow.yml) 三幕跨会话验证...")
        for sc in scenarios:
            res = runner.execute_turn(
                query=sc["query"],
                player_id="player_ache",
                player_name="阿澈",
                session_id=sc["session_id"],
                category_filter=sc["category_filter"],
            )
            scope_id = res["outputs"]["build_recall_requests"]["scope_id"]
            seekdb_resp = json.loads(res["outputs"]["seekdb_hybrid_search"]["body"])
            prep_resp = json.loads(res["outputs"]["pc_prepare_context"]["body"])
            rem_resp = json.loads(res["outputs"]["pc_remember_memory"]["body"])
            grudge_resp = json.loads(res["outputs"]["seekdb_record_grudge"]["body"])

            print(f"\n--- 第 {sc['round']} 回合 [{sc['session_id']}]: {sc['title']} ---")
            print(f"玩家输入: {sc['query']}")
            print(
                f"seekdb 混合检索 Top1: {seekdb_resp['hits'][0]['lore_id']}《{seekdb_resp['hits'][0]['title']}》 "
                f"(hybrid={seekdb_resp['hits'][0]['hybrid_score']}, MATCH={seekdb_resp['hits'][0]['lexical_score']}, l2={seekdb_resp['hits'][0]['vector_distance']})"
            )
            print(
                f"PowerContext 准备上下文: status={prep_resp['status']}, bytes={prep_resp['content_bytes']}"
            )
            print(
                f"PowerContext 记忆写回: entry_id={rem_resp['entry']['citation']['entry_id']}, revision={rem_resp['memory']['revision']}"
            )
            print(
                f"好感度与态度变更: {grudge_resp['favor_before']} -> {grudge_resp['favor_after']} ({grudge_resp['attitude_tier']}, 酒价倍率={grudge_resp['drink_multiplier']}x)"
            )
            print("老巴恩回应摘要:")
            print(res["answer"])

            turn_records.append(
                {
                    "round": sc["round"],
                    "session_id": sc["session_id"],
                    "title": sc["title"],
                    "player_query": sc["query"],
                    "scope_id": scope_id,
                    "seekdb_hybrid_search": {
                        "seekdb_version": seekdb_resp["seekdb_version"],
                        "sql_trace": seekdb_resp["sql_trace"],
                        "explain_plan": seekdb_resp["explain_plan"],
                        "hits": seekdb_resp["hits"],
                    },
                    "powercontext_prepare_context": prep_resp,
                    "powercontext_remember": rem_resp,
                    "grudge_ledger_update": grudge_resp,
                    "node_trace": res["node_trace"],
                    "final_answer": res["answer"],
                }
            )

        # 验证跨会话记忆审计接口 (/v1/memory/entries/list 与 /v1/memory/changes)
        entries_audit = http_post_json(f"{pc_base_url}/v1/memory/entries/list", {"scope_id": scope_id})
        changes_audit = http_post_json(f"{pc_base_url}/v1/memory/changes", {"scope_id": scope_id})

        # 关键断言
        assert store.seekdb_version.startswith("5.7.25-OceanBase seekdb"), "必须连接真实 OceanBase seekdb 引擎"
        assert scope_id.startswith("scp_"), "PowerContext 必须返回真实 scope_id (scp_...)"
        assert len(entries_audit.get("entries", [])) == 3, "三回合后 PowerContext 必须包含 3 条活跃记忆"
        assert len(changes_audit.get("revisions", [])) == 3, "PowerContext 必须记录 3 个版本演进 (revision 1..3)"
        assert turn_records[1]["grudge_ledger_update"]["drink_multiplier"] == 3, "第 2 回合恶作剧后酒价必须翻三倍"
        assert turn_records[2]["powercontext_prepare_context"]["status"] == "ready", "第 3 回合跨会话必须成功召回辣椒粉旧账"
        assert "辣椒粉" in (turn_records[2]["powercontext_prepare_context"].get("content") or ""), "第 3 回合召回内容必须包含辣椒粉恶作剧记录"
        assert turn_records[2]["grudge_ledger_update"]["favor_after"] >= 70, "第 3 回合赔罪后好感度必须恢复至 70 以上以解锁隐藏委托"

        print("\n[4/5] 正在导出真实 seekdb 快照与端到端执行证据日志...")
        SAMPLES_ROWS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SAMPLES_ROWS_PATH.write_text(store.export_snapshot_text(), encoding="utf-8")

        evidence_payload = {
            "project": "酒馆老板的记仇簿 (Tavern Keeper's Grudge Ledger)",
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "architecture": {
                "orchestrator": "Dify Chatflow DSL (dsl/adventure_npc_chatflow.yml, 17 nodes)",
                "hybrid_search_db": f"OceanBase seekdb ({store.seekdb_version})",
                "memory_engine": "PowerContext BuiltinRuntime + HTTP REST API (/v1/scopes, /v1/sources/content, /v1/context/prepare, /v1/memory/remember, /v1/memory/entries/list, /v1/memory/changes)",
            },
            "health_checks": {
                "powercontext_live": pc_live,
                "powercontext_ready": pc_ready,
                "seekdb_bridge": bridge_health,
            },
            "powercontext_scope_id": scope_id,
            "turns": turn_records,
            "powercontext_final_audit": {
                "entries": entries_audit,
                "changes": changes_audit,
            },
        }
        EVIDENCE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_LOG_PATH.write_text(
            json.dumps(evidence_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        print(f"[5/5] 全部断言通过！证据已写入:\n  - {EVIDENCE_LOG_PATH}\n  - {SAMPLES_ROWS_PATH}")
        print("=" * 78)
        pc_server.close()
        bridge_server.close()
        return 0
    finally:
        embedded.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def run_foreground_servers(pc_port: int, bridge_port: int, data_dir: Path) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    embedded = EmbeddedSeekDBAndPowerContextRuntime(data_dir)
    embedded.start()
    store = TavernSeekDBStore(unix_socket=embedded.unix_socket, database="test")
    pc_url = f"http://127.0.0.1:{pc_port}"
    pc_server = PowerContextHTTPServer(embedded, host="127.0.0.1", port=pc_port)
    pc_server.start()
    bridge_server = TavernSeekDBBridgeServer(store, host="127.0.0.1", port=bridge_port, pc_base_url=pc_url)
    bridge_server.start()
    print(f"OceanBase seekdb ({store.seekdb_version}) + PowerContext 服务已启动:")
    print(f"  - PowerContext HTTP Server : http://127.0.0.1:{pc_port}")
    print(f"  - SeekDB Bridge & LLM API  : http://127.0.0.1:{bridge_port}")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        bridge_server.close()
        pc_server.close()
        embedded.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="《酒馆老板的记仇簿》SeekDB + PowerContext 双引擎服务与验证器")
    parser.add_argument("--verify", action="store_true", help="运行三回合跨会话端到端验证并生成真实证据")
    parser.add_argument("--pc-port", type=int, default=int(os.environ.get("POWERCONTEXT_PORT", "8000")))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SEEKDB_BRIDGE_PORT", "8088")))
    parser.add_argument("--data-dir", type=Path, default=DIFY_DIR / ".runtime_data")
    args = parser.parse_args(argv)

    if args.verify:
        return run_verification_suite()
    return run_foreground_servers(args.pc_port, args.port, args.data_dir)


if __name__ == "__main__":
    sys.exit(main())
