"""治理域登录与守卫测试（需要数据库）。

验证：
- 工号 + 口令 → JWT，`GET /admin/me` 可用；
- 错误口令 401 且不区分"账号不存在/口令错"（防账号枚举）；
- 无 token / 伪造 token / widget 会话 id → 401（双链隔离）。
"""

from __future__ import annotations

import base64
import re

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.routers.deps import get_conn
from app.security import hash_password
from app.users import create_user, get_user_by_account

pytestmark = requires_db

ACCOUNT = "T-S1-ADMIN"
PASSWORD = "Unit-Test-Pass-1"


@pytest.fixture()
def admin_account(conn) -> str:
    user = get_user_by_account(conn, ACCOUNT)
    if user is None:
        create_user(
            conn,
            account=ACCOUNT,
            name="测试管理员",
            role="admin",
            password_hash=hash_password(PASSWORD),
        )
    return ACCOUNT


def make_client(conn) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: Settings(
        jwt_secret="unit-test-secret-32-bytes-long!!"
    )
    return TestClient(app)


def _solve_captcha(image: str) -> str:
    svg = base64.b64decode(image.split(",", 1)[1]).decode()
    return "".join(re.findall(r"<text[^>]*>([A-Z2-9])</text>", svg))


def test_captcha_disabled_by_default(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("delete from app_config where key = 'config.security'")
    conn.commit()
    client = make_client(conn)
    body = client.post("/admin/auth/captcha").json()
    assert body["enabled"] is False


def test_captcha_enabled_is_one_time_and_required(conn, admin_account: str) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "insert into app_config(key, value, updated_at) values "
            "('config.security', %s, now()) on conflict (key) do update set value = excluded.value",
            (Jsonb({"captchaEnabled": True}),),
        )
    conn.commit()
    client = make_client(conn)
    try:
        challenge = client.post("/admin/auth/captcha").json()
        assert challenge["enabled"] is True
        assert challenge["image"].startswith("data:image/svg+xml;base64,")
        code = _solve_captcha(challenge["image"])
        assert len(code) == 4

        missing = client.post(
            "/admin/auth/login",
            json={"account": admin_account, "password": PASSWORD},
        )
        assert missing.status_code == 401
        assert missing.json()["code"] == "CAPTCHA_INVALID"

        wrong = client.post(
            "/admin/auth/login",
            json={
                "account": admin_account,
                "password": PASSWORD,
                "captchaId": challenge["captchaId"],
                "captchaCode": "ZZZZ",
            },
        )
        assert wrong.status_code == 401

        challenge2 = client.post("/admin/auth/captcha").json()
        code2 = _solve_captcha(challenge2["image"])
        ok = client.post(
            "/admin/auth/login",
            json={
                "account": admin_account,
                "password": PASSWORD,
                "captchaId": challenge2["captchaId"],
                "captchaCode": code2,
            },
        )
        assert ok.status_code == 200, ok.text

        reused = client.post(
            "/admin/auth/login",
            json={
                "account": admin_account,
                "password": PASSWORD,
                "captchaId": challenge2["captchaId"],
                "captchaCode": code2,
            },
        )
        assert reused.status_code == 401, "验证码必须一次性"
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from app_config where key = 'config.security'")
            cur.execute("delete from captcha_challenges")
        conn.commit()


def test_security_and_preview_config_are_wired_in_runtime(conn, admin_account: str) -> None:
    from psycopg.types.json import Jsonb

    client = make_client(conn)
    login = client.post(
        "/admin/auth/login", json={"account": admin_account, "password": PASSWORD}
    )
    assert login.status_code == 200, "验证码默认关闭时仍可正常登录"
    token = login.json()["token"]
    with conn.cursor() as cur:
        cur.execute(
            "insert into app_config(key, value, updated_at) values "
            "('config.security', %s, now()), ('config.preview', %s, now()) "
            "on conflict (key) do update set value = excluded.value",
            (Jsonb({"captchaEnabled": True}), Jsonb({"enabled": False})),
        )
    conn.commit()
    try:
        cfg = client.get(
            "/admin/config", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert cfg["runtime"]["security"]["captchaEnabled"] is True
        assert cfg["runtime"]["preview"]["enabled"] is False
        assert cfg["effect"]["groups"]["security"]["wired"] is True
        assert cfg["effect"]["groups"]["preview"]["wired"] is True
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "delete from app_config where key in ('config.security', 'config.preview')"
            )
        conn.commit()


def test_public_brand_is_readable_without_login(conn) -> None:
    """登录页在未登录时也要能读到 app_config 的品牌展示字段。"""
    from psycopg.types.json import Jsonb

    saved = {
        "systemName": "品牌测试管理台",
        "productName": "品牌测试助手",
        "orgName": "品牌测试组织",
        "primaryColor": "#123456",
        "logo": "default",
        "logoFileName": "",
    }
    with conn.cursor() as cur:
        cur.execute(
            "insert into app_config(key, value, updated_at) values ('config.brand', %s, now()) "
            "on conflict (key) do update set value = excluded.value",
            (Jsonb(saved),),
        )
    conn.commit()
    try:
        client = make_client(conn)
        resp = client.get("/admin/auth/brand")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["systemName"] == saved["systemName"]
        assert body["productName"] == saved["productName"]
        assert body["orgName"] == saved["orgName"]
        assert body["primaryColor"] == saved["primaryColor"]
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from app_config where key = 'config.brand'")
        conn.commit()


def test_login_then_me(conn, admin_account: str) -> None:
    client = make_client(conn)
    resp = client.post("/admin/auth/login", json={"account": admin_account, "password": PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token"]
    assert body["expiresAt"]
    assert body["user"]["account"] == admin_account
    assert body["user"]["role"] == "admin"
    assert "password_hash" not in str(body), "响应绝不能带口令散列"

    me = client.get("/admin/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["account"] == admin_account


def test_wrong_password_is_401_without_enumeration(conn, admin_account: str) -> None:
    client = make_client(conn)
    wrong = client.post("/admin/auth/login", json={"account": admin_account, "password": "nope"})
    missing = client.post("/admin/auth/login", json={"account": "T-NOBODY", "password": "nope"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["message"] == missing.json()["message"]


def test_me_requires_valid_bearer(conn) -> None:
    client = make_client(conn)
    assert client.get("/admin/me").status_code == 401
    assert client.get("/admin/me", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_widget_session_cannot_access_admin(conn) -> None:
    """双链隔离：工程师侧 sessionId 不能当治理域凭证用。"""
    from app.sessions import create_session

    session = create_session(conn, requested_source="host_injected", claimed_account=None)
    client = make_client(conn)
    resp = client.get(
        "/admin/me", headers={"Authorization": f"Bearer {session['sessionId']}"}
    )
    assert resp.status_code == 401
