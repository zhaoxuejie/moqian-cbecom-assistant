"""契约自检：`contracts/openapi.yaml` 必须是可解析、可解析引用、且守住两条链的边界。

这一组用例是「契约先行」的门禁：后端加接口不更新契约、或前端字段名漂移，
CI 会在 pytest 里红，而不是等到联调才发现。

检查项：
1. 文件编码/行尾（UTF-8 无 BOM、LF、无制表符）；
2. 元信息与基线绑定（openapi 3.1、开源项目标识、Apache-2.0 协议）；
3. 所有 $ref 都能解析；
4. 每个 operation 有唯一 operationId、summary、已声明的 tag、成功响应；
5. **OpenAPI 的 path 集合与 FastAPI 运行时路由集合双向一致**（少了/多了都红）；
6. `/chat` 的 SSE 契约：事件名集合与文档一致，载荷 schema 都在 components 里；
7. 双链隔离：`/admin/*` 不接受 widget 凭证，`/widget/*`、`/chat` 不接受管理员凭证；
8. 治理域接口都声明了 401（有鉴权就必须写清失败态）。

原制造业版本的第 5 项是「原型映射表接口不漏项」（读 prototype-mapping.md），
跨境项目不 fork 原型，改为直接和 FastAPI 路由表对账，见 `test_contract_covers_app_routes`。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from openapi_spec_validator import validate as validate_openapi

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "openapi.yaml"

HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}

# widget-interaction-spec §10 规定的事件名，一个都不能少、也不许自造
# （refused 是演示站额度用完的收尾事件，只在 DEMO_ENABLED 时出现）
SSE_EVENTS = {
    "structured",
    "conflict",
    "ask",
    "tool",
    "steps",
    "citations",
    "done",
    "gap",
    "degraded",
    "safe",
    "handoff",
    "refused",
    "error",
}

# prototype-mapping.md 列出的"主要接口"（页面 → 接口不漏项）
EXPECTED_OPERATIONS = {
    ("get", "/health"),
    ("get", "/health/db"),
    # widget 链
    ("get", "/widget/config"),
    ("post", "/widget/session"),
    ("post", "/chat"),
    ("get", "/widget/evidence/{evidenceId}"),
    ("post", "/widget/feedback"),
    ("post", "/widget/handoff"),
    # 后台认证与个人域
    ("post", "/admin/auth/captcha"),
    ("post", "/admin/auth/login"),
    ("get", "/admin/me"),
    ("put", "/admin/me/password"),
    ("get", "/admin/me/messages"),
    ("get", "/admin/me/saved"),
    # 知识源 / 切片 / 任务
    ("get", "/admin/documents"),
    ("post", "/admin/documents"),
    ("get", "/admin/documents/{documentId}"),
    ("patch", "/admin/documents/{documentId}"),
    ("post", "/admin/documents/{documentId}/enable"),
    ("get", "/admin/documents/{documentId}/chunks"),
    ("get", "/admin/documents/{documentId}/versions"),
    ("patch", "/admin/chunks/{chunkId}"),
    ("get", "/admin/jobs"),
    ("patch", "/admin/jobs/{jobId}"),
    ("post", "/admin/jobs/{jobId}/retry"),
    # 治理
    ("get", "/admin/settlements"),
    ("post", "/admin/settlements/from-ticket"),
    ("post", "/admin/settlements/{settlementId}/confirm"),
    ("post", "/admin/settlements/{settlementId}/reject"),
    ("get", "/admin/messages"),
    ("get", "/admin/messages/{messageId}/chain"),
    ("get", "/admin/gaps"),
    ("patch", "/admin/gaps/{gapId}"),
    ("get", "/admin/users"),
    ("post", "/admin/users"),
    ("patch", "/admin/users/{userId}"),
    ("delete", "/admin/users/{userId}"),
    ("post", "/admin/users/{userId}/reset-password"),
    ("get", "/admin/identity/pending"),
    ("post", "/admin/identity/pending/{account}/activate"),
    # 统计
    ("get", "/admin/stats/overview"),
    ("get", "/admin/stats/north-star"),
    ("get", "/admin/stats/engineers"),
    ("get", "/admin/stats/recognizability"),
    ("get", "/admin/reports/weekly"),
    # 配置 / 主题 / 评测
    ("get", "/admin/config"),
    ("put", "/admin/config"),
    ("post", "/admin/config/test-llm"),
    ("put", "/admin/theme"),
    ("get", "/admin/evals"),
    ("post", "/admin/evals/run"),
    ("get", "/admin/evals/{runId}"),
}


@pytest.fixture(scope="module")
def raw() -> str:
    return CONTRACT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def spec(raw: str) -> dict[str, Any]:
    loaded = yaml.safe_load(raw)
    assert isinstance(loaded, dict), "openapi.yaml 顶层必须是对象"
    return loaded


def iter_operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in spec.get("paths", {}).items():
        for method, op in item.items():
            if method in HTTP_METHODS:
                yield method, path, op


def iter_refs(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from iter_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_refs(value)


def resolve_ref(spec: dict[str, Any], ref: str) -> Any:
    assert ref.startswith("#/"), f"只支持本地引用：{ref}"
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            raise AssertionError(f"引用无法解析：{ref}（缺 {part}）")
        node = node[part]
    return node


def test_contract_encoding(raw: str) -> None:
    """契约文件必须是 UTF-8 无 BOM、LF 行尾、无制表符（AGENTS.md §5 编码约定）。"""
    assert not raw.startswith("\ufeff"), "不要带 BOM"
    assert "\t" not in raw, "YAML 里禁止制表符（缩进必须空格）"
    assert "\r\n" not in raw, "必须 LF 行尾"


def test_contract_is_valid_openapi(spec: dict[str, Any]) -> None:
    """过一遍官方 OpenAPI 3.1 校验器，别名写错、类型非法都会在这里暴露。"""
    validate_openapi(spec)


def test_contract_metadata(spec: dict[str, Any]) -> None:
    assert str(spec["openapi"]).startswith("3.1"), "契约用 OpenAPI 3.1"
    assert spec["info"]["x-baseline"] == "cbecom-open-source"
    assert spec["info"]["x-status"] == "open-source"
    assert spec["info"]["license"]["name"] == "Apache-2.0"
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert spec["info"]["version"] == declared["project"]["version"]


def test_all_refs_resolve(spec: dict[str, Any]) -> None:
    refs = list(iter_refs(spec))
    assert refs, "契约里应该用 $ref 组织 schema"
    for ref in refs:
        resolve_ref(spec, ref)


def test_operations_have_identity(spec: dict[str, Any]) -> None:
    declared_tags = {tag["name"] for tag in spec["tags"]}
    seen_ids: dict[str, str] = {}
    for method, path, op in iter_operations(spec):
        where = f"{method.upper()} {path}"
        assert op.get("operationId"), f"{where} 缺 operationId"
        assert op.get("summary"), f"{where} 缺 summary"
        assert op.get("tags"), f"{where} 缺 tags"
        for tag in op["tags"]:
            assert tag in declared_tags, f"{where} 用了未声明的 tag：{tag}"
        assert op.get("responses"), f"{where} 缺 responses"
        success = [code for code in op["responses"] if code.startswith("2")]
        assert success, f"{where} 没有成功响应"
        op_id = op["operationId"]
        assert op_id not in seen_ids, f"operationId 重复：{op_id}"
        seen_ids[op_id] = where


def test_mapping_endpoints_covered(spec: dict[str, Any]) -> None:
    """映射表里的接口一个都不能缺，否则页面开发时才会发现后端没这接口。"""
    present = {(method, path) for method, path, _ in iter_operations(spec)}
    missing = EXPECTED_OPERATIONS - present
    assert not missing, f"映射表接口未落到契约：{sorted(missing)}"


def test_contract_covers_app_routes(spec: dict[str, Any]) -> None:
    """契约与运行时路由必须双向一致。

    为什么不用「照抄一份期望清单」：那份清单会和代码一起腐烂。
    直接对账 FastAPI 的路由表，任何一边新增/删除接口都会在这里红：

    - 代码加了路由、契约没加 → 前端拿不到 schema，联调才发现；
    - 契约加了路径、代码没实现 → 前端照着写了调用，运行时 404。

    `include_in_schema=False` 的路由（静态站重定向、favicon 等）不参与比对。

    用 `app.openapi()` 而不是遍历 `app.routes`：新版 FastAPI 会把 include_router 的
    结果包成 `_IncludedRouter`，不再摊平成 APIRoute；生成的 schema 才是运行时真相。
    """
    from app.main import create_app

    generated = create_app().openapi()
    runtime_ops: set[tuple[str, str]] = set()
    for path, item in generated.get("paths", {}).items():
        for method in item:
            if method in HTTP_METHODS:
                runtime_ops.add((method, path))

    contract_ops = {(method, path) for method, path, _ in iter_operations(spec)}

    # 路径参数名允许不同：FastAPI 源码用 snake_case（{document_id}），
    # 契约对外统一 camelCase（{documentId}，前端直接用）。比对时只比路径形状。
    def shape(path: str) -> str:
        return re.sub(r"\{[^}]+\}", "{}", path)

    runtime_ops = {(method, shape(path)) for method, path in runtime_ops}
    contract_ops = {(method, shape(path)) for method, path in contract_ops}

    missing_in_contract = sorted(runtime_ops - contract_ops)
    missing_in_app = sorted(contract_ops - runtime_ops)
    assert not missing_in_contract, f"代码有路由但契约没写：{missing_in_contract}"
    assert not missing_in_app, f"契约写了但代码没实现：{missing_in_app}"


def test_paths_are_grouped(spec: dict[str, Any]) -> None:
    prefixes = ("/health", "/widget/", "/chat", "/admin/")
    for _, path, _ in iter_operations(spec):
        assert path.startswith(prefixes), f"路径前缀不在约定分组内：{path}"


def test_sse_contract(spec: dict[str, Any]) -> None:
    op = spec["paths"]["/chat"]["post"]
    content = op["responses"]["200"]["content"]
    assert "text/event-stream" in content, "/chat 必须是 SSE"

    events = op.get("x-sse-events")
    assert isinstance(events, dict), "/chat 必须声明 x-sse-events"
    assert set(events) == SSE_EVENTS, f"事件名与 widget spec §10 不一致：{set(events) ^ SSE_EVENTS}"
    for event, ref in events.items():
        schema = resolve_ref(spec, ref)
        assert isinstance(schema, dict), f"事件 {event} 的载荷 schema 应为对象"
        assert schema.get("type") == "object"
        assert "properties" in schema or "allOf" in schema, f"事件 {event} 没有字段定义"

    # 步骤事件必须自带证据 ID（widget spec §10：每一步必须携带自己的证据）
    steps = spec["components"]["schemas"]["SseSteps"]
    assert "citations" in steps["properties"]
    assert steps["properties"]["citations"]["items"]["type"] == "string"

    # 追问卡必须给至少两个选项（否则不算"条件不足"）
    ask = spec["components"]["schemas"]["SseAsk"]["properties"]["ask"]["properties"]["options"]
    assert ask["minItems"] >= 2


def test_security_double_chain_isolation(spec: dict[str, Any]) -> None:
    global_security = spec.get("security", [])
    admin_schemes = set()
    widget_schemes = set()

    for method, path, op in iter_operations(spec):
        security = op["security"] if "security" in op else global_security
        schemes = {next(iter(entry)) for entry in security if entry}
        where = f"{method.upper()} {path}"
        if path.startswith("/admin/"):
            assert schemes <= {"adminBearer"}, f"{where} 只接受治理域凭证，实际：{schemes}"
            admin_schemes |= schemes
        else:
            assert schemes <= {"widgetSession"}, f"{where} 只接受工程师域凭证，实际：{schemes}"
            widget_schemes |= schemes

    assert admin_schemes == {"adminBearer"}
    assert widget_schemes == {"widgetSession"}

    # 双链隔离要求两个凭证方案是两件不同的事，不能共用一个
    schemes = spec["components"]["securitySchemes"]
    assert set(schemes) == {"adminBearer", "widgetSession"}

    # 无状态入口（登录、验证码、会话建立、配置、探活）必须显式声明 security: []
    for method, path in [
        ("get", "/widget/config"),
        ("post", "/widget/session"),
        ("post", "/admin/auth/login"),
        ("post", "/admin/auth/captcha"),
        ("get", "/health"),
        ("get", "/health/db"),
    ]:
        op = spec["paths"][path][method]
        assert op.get("security") == [], f"{method.upper()} {path} 应显式声明 security: []"

    # 问答主链路必须带 widget 会话（未识别也要有会话，统计才有归属）
    chat = spec["paths"]["/chat"]["post"]
    assert chat["security"] == [{"widgetSession": []}]


def test_admin_operations_document_401(spec: dict[str, Any]) -> None:
    """有鉴权的治理接口必须写清 401；否则前端不知道要跳登录。"""
    for method, path, op in iter_operations(spec):
        if not path.startswith("/admin/") or op.get("security") == []:
            continue
        assert "401" in op["responses"], f"{method.upper()} {path} 缺 401 响应"


def test_schema_naming_is_consistent(spec: dict[str, Any]) -> None:
    """schema 名 PascalCase、字段 camelCase；SSE 事件统一 Sse 前缀。"""
    for name in spec["components"]["schemas"]:
        assert re.fullmatch(r"Sse[A-Z]\w+|[A-Z]\w+", name), f"schema 命名不规范：{name}"
        if name.startswith("Sse"):
            assert name[3].isupper()
    for name, schema in spec["components"]["schemas"].items():
        for prop in (schema.get("properties") or {}):
            assert re.fullmatch(r"[a-z][A-Za-z0-9]*", prop), f"{name}.{prop} 应为 camelCase"
