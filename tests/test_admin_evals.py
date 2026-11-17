"""后台「评测面板 + 主管驾驶舱」接口测试（需要数据库）。

覆盖：
- 鉴权：所有端点无 token 一律 401（双链隔离的治理域守卫）；
- 评测：列表/详情字段形状（camelCase）、触发一次评测（monkeypatch 假 runner，不调模型）、
  用例筛选、未知用例 422、已有 running 批次时 409、runner 抛错时批次落 failed；
- 统计：overview / north-star / engineers / recognizability / 周报的口径与交叉一致性，
  且 **admin_preview 会话被排除**；未识别轮次归 __unidentified__；
- 空周与非法周：比例返回 0、不编造；非法 week 返回 422。

测试用未来周（2099-W05/W06）隔离数据，避免被本机其它开发数据干扰；
所有自建行在 fixture 收尾时删除（不真实调模型、不改共享文件）。
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import requires_db
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from psycopg.types.json import Jsonb

from app.config import Settings, get_settings
from app.main import create_app
from app.routers import admin_evals
from app.routers.deps import get_conn
from app.security import create_admin_token, hash_password
from app.users import create_user, get_user_by_account
from evals.metrics import CaseResult
from evals.schema import load_cases

pytestmark = requires_db

ACCOUNT = "T-EVAL-ADMIN"
PASSWORD = "Unit-Test-Pass-Eval"
JWT_SECRET = "unit-test-secret-32-bytes-long!!"

WEEK = "2099-W05"
EMPTY_WEEK = "2099-W06"
WEEK_MONDAY = datetime.fromisocalendar(2099, 5, 1).replace(tzinfo=UTC)
BASE_DAY = WEEK_MONDAY + timedelta(days=1)  # 周二，避开周一 0 点的边界

A_ACCOUNT = "T-EVAL-ENG-A"
B_ACCOUNT = "T-EVAL-ENG-B"


def _ensure_user(conn, account: str, name: str, created: dict) -> int:
    existing = get_user_by_account(conn, account)
    if existing is not None:
        return int(existing["id"])
    # credential=domain：身份来自企业域，本平台不保存口令（避免误建可登录账号）
    user_id = create_user(conn, account=account, name=name, role="user", credential="domain")
    created["users"].append(user_id)
    return user_id


def _new_session(
    conn,
    created: dict,
    *,
    engineer_id: int | None = None,
    channel: str = "widget",
    auth_source: str = "host_injected",
    claimed: str | None = None,
    created_at: datetime | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into sessions
                (auth_source, claimed_engineer_id, engineer_id, channel, host_context, created_at)
            values (%s, %s, %s, %s, %s::jsonb, coalesce(%s, now()))
            returning id
            """,
            (
                auth_source,
                claimed,
                engineer_id,
                channel,
                '{"deviceModel": "BK系列"}',
                created_at,
            ),
        )
        session_id = int(cur.fetchone()[0])
    conn.commit()
    created["sessions"].append(session_id)
    return session_id


def _new_message(
    conn,
    session_id: int,
    *,
    role: str,
    content: str = "制动器间隙标准值是多少？",
    created_at: datetime | None = None,
    question_at: datetime | None = None,
    refused: bool = False,
    gate_reason: str | None = None,
    resolved: bool | None = None,
    resolved_at: datetime | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into messages
                (session_id, role, content, refused, gate_reason, resolved, resolved_at,
                 question_at, created_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, now()))
            returning id
            """,
            (
                session_id,
                role,
                content,
                refused,
                gate_reason,
                resolved,
                resolved_at,
                question_at,
                created_at,
            ),
        )
        message_id = int(cur.fetchone()[0])
    conn.commit()
    return message_id


def _new_round(
    conn,
    session_id: int,
    *,
    asked_at: datetime,
    resolved: bool | None = None,
    refused: bool = False,
    gate_reason: str | None = None,
    question: str = "制动器间隙标准值是多少？",
) -> None:
    """一轮问答：user 提问 + 紧随其后的 assistant 回答（反馈写在 assistant 上）。"""
    _new_message(
        conn,
        session_id,
        role="user",
        content=question,
        created_at=asked_at,
        question_at=asked_at,
    )
    resolved_at = asked_at + timedelta(minutes=5) if resolved else None
    _new_message(
        conn,
        session_id,
        role="assistant",
        content="回答",
        created_at=asked_at + timedelta(minutes=1),
        refused=refused,
        gate_reason=gate_reason,
        resolved=resolved,
        resolved_at=resolved_at,
    )


@pytest.fixture()
def seeded(conn):
    user = get_user_by_account(conn, ACCOUNT)
    if user is None:
        user_id = create_user(
            conn,
            account=ACCOUNT,
            name="评测面板测试管理员",
            role="admin",
            password_hash=hash_password(PASSWORD),
        )
    else:
        user_id = int(user["id"])

    settings = Settings(jwt_secret=JWT_SECRET)
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    token, _ = create_admin_token(user_id, ACCOUNT, "admin", settings)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    created: dict[str, list] = {"runs": [], "cases": [], "sessions": [], "users": []}
    # 评测批次：记下测试前的最大 id，收尾时按 id 段清理（断言失败也不会留下脏数据）
    with conn.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from eval_runs")
        run_baseline = int(cur.fetchone()[0])
    try:
        yield {"client": client, "conn": conn, "created": created, "settings": settings}
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from eval_runs where id > %s", (run_baseline,))
            if created["cases"]:
                cur.execute("delete from eval_cases where id = any(%s)", (created["cases"],))
            if created["sessions"]:
                cur.execute("delete from sessions where id = any(%s)", (created["sessions"],))
            if created["users"]:
                cur.execute("delete from users where id = any(%s)", (created["users"],))
        conn.commit()


# ============================================================
# 鉴权
# ============================================================
def test_admin_evals_endpoints_require_admin(conn) -> None:
    settings = Settings(jwt_secret=JWT_SECRET)
    app = create_app()
    app.dependency_overrides[get_conn] = lambda: conn
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)

    requests = [
        ("get", "/admin/evals", None),
        ("get", "/admin/evals/run-1", None),
        ("post", "/admin/evals/run", {}),
        ("get", "/admin/evals/cases/template", None),
        ("post", "/admin/evals/cases/reset", {}),
        ("get", "/admin/stats/overview", None),
        ("get", "/admin/stats/north-star", None),
        ("get", "/admin/stats/engineers", None),
        ("get", "/admin/stats/recognizability", None),
        ("get", f"/admin/reports/weekly?week={WEEK}", None),
    ]
    for method, url, body in requests:
        resp = getattr(client, method)(url) if body is None else getattr(client, method)(
            url, json=body
        )
        assert resp.status_code == 401, f"{method.upper()} {url} 未鉴权应为 401"
        payload = resp.json()
        assert payload["code"] == "UNAUTHORIZED"
        assert payload["message"]


# ============================================================
# 评测：列表 / 详情 / 触发
# ============================================================
def _insert_done_run(conn, created: dict, *, case_count: int = 2) -> int:
    metrics = {
        "caseCount": case_count,
        "citationVerifiable": 0.9,
        "underRefusal": 0.0,
        "overRefusal": 0.1,
        "keyPointHit": 0.8,
        "summary": {"total": case_count, "citation_coverage": 0.9},
        "cases": [
            {
                "caseId": "ans-1",
                "question": "制动器间隙标准值是多少？",
                "answered": True,
                "refused": False,
                "refusalKind": None,
                "citations": 2,
                "citationValid": None,
                "keyPointHit": True,
                "elapsedMs": 1200,
            }
        ],
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into eval_runs (started_at, finished_at, model_snapshot, metrics, status)
            values (now(), now(), %s::jsonb, %s::jsonb, 'done')
            returning id
            """,
            (
                '{"label": "deepseek-flash · bge-m3 · 未接重排", "llm": "deepseek-flash"}',
                json.dumps(metrics, ensure_ascii=False),
            ),
        )
        run_id = int(cur.fetchone()[0])
    conn.commit()
    created["runs"].append(run_id)
    return run_id


def test_eval_list_and_detail_shape(seeded) -> None:
    conn = seeded["conn"]
    client = seeded["client"]
    run_id = _insert_done_run(conn, seeded["created"])

    listing = client.get("/admin/evals")
    assert listing.status_code == 200
    body = listing.json()
    assert body["caseTotal"] >= 1
    assert len(body["cases"]) == min(body["caseTotal"], 20)
    first_case = body["cases"][0]
    assert {"id", "question", "category", "hasBaseline"} <= set(first_case)

    run = next(item for item in body["runs"] if item["id"] == f"run-{run_id}")
    assert run["status"] == "done"
    assert run["caseCount"] == 2
    assert "deepseek-flash" in run["modelSnapshot"]
    assert run["metrics"]["citationVerifiable"] == 0.9
    assert run["metrics"]["keyPointHit"] == 0.8
    assert run["metrics"].get("batchConfusion") is None, "runner 无跨批次判定，不编造混淆率"
    assert run["startedAt"] and run["finishedAt"]

    detail = client.get(f"/admin/evals/run-{run_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["id"] == f"run-{run_id}"
    assert detail_body["cases"][0]["caseId"] == "ans-1"
    assert detail_body["cases"][0]["citationValid"] is None
    # runner 没有跨批次判定：不得用 false 冒充"没混淆"
    assert "batchConfused" not in detail_body["cases"][0]

    missing = client.get("/admin/evals/run-99999999")
    assert missing.status_code == 404
    assert missing.json()["code"] == "NOT_FOUND"
    assert client.get("/admin/evals/not-a-run").status_code == 404


def _fake_runner(seen: dict):
    def fake(*, cases, settings, conn):
        seen["ids"] = [case.id for case in cases]
        return [
            CaseResult(
                id=case.id,
                question=case.question,
                expect_refuse=case.expect_refuse,
                expect_safe=case.expect_safe,
                refused=case.expect_refuse,
                gate_reason="gap" if case.expect_refuse else None,
                citations=2,
                steps=2,
                steps_with_citations=2,
                stripped=[],
                latency_ms=15,
                keywords=list(case.keywords),
                keyword_hits=list(case.keywords),
            )
            for case in cases
        ]

    return fake


_CASE_COLUMNS = (
    "id",
    "question",
    "expected_points",
    "fault_category",
    "batch_pair_id",
    "baseline_answer",
    "expected_refuse",
    "expected_safety",
    "created_at",
    "source_id",
    "context",
)


@pytest.fixture()
def isolated_eval_cases(conn):
    """用例表整表快照/恢复：重置与导入会动全表，收尾必须还原本机数据。"""
    select = ", ".join(_CASE_COLUMNS)
    insert = (
        f"insert into eval_cases ({select}) values "
        f"({', '.join(['%s'] * len(_CASE_COLUMNS))})"
    )
    with conn.cursor() as cur:
        cur.execute(f"select {select} from eval_cases order by id")
        snapshot = cur.fetchall()
    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute("delete from eval_cases")
            for row in snapshot:
                values = list(row)
                values[2] = Jsonb(values[2])
                values[10] = Jsonb(values[10]) if values[10] is not None else None
                cur.execute(insert, values)
        conn.commit()


def test_reset_yaml_cases_restores_default_set(
    seeded, isolated_eval_cases
) -> None:
    """用例重置：清空自定义用例，恢复 evals/cases.yaml 默认集；重复重置幂等。"""
    yaml_path = Path(__file__).resolve().parents[1] / "evals" / "cases.yaml"
    expected = len(load_cases(yaml_path))
    conn = isolated_eval_cases
    with conn.cursor() as cur:
        cur.execute(
            "insert into eval_cases (question, source_id) values (%s, %s)",
            ("自定义用例（测试）", "import:unit-reset-demo"),
        )
    conn.commit()

    resp = seeded["client"].post("/admin/evals/cases/reset")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reset"] is True
    assert body["imported"] == expected
    assert body["removed"] >= 1

    listing = seeded["client"].get("/admin/evals", params={"pageSize": 100}).json()
    assert listing["caseSource"] == "table"
    by_id = {case["id"]: case for case in listing["cases"]}
    assert "A-001" in by_id, "重置后应是跨境默认用例集（A 层首条）"
    assert "import:unit-reset-demo" not in by_id

    again = seeded["client"].post("/admin/evals/cases/reset")
    assert again.status_code == 200
    with conn.cursor() as cur:
        cur.execute("select count(*) from eval_cases")
        assert int(cur.fetchone()[0]) == expected


def test_import_cases_from_csv_is_idempotent(
    seeded, isolated_eval_cases
) -> None:
    """CSV 导入：按用例ID幂等更新，期望要点/型号/批次入库。"""
    client = seeded["client"]
    conn = isolated_eval_cases
    with conn.cursor() as cur:
        cur.execute("delete from eval_cases where source_id like 'import:%'")
    conn.commit()
    csv_text = (
        "问题,期望要点,设备型号,批次,故障类型,期望拒答,期望安全拦截\n"
        "制动器间隙怎么调整？,间隙｜调整螺栓,BK系列,B22,异响,否,否\n"
        "物联网网关怎么配置？,,, ,联网,是,否\n"
    )
    files = {
        "file": (
            "cases.csv",
            csv_text.encode("utf-8-sig"),
            "text/csv",
        )
    }
    resp = client.post("/admin/evals/cases/import-file", files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["imported"] == 2
    assert body["created"] == 2
    assert body["updated"] == 0

    with conn.cursor() as cur:
        cur.execute(
            "select source_id, expected_points, context from eval_cases "
            "where source_id like 'import:auto:%' order by source_id"
        )
        rows = cur.fetchall()
    assert len(rows) == 2
    points = [list(row[1]) for row in rows]
    assert ["间隙", "调整螺栓"] in points
    assert all(row[2].get("deviceModel") == "BK系列" for row in rows if row[2])

    again = client.post("/admin/evals/cases/import-file", files=files)
    assert again.status_code == 200
    assert again.json()["created"] == 0
    assert again.json()["updated"] == 2


def test_import_cases_accepts_layered_headers(seeded, isolated_eval_cases) -> None:
    """跨境模板：问题/层/国家/语言/期望要点/期望拒答/用例ID。"""
    csv_text = (
        "问题,层,国家,语言,期望要点,期望拒答,用例ID\n"
        "How long is the US return window?,A,US,en,30 days,否,A-900\n"
    )
    resp = seeded["client"].post(
        "/admin/evals/cases/import-file",
        files={"file": ("cases.csv", csv_text.encode("utf-8-sig"), "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1
    with isolated_eval_cases.cursor() as cur:
        cur.execute(
            "select context, expected_points from eval_cases where source_id = 'import:A-900'"
        )
        row = cur.fetchone()
    assert row is not None
    context = row[0]
    assert context["layer"] == "A"
    assert context["country"] == "US"
    assert context["locale"] == "en"
    assert list(row[1]) == ["30 days"]


def test_import_cases_from_xlsx(seeded, isolated_eval_cases) -> None:
    """Excel 模板同样可导入（含中文表头与布尔列）。"""
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["问题", "期望要点", "设备型号", "批次", "期望拒答", "期望安全拦截"])
    sheet.append(["制动器线圈怎么检查？", "线圈｜电阻", "BK系列", "B22", "否", "否"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    resp = seeded["client"].post(
        "/admin/evals/cases/import-file",
        files={
            "file": (
                "cases.xlsx",
                buffer.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 1


def test_import_cases_rejects_bad_rows(seeded, isolated_eval_cases) -> None:
    """任一行不合法整批拒绝：不做「跳过几条」的静默导入。"""
    client = seeded["client"]
    missing_question = "问题\n\"\"\n".encode("utf-8-sig")
    resp = client.post(
        "/admin/evals/cases/import-file",
        files={"file": ("bad.csv", missing_question, "text/csv")},
    )
    assert resp.status_code == 422
    assert "问题" in resp.json()["message"]

    conflict = "问题,期望拒答,期望安全拦截\n带电检修能短接吗？,是,是\n".encode("utf-8-sig")
    resp = client.post(
        "/admin/evals/cases/import-file",
        files={"file": ("bad.csv", conflict, "text/csv")},
    )
    assert resp.status_code == 422
    assert "不能同时" in resp.json()["message"]


def test_eval_case_template_downloads(seeded) -> None:
    """CSV 模板带 BOM 便于 Excel 打开；Excel 模板首格是「问题」。"""
    client = seeded["client"]
    csv_resp = client.get("/admin/evals/cases/template", params={"format": "csv"})
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert csv_resp.content.startswith(b"\xef\xbb\xbf")
    assert "问题" in csv_resp.content.decode("utf-8-sig")

    xlsx_resp = client.get("/admin/evals/cases/template", params={"format": "xlsx"})
    assert xlsx_resp.status_code == 200
    assert "spreadsheetml" in xlsx_resp.headers["content-type"]
    workbook = load_workbook(io.BytesIO(xlsx_resp.content), read_only=True)
    try:
        assert workbook.active.cell(row=1, column=1).value == "问题"
    finally:
        workbook.close()


def test_run_eval_with_fake_runner(seeded, monkeypatch) -> None:
    client = seeded["client"]
    seen: dict = {}
    monkeypatch.setattr(admin_evals, "run_cases", _fake_runner(seen))

    cases = client.get("/admin/evals").json()["cases"]
    assert cases, "评测集不应为空（表为空时回落到 evals/cases.yaml）"
    case_id = cases[0]["id"]

    resp = client.post("/admin/evals/run", json={"caseIds": [case_id]})
    assert resp.status_code == 202
    started = resp.json()
    seeded["created"]["runs"].append(int(started["id"][4:]))
    assert started["status"] == "running"
    assert started["caseCount"] == 1
    assert started["id"].startswith("run-")
    assert seen["ids"] == [case_id]

    detail = client.get(f"/admin/evals/{started['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "done", "TestClient 会等后台任务结束"
    assert body["caseCount"] == 1
    assert body["metrics"]["citationVerifiable"] == 1.0
    assert body["cases"][0]["caseId"] == case_id
    assert body["cases"][0]["keyPointHit"] is True, (
        "expected_points 要回填成 keywords，否则面板的命中指标会一直是 null"
    )
    assert body["finishedAt"]


def test_run_eval_category_filter(seeded, monkeypatch) -> None:
    conn = seeded["conn"]
    client = seeded["client"]
    seen: dict = {}
    monkeypatch.setattr(admin_evals, "run_cases", _fake_runner(seen))

    inserted = []
    with conn.cursor() as cur:
        for question in ("测试异响一", "测试异响二"):
            cur.execute(
                """
                insert into eval_cases (question, fault_category, expected_refuse, expected_safety)
                values (%s, 'T-异响', false, false)
                returning id
                """,
                (question,),
            )
            inserted.append(int(cur.fetchone()[0]))
    conn.commit()
    seeded["created"]["cases"].extend(inserted)

    resp = client.post("/admin/evals/run", json={"category": "T-异响"})
    assert resp.status_code == 202
    body = resp.json()
    seeded["created"]["runs"].append(int(body["id"][4:]))
    assert body["caseCount"] == 2
    assert seen["ids"] == [f"case-{case_id}" for case_id in inserted]

    empty = client.post("/admin/evals/run", json={"category": "不存在的类别"})
    assert empty.status_code == 422
    assert empty.json()["code"] == "VALIDATION_ERROR"


def test_run_eval_rejects_unknown_case(seeded, monkeypatch) -> None:
    client = seeded["client"]
    monkeypatch.setattr(admin_evals, "run_cases", _fake_runner({}))
    resp = client.post("/admin/evals/run", json={"caseIds": ["no-such-case"]})
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_ERROR"


def test_run_eval_conflict_when_running(seeded, monkeypatch) -> None:
    conn = seeded["conn"]
    client = seeded["client"]
    monkeypatch.setattr(admin_evals, "run_cases", _fake_runner({}))
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into eval_runs (started_at, model_snapshot, metrics, status)
            values (now(), '{}'::jsonb, '{}'::jsonb, 'running')
            returning id
            """
        )
        running_id = int(cur.fetchone()[0])
    conn.commit()
    seeded["created"]["runs"].append(running_id)

    resp = client.post("/admin/evals/run", json={})
    assert resp.status_code == 409
    assert resp.json()["code"] == "EVAL_RUNNING"


def test_run_eval_recovers_stale_running_batch(seeded, monkeypatch) -> None:
    """进程重启留下的僵尸 running 批次（超过阈值）不应永久阻塞评测。"""
    conn = seeded["conn"]
    client = seeded["client"]
    monkeypatch.setattr(admin_evals, "run_cases", _fake_runner({}))
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into eval_runs (started_at, model_snapshot, metrics, status)
            values (now() - interval '3 hours', '{}'::jsonb, '{}'::jsonb, 'running')
            returning id
            """
        )
        stale_id = int(cur.fetchone()[0])
    conn.commit()
    seeded["created"]["runs"].append(stale_id)

    resp = client.post("/admin/evals/run", json={})
    assert resp.status_code == 202
    seeded["created"]["runs"].append(int(resp.json()["id"][4:]))
    with conn.cursor() as cur:
        cur.execute("select status from eval_runs where id = %s", (stale_id,))
        assert cur.fetchone()[0] == "failed"


def test_run_eval_marks_failed_when_runner_raises(seeded, monkeypatch) -> None:
    client = seeded["client"]

    def boom(*, cases, settings, conn):
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(admin_evals, "run_cases", boom)
    case_id = client.get("/admin/evals").json()["cases"][0]["id"]
    resp = client.post("/admin/evals/run", json={"caseIds": [case_id]})
    assert resp.status_code == 202
    run_id = resp.json()["id"]
    seeded["created"]["runs"].append(int(run_id[4:]))
    detail = client.get(f"/admin/evals/{run_id}").json()
    assert detail["status"] == "failed"


# ============================================================
# 统计：口径与一致性
# ============================================================
@pytest.fixture()
def stats_seeded(seeded):
    conn = seeded["conn"]
    created = seeded["created"]
    engineer_a = _ensure_user(conn, A_ACCOUNT, "甲工程师", created)
    engineer_b = _ensure_user(conn, B_ACCOUNT, "乙工程师", created)

    session_a = _new_session(conn, created, engineer_id=engineer_a, created_at=BASE_DAY)
    _new_round(conn, session_a, asked_at=BASE_DAY + timedelta(minutes=1), resolved=True)
    _new_round(
        conn,
        session_a,
        asked_at=BASE_DAY + timedelta(hours=1),
        resolved=False,
        question="制动器间隙怎么调整",
    )
    with conn.cursor() as cur:
        cur.execute(
            "insert into handoffs (session_id, context, created_at) values (%s, '{}'::jsonb, %s)",
            (session_a, BASE_DAY + timedelta(minutes=10)),
        )
    conn.commit()

    session_b = _new_session(conn, created, engineer_id=engineer_b, created_at=BASE_DAY)
    _new_round(
        conn,
        session_b,
        asked_at=BASE_DAY + timedelta(hours=2),
        refused=True,
        gate_reason="gap",
        question="门机控制器 E30 怎么处理",
    )

    # 未识别：声明了工号但不在册，engineer_id 为空 → 归 __unidentified__
    session_u = _new_session(
        conn,
        created,
        engineer_id=None,
        auth_source="anonymous",
        claimed="E9999",
        created_at=BASE_DAY,
    )
    _new_round(
        conn,
        session_u,
        asked_at=BASE_DAY + timedelta(hours=3),
        resolved=True,
        question="轿厢异响",
    )

    # 管理员自测：channel=admin_preview，必须被所有统计排除
    session_p = _new_session(
        conn,
        created,
        engineer_id=None,
        channel="admin_preview",
        auth_source="anonymous",
        created_at=BASE_DAY,
    )
    _new_round(conn, session_p, asked_at=BASE_DAY + timedelta(hours=4), resolved=True)

    return {**seeded, "engineer_a": engineer_a, "engineer_b": engineer_b}


def test_stats_overview_and_cross_consistency(stats_seeded) -> None:
    client = stats_seeded["client"]

    overview = client.get(f"/admin/stats/overview?week={WEEK}")
    assert overview.status_code == 200
    body = overview.json()
    assert body["week"] == WEEK
    kpi = body["kpi"]
    # 4 轮（3 轮可识别 + 1 轮未识别）；admin_preview 的那轮不计入
    assert kpi["questions"] == 4
    assert kpi["resolveRate"] == pytest.approx(1 / 3, abs=1e-4)
    assert kpi["refusalRate"] == pytest.approx(1 / 4, abs=1e-4)
    assert kpi["handoffRate"] == pytest.approx(1 / 3, abs=1e-4)
    assert body["sessionResolveRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert body["recognizableRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert body["badCases"] == 2  # 一条未解决标记 + 一条知识缺口拒答
    assert len(body["weekly"]) == 6
    assert body["weekly"][-1]["week"] == "W05"
    assert body["weekly"][-1]["resolveRate"] == pytest.approx(kpi["resolveRate"], abs=1e-4)
    assert body["weekly"][-1]["questions"] == 4
    assert body["topQuestions"][0]["count"] == 1
    assert sum(bucket["count"] for bucket in body["firstFixBuckets"]) == 2

    north = client.get(f"/admin/stats/north-star?week={WEEK}").json()
    assert north["north"] == {"asked": 3, "resolved": 1}
    assert north["resolveRate"] == pytest.approx(1 / 3, abs=1e-4)
    assert north["sessionResolveRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert north["recognizableRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert north["threshold"] == 0.8
    assert "低于阈值" in north["note"]

    recognizability = client.get(f"/admin/stats/recognizability?week={WEEK}").json()
    assert recognizability["totalSessions"] == 3
    assert recognizability["identifiableSessions"] == 2
    assert recognizability["recognizableRate"] == pytest.approx(2 / 3, abs=1e-4)
    assert sum(item["sessions"] for item in recognizability["bySource"]) == 3
    sources = {item["source"]: item["label"] for item in recognizability["bySource"]}
    assert sources["anonymous"] == "未识别"
    assert sources["host_injected"] == "宿主注入"

    # 周报与看板同源：同一批数字
    report = client.get(f"/admin/reports/weekly?week={WEEK}")
    assert report.status_code == 200
    report_body = report.json()
    assert report_body["week"] == WEEK
    assert report_body["format"] == "markdown"
    assert report_body["range"].endswith(WEEK)
    sections = report_body["sections"]
    assert len(sections) == 8
    assert [section["title"] for section in sections[:2]] == [
        "一、本周摘要",
        "二、北极星与统计口径",
    ]
    assert sections[0]["metrics"][0]["label"] == "提问量"
    assert sections[0]["metrics"][0]["value"] == 4
    assert sections[1]["metrics"][0]["value"] == "1 / 3 轮"
    assert sections[7]["body"], "口径与限制必须有说明"
    assert any("__unidentified__" in item["body"] for item in sections if item["body"])

    # 附加字段 markdown：由服务端同一批聚合生成（数字不另算）
    markdown = report_body["markdown"]
    assert isinstance(markdown, str) and markdown.startswith("# 客服智能体 · 周报")
    assert "## 一、本周摘要" in markdown
    assert "- **提问量**：4 次" in markdown
    assert "__unidentified__" in markdown


def test_stats_engineers_pagination(stats_seeded) -> None:
    client = stats_seeded["client"]
    resp = client.get(f"/admin/stats/engineers?week={WEEK}&page=1&pageSize=100")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["page"] == 1
    assert body["pagination"]["pageSize"] == 100
    assert body["pagination"]["total"] >= 3

    by_account = {item["account"]: item for item in body["items"]}
    engineer_a = by_account[A_ACCOUNT]
    assert engineer_a["questions"] == 2
    assert engineer_a["resolveRate"] == pytest.approx(0.5, abs=1e-4)
    assert engineer_a["state"] in {"active", "idle"}
    assert engineer_a["lastActive"]

    engineer_b = by_account[B_ACCOUNT]
    assert engineer_b["questions"] == 1
    assert engineer_b["resolveRate"] == 0.0

    sentinel = by_account["__unidentified__"]
    assert sentinel["questions"] == 1
    assert sentinel["resolveRate"] == 1.0
    assert sentinel["name"] == "未识别"

    # 按人分布与周报里的"使用情况"同源：口径必须逐项对得上
    report = client.get(f"/admin/reports/weekly?week={WEEK}").json()
    usage = next(item for item in report["sections"] if item["title"] == "三、使用情况")
    labels = {metric["label"]: metric["value"] for metric in usage["metrics"]}
    expected = {"正常使用人数": 0, "近期少用人数": 0, "长期未使用人数": 0}
    label_of_state = {
        "active": "正常使用人数",
        "idle": "近期少用人数",
        "inactive": "长期未使用人数",
    }
    for item in body["items"]:
        if item["account"] == "__unidentified__":
            continue
        expected[label_of_state[item["state"]]] += 1
    for key, value in expected.items():
        assert labels[key] == value
    assert expected["正常使用人数"] >= 2, "两个种子工程师应被判为正常使用"


def test_stats_empty_week_returns_zero_without_fabrication(stats_seeded) -> None:
    client = stats_seeded["client"]

    overview = client.get(f"/admin/stats/overview?week={EMPTY_WEEK}").json()
    assert overview["kpi"] == {
        "questions": 0,
        "resolveRate": 0.0,
        "refusalRate": 0.0,
        "handoffRate": 0.0,
    }
    assert overview["sessionResolveRate"] == 0.0
    assert overview["recognizableRate"] == 0.0
    assert overview["badCases"] == 0
    # 空周 = 2099-W06；上一周（W05）是种子数据，趋势里应能看到
    assert overview["weekly"][-1]["questions"] == 0
    assert overview["weekly"][-2]["questions"] == 4
    assert all(point["questions"] == 0 for point in overview["weekly"][:4])
    assert overview["topQuestions"] == []
    assert sum(bucket["count"] for bucket in overview["firstFixBuckets"]) == 0

    north = client.get(f"/admin/stats/north-star?week={EMPTY_WEEK}").json()
    assert north["north"] == {"asked": 0, "resolved": 0}
    assert north["resolveRate"] == 0.0

    recognizability = client.get(f"/admin/stats/recognizability?week={EMPTY_WEEK}").json()
    assert recognizability["totalSessions"] == 0
    assert recognizability["identifiableSessions"] == 0
    assert recognizability["recognizableRate"] == 0.0
    assert recognizability["bySource"] == []

    report = client.get(f"/admin/reports/weekly?week={EMPTY_WEEK}")
    assert report.status_code == 200
    assert len(report.json()["sections"]) == 8


def test_stats_reject_invalid_week(seeded) -> None:
    client = seeded["client"]
    for url in (
        "/admin/stats/overview?week=2026-42",
        "/admin/stats/overview?week=2099-W99",
        "/admin/reports/weekly?week=not-a-week",
    ):
        resp = client.get(url)
        assert resp.status_code == 422, url
        assert resp.json()["code"] == "VALIDATION_ERROR"
