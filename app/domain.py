"""跨境客服领域词汇与提示词：**行业相关的部分集中在这里**，不进 `modules`、不进 widget 代码。

拆分线（AGENTS.md §5.4）：检索 / 证据门 / 引用校验是机制（`modules`）；
订单、退款、国家政策、话术是词汇（`app`），部署时可按品牌替换。

本文件只放"词汇"：高风险词表、系统提示词、缺口/冲突/转人工话术、证据前缀与来源标签。
意图分流与槽位抽取在 `app/intent.py`，别在这里再加规则。
"""

from __future__ import annotations

import re

# 高风险话题词表（PRD §4.2 四类硬转）：命中即短路，不生成任何承诺、金额与时限，转人工。
#
# 采用**短语**而不是单词：裸 `refund` 会把 "What is your refund policy?"（正常政策问答）
# 也拦下来。这里只匹配"表达诉求"的说法。
# P4 会把本表迁到 app_config（后台可开关），并补 casefold 匹配
#（当前 match_safety_word 区分大小写）。
DEFAULT_SAFETY_WORDS: tuple[str, ...] = (
    # 退款 / 金额
    "want a refund", "need a refund", "refund me", "give me a refund", "refund my order",
    "want my money", "money back", "chargeback", "charge back", "paypal dispute",
    "quiero un reembolso", "reembolsad", "je veux un remboursement", "remboursez",
    "ich will mein geld zurück",
    # 投诉 / 差评威胁
    "file a complaint", "make a complaint", "complain to", "bad review", "leave a review",
    "negative review", "trustpilot", "1-star", "one star review",
    "queja", "reclamación", "réclamation", "beschwerde",
    # 法务
    "lawyer", "attorney", "sue you", "lawsuit", "legal action", "take legal",
    "press charges", "abogado", "demanda", "avocat", "poursuite", "anwalt", "klage",
    # 账户安全
    "hacked", "account was stolen", "account stolen", "unauthorized charge",
    "unauthorised charge", "charge i didn't make", "someone used my card",
    "account security", "hackearon", "cargo no autorizado", "compte piraté", "konto gehackt",
)

# 骨架模式的系统提示词：明确告知"没有检索、不要编"，避免演示时把无依据输出当正式答案。
SKELETON_SYSTEM_PROMPT = (
    "You are a connectivity test channel for a customer support assistant. "
    "No knowledge base is connected in this mode. Reply in one or two sentences confirming "
    "the link works, and say that the production version answers with cited sources. "
    "Do not invent policies, prices, order details, delivery dates or legal/financial advice."
)

# 骨架模式给访客的固定前缀：让"这不是正式答案"在界面上可见
SKELETON_NOTICE = (
    "(Skeleton mode: no knowledge base connected — this reply only verifies connectivity, "
    "it is not an answer.)"
)

# 高风险拦截卡文案（widget 侧可见）：只说明"已转人工"与联系方式，不含任何承诺
SAFETY_TITLE = "I'm connecting you with a human agent"
SAFETY_BODY = (
    "Refunds, complaints, legal matters and account security are handled by our support team, "
    "not by this assistant — I can't make promises about money, compensation or timelines.\n"
    "A human agent will pick this up. You can also email support@wanderbrim.com "
    "(Mon–Fri 9:00–17:00 MST)."
)

# 证据 ID 前缀（行业词汇）：前缀进 `chunks.evidence_id`，看到 ID 能猜出来自哪类资料。
#
# 约束：`modules/citation/validator.py` 的正则只接受 **1–2 个大写字母** + 3–10 位数字
# （形如 [RP01001]）。所以不能用 POL-/SIZE- 这种三字母前缀——引用会被判非法并整条剥离，
# 表现为"回答了但没有引用"，很难排查。这里统一用两字母编码。
EVIDENCE_ID_PREFIX: dict[str, str] = {
    # 跨境语料分类（P1 起的正式分类）
    "policy": "RP",  # returns & refunds policy
    "shipping": "SP",  # shipping & delivery
    "size": "SZ",  # size guide
    "payment": "PM",  # payment FAQ
    "promotion": "PR",  # promotion terms
    "privacy": "PV",  # privacy & GDPR
    "order": "OP",  # order modification / cancellation
    "refund": "RF",  # refund process FAQ
    "faq": "FQ",  # product / B2B FAQ
    # legacy：制造业分类，保留是为了历史数据仍能解析；新语料不再使用
    "manual": "M",
    "ticket": "T",
    "drawing": "D",
    "excel": "X",
    "table": "X",
}
DEFAULT_EVIDENCE_PREFIX = "E"

# 兜底转人工文案（P4 起由后台配置覆盖；这里给 app 层默认值）
DEFAULT_HANDOFF_TEXT = (
    "Connecting you to a human agent — support@wanderbrim.com, Mon–Fri 9:00–17:00 MST."
)

# ---------- 带证据回答的提示词与话术（行业词汇，只留在 app 层） ----------
#
# 客户服务口径的三条硬约束：
#   1) 政策口径以英文知识库原文为准，翻译只换语言、不改数字与范围；
#   2) 事实（订单状态、物流节点、到达时间）只能来自工具返回，不许"根据常识"补全；
#   3) 冲突的政策版本并列展示并标注生效日期，系统不替客户选。
RAG_SYSTEM_PROMPT = (
    "You are the customer support assistant for Wanderbrim Headwear, a direct-to-consumer store. "
    "Follow these rules strictly:\n"
    "1. Use ONLY the evidence blocks provided. Never use outside knowledge and never invent "
    "order numbers, tracking events, dates, amounts, prices or policy terms;\n"
    "2. End every step with the evidence id in square brackets, e.g. [RP01001]. "
    "If one step relies on several sources, write [RP01001][SP02003];\n"
    "3. If the evidence contains relevant policy text, answer from it. Do not refuse just "
    "because the evidence does not mention this customer's exact order;\n"
    "4. If sources disagree (for example an older and a newer policy version), present each "
    "with its effective dates and scope. Never pick one for the customer and never hide one;\n"
    "5. Only when nothing in the evidence is relevant, reply exactly: "
    "\"I don't have verified information about that.\" Do not guess;\n"
    "6. Never promise refunds, compensation, delivery dates or legal outcomes. Refunds, "
    "complaints, legal matters and account security belong to a human agent;\n"
    "7. Answer in the same language as the customer's question — including units and dates "
    "(\"14 días\" / \"14 jours\" / \"14 Tage\"). Keep the numeric value and country exactly as "
    "written in the evidence;\n"
    "8. Output at most 8 numbered lines (\"1. \"), one step per line. No headings, no preamble, "
    "no closing summary."
)

GAP_TITLE = "I don't have verified information for that"
GAP_BODY = (
    "I couldn't find a policy or FAQ entry that directly answers this question. "
    "To avoid giving you something I can't back up with a source, I'm not answering this time.\n"
    "The question has been logged as a knowledge gap so our team can add the missing document."
)

CONFLICT_MESSAGE = (
    "I found more than one policy version that could apply, and they differ. "
    "They are listed side by side with their effective dates — the right one depends on when "
    "your order was placed. If you're not sure, our support team can confirm."
)

# 证据来源标题（客户能看到的引用卡片标题）。
#
# 为什么在 app 层改写：`modules/retrieval/types.py::to_contract()` 里 title 写死成
# 《维修手册》§…、kind 写死 "manual" —— 这是上游模块里残留的行业词汇。
# 按纪律（行业词汇不进公共模块）本应由数据提供，但公共层这轮不改，
# 所以由 app 在这里按证据前缀覆盖标题；kind 保持契约枚举不变。
EVIDENCE_KIND_LABELS: dict[str, str] = {
    "RP": "Returns & refunds policy",
    "SP": "Shipping & delivery policy",
    "SZ": "Size guide",
    "PM": "Payment FAQ",
    "PR": "Promotion terms",
    "PV": "Privacy & GDPR",
    "OP": "Order modification policy",
    "RF": "Refund process FAQ",
    "FQ": "Product FAQ",
    # legacy：历史数据仍可能带这些前缀
    "M": "Knowledge base",
    "T": "Support ticket",
    "F": "FAQ",
    "D": "Drawing",
    "X": "Data table",
    "E": "Knowledge base",
}


def citation_title(evidence_id: str) -> str:
    """按证据 ID 前缀给出来源标题（`RP424002` → Returns & refunds policy）。"""
    match = re.match(r"([A-Z]+)", (evidence_id or "").strip())
    prefix = match.group(1) if match else ""
    return EVIDENCE_KIND_LABELS.get(prefix, "Knowledge base")
