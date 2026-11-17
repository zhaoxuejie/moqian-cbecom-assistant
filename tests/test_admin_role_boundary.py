"""治理域角色边界（需要数据库）：普通用户可登录，但治理接口必须 403。

PRD §2.3：身份/角色用于治理域访问与统计归属，不用于 Widget 身份链。
"""

from __future__ import annotations

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.routers.deps import get_conn
from app.security import create_admin_token, hash_password
from app.users import create_user, get_user_by_account

pytestmark = requires_db

SECRET = "unit-test-secret-32-bytes-long!!"
ADMIN = "T-ROLE-ADMIN"
USER = "T-ROLE-USER"


@pytest.fixture()
def clients(conn, tmp_path):
    ids: dict[str, int] = {}
    for account, name, role in ((ADMIN, "角色测试管理员", "admin"), (USER, "角色测试用户", "user")):
        row = get_user_by_account(conn, account)
        ids[account] = (
            int(row["id"])
            if row
            else create_user(
                conn,
                account=account,
                name=name,
                role=role,
                password_hash=hash_password("Unit-Test-Pass-9"),
            )
        )
    settings = Settings(data_dir=str(tmp_path), jwt_secret=SECRET)
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    admin = TestClient(app)
    user = TestClient(app)
    admin_token, _ = create_admin_token(ids[ADMIN], ADMIN, "admin", Settings(jwt_secret=SECRET))
    user_token, _ = create_admin_token(ids[USER], USER, "user", Settings(jwt_secret=SECRET))
    admin.headers.update({"Authorization": f"Bearer {admin_token}"})
    user.headers.update({"Authorization": f"Bearer {user_token}"})
    yield admin, user
    with conn.cursor() as cur:
        cur.execute("delete from users where account = any(%s)", ([ADMIN, USER],))
    conn.commit()


def test_normal_user_is_forbidden_on_governance_apis(clients) -> None:
    _, user = clients
    for path in (
        "/admin/documents",
        "/admin/jobs",
        "/admin/config",
        "/admin/messages",
        "/admin/gaps",
        "/admin/settlements",
        "/admin/evals",
    ):
        resp = user.get(path)
        assert resp.status_code == 403, path
        assert resp.json()["code"] == "FORBIDDEN"


def test_normal_user_can_use_personal_apis(clients) -> None:
    _, user = clients
    resp = user.get("/admin/me")
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"


def test_admin_can_use_governance_apis(clients) -> None:
    admin, _ = clients
    assert admin.get("/admin/documents").status_code == 200
    assert admin.get("/admin/config").status_code == 200
