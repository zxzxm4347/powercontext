"""Static validator for the 碎锚酒馆 · 记仇老板老巴恩 Dify Chatflow DSL.

Validates Dify 1.9.2+ workflow entity rules, node geometry, environment variables,
JSON body template substitution, Python AST main() parameter/return key alignment,
and upstream selector reachability.

Usage:
    python dify/scripts/validate_dsl.py [dify/dsl/adventure_npc_chatflow.yml]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

DEFAULT_DSL = Path(__file__).resolve().parents[1] / "dsl" / "adventure_npc_chatflow.yml"

NODE_TYPES = {
    "start",
    "code",
    "http-request",
    "llm",
    "answer",
    "if-else",
    "template-transform",
    "variable-aggregator",
    "question-classifier",
    "end",
    "iteration",
    "loop",
    "tool",
    "knowledge-retrieval",
    "parameter-extractor",
    "document-extractor",
    "assigner",
}
START_VARIABLE_TYPES = {
    "text-input",
    "paragraph",
    "select",
    "number",
    "checkbox",
    "file",
    "file-list",
    "external_data_tool",
}
SELECTOR_RE = re.compile(r"\{\{#([^#]+)#\}\}")
SYS_VARS = {
    "query",
    "files",
    "conversation_id",
    "user_id",
    "dialogue_count",
    "app_id",
    "workflow_id",
    "workflow_run_id",
}
CODE_OUTPUT_TYPES = {
    "string",
    "number",
    "object",
    "boolean",
    "array[string]",
    "array[number]",
    "array[object]",
    "array[boolean]",
}
FILE_TYPES = {"image", "document", "audio", "video", "custom"}
FILE_TRANSFER_METHODS = {"remote_url", "local_file", "tool_file", "datasource_file"}
ENV_VALUE_TYPES = {
    "string",
    "secret",
    "number",
    "integer",
    "float",
    "boolean",
    "object",
    "array[string]",
    "array[number]",
    "array[object]",
    "array[boolean]",
}


class Checker:
    def __init__(self, document: dict[str, Any], path: Path) -> None:
        self.doc = document
        self.path = path
        self.errors: list[str] = []
        self.warnings: list[str] = []
        workflow = document.get("workflow") or {}
        self.nodes = workflow.get("graph", {}).get("nodes", []) or []
        self.edges = workflow.get("graph", {}).get("edges", []) or []
        self.by_id = {node.get("id"): node for node in self.nodes}
        self.env_names = {item.get("name") for item in workflow.get("environment_variables") or []}
        self.kinds: dict[str, int] = {}

    def err(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def data(self, node: dict[str, Any]) -> dict[str, Any]:
        return node.get("data") or {}

    def node_name(self, node_id: str) -> str:
        node = self.by_id.get(node_id) or {}
        return f"{node_id}({self.data(node).get('title', '?')})"

    def output_names(self, node_id: str) -> set[str]:
        node = self.by_id.get(node_id) or {}
        data = self.data(node)
        kind = data.get("type")
        if kind == "start":
            return {item.get("variable") for item in data.get("variables") or []}
        if kind == "code":
            return set((data.get("outputs") or {}).keys())
        if kind == "http-request":
            return {"body", "status_code", "headers"}
        if kind == "llm":
            return {"text", "usage", "reasoning_content"}
        if kind == "answer":
            return {"answer"}
        return set()

    def check_document(self) -> None:
        for key in ("app", "kind", "version", "workflow", "dependencies"):
            if key not in self.doc:
                self.err(f"missing top-level key: {key}")
        if self.doc.get("kind") != "app":
            self.err(f"kind must be 'app', got {self.doc.get('kind')!r}")
        app = self.doc.get("app") or {}
        if app.get("mode") != "advanced-chat":
            self.err(f"app.mode must be 'advanced-chat', got {app.get('mode')!r}")
        for key in ("name", "icon", "icon_background"):
            if not app.get(key):
                self.err(f"app.{key} is empty")
        version = str(self.doc.get("version") or "")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            self.err(f"version {version!r} is not major.minor.patch")
        self.check_viewport()
        self.check_features()
        self.check_environment_variables()

    def check_environment_variables(self) -> None:
        seen: set[str] = set()
        for item in (self.doc.get("workflow") or {}).get("environment_variables") or []:
            name = item.get("name")
            if not name:
                self.err("environment variable without name")
                continue
            if name in seen:
                self.err(f"duplicate environment variable {name}")
            seen.add(name)
            if item.get("value_type") not in ENV_VALUE_TYPES:
                self.err(f"env.{name}: value_type {item.get('value_type')!r} is not accepted by Dify 1.9.2")
            if "value" not in item or item.get("value") is None:
                self.err(f"env.{name}: value missing")
            if item.get("value_type") == "string" and not isinstance(item.get("value"), str):
                self.err(f"env.{name}: string variable needs a string value")

    def check_viewport(self) -> None:
        viewport = (self.doc.get("workflow") or {}).get("graph", {}).get("viewport")
        if not isinstance(viewport, dict):
            self.warn("workflow.graph.viewport missing; Dify falls back to the default camera")
            return
        for key in ("x", "y", "zoom"):
            if not isinstance(viewport.get(key), (int, float)):
                self.err(f"workflow.graph.viewport.{key} must be a number")

    def check_features(self) -> None:
        features = (self.doc.get("workflow") or {}).get("features") or {}
        for key in (
            "file_upload",
            "opening_statement",
            "retriever_resource",
            "sensitive_word_avoidance",
            "speech_to_text",
            "suggested_questions",
            "suggested_questions_after_answer",
            "text_to_speech",
        ):
            if key not in features:
                self.warn(f"features.{key} missing; Dify fills a default on import")
        if not features.get("opening_statement"):
            self.warn("features.opening_statement is empty; the app opens with a blank page")
        file_upload = features.get("file_upload")
        if file_upload is not None:
            if not isinstance(file_upload, dict):
                self.err("features.file_upload must be an object")
            else:
                for file_type in file_upload.get("allowed_file_types") or []:
                    if file_type not in FILE_TYPES:
                        self.err(f"features.file_upload.allowed_file_types has unknown value {file_type!r}")
                for method in file_upload.get("allowed_file_upload_methods") or []:
                    if method not in FILE_TRANSFER_METHODS:
                        self.err(f"features.file_upload.allowed_file_upload_methods has unknown value {method!r}")
        for key in ("suggested_questions",):
            questions = features.get(key)
            if questions is not None and not (
                isinstance(questions, list) and all(isinstance(item, str) for item in questions)
            ):
                self.err(f"features.{key} must be a list of strings")

    def check_nodes(self) -> None:
        if not self.nodes:
            self.err("workflow.graph.nodes is empty")
        seen: set[str] = set()
        kinds: dict[str, int] = {}
        for node in self.nodes:
            node_id = node.get("id")
            if not node_id:
                self.err("node without id")
                continue
            if node_id in seen:
                self.err(f"duplicate node id: {node_id}")
            seen.add(node_id)
            data = self.data(node)
            kind = data.get("type")
            if not kind:
                self.err(f"{node_id}: data.type missing")
                continue
            if kind not in NODE_TYPES:
                self.err(f"{node_id}: unknown node type {kind!r}")
            kinds[kind] = kinds.get(kind, 0) + 1
            if not data.get("title"):
                self.err(f"{node_id}: data.title is empty")
            for key in ("position", "positionAbsolute"):
                if not isinstance(node.get(key), dict):
                    self.err(f"{node_id}: {key} missing")
            getattr(self, f"check_{kind.replace('-', '_')}")(node_id, data)
        if kinds.get("start") != 1:
            self.err(f"expected exactly one start node, found {kinds.get('start', 0)}")
        if kinds.get("answer") != 1:
            self.warn(f"expected exactly one answer node, found {kinds.get('answer', 0)}")
        self.kinds = kinds

    def check_start(self, node_id: str, data: dict[str, Any]) -> None:
        seen: set[str] = set()
        for variable in data.get("variables") or []:
            name = variable.get("variable")
            if not name:
                self.err(f"{node_id}: start variable without name")
                continue
            if name in seen:
                self.err(f"{node_id}: duplicate start variable {name}")
            seen.add(name)
            if not variable.get("label"):
                self.err(f"{node_id}.{name}: label missing (required by Dify)")
            if variable.get("type") not in START_VARIABLE_TYPES:
                self.err(f"{node_id}.{name}: unsupported type {variable.get('type')!r}")
            if variable.get("type") in {"text-input", "paragraph"} and not variable.get("max_length"):
                self.warn(f"{node_id}.{name}: max_length unset")

    def check_code(self, node_id: str, data: dict[str, Any]) -> None:
        if data.get("code_language") not in {"python3", "javascript"}:
            self.err(f"{node_id}: code_language must be python3/javascript")
        code = data.get("code") or ""
        if not code.strip():
            self.err(f"{node_id}: code is empty")
        outputs = data.get("outputs") or {}
        if not outputs:
            self.err(f"{node_id}: outputs is empty")
        for name, spec in outputs.items():
            spec = spec or {}
            if spec.get("type") not in CODE_OUTPUT_TYPES:
                self.err(
                    f"{node_id}.{name}: output type {spec.get('type')!r} is not allowed for a Dify code node "
                    f"(allowed: {sorted(CODE_OUTPUT_TYPES)})"
                )
            children = spec.get("children")
            if children is not None and not isinstance(children, dict):
                self.err(f"{node_id}.{name}: children must be null or an object")
        for item in data.get("variables") or []:
            if not item.get("variable"):
                self.err(f"{node_id}: input without variable name")
            selector = item.get("value_selector")
            if not (isinstance(selector, list) and len(selector) == 2):
                self.err(f"{node_id}: input {item.get('variable')!r} needs a 2-item value_selector")
        if data.get("code_language") == "python3":
            try:
                tree = ast.parse(code)
                funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
                if not funcs:
                    self.err(f"{node_id}: python3 code must define main(...)")
                else:
                    params = [arg.arg for arg in funcs[0].args.args]
                    declared_vars = [item.get("variable") for item in data.get("variables") or []]
                    if params != declared_vars:
                        self.err(f"{node_id}: main() params {params} != declared variables {declared_vars}")
                    ret_keys: set[str] = set()
                    for n in ast.walk(funcs[0]):
                        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
                            for k in n.value.keys:
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    ret_keys.add(k.value)
                    if ret_keys and ret_keys != set(outputs.keys()):
                        self.err(f"{node_id}: outputs {sorted(outputs.keys())} != main() return keys {sorted(ret_keys)}")
            except SyntaxError as exc:
                self.err(f"{node_id}: Python syntax error: {exc}")

    def check_http_request(self, node_id: str, data: dict[str, Any]) -> None:
        if not data.get("url"):
            self.err(f"{node_id}: url missing")
        if str(data.get("method", "")).lower() not in {"get", "post", "put", "patch", "delete", "head"}:
            self.err(f"{node_id}: bad method {data.get('method')!r}")
        authorization = data.get("authorization") or {}
        if authorization.get("type") not in {"no-auth", "api-key"}:
            self.err(f"{node_id}: authorization.type must be no-auth/api-key")
        body = data.get("body") or {}
        if body.get("type") not in {"none", "form-data", "x-www-form-urlencoded", "raw-text", "json", "binary"}:
            self.err(f"{node_id}: bad body.type {body.get('type')!r}")
        if body.get("type") == "json":
            for item in body.get("data") or []:
                payload = item.get("value") or ""
                payload = self.substitute_selectors(payload)
                try:
                    json.loads(payload)
                except Exception as exc:  # noqa: BLE001
                    self.err(f"{node_id}: body is not valid JSON after substitution: {exc}")
        if not isinstance(data.get("headers"), str):
            self.err(f"{node_id}: headers must be a string")
        if not isinstance(data.get("params"), str):
            self.err(f"{node_id}: params must be a string")
        if authorization.get("type") == "no-auth" and authorization.get("config") is not None:
            self.warn(f"{node_id}: no-auth node carries authorization.config; Dify forces it to null")
        for item in body.get("data") or []:
            if item.get("type") not in {"file", "text"}:
                self.err(f"{node_id}: body item type must be file/text")
            if not isinstance(item.get("key"), str):
                self.err(f"{node_id}: body item key must be a string")
            if item.get("type") == "text" and not isinstance(item.get("value"), str):
                self.err(f"{node_id}: body item value must be a string")
        timeout = data.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, dict):
                self.err(f"{node_id}: timeout must be an object")
            else:
                for key in ("connect", "read", "write"):
                    if key in timeout and not isinstance(timeout[key], int):
                        self.err(f"{node_id}: timeout.{key} must be an int")
        retry = data.get("retry_config") or {}
        if retry.get("max_retries") and not retry.get("retry_enabled"):
            self.warn(f"{node_id}: retries configured but retry_enabled is false")

    def check_llm(self, node_id: str, data: dict[str, Any]) -> None:
        model = data.get("model") or {}
        if not model.get("provider") or not model.get("name"):
            self.err(f"{node_id}: model.provider/name missing")
        if model.get("mode") != "chat":
            self.err(f"{node_id}: model.mode must be 'chat'")
        completion = model.get("completion_params")
        if completion is not None and not isinstance(completion, dict):
            self.err(f"{node_id}: model.completion_params must be an object")
        templates = data.get("prompt_template") or []
        if not templates:
            self.err(f"{node_id}: prompt_template is empty")
        for message in templates:
            if message.get("role") not in {"system", "user", "assistant", "tool"}:
                self.err(f"{node_id}: bad prompt role {message.get('role')!r}")
            if not (message.get("text") or "").strip():
                self.err(f"{node_id}: prompt message text is empty")
        if not any(message.get("role") == "system" for message in templates):
            self.warn(f"{node_id}: no system prompt")
        context = data.get("context")
        if not isinstance(context, dict) or "enabled" not in context:
            self.err(f"{node_id}: context is required by Dify 1.9.2")
        elif context.get("enabled"):
            selector = context.get("variable_selector")
            if not (isinstance(selector, list) and len(selector) == 2):
                self.err(f"{node_id}: context.enabled needs a 2-item variable_selector")
        vision = data.get("vision")
        if vision is not None and not isinstance(vision, dict):
            self.err(f"{node_id}: vision must be an object")

    def check_answer(self, node_id: str, data: dict[str, Any]) -> None:
        if not (data.get("answer") or "").strip():
            self.err(f"{node_id}: answer is empty")

    def check_edges(self) -> None:
        ids = set(self.by_id)
        seen: set[tuple[Any, Any, Any]] = set()
        for edge in self.edges:
            source, target = edge.get("source"), edge.get("target")
            if source not in ids:
                self.err(f"edge {edge.get('id')}: unknown source {source!r}")
            if target not in ids:
                self.err(f"edge {edge.get('id')}: unknown target {target!r}")
            if not edge.get("id"):
                self.err("edge without id")
            key = (source, edge.get("sourceHandle"), target)
            if key in seen:
                self.err(f"duplicate edge {source} -> {target}")
            seen.add(key)
            data = edge.get("data") or {}
            if source in self.by_id and data.get("sourceType"):
                actual = self.data(self.by_id[source]).get("type")
                if data["sourceType"] != actual:
                    self.err(f"edge {edge.get('id')}: sourceType {data['sourceType']!r} != {actual!r}")
            if target in self.by_id and data.get("targetType"):
                actual = self.data(self.by_id[target]).get("type")
                if data["targetType"] != actual:
                    self.err(f"edge {edge.get('id')}: targetType {data['targetType']!r} != {actual!r}")
        for node_id, node in self.by_id.items():
            kind = self.data(node).get("type")
            outgoing = [edge for edge in self.edges if edge.get("source") == node_id]
            incoming = [edge for edge in self.edges if edge.get("target") == node_id]
            if kind != "answer" and kind != "end" and not outgoing and self.kinds.get("answer"):
                self.warn(f"{node_id}: has no outgoing edge (its output is unused)")
            if kind != "start" and not incoming:
                self.err(f"{node_id}: unreachable, no incoming edge")

    def substitute_selectors(self, payload: str) -> str:
        out: list[str] = []
        position = 0
        for match in SELECTOR_RE.finditer(payload):
            out.append(payload[position : match.start()])
            prefix = "".join(out)
            in_string = prefix.count('"') % 2 == 1
            out.append("PLACEHOLDER" if in_string else '"PLACEHOLDER"')
            position = match.end()
        out.append(payload[position:])
        return "".join(out)

    def check_selectors(self) -> None:
        for node in self.nodes:
            node_id = node.get("id")
            text = yaml.safe_dump(self.data(node), allow_unicode=True)
            for raw in SELECTOR_RE.findall(text):
                parts = raw.split(".")
                if parts[0] in {"sys", "env", "conversation"}:
                    if parts[0] == "sys" and parts[-1] not in SYS_VARS:
                        self.err(f"{node_id}: unknown system variable {{#sys.{parts[-1]}#}}")
                    if parts[0] == "env" and parts[-1] not in self.env_names:
                        self.err(f"{node_id}: env variable {parts[-1]!r} is not declared")
                    continue
                if len(parts) < 2:
                    self.err(f"{node_id}: malformed selector {{#{raw}#}}")
                    continue
                if parts[0] not in self.by_id:
                    self.err(f"{node_id}: selector references unknown node {parts[0]!r}")
                    continue
                if parts[1] not in self.output_names(parts[0]):
                    self.err(f"{node_id}: {self.node_name(parts[0])} has no output {parts[1]!r}")
                    continue
                if not self.is_upstream(parts[0], node_id):
                    self.err(f"{node_id}: selector reads {self.node_name(parts[0])}, which is not upstream")
            for item in self.data(node).get("variables") or []:
                if self.data(node).get("type") == "start":
                    continue
                selector = item.get("value_selector")
                if not selector:
                    self.err(f"{node_id}: variable {item.get('variable')!r} has no value_selector")
                    continue
                if selector[0] == "sys":
                    if selector[1] not in SYS_VARS:
                        self.err(f"{node_id}: unknown system variable {selector}")
                elif selector[0] == "env":
                    if selector[1] not in self.env_names:
                        self.err(f"{node_id}: env variable {selector[1]!r} is not declared")
                elif selector[0] not in self.by_id:
                    self.err(f"{node_id}: value_selector references unknown node {selector[0]!r}")
                elif selector[1] not in self.output_names(selector[0]):
                    self.err(f"{node_id}: {selector[0]} has no output {selector[1]!r}")
                elif not self.is_upstream(selector[0], node_id):
                    self.err(f"{node_id}: value_selector reads non-upstream {self.node_name(selector[0])}")

    def is_upstream(self, candidate: str, node_id: str) -> bool:
        stack, visited = [node_id], set()
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            for edge in self.edges:
                if edge.get("target") == current:
                    parent = edge.get("source")
                    if parent == candidate:
                        return True
                    stack.append(parent)
        return False

    def check_reachability(self) -> None:
        starts = [n.get("id") for n in self.nodes if self.data(n).get("type") == "start"]
        if not starts:
            return
        reachable = {starts[0]}
        changed = True
        while changed:
            changed = False
            for edge in self.edges:
                if edge.get("source") in reachable and edge.get("target") not in reachable:
                    reachable.add(edge.get("target"))
                    changed = True
        for node in self.nodes:
            if node.get("id") not in reachable:
                self.err(f"{node.get('id')}: not reachable from start")

    def run(self) -> int:
        self.check_document()
        self.check_nodes()
        self.check_edges()
        self.check_selectors()
        self.check_reachability()
        return self.report()

    def report(self) -> int:
        print(f"DSL: {self.path}")
        print(f"nodes={len(self.nodes)} edges={len(self.edges)} type_counts={self.kinds}")
        for warning in self.warnings:
            print(f"WARN  {warning}")
        for error in self.errors:
            print(f"ERROR {error}")
        if self.errors:
            print(f"\nFAILED: {len(self.errors)} error(s), {len(self.warnings)} warning(s)")
            return 1
        print(f"\nOK: 0 errors, {len(self.warnings)} warning(s)")
        return 0


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) >= 2 else DEFAULT_DSL
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        print("document root must be a mapping")
        return 1
    return Checker(document, path).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
