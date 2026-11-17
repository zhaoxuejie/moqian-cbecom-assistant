"""后台「用户管理 + 我的账号」接口测试（需要数据库）。

覆盖口径（contracts/openapi.yaml）：
- 账号即工号：唯一、不可改、不可复用；`__unidentified__` 不进列表；
- 角色边界：普通用户访问治理域 403，个人域（/admin/me*）可用；
- 待开通工号：`identity_pending` 与会话声明取并集，开通后清 pending、不改历史会话归属；
- 我的问答 / 我的收藏只返回本人会话，排除 `admin_preview`；
- 重置口令一次性返回临时口令；改密必须校验旧口令，域账号 409。

纪律：所有测试账号使用 `T-USERS-` 前缀，用例前后清理；**不修改 admin 等演示账号**。
前端静态守卫（不依赖数据库）也放在本文件：两页必须按分层顺序加载脚本，
adapter 不碰 DOM、controller 不发请求。
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.messages import (
    log_assistant_message,
    log_user_message,
    set_feedback,
)
from app.routers.deps import get_conn
from app.security import create_admin_token, hash_password
from app.sessions import create_session
from app.users import create_user, get_user_by_account

_REPO_ROOT = Path(__file__).resolve().parent.parent
_JWT_SECRET = "unit-test-secret-32-bytes-long!!"

# 带进程号：多个人/多个 pytest 同时跑共用同一个 PostgreSQL 时，互不删除对方数据
PREFIX = f"T-USERS-{os.getpid()}-"
PASSWORD = "Unit-Test-Pass-1"


# ---------------------------------------------------------------- 夹具与清理
def _purge(conn) -> None:
    """清掉本文件创建的账号、会话、待开通记录（按前缀，绝不碰演示数据）。"""
    with conn.cursor() as cur:
        cur.execute("select id from users where account like %s", (PREFIX + "%",))
        ids = [row[0] for row in cur.fetchall()]
        if ids:
            cur.execute("delete from sessions where engineer_id = any(%s)", (ids,))
        cur.execute(
            "delete from sessions where claimed_engineer_id like %s", (PREFIX + "%",)
        )
        cur.execute(
            "delete from identity_pending where account like %s", (PREFIX + "%",)
        )
        cur.execute("delete from users where account like %s", (PREFIX + "%",))
    conn.commit()


@pytest.fixture()
def db(conn):
    """带清理的数据库连接：用例前后各清一次 T-USERS- 前缀数据。"""
    _purge(conn)
    try:
        yield conn
    finally:
        _purge(conn)


@pytest.fixture()
def client(conn, db) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: _settings()
    return TestClient(app)


def _settings() -> Settings:
    return Settings(jwt_secret=_JWT_SECRET)


def _make_user(
    conn,
    suffix: str,
    *,
    role: str = "user",
    credential: str = "local",
    password: str = PASSWORD,
    name: str | None = None,
) -> str:
    account = PREFIX + suffix
    create_user(
        conn,
        account=account,
        name=name or f"测试{suffix}",
        role=role,
        credential=credential,
        password_hash=hash_password(password) if credential == "local" else None,
        group_name="测试组",
    )
    return account


def _login(client: TestClient, account: str, password: str = PASSWORD) -> dict[str, str]:
    resp = client.post("/admin/auth/login", json={"account": account, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _error_code(resp) -> str:
    body = resp.json()
    return str(body.get("code", ""))


# ---------------------------------------------------------------- 用户列表 / 新建
def test_user_list_excludes_sentinel_and_uses_camel_case(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    member = _make_user(db, "MEMBER", role="user")
    headers = _login(client, admin)

    resp = client.get("/admin/users", params={"page": 1, "pageSize": 100}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    accounts = [item["account"] for item in body["items"]]
    assert "__unidentified__" not in accounts
    assert member in accounts
    assert body["pagination"]["pageSize"] == 100
    assert body["pagination"]["total"] >= 2
    for item in body["items"]:
        assert "password_hash" not in item
        assert "lastActive" in item

    filtered = client.get(
        "/admin/users", params={"role": "user", "q": member}, headers=headers
    )
    assert filtered.status_code == 200
    assert [item["account"] for item in filtered.json()["items"]] == [member]


def test_create_user_hashes_password_and_rejects_duplicates(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    headers = _login(client, admin)
    account = PREFIX + "NEW"

    created = client.post(
        "/admin/users",
        json={
            "account": account,
            "name": "新工",
            "role": "user",
            "credential": "local",
            "team": "售后一组",
            "password": "Init-Pass-1",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["account"] == account
    assert body["team"] == "售后一组"
    assert body["role"] == "user"
    assert body["status"] == "active"
    assert "password" not in str(body)
    # 口令确实可登录（说明散列后能校验），且库里不是明文
    _login(client, account, "Init-Pass-1")
    stored = get_user_by_account(db, account)
    assert stored is not None
    assert stored["password_hash"] != "Init-Pass-1"

    duplicate = client.post(
        "/admin/users",
        json={"account": account, "name": "重复", "role": "user", "credential": "local",
              "password": "Init-Pass-1"},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert _error_code(duplicate) == "CONFLICT"

    bad_account = client.post(
        "/admin/users",
        json={"account": "T USERS BAD", "name": "坏工号", "role": "user",
              "credential": "local", "password": "Init-Pass-1"},
        headers=headers,
    )
    assert bad_account.status_code == 422
    assert _error_code(bad_account) == "VALIDATION_ERROR"

    missing_password = client.post(
        "/admin/users",
        json={"account": PREFIX + "NOPASS", "name": "无密码", "role": "user",
              "credential": "local"},
        headers=headers,
    )
    assert missing_password.status_code == 422

    sentinel = client.post(
        "/admin/users",
        json={"account": "__unidentified__", "name": "哨兵", "role": "user",
              "credential": "domain"},
        headers=headers,
    )
    assert sentinel.status_code == 422


# ---------------------------------------------------------------- 编辑 / 停用 / 删除
def test_patch_user_keeps_account_immutable(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    member = _make_user(db, "MEMBER", role="user")
    headers = _login(client, admin)
    member_id = get_user_by_account(db, member)["id"]

    patched = client.patch(
        f"/admin/users/{member_id}",
        json={"name": "改名后", "team": "维保二组", "role": "admin"},
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "改名后"
    assert patched.json()["team"] == "维保二组"
    assert patched.json()["role"] == "admin"

    renamed_account = client.patch(
        f"/admin/users/{member_id}", json={"account": PREFIX + "OTHER"}, headers=headers
    )
    assert renamed_account.status_code == 422
    assert _error_code(renamed_account) == "VALIDATION_ERROR"

    disabled = client.patch(
        f"/admin/users/{member_id}", json={"status": "disabled"}, headers=headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert client.post(
        "/admin/auth/login", json={"account": member, "password": PASSWORD}
    ).status_code == 401

    missing = client.patch("/admin/users/u-99999999", json={"name": "不存在"}, headers=headers)
    assert missing.status_code == 404


def test_admin_cannot_demote_or_disable_self(db, client) -> None:
    """管理员不能把自己降级/停用，避免把自己锁在治理域外。"""
    admin = _make_user(db, "ADMIN", role="admin")
    headers = _login(client, admin)
    admin_id = get_user_by_account(db, admin)["id"]

    demote = client.patch(
        f"/admin/users/{admin_id}", json={"role": "user"}, headers=headers
    )
    assert demote.status_code == 409
    assert _error_code(demote) == "CONFLICT"

    disable = client.patch(
        f"/admin/users/{admin_id}", json={"status": "disabled"}, headers=headers
    )
    assert disable.status_code == 409
    assert _error_code(disable) == "CONFLICT"

    row = get_user_by_account(db, admin)
    assert row["role"] == "admin"
    assert row["status"] == "active"


def test_delete_is_soft_delete_with_guards(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    target = _make_user(db, "TARGET", role="user")
    headers = _login(client, admin)
    target_id = get_user_by_account(db, target)["id"]

    deleted = client.delete(f"/admin/users/{target_id}", headers=headers)
    assert deleted.status_code == 204
    row = get_user_by_account(db, target)
    assert row is not None, "软删除不能物理删行，历史归属要保留"
    assert row["status"] == "disabled"

    self_delete = client.delete(f"/admin/users/{get_user_by_account(db, admin)['id']}",
                                headers=headers)
    assert self_delete.status_code == 409
    assert _error_code(self_delete) == "CONFLICT"

    sentinel = get_user_by_account(db, "__unidentified__")
    if sentinel is not None:
        sentinel_delete = client.delete(f"/admin/users/{sentinel['id']}", headers=headers)
        assert sentinel_delete.status_code == 409


def test_reset_password_returns_one_time_password(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    target = _make_user(db, "RESETME", role="user")
    domain_user = _make_user(db, "DOMAIN", credential="domain")
    headers = _login(client, admin)
    target_id = get_user_by_account(db, target)["id"]

    reset = client.post(f"/admin/users/{target_id}/reset-password", headers=headers)
    assert reset.status_code == 200, reset.text
    temporary = reset.json()["temporaryPassword"]
    assert re.fullmatch(r"[A-Za-z0-9]{4}-[A-Za-z0-9]{4}", temporary or "")
    assert client.post(
        "/admin/auth/login", json={"account": target, "password": PASSWORD}
    ).status_code == 401
    _login(client, target, temporary)

    domain_id = get_user_by_account(db, domain_user)["id"]
    domain_reset = client.post(f"/admin/users/{domain_id}/reset-password", headers=headers)
    assert domain_reset.status_code == 409
    assert _error_code(domain_reset) == "DOMAIN_ACCOUNT"


# ---------------------------------------------------------------- 角色边界
def test_normal_user_gets_403_on_governance_endpoints(db, client) -> None:
    member = _make_user(db, "MEMBER", role="user")
    headers = _login(client, member)

    forbidden = client.get("/admin/users", headers=headers)
    assert forbidden.status_code == 403
    assert _error_code(forbidden) == "FORBIDDEN"
    assert client.get("/admin/identity/pending", headers=headers).status_code == 403
    assert client.post(
        "/admin/users",
        json={"account": PREFIX + "X", "name": "X", "role": "user", "credential": "domain"},
        headers=headers,
    ).status_code == 403

    me = client.get("/admin/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["account"] == member


# ---------------------------------------------------------------- 我的账号
def test_change_my_password_checks_old_password(db, client) -> None:
    member = _make_user(db, "MEMBER", role="user")
    headers = _login(client, member)

    wrong_old = client.put(
        "/admin/me/password",
        json={"oldPassword": "not-my-password", "newPassword": "New-Pass-2"},
        headers=headers,
    )
    assert wrong_old.status_code == 400
    assert _error_code(wrong_old) == "VALIDATION_ERROR"

    too_short = client.put(
        "/admin/me/password",
        json={"oldPassword": PASSWORD, "newPassword": "123"},
        headers=headers,
    )
    assert too_short.status_code == 422

    changed = client.put(
        "/admin/me/password",
        json={"oldPassword": PASSWORD, "newPassword": "New-Pass-2"},
        headers=headers,
    )
    assert changed.status_code == 200
    assert changed.json()["ok"] is True
    assert client.post(
        "/admin/auth/login", json={"account": member, "password": PASSWORD}
    ).status_code == 401
    _login(client, member, "New-Pass-2")


def test_change_password_for_domain_account_is_409(db, client) -> None:
    """域账号在域里改口令；用直接签发的 token 覆盖这条分支（域账号无法平台登录）。"""
    domain_user = _make_user(db, "DOMAIN", credential="domain", role="user")
    user = get_user_by_account(db, domain_user)
    token, _ = create_admin_token(int(user["id"]), domain_user, "user", _settings())
    response = client.put(
        "/admin/me/password",
        json={"oldPassword": "x", "newPassword": "New-Pass-2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert _error_code(response) == "DOMAIN_ACCOUNT"


def test_my_messages_and_saved_only_include_my_sessions(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    other = _make_user(db, "OTHER", role="user")
    headers = _login(client, admin)

    mine = create_session(db, requested_source="host_injected", claimed_account=admin)
    question_id = log_user_message(db, session_id=mine["sessionId"], question="主机异响怎么排查？")
    assert question_id
    answer_id = log_assistant_message(
        db,
        session_id=mine["sessionId"],
        content="按手册 §4.4.1 排查。",
        citations=[
            {
                "evidenceId": "M29023",
                "kind": "manual",
                "title": "《维修手册》",
                "loc": "§4.4.1 · P10–11",
            }
        ],
    )
    assert answer_id
    assert set_feedback(
        db, session_id=mine["sessionId"], message_id=answer_id, feedback="saved"
    )

    # 管理员自测会话（channel=admin_preview）不得出现
    preview = create_session(
        db, requested_source="host_injected", claimed_account=admin, channel="admin_preview"
    )
    log_user_message(db, session_id=preview["sessionId"], question="自测问题不该出现")
    preview_answer = log_assistant_message(
        db, session_id=preview["sessionId"], content="自测回答"
    )
    assert preview_answer
    set_feedback(db, session_id=preview["sessionId"], message_id=preview_answer, feedback="saved")

    # 别人的会话不得出现
    others = create_session(db, requested_source="host_injected", claimed_account=other)
    log_user_message(db, session_id=others["sessionId"], question="别人的问题")

    messages = client.get("/admin/me/messages", headers=headers)
    assert messages.status_code == 200, messages.text
    body = messages.json()
    questions = [item["question"] for item in body["items"]]
    assert questions == ["主机异响怎么排查？"]
    item = body["items"][0]
    assert item["id"] == question_id
    assert item["outcome"] == "answered"
    assert item["identity"]["engineer"] == admin
    assert item["citations"] == 1
    assert item["hits"][0]["evidenceId"] == "M29023"
    assert item["sessionId"] == mine["sessionId"]

    saved = client.get("/admin/me/saved", headers=headers)
    assert saved.status_code == 200, saved.text
    saved_body = saved.json()
    assert len(saved_body["items"]) == 1
    assert saved_body["items"][0]["question"] == "主机异响怎么排查？"
    assert saved_body["items"][0]["messageId"] == answer_id

    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date().isoformat()
    empty = client.get("/admin/me/messages", params={"from": tomorrow}, headers=headers)
    assert empty.status_code == 200
    assert empty.json()["items"] == []


def test_me_endpoints_require_login(db, client) -> None:
    assert client.get("/admin/me/messages").status_code == 401
    assert client.get("/admin/me/saved").status_code == 401


# ---------------------------------------------------------------- 待开通工号
def _add_pending(db, account: str, name: str) -> None:
    with db.cursor() as cur:
        cur.execute(
            "insert into identity_pending (account, name) values (%s, %s) "
            "on conflict (account) do nothing",
            (account, name),
        )
    db.commit()


def test_pending_identity_list_and_activate(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    headers = _login(client, admin)
    account = PREFIX + "PEND"
    _add_pending(db, account, "待开通工程师")
    # 宿主声明过这个工号（未在册 → engineer_id 为空）
    claimed_session = create_session(db, requested_source="host_injected", claimed_account=account)

    pending = client.get("/admin/identity/pending", headers=headers)
    assert pending.status_code == 200, pending.text
    entries = {item["account"]: item for item in pending.json()["items"]}
    assert account in entries
    assert entries[account]["count"] >= 1
    assert entries[account]["lastAt"]

    activated = client.post(
        f"/admin/identity/pending/{account}/activate",
        json={"name": "开通后姓名", "team": "华东一组", "role": "user", "credential": "domain"},
        headers=headers,
    )
    assert activated.status_code == 201, activated.text
    body = activated.json()
    assert body["account"] == account
    assert body["name"] == "开通后姓名"
    assert body["credential"] == "domain"
    assert body["team"] == "华东一组"

    # 开通后从待开通清单消失，进入账号列表
    after = client.get("/admin/identity/pending", headers=headers).json()["items"]
    assert account not in {item["account"] for item in after}
    listed = client.get("/admin/users", params={"q": account}, headers=headers)
    assert [item["account"] for item in listed.json()["items"]] == [account]

    # 历史会话归属不被追溯改写（契约明确要求）
    with db.cursor() as cur:
        cur.execute("select engineer_id from sessions where id = %s",
                    (int(claimed_session["sessionId"].replace("ses-", "")),))
        assert cur.fetchone()[0] is None

    again = client.post(
        f"/admin/identity/pending/{account}/activate",
        json={"name": "重复开通", "credential": "domain"},
        headers=headers,
    )
    assert again.status_code == 409
    unknown = client.post(
        f"/admin/identity/pending/{PREFIX}NOPE/activate",
        json={"name": "不存在", "credential": "domain"},
        headers=headers,
    )
    assert unknown.status_code == 404


def test_activate_local_account_then_reset_gets_temporary_password(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    headers = _login(client, admin)
    account = PREFIX + "PENDLOCAL"
    _add_pending(db, account, "待开通本地账号")

    activated = client.post(
        f"/admin/identity/pending/{account}/activate",
        json={"name": "本地开通", "role": "user", "credential": "local"},
        headers=headers,
    )
    assert activated.status_code == 201, activated.text
    user = activated.json()
    # local 账号开通时不落可选口令（契约请求体没有 password），由管理员重置拿一次性口令
    reset = client.post(f"/admin/users/{user['id']}/reset-password", headers=headers)
    assert reset.status_code == 200
    temporary = reset.json()["temporaryPassword"]
    assert temporary
    _login(client, account, temporary)


def test_activate_requires_pending_account(db, client) -> None:
    admin = _make_user(db, "ADMIN", role="admin")
    headers = _login(client, admin)
    response = client.post(
        f"/admin/identity/pending/{PREFIX}GHOST/activate",
        json={"name": "幽灵", "credential": "domain"},
        headers=headers,
    )
    assert response.status_code == 404
    assert get_user_by_account(db, PREFIX + "GHOST") is None


# ---------------------------------------------------------------- 前端静态守卫（不依赖数据库）
_ADMIN = _REPO_ROOT / "frontend" / "admin"


def _read(relative: str) -> str:
    return (_ADMIN / relative).read_text(encoding="utf-8")


def _assert_script_order(html: str, page: str) -> None:
    expected = [
        "/ui-design/tokens.css",
        "../styles/admin.css",
        "../adapters/real-api.js",
        "../adapters/users-api.js",
        "../controllers/layout.js",
        "../controllers/app.js",
        "../controllers/preview.js",
        f"../controllers/{page}.js",
    ]
    positions = [html.find(item) for item in expected]
    assert all(pos >= 0 for pos in positions), (page, expected, positions)
    assert positions == sorted(positions), f"{page} 页脚本/样式加载顺序不符合分层约定"
    assert f'data-page="{page}"' in html
    assert "admin-prototype" not in html


def test_frontend_pages_load_layered_assets() -> None:
    _assert_script_order(_read("pages/users.html"), "users")
    _assert_script_order(_read("pages/account.html"), "account")
    _assert_script_order(_read("pages/my-qa.html"), "my-qa")
    _assert_script_order(_read("pages/my-save.html"), "my-save")


def test_account_lists_are_split_into_my_group_pages() -> None:
    """个人域拆分：账号页不再混入两张列表，左侧「我的」组固定三项。"""
    account_html = _read("pages/account.html")
    assert 'id="qaRows"' not in account_html
    assert 'id="favBox"' not in account_html
    assert "PRD §2.3" not in account_html

    qa_html = _read("pages/my-qa.html")
    assert 'data-page="my-qa"' in qa_html
    assert 'id="qaRows"' in qa_html
    assert 'id="qaPager"' in qa_html
    assert "PRD §2.3" not in qa_html

    saved_html = _read("pages/my-save.html")
    assert 'data-page="my-save"' in saved_html
    assert 'id="favBox"' in saved_html
    assert 'id="favPager"' in saved_html

    layout = _read("controllers/layout.js")
    assert '{ group: "我的" }' in layout
    assert 'label: "我的账号"' in layout
    assert 'label: "我的问答"' in layout
    assert 'label: "我的收藏"' in layout
    assert '"my-qa": true' in layout and '"my-save": true' in layout


def test_users_search_and_knowledge_page_size_preferences_are_explicit() -> None:
    """用户搜索不预填；每页条数统一写 localStorage，刷新可恢复（所有分页列表同一份实现）。"""
    users_html = _read("pages/users.html")
    assert 'autocomplete="off"' in users_html
    assert "../controllers/users.js?v=" in users_html
    assert "../adapters/users-api.js?v=" in users_html
    users_js = _read("controllers/users.js")
    assert "function clearSearch" in users_js
    assert 'window.addEventListener("pageshow", clearSearch)' in users_js

    app_js = _read("controllers/app.js")
    # 一份分页组件 + 一份"每页条数"读写；各页只传自己的 key，不再各写一遍
    assert 'moqian_sources_page_size' in app_js
    assert 'moqian_jobs_page_size' in app_js
    assert "function readPageSize" in app_js
    assert "function savePageSize" in app_js
    assert "sizeKey" in app_js


def test_frontend_adapter_does_not_touch_dom_and_controller_does_not_fetch() -> None:
    adapter = _read("adapters/users-api.js")
    for banned in ("document.", "XMLHttpRequest", "fetch(", "window.MOCK"):
        assert banned not in adapter, f"adapter 只做接口适配，不应出现 {banned}"
    assert "AdminAPI.request" in adapter

    users_controller = _read("controllers/users.js")
    account_controller = _read("controllers/account.js")
    for source in (users_controller, account_controller):
        for banned in ("XMLHttpRequest", "fetch(", "AdminAPI.request"):
            assert banned not in source, f"controller 不发请求，不应出现 {banned}"
    assert "window.AdminPages.users" in users_controller
    assert "window.AdminPages.account" in account_controller
