"""Replay the 碎锚酒馆 · 记仇老板老巴恩 Chatflow graph: real PowerContext HTTP calls + code nodes.

Walks `dify/dsl/adventure_npc_chatflow.yml` from `start` to `answer`:
- `code` nodes are executed with `exec()` and their declared `value_selector` inputs;
- `http-request` nodes render `{{#...#}}` into their URL and JSON body and send real HTTP requests;
- `llm` nodes call an OpenAI-compatible API if `OPENAI_API_KEY` is set, or use the structured
  test fixture in `dify/samples/judge/*.json` for deterministic offline verification.

Usage:
    python dify/scripts/replay_chatflow.py --player shen
    python dify/scripts/replay_chatflow.py --player shen --query "上次往麦酒桶倒粗盐是我混账，今天拎来三坛陈年朗姆酒赔罪，酒钱双倍奉上。" --judge-file dify/samples/judge/favor.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent
DIFY_DIR = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pc_http_demo import ensure_powercontext_available  # noqa: E402

SELECTOR_RE = re.compile(r"\{\{#([^.#]+)\.([^#]+)#\}\}")

DEFAULT_JUDGE_JSON = json.dumps(
    {
        "kind": "offense",
        "deed": "冒险者在碎锚酒馆吧台前故意打翻橡木麦酒桶，往麦酒里倒了一大把粗盐还拒不赔钱",
        "quote": "我往你的橡木麦酒桶里倒了一把粗盐，一文钱也不赔，你能拿我怎样？",
        "delta": 25,
    },
    ensure_ascii=False,
)


def build_offline_narration(ledger_text: str, context: str, grudge: str, query: str) -> str:
    g_val = int(grudge or "0")
    has_salt = "粗盐" in ledger_text or "麦酒桶" in ledger_text or "粗盐" in context
    has_rum = "朗姆酒" in ledger_text or "朗姆酒" in context
    if g_val == 0 and "第一次来" in ledger_text:
        return (
            "（老巴恩用缺了口的铁钩重重砸在橡木吧台上，独眼里寒光一闪）"
            "好大的狗胆！头一回踏进我这碎锚酒馆，就敢往老子的橡木麦酒桶里撒粗盐？"
            "后厨的水手已经把生锈的鱼叉抄起来了，我这本《碎锚恩怨账本》给你记上头一笔血账！"
            "今天这五十枚金海妖的酒钱，你是现在掏，还是等下回连本带利一起算？\n\n"
            "1. 拍下双倍金币当场赔礼\n"
            "2. 嘴硬到底，扬长而去\n"
            "3. 点一杯苦艾酒压惊"
        )
    if has_salt and not has_rum:
        return (
            f"（老巴恩冷笑一声，翻开浸透酒渍的账本，铁钩尖死死点在上一行字上，当前怨气 {g_val}/100）"
            "你还敢进碎锚酒馆的门？上回你往我的橡木麦酒桶里倒了一大把粗盐、一文钱不赔就溜号的账，墨迹还没干呢！"
            "怎么，今天拎着酒过来，是打算把那桶麦酒的旧账清了，还是又想耍什么新花样？\n\n"
            "1. 奉上三坛陈年朗姆酒赔罪\n"
            "2. 帮忙加固酒馆漏风的船板\n"
            "3. 打听暴风海黑帆船长的下落"
        )
    return (
        f"（老巴恩哼了一声，合上恩怨账本，往你面前推了一碟海盐烤鱼干，当前怨气 {g_val}/100）"
        "之前你往橡木麦酒桶撒粗盐那笔烂账，看在你后来拎来三坛陈年朗姆酒赔罪的份上，老子暂且不多计较。"
        "这碎锚酒馆在码头立了四十二年，什么海盗怪客没见过？想知道暴风海的秘密，先把杯里的酒干了！\n\n"
        "1. 追问黑帆船长的沉船海图\n"
        "2. 再续一壶热朗姆酒\n"
        "3. 查看自己在酒馆的恩怨账目"
    )


class Replay:
    def __init__(self, dsl: dict[str, Any], base_url: str, judge_text: str) -> None:
        workflow = dsl["workflow"]
        self.nodes = {node["id"]: node for node in workflow["graph"]["nodes"]}
        self.edges = workflow["graph"]["edges"]
        self.env = {item["name"]: item["value"] for item in workflow.get("environment_variables") or []}
        self.env["pc_base_url"] = ensure_powercontext_available(base_url).rstrip("/")
        self.outputs: dict[str, dict[str, Any]] = {}
        self.sys: dict[str, str] = {}
        self.judge_text = judge_text

    def resolve(self, selector: list[str]) -> str:
        scope, name = selector[0], selector[1]
        if scope == "env":
            return str(self.env[name])
        if scope == "sys":
            return str(self.sys.get(name, ""))
        return str(self.outputs[scope][name])

    def render_text(self, template: str) -> str:
        return SELECTOR_RE.sub(lambda m: self.resolve([m.group(1), m.group(2)]), template)

    def render_json_body(self, template: str) -> Any:
        chunks: list[str] = []
        position = 0
        for match in SELECTOR_RE.finditer(template):
            chunks.append(template[position : match.start()])
            prefix = "".join(chunks)
            value = self.resolve([match.group(1), match.group(2)])
            if prefix.count('"') % 2 == 1:
                chunks.append(json.dumps(value, ensure_ascii=False)[1:-1])
            else:
                try:
                    json.loads(value)
                except ValueError:
                    chunks.append(json.dumps(value, ensure_ascii=False))
                else:
                    chunks.append(value)
            position = match.end()
        chunks.append(template[position:])
        return json.loads("".join(chunks))

    def run_code(self, node: dict[str, Any]) -> dict[str, str]:
        data = node["data"]
        namespace: dict[str, Any] = {}
        exec(data["code"], namespace)  # noqa: S102
        kwargs = {
            item["variable"]: self.resolve(item["value_selector"]) for item in data["variables"]
        }
        produced = namespace["main"](**kwargs)
        return {key: str(produced.get(key, "")) for key in data["outputs"]}

    def run_http(self, node: dict[str, Any]) -> dict[str, str]:
        data = node["data"]
        url = self.render_text(data["url"])
        payload = self.render_json_body(data["body"]["data"][0]["value"])
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=data["method"].upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                status_code = response.status
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            if data.get("error_strategy") == "default-value":
                defaults = {item["key"]: item["value"] for item in data.get("default_value") or []}
                return {
                    "body": str(defaults.get("body", "{}")),
                    "status_code": str(defaults.get("status_code", 0)),
                    "headers": str(defaults.get("headers", "{}")),
                }
            raise SystemExit(f"{node['id']} -> HTTP error: {error}") from error
        return {"body": body, "status_code": str(status_code)}

    def run_start(self, node: dict[str, Any]) -> dict[str, str]:
        return {
            item["variable"]: str(self.sys.get(item["variable"], ""))
            for item in node["data"]["variables"]
        }

    def run_llm(self, node: dict[str, Any]) -> dict[str, str]:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1").rstrip("/")
        if api_key:
            messages = [
                {"role": m["role"], "content": self.render_text(m["text"])}
                for m in node["data"].get("prompt_template") or []
            ]
            model_cfg = node["data"].get("model") or {}
            req_body = {
                "model": os.environ.get("OPENAI_MODEL", model_cfg.get("name", "gpt-4o-mini")),
                "messages": messages,
                "temperature": (model_cfg.get("completion_params") or {}).get("temperature", 0.7),
            }
            req = urllib.request.Request(
                f"{api_base}/chat/completions",
                data=json.dumps(req_body, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    res_json = json.loads(resp.read().decode("utf-8"))
                    content = res_json["choices"][0]["message"]["content"]
                    return {"text": content}
            except Exception:
                pass

        if node["id"] == "judge":
            return {"text": self.judge_text}
        panel_out = self.outputs.get("panel") or {}
        narration = build_offline_narration(
            ledger_text=panel_out.get("ledger_text", ""),
            context=panel_out.get("context", ""),
            grudge=panel_out.get("grudge", "0"),
            query=self.sys.get("query", ""),
        )
        return {"text": narration}

    def run_answer(self, node: dict[str, Any]) -> None:
        text = self.render_text(node["data"]["answer"])
        print("\n### answer\n" + text)
        if "恩怨账本" not in text:
            raise SystemExit("answer is missing the ledger panel")

    def start(self) -> tuple[str, str]:
        current = next(node_id for node_id, node in self.nodes.items() if node["data"]["type"] == "start")
        while True:
            node = self.nodes[current]
            kind = node["data"]["type"]
            print(f"\n### {current} ({kind})")
            if kind == "start":
                produced = self.run_start(node)
            elif kind == "code":
                produced = self.run_code(node)
            elif kind == "http-request":
                produced = self.run_http(node)
            elif kind == "llm":
                produced = self.run_llm(node)
            elif kind == "answer":
                self.run_answer(node)
                return current, "done"
            else:
                raise SystemExit(f"replay does not support node type {kind} ({current})")
            self.outputs[current] = produced
            for key, value in produced.items():
                print(f"  {key} = {value[:160]}")
            outgoing = [edge for edge in self.edges if edge["source"] == current]
            if not outgoing:
                raise SystemExit(f"{current} has no outgoing edge")
            current = outgoing[0]["target"]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsl", default=str(DIFY_DIR / "dsl" / "adventure_npc_chatflow.yml"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--player", default="shen")
    parser.add_argument("--user-id", default="dify-replay-user", help="stand-in for sys.user_id when --player is empty")
    parser.add_argument("--query", default="我往你的橡木麦酒桶里倒了一把粗盐，一文钱也不赔，你能拿我怎样？")
    parser.add_argument("--judge", default=DEFAULT_JUDGE_JSON, help="canned judge JSON")
    parser.add_argument("--judge-file", help="read the canned judge JSON from a file")
    args = parser.parse_args(argv[1:])

    judge_text = Path(args.judge_file).read_text(encoding="utf-8") if args.judge_file else args.judge
    dsl = yaml.safe_load(Path(args.dsl).read_text(encoding="utf-8"))
    replay = Replay(dsl, args.base_url, judge_text=judge_text)
    replay.sys = {"query": args.query, "user_id": args.user_id, "player_id": args.player}
    replay.start()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
