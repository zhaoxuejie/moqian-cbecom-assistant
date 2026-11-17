"""mock 数据访问：只读 SQL，返回 dict 或 None（不存在就是 None，不编）。

表由迁移 `0012_mock_commerce.sql` 建立并写入 15 条确定性数据。
"""

from __future__ import annotations

from typing import Any

import psycopg


def query_order(conn: psycopg.Connection, order_no: str) -> dict[str, Any] | None:
    """按订单号查订单；带出物流概览（有单号时）。"""
    normalized = (order_no or "").strip().upper()
    if not normalized:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            select order_no, country, status, payment_status, address, tracking_no, anomaly,
                   updated_at
              from mock_orders
             where order_no = %s
            """,
            (normalized,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    data: dict[str, Any] = {
        "orderNo": row[0],
        "country": row[1],
        "status": row[2],
        "paymentStatus": row[3],
        "address": row[4] or {},
        "trackingNo": row[5],
        "anomaly": row[6],
        "updatedAt": row[7].isoformat() if row[7] else None,
    }
    if row[5]:
        shipment = query_logistics(conn, row[5])
        if shipment:
            data["shipment"] = {
                "carrier": shipment["carrier"],
                "currentNode": shipment["currentNode"],
                "eta": shipment["eta"],
                "anomaly": shipment["anomaly"],
            }
    return data


def query_logistics(conn: psycopg.Connection, tracking_no: str) -> dict[str, Any] | None:
    """按物流单号查轨迹。"""
    normalized = (tracking_no or "").strip().upper()
    if not normalized:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            select tracking_no, order_no, carrier, events, current_node, eta, anomaly
              from mock_shipments
             where tracking_no = %s
            """,
            (normalized,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "trackingNo": row[0],
        "orderNo": row[1],
        "carrier": row[2],
        "events": row[3] or [],
        "currentNode": row[4],
        "eta": row[5].isoformat() if row[5] else None,
        "anomaly": row[6],
    }
