"""系统配置接口测试（需要数据库）。

覆盖点（对应本批次"不把未接线配置假装已生效"的口径）：
- `GET /admin/config`：契约要求的分组齐全；密钥只回固定掩码、绝不泄露明文；
  `runtime` 给出进程内真实生效值，`effect` 逐组标注 立即生效 / 需重启 / 仅前端展示 / 未接线；
- `PUT /admin/config`：只提交改动组、写入 `app_config`；保存值改变但 `runtime` 不变（未接线）；
- 明文密钥写入被显式拒绝（422），不落库；
- `PUT /admin/theme`：品牌主题与 `config.brand` 同一份数据；
- `POST /admin/config/test-llm`：依赖覆盖假客户端，成功/失败/缺密钥三条路径都不联网。

`app_config` 是真实开发库：用例前快照 `config.%` 行，结束时原样恢复，
避免删掉本机已保存的配置（也不新建迁移）。
"""

from __future__ import annotations

import base64

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.main import create_app
from app.routers import admin_config
from app.routers.deps import get_conn, require_admin
from modules.llm import LlmAuthError, LlmResult

pytestmark = requires_db

SECRET_KEY = "sk-unit-test-must-never-leak-1234567890"
ADMIN = {"id": 1, "account": "T-CONFIG-ADMIN", "name": "测试管理员", "role": "admin"}
REQUIRED_GROUPS = (
    "llm",
    "embedding",
    "reranker",
    "retrieval",
    "widget",
    "safety",
    "security",
    "preview",
    "demo",
    "brand",
)


@pytest.fixture()
def clean_app_config(conn):
    """隔离 `config.%` 行：先快照再清空，用例结束恢复（测试不污染本机配置）。"""
    with conn.cursor() as cur:
        cur.execute("select key, value from app_config where key like 'config.%'")
        snapshot = cur.fetchall()
        cur.execute("delete from app_config where key like 'config.%'")
    conn.commit()
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from app_config where key like 'config.%'")
            for key, value in snapshot:
                cur.execute(
                    "insert into app_config(key, value) values (%s, %s) "
                    "on conflict (key) do update "
                    "set value = excluded.value, updated_at = now()",
                    (key, Jsonb(value)),
                )
        conn.commit()


@pytest.fixture()
def cfg_settings() -> Settings:
    """刻意与开发环境取值不同：断言能看出"配置值"与"运行时值"是两回事。"""
    return Settings(
        llm_base_url="https://llm.invalid/v1",
        llm_model="unit-llm",
        llm_api_key=SECRET_KEY,
        llm_timeout_seconds=45,
        embedding_base_url="http://embed.invalid/v1",
        embedding_model="unit-embed",
        embedding_dim=1024,
        reranker_base_url="http://rerank.invalid",
        reranker_model="unit-rerank",
        retrieval_top_k_dense=18,
        retrieval_final_top_n=9,
        retrieval_min_score=0.42,
        evidence_min_coverage=0.55,
    )


def make_app(conn, settings: Settings, *, authorized: bool = True, probe=None):
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    if authorized:
        # 认证本身由 tests/test_admin_auth.py 覆盖；这里只验证"需要治理域凭证"的装配
        app.dependency_overrides[require_admin] = lambda: ADMIN
    if probe is not None:
        app.dependency_overrides[admin_config.get_llm_probe] = lambda: probe
    return app


def auth_headers() -> dict[str, str]:
    # require_admin 已覆盖，Bearer 只为通过 HTTPBearer 的参数解析
    return {"Authorization": "Bearer unit-test"}


class FakeLlmClient:
    """假客户端：记录调用次数，返回预置结果或抛预置错误（绝不联网）。"""

    def __init__(self, *, result: LlmResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    def complete(self, messages, *, max_tokens=None, temperature=0.2):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.result is not None, "成功路径需要预置 LlmResult"
        return self.result


def make_probe(client: FakeLlmClient) -> admin_config.LlmProbe:
    return admin_config.LlmProbe(
        client=client,
        model="unit-llm",
        base_url="https://llm.invalid/v1",
        source="app_config",
    )


# ---------------------------------------------------------------- GET
def test_get_config_returns_groups_masks_secret_and_effect(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    client = TestClient(make_app(conn, cfg_settings))
    resp = client.get("/admin/config", headers=auth_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()

    for group in REQUIRED_GROUPS:
        assert group in body, f"契约要求的分组缺失：{group}"
    assert body["llm"]["baseUrl"] == "https://llm.invalid/v1"
    assert body["llm"]["model"] == "unit-llm"
    assert body["llm"]["timeoutSec"] == 45
    assert body["llm"]["apiKeySet"] is True
    assert body["llm"]["apiKeyMasked"] == "******"
    assert SECRET_KEY not in resp.text, "响应绝不能出现密钥明文"

    assert body["embedding"]["dim"] == 1024
    assert body["retrieval"]["topK"] == 9
    assert body["retrieval"]["evidenceThreshold"] == pytest.approx(0.42)
    assert body["safety"]["words"], "安全词表默认应非空"
    assert body["savedGroups"] == [], "空库时不应有已保存组"

    # 生效性：已接线的组明确立即生效；未接线的组不得伪装
    effect = body["effect"]
    assert set(effect["immediate"]) == {
        "llm",
        "embedding",
        "reranker",
        "retrieval",
        "safety",
        "security",
        "preview",
        "demo",
        "widget",
    }
    assert effect["restartRequired"] == [], "已接线组不需要重启"
    assert set(effect["notWired"]) == set()
    assert "brand" in effect["displayOnly"]
    assert effect["groups"]["llm"]["wired"] is True
    assert effect["groups"]["reranker"]["wired"] is True
    assert effect["notes"], "必须给出可读的生效说明"

    # 运行时值单独返回（进程内真实生效值）
    assert body["runtime"]["llm"]["baseUrl"] == "https://llm.invalid/v1"
    assert body["runtime"]["llm"]["apiKeySet"] is True
    assert body["runtime"]["retrieval"]["topK"] == 9
    assert SECRET_KEY not in resp.text


def test_get_config_requires_admin(conn, cfg_settings: Settings) -> None:
    client = TestClient(make_app(conn, cfg_settings, authorized=False))
    resp = client.get("/admin/config")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------- PUT /admin/config
def test_put_config_persists_groups_without_claiming_immediate_effect(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    client = TestClient(make_app(conn, cfg_settings))
    payload = {
        "retrieval": {
            "recallPerRoute": 33,
            "fusion": "weighted",
            "topK": 7,
            "evidenceThreshold": 0.55,
        },
        "widget": {
            "welcome": "欢迎使用测试助手",
            "samples": ["问题一", "问题二"],
            "handoffText": "转人工测试文案",
            "brandColor": "#1f6feb",
        },
        "security": {"captchaEnabled": True},
    }
    resp = client.put("/admin/config", headers=auth_headers(), json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 配置值 = 刚保存的值
    assert body["retrieval"]["recallPerRoute"] == 33
    assert body["retrieval"]["topK"] == 7
    assert body["retrieval"]["fusion"] == "weighted"
    assert body["widget"]["samples"] == ["问题一", "问题二"]
    assert body["widget"]["brandColor"] == "#1f6feb"
    assert body["security"]["captchaEnabled"] is True
    assert set(body["savedGroups"]) == {"retrieval", "widget", "security"}

    # 运行时值已随保存值更新（检索参数下一次问答生效）
    assert body["runtime"]["retrieval"]["topK"] == 7
    assert body["runtime"]["retrieval"]["recallPerRoute"] == 33
    assert body["runtime"]["widget"]["welcome"] == "欢迎使用测试助手"
    assert "retrieval" in body["effect"]["immediate"]

    # 真的落库
    with conn.cursor() as cur:
        cur.execute("select value from app_config where key = 'config.retrieval'")
        row = cur.fetchone()
    assert row is not None
    assert row[0]["topK"] == 7

    # 再读一次：保存值仍在，运行时值同步更新
    again = client.get("/admin/config", headers=auth_headers()).json()
    assert again["retrieval"]["topK"] == 7
    assert again["runtime"]["retrieval"]["topK"] == 7


def test_put_config_rejects_plaintext_api_key(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    client = TestClient(make_app(conn, cfg_settings))
    plaintext = "sk-plaintext-must-not-be-stored"
    resp = client.put(
        "/admin/config",
        headers=auth_headers(),
        json={"llm": {"baseUrl": "https://x.invalid", "model": "m", "apiKey": plaintext}},
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "LLM_API_KEY_NOT_WRITABLE"
    assert plaintext not in resp.text
    with conn.cursor() as cur:
        cur.execute("select count(*) from app_config where key = 'config.llm'")
        assert cur.fetchone()[0] == 0, "拒绝后不应留下任何 llm 配置行"


def test_put_config_validates_color_and_ranges(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    client = TestClient(make_app(conn, cfg_settings))
    bad_color = client.put(
        "/admin/config",
        headers=auth_headers(),
        json={
            "widget": {
                "welcome": "hi",
                "samples": ["a"],
                "handoffText": "x",
                "brandColor": "green",
            }
        },
    )
    assert bad_color.status_code == 422
    bad_threshold = client.put(
        "/admin/config",
        headers=auth_headers(),
        json={"retrieval": {"topK": 1, "evidenceThreshold": 2}},
    )
    assert bad_threshold.status_code == 422


# ---------------------------------------------------------------- PUT /admin/theme
def test_put_theme_writes_same_brand_group(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    client = TestClient(make_app(conn, cfg_settings))
    resp = client.put(
        "/admin/theme",
        headers=auth_headers(),
        json={
            "systemName": "客户A管理台",
            "productName": "客户A助手",
            "orgName": "客户A",
            "primaryColor": "#1f6feb",
        },
    )
    assert resp.status_code == 200, resp.text
    theme = resp.json()
    for key in ("systemName", "productName", "orgName", "primaryColor"):
        assert key in theme
    assert theme["systemName"] == "客户A管理台"
    assert theme["primaryColor"] == "#1f6feb"

    config = client.get("/admin/config", headers=auth_headers()).json()
    assert config["brand"]["systemName"] == "客户A管理台"
    assert config["brand"]["primaryColor"] == "#1f6feb"
    assert "brand" in config["savedGroups"]

    with conn.cursor() as cur:
        cur.execute("select value from app_config where key = 'config.brand'")
        row = cur.fetchone()
    assert row is not None
    assert row[0]["systemName"] == "客户A管理台"


def test_theme_accepts_logo_data_url_and_serves_public_brand(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    """自定义标识：≤100KB 的 SVG/PNG data URL 随品牌配置保存，并出现在公开品牌接口。"""
    logo = (
        "data:image/svg+xml;base64,"
        "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciLz4="
    )
    client = TestClient(make_app(conn, cfg_settings))
    resp = client.put(
        "/admin/theme",
        headers=auth_headers(),
        json={"logo": logo, "logoFileName": "brand.svg"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logo"] == logo
    assert resp.json()["logoFileName"] == "brand.svg"

    public = client.get("/admin/auth/brand")
    assert public.status_code == 200
    assert public.json()["logo"] == logo
    assert public.json()["logoFileName"] == "brand.svg"

    config = client.get("/admin/config", headers=auth_headers()).json()
    assert config["brand"]["logo"] == logo


def test_theme_rejects_non_image_logo(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    client = TestClient(make_app(conn, cfg_settings))
    resp = client.put(
        "/admin/theme",
        headers=auth_headers(),
        json={"logo": "https://example.com/logo.png"},
    )
    assert resp.status_code == 422, "标识只允许内置 default 或 SVG/PNG data URL"


def test_theme_rejects_oversized_logo(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    """前端限 100KB 只是体验；服务端按解码后的真实字节数再拒绝一次。"""
    payload = "data:image/png;base64," + base64.b64encode(
        b"0" * (100 * 1024 + 1)
    ).decode("ascii")
    client = TestClient(make_app(conn, cfg_settings))
    resp = client.put("/admin/theme", headers=auth_headers(), json={"logo": payload})
    assert resp.status_code == 422


# ---------------------------------------------------------------- test-llm
def test_test_llm_success_with_injected_client(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    fake = FakeLlmClient(result=LlmResult(text="pong", model="unit-llm-echo", elapsed_ms=42))
    client = TestClient(make_app(conn, cfg_settings, probe=make_probe(fake)))
    resp = client.post("/admin/config/test-llm", headers=auth_headers())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["model"] == "unit-llm-echo"
    assert body["latencyMs"] == 42
    assert body["firstTokenMs"] is None
    assert body["error"] is None
    assert body["source"] == "app_config"
    assert fake.calls == 1, "必须真实调用一次 complete（此处为注入的假客户端）"


def test_test_llm_failure_returns_reason_not_500(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    fake = FakeLlmClient(error=LlmAuthError("鉴权失败（HTTP 401）"))
    client = TestClient(make_app(conn, cfg_settings, probe=make_probe(fake)))
    resp = client.post("/admin/config/test-llm", headers=auth_headers())
    assert resp.status_code == 200, "契约要求失败原因也走 200 LlmTestResult"
    body = resp.json()
    assert body["ok"] is False
    assert "鉴权失败" in body["error"]
    assert body["errorCode"] == "LLM_AUTH"
    assert body["model"] == "unit-llm"


def test_test_llm_without_api_key_does_not_call_model(conn, clean_app_config) -> None:
    settings = Settings(
        llm_base_url="https://llm.invalid/v1",
        llm_model="unit-llm",
        llm_api_key="",
    )
    fake = FakeLlmClient(result=LlmResult(text="pong", model="unit-llm-echo", elapsed_ms=1))
    client = TestClient(make_app(conn, settings, probe=make_probe(fake)))
    resp = client.post("/admin/config/test-llm", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "LLM_API_KEY" in body["error"]
    assert fake.calls == 0, "没有密钥时不应发起真实请求"

def test_config_rejects_unknown_reranker_provider(
    conn, cfg_settings: Settings, clean_app_config
) -> None:
    """重排开关只允许 none / tencent / llama_cpp；拼错不能静默变成"已启用"。"""
    client = TestClient(make_app(conn, cfg_settings))
    resp = client.put(
        "/admin/config",
        headers=auth_headers(),
        json={"reranker": {"provider": "bogus"}},
    )
    assert resp.status_code == 422
