"""演示入口码管理（仅演示站需要）。

    python scripts/demo_code.py list
    python scripts/demo_code.py create --label "某客户 · 张工" --channel wechat --days 30
    python scripts/demo_code.py disable <code>

码只做分流与归因，不是安全边界：公开码写在官网链接里，等于公开；
真正的成本上限由 `DEMO_*` 配额保证（见 app/demo_gate.py）。
"""

from __future__ import annotations

import argparse
import secrets
from datetime import UTC, datetime, timedelta

import psycopg

from app.db import connect


def _new_code() -> str:
    # 16 位 url-safe 随机串：不追求密码学强度，只是让人猜不出来
    return secrets.token_urlsafe(12)


def cmd_list(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "select code, label, channel, enabled, expires_at, created_at "
            "from demo_access_codes order by created_at desc"
        )
        rows = cur.fetchall()
    if not rows:
        print("（还没有演示码）")
        return 0
    print(f"{'code':<20} {'启用':<4} {'渠道':<10} {'到期':<20} 备注")
    for code, label, channel, enabled, expires_at, _created in rows:
        expires = expires_at.strftime("%Y-%m-%d %H:%M") if expires_at else "长期"
        state = "是" if enabled else "否"
        print(f"{code:<20} {state:<4} {channel or '-':<10} {expires:<20} {label or '-'}")
    return 0


def cmd_create(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    code = args.code or _new_code()
    expires_at = None
    if args.days and args.days > 0:
        expires_at = datetime.now(UTC) + timedelta(days=args.days)
    with conn.cursor() as cur:
        cur.execute(
            "insert into demo_access_codes (code, label, channel, expires_at) "
            "values (%s, %s, %s, %s) on conflict (code) do nothing",
            (code, args.label or "", args.channel or "", expires_at),
        )
        inserted = cur.rowcount > 0
    conn.commit()
    if not inserted:
        print(f"码 {code} 已存在，未改动")
        return 1
    print(f"已创建：{code}")
    print(f"入口链接：{args.base_url.rstrip('/')}/?code={code}")
    if expires_at:
        print(f"有效期至：{expires_at.strftime('%Y-%m-%d %H:%M')} (UTC)")
    return 0


def cmd_disable(conn: psycopg.Connection, args: argparse.Namespace) -> int:
    with conn.cursor() as cur:
        cur.execute("update demo_access_codes set enabled = false where code = %s", (args.code,))
        updated = cur.rowcount
    conn.commit()
    print("已停用" if updated else "未找到该码")
    return 0 if updated else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="演示码管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出所有演示码")

    create = sub.add_parser("create", help="创建演示码")
    create.add_argument("--code", default=None, help="指定码（默认随机 16 位）")
    create.add_argument("--label", default="", help="备注：客户名 / 用途")
    create.add_argument("--channel", default="", help="渠道：官网 / 微信 / 会议")
    create.add_argument("--days", type=int, default=0, help="有效天数（0 = 长期）")
    create.add_argument(
        "--base-url",
        default="http://localhost:8002",
        help="部署地址（含协议），用于打印入口链接",
    )

    disable = sub.add_parser("disable", help="停用演示码")
    disable.add_argument("code")

    args = parser.parse_args(argv)
    conn = connect()
    try:
        if args.cmd == "list":
            return cmd_list(conn)
        if args.cmd == "create":
            return cmd_create(conn, args)
        return cmd_disable(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
