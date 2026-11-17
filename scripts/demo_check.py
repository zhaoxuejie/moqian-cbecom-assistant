"""M0 演示前自检：一条命令确认"演示环境能不能上场"。

检查项：服务健康、静态页可访问、语料/向量完整、Ollama 可用、模型可用、后台可登录、任务队列状态。
用法：python scripts/demo_check.py [--base-url http://127.0.0.1:8002]

会调用模型、创建登录审计与访客会话；任一 FAIL 退出码非 0（方便演示前一键确认）。
"""

from __future__ import annotations

import argparse
import time
from typing import Any

import httpx

from app.config import get_settings
from app.db import connect
from app.embedding import get_embedding_client
from modules.llm import LlmClient, config_from_settings

_results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    _results.append((name, status, detail))


def run(name: str, fn) -> Any:
    started = time.perf_counter()
    try:
        detail = fn()
        elapsed = int((time.perf_counter() - started) * 1000)
        record(name, "PASS", f"{detail}（{elapsed}ms）")
        return True
    except Exception as exc:  # noqa: BLE001 - 自检脚本要把失败原因全部展示出来
        record(name, "FAIL", f"{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    settings = get_settings()

    with httpx.Client(timeout=20.0) as client:

        def health() -> str:
            resp = client.get(f"{base}/health")
            resp.raise_for_status()
            data = resp.json()
            return f"{data['status']} · baseline {data['baseline']} · env {data['env']}"

        run("服务健康", health)

        def static_pages() -> str:
            # 入口 URL 允许 307 跳到分层目录（/admin-app/ → /admin-app/pages/），按浏览器行为跟随
            for path in ("/widget-app/index.html", "/widget-app/widget.html", "/admin-app/"):
                resp = client.get(base + path, follow_redirects=True)
                if resp.status_code != 200:
                    raise RuntimeError(f"{path} -> {resp.status_code}")
            return "widget 宿主页 / 浮层 / 后台登录页均 200"

        run("静态页面", static_pages)

        def corpus() -> str:
            with connect() as conn, conn.cursor() as cur:
                cur.execute("select count(*) from documents where status <> 'disabled'")
                documents = cur.fetchone()[0]
                cur.execute("select count(*) from chunks where disabled = false")
                chunks = cur.fetchone()[0]
                cur.execute(
                    "select count(*) from chunks where disabled = false and embedding is null"
                )
                missing = cur.fetchone()[0]
            if documents == 0 or chunks == 0:
                raise RuntimeError("语料为空：先跑 python -m app.import_docs")
            if missing:
                raise RuntimeError(f"{missing} 个切片没有向量：先补 embedding")
            return f"{documents} 份文档 / {chunks} 个切片，向量齐全"

        run("知识库", corpus)

        def ollama() -> str:
            client_ = get_embedding_client(settings)
            vector = client_.embed_one("What is the US return window?")
            return f"{settings.embedding_model} · {len(vector)} 维"

        run("Embedding", ollama)

        def llm() -> str:
            if not settings.llm_api_key:
                raise RuntimeError("LLM_API_KEY 未配置（检查 .env）")
            started = time.perf_counter()
            result = LlmClient(config_from_settings(settings)).complete(
                [{"role": "user", "content": "只回复两个字：正常"}],
                max_tokens=16,
                temperature=0.0,
            )
            elapsed = int((time.perf_counter() - started) * 1000)
            return (
                f"{settings.llm_model} · 首包 {elapsed}ms · {result.text.strip()[:20]}"
            )

        run("LLM", llm)

        def admin_login() -> str:
            if not settings.admin_initial_password:
                return "SKIP：未配置 ADMIN_INITIAL_PASSWORD"
            resp = client.post(
                f"{base}/admin/auth/login",
                json={
                    "account": settings.admin_initial_account,
                    "password": settings.admin_initial_password,
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"登录失败（{resp.status_code}）：{resp.text[:120]}")
            return f"{settings.admin_initial_account} 登录成功"

        run("后台登录", admin_login)

        def widget_session() -> str:
            payload: dict[str, Any] = {
                "identity": {"source": "host_injected"},
                "hostContext": {
                    "orderNo": "WB-10001",
                    "country": "US",
                    "locale": "en",
                },
            }
            # 演示门禁打开时必须带码，否则 /widget/session 一律 401。
            # 自检用公开码：它只做归因与配额归属，不占专属码的额度。
            if settings.demo_enabled and settings.demo_require_code:
                payload["demoCode"] = settings.demo_public_code
            resp = client.post(f"{base}/widget/session", json=payload)
            resp.raise_for_status()
            session_id = resp.json()["sessionId"]
            return f"会话 {session_id} 建立成功"

        run("Widget 会话", widget_session)

        def jobs() -> str:
            token = ""
            if settings.admin_initial_password:
                login = client.post(
                    f"{base}/admin/auth/login",
                    json={
                        "account": settings.admin_initial_account,
                        "password": settings.admin_initial_password,
                    },
                )
                if login.status_code == 200:
                    token = login.json()["token"]
            if not token:
                return "SKIP：未配置管理员口令，无法读取"
            resp = client.get(
                f"{base}/admin/jobs?withStats=1", headers={"Authorization": f"Bearer {token}"}
            )
            resp.raise_for_status()
            counts = (resp.json().get("stats") or {}).get("counts") or {}
            return "排队 {queued} / 运行 {running} / 完成 {done} / 失败 {failed}".format(
                **{key: counts.get(key, 0) for key in ("queued", "running", "done", "failed")}
            )

        run("任务队列", jobs)

    width = max(len(name) for name, _, _ in _results) + 2
    print("\n跨境客服环境自检")
    print("-" * 78)
    for name, status, detail in _results:
        print(f"{name:<{width}} {status:<5} {detail}")
    print("-" * 78)
    failed = [item for item in _results if item[1] == "FAIL"]
    skipped = [item for item in _results if item[1] == "SKIP"]
    print(
        f"结果：{len(_results) - len(failed)} 项通过，"
        f"{len(failed)} 项失败，{len(skipped)} 项跳过"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
