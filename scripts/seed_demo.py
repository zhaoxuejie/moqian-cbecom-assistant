"""演示站数据种子（幂等）：公开演示码 + 演示客服账号。

    python -m scripts.seed_demo --password <your-demo-password>

**不导入知识库**：语料导入步骤见 README.md 的快速开始：

    python -m app.import_docs --root /app/data/demo-corpus --parse-now --no-enqueue

为什么恰好是这三个账号：Widget 首页的「宿主注入身份」下拉写的就是这三个人
（`frontend/widget/index.html` 的 IDENTITIES），用来验证「宿主声明工号 → 后端裁决」
这条链路；账号名即工号，会话、问答日志与导出证据都按它对账。

重复执行不会改已有账号的密码，也不会产生重复数据。演示码只做分流与归因，
不是安全边界——成本上限由 `DEMO_*` 配额保证（见 `app/demo_gate.py`）。
"""

from __future__ import annotations

import argparse

import psycopg

from app import users
from app.config import get_settings
from app.db import connect
from app.security import hash_password

# 与 Widget 宿主演示页的身份下拉保持同一份名单
DEMO_USERS: tuple[tuple[str, str], ...] = (
    ("agent_alice", "客服 Alice"),
    ("agent_bob", "客服 Bob"),
    ("agent_chen", "客服 Chen"),
)

def ensure_public_code(conn: psycopg.Connection, code: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into demo_access_codes (code, label, channel) "
            "values (%s, '官网入口', '官网') on conflict (code) do nothing",
            (code,),
        )
        created = cur.rowcount > 0
    conn.commit()
    print(("[新建] " if created else "[已存在] ") + f"公开码 {code}")


def ensure_demo_users(conn: psycopg.Connection, password: str) -> None:
    created = 0
    for account, name in DEMO_USERS:
        if users.get_user_by_account(conn, account):
            continue
        users.create_user(
            conn,
            account=account,
            name=name,
            role="user",
            credential="local",
            password_hash=hash_password(password),
        )
        created += 1
    print(f"演示账号：新建 {created} 个，已有 {len(DEMO_USERS) - created} 个")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="演示站数据种子（幂等）")
    parser.add_argument(
        "--password",
        required=True,
        help="演示账号初始密码（必填，只对新建账号生效）",
    )
    args = parser.parse_args(argv)

    if not args.password.strip():
        parser.error("--password 不能为空")

    settings = get_settings()
    conn = connect()
    try:
        ensure_public_code(conn, settings.demo_public_code)
        ensure_demo_users(conn, args.password)

        with conn.cursor() as cur:
            cur.execute("select count(*) from documents")
            documents = int(cur.fetchone()[0])
            cur.execute("select count(*) from chunks")
            chunks = int(cur.fetchone()[0])
        print(f"知识库现状：documents={documents}，chunks={chunks}")
        if documents == 0:
            print("提示：知识库还是空的，按 README 导入演示语料后再对客户开放。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
