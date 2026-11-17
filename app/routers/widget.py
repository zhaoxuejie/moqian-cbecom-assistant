"""工程师端路由：浮层配置、会话建立、引用原文、反馈、转人工（S1 起，S4 补齐）。

文案、示例问题、品牌色全部由 app 层注入（纪律 6），widget 代码里不写死行业词。
`/widget/*` 只认 `X-Widget-Session`（双链隔离），与 `/admin/*` 的 JWT 互不通用。
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, status

from .. import demo_gate, widget_service
from ..config import Settings, get_settings
from ..domain import DEFAULT_HANDOFF_TEXT
from ..messages import _session_pk
from ..runtime_config import load_config
from ..schemas import FeedbackRequest, HandoffRequest, SessionRequest
from ..sessions import create_session, get_session
from .deps import get_conn

router = APIRouter(prefix="/widget", tags=["widget"])

# 示例问题属行业词汇，放 app 层；后台「系统配置」页可覆盖（只存文本）。
# 跨境口径：政策 / 物流 / 尺码三类最常见，示例按这个顺序给。
DEFAULT_EXAMPLES = (
    {"q": "How long is the US return window?", "tag": "Returns", "scene": ""},
    {"q": "Where is my order WB-10001?", "tag": "Shipping", "scene": ""},
    {"q": "Which size should I choose for a 57 cm head?", "tag": "Sizing", "scene": ""},
)

# 浮层界面文案：由接口下发，widget 代码里不写死（纪律 6）。
# 这些是**产品文案**，不是行业知识；后台可覆盖 widget 组里的同名键。
UI_DEFAULTS: dict[str, str] = {
    "productName": "Wanderbrim Support",
    "inputPlaceholder": "Ask about shipping, returns, sizing or an order…",
    "footerNote": "AI-generated. Policy answers cite the knowledge base — check the sources.",
}

# 结构化卡字段标签：P2 起字段会换成 intent/orderNo/country，这里先两组都映射好，
# 前端按 key 取，取不到用字段名本身。
FIELD_LABELS_DEFAULTS: dict[str, str] = {
    "symptom": "Topic",
    "model": "Order",
    "batch": "Country",
    "code": "Reference",
    "intent": "Intent",
    "orderNo": "Order",
    "country": "Country",
    "locale": "Language",
}


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
    )


def require_widget_session(
    conn: psycopg.Connection = Depends(get_conn),
    x_widget_session: str | None = Header(default=None, alias="X-Widget-Session"),
) -> dict:
    """会话校验：缺失 / 不存在 / 已失效一律 401（不泄露会话是否存在之外的细节）。"""
    if not x_widget_session:
        raise _unauthorized("UNAUTHORIZED", "Session missing - start a session first")
    session = get_session(conn, x_widget_session)
    if session is None:
        raise _unauthorized("SESSION_INVALID", "Session not found or expired")
    return session


@router.get("/config")
def widget_config(
    conn=Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict:
    """浮层配置：`app_config` 的 widget/brand 组覆盖代码默认值（D-012）。"""
    overrides = load_config(conn, ("widget", "brand"))
    widget = overrides.get("widget", {})
    brand = overrides.get("brand", {})
    enabled, paused = demo_gate.demo_state(conn, settings)
    samples = widget.get("samples")
    examples = list(DEFAULT_EXAMPLES)
    if isinstance(samples, list) and samples:
        examples = []
        for index, text in enumerate(samples):
            text = str(text).strip()
            if not text:
                continue
            base = DEFAULT_EXAMPLES[index] if index < len(DEFAULT_EXAMPLES) else {}
            examples.append(
                {
                    "q": text,
                    "tag": base.get("tag", "Example"),
                    "scene": base.get("scene", ""),
                }
            )
    return {
        "welcome": widget.get("welcome")
        or "Hi! I can help with orders, shipping, returns and our policies — "
        "every answer links to the passage it came from.",
        "examples": examples,
        "handoffText": widget.get("handoffText") or DEFAULT_HANDOFF_TEXT,
        "brandColor": widget.get("brandColor")
        or brand.get("primaryColor")
        or "#b25c2a",
        # 界面文案：产品名 / 输入框提示 / 页脚声明 / 结构字段标签（纪律 6：不进 widget 代码）
        "ui": {
            **UI_DEFAULTS,
            **{
                key: str(widget[key]).strip()
                for key in UI_DEFAULTS
                if str(widget.get(key) or "").strip()
            },
            "fieldLabels": {
                **FIELD_LABELS_DEFAULTS,
                **(widget.get("fieldLabels") or {}),
            },
        },
        # 多语言：英文知识库为唯一口径；en/es 上界面，fr/de 仅评测覆盖
        "locales": [part.strip() for part in settings.supported_locales.split(",") if part.strip()],
        "features": {
            "evidence": True,
            "feedback": True,
            "handoff": True,
            # P3 接入 mock 订单/物流工具；前端据此决定是否显示"查询订单"入口
            "tools": True,
        },
        # 演示站信息：前端据此决定"没有入口码 / 被暂停"的提示与剩余轮次。
        # 只下会话级轮次（用户看得见的那一句），不下每日额度——那是运营参数。
        "demo": {
            "enabled": enabled,
            "paused": paused,
            "requiresCode": enabled and settings.demo_require_code,
            "sessionTurns": int(settings.demo_session_turns) if enabled else 0,
            "bookingUrl": settings.demo_booking_url,
            "pausedTitle": demo_gate.PAUSED_TITLE if paused else "",
            "pausedText": demo_gate.PAUSED_BODY if paused else "",
        },
    }


@router.post("/session")
def create_widget_session(
    payload: SessionRequest,
    conn: psycopg.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict:
    demo_code: str | None = None
    if settings.demo_enabled:
        _, paused = demo_gate.demo_state(conn, settings)
        if paused:
            # 后台总开关关闭：即使带了入口码也不建会话（否则绕过"暂停"）
            raise _unauthorized("DEMO_PAUSED", demo_gate.PAUSED_BODY)
        code = (payload.demo_code or "").strip()
        if settings.demo_require_code:
            if not code:
                raise _unauthorized(
                    "DEMO_CODE_REQUIRED",
                    "An access code is required for this demo — open it from the official "
                    "case page.",
                )
            if demo_gate.verify_access_code(conn, code) is None:
                raise _unauthorized(
                    "DEMO_CODE_INVALID",
                    "This access code is invalid or expired. Reopen the demo from the "
                    "website, or book a walkthrough.",
                )
        demo_code = code or None
    return create_session(
        conn,
        requested_source=payload.identity.source,
        claimed_account=payload.identity.claimed_engineer_id,
        host_context=payload.host_context.to_dict() if payload.host_context else None,
        demo_code=demo_code,
    )


@router.get("/evidence/{evidence_id}")
def get_widget_evidence(
    evidence_id: str,
    session: dict = Depends(require_widget_session),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict:
    evidence = widget_service.get_evidence(
        conn, session_id=f"ses-{session['id']}", evidence_id=evidence_id
    )
    if evidence is None:
        # 只在"本会话引用过"的证据里找；没有就 404，不泄露库内其它切片
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "EVIDENCE_NOT_FOUND",
                "message": "This citation is not part of the current evidence set",
            },
        )
    return evidence


@router.post("/feedback")
def post_widget_feedback(
    payload: FeedbackRequest,
    session: dict = Depends(require_widget_session),
    conn: psycopg.Connection = Depends(get_conn),
) -> dict:
    if _session_pk(payload.session_id) != session["id"]:
        raise _unauthorized("SESSION_INVALID", "Session mismatch")
    recorded = widget_service.submit_feedback(
        conn,
        session_id=f"ses-{session['id']}",
        message_id=payload.message_id,
        feedback=payload.type,
        note=payload.note,
    )
    if not recorded:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "MESSAGE_NOT_FOUND", "message": "Message not found in this session"},
        )
    return {"ok": True}


@router.post("/handoff")
def post_widget_handoff(
    payload: HandoffRequest,
    session: dict = Depends(require_widget_session),
    conn: psycopg.Connection = Depends(get_conn),
    settings: Settings = Depends(get_settings),
) -> dict:
    if _session_pk(payload.session_id) != session["id"]:
        raise _unauthorized("SESSION_INVALID", "Session mismatch")
    return widget_service.build_handoff(
        conn,
        session=session,
        message_id=payload.message_id,
        settings=settings,
    )
