---
doc_id: corpus-readme
brand: Wanderbrim Headwear
purpose: 跨境客服智能体样板语料包（虚构数据）
language: en
---

# Wanderbrim Headwear — Knowledge Base Corpus

> **演示数据，全部虚构。** 本目录是 `moqian-cbecom-assistant` 样板项目的知识库语料，
> 共 13 份英文业务文档，用于演示、测试与可复算评测。

## 品牌设定

| 项 | 值 |
|---|---|
| Brand | Wanderbrim Headwear |
| Category | Outdoor sun-protection headwear（UPF 50+ hats, caps, bucket hats, beanies） |
| Founded | 2023（虚构） |
| Market | US 为主，EU（DE/FR/ES）+ UK 扩张中 |
| Price band | $29.99 – $49.99 |
| Support email | support@wanderbrim.com |
| Support hours | Mon–Fri, 9:00–17:00 MST（US Mountain Time） |
| Returns warehouse (US) | Phoenix, AZ |
| Returns warehouse (EU) | Leipzig, Germany（服务 DE/FR/ES） |
| Returns warehouse (UK) | Manchester, UK |

## 目的地国家（5 个）

US / UK / DE / FR / ES。物流渠道、时效、退货政策因国而异。

## 文档清单

| 文件 | 版本状态 | 用途 |
|---|---|---|
| `00-brand-story.md` | current | 品牌介绍、产品线、材质卖点 |
| `01-contact-support-hours.md` | current | 客服时间、时区、联系方式 |
| `02-shipping-policy.md` | **v2.1 current** | 物流配送：渠道、时效、运费、关税、异常处理 |
| `03-return-policy.md` | **v2.0 current** | 退换货：按国家窗口、流程、退款方式 |
| `04-return-policy-v1-archived.md` | **v1.0 archived** | 旧版退货政策（已过期），用于测试"按生效日期过滤过期条目" |
| `05-size-guide.md` | current | 头围尺码表、尺码测量、弹性款说明 |
| `06-product-faq.md` | current | 材质/UPF/防水/洗涤/适用场景 |
| `07-payment-faq.md` | current | 支付失败常见原因与解决 |
| `08-promotion-terms.md` | current | 折扣码、满减、免邮门槛规则 |
| `09-order-modification.md` | current | 修改地址、取消订单、未发货/已发货边界 |
| `10-privacy-gdpr.md` | current | 隐私与 GDPR 简述（跨境合规话术） |
| `11-b2b-wholesale-faq.md` | current | 批发/B2B 询盘（MOQ、报价、交期）；只覆盖 FAQ，不做工单流 |
| `12-refund-process.md` | current | 退款流程与到账时效（退款审批仍是人工，AI 不承诺金额） |

## 评测用"新旧政策冲突"样例

- **退货窗口（美国）**：v1（`04-return-policy-v1-archived.md`，2025-03-01 ~ 2026-02-28）写 **60 天**；v2（`03-return-policy.md`，2026-03-01 起生效）改为 **30 天**。
- 评测：问"What's the US return window?"，当前政策有效期内应答 **30 天**，不能命中 v1 的 60 天。

## 语料元数据约定

每份文档顶部 YAML 含：`doc_id / version / effective_date / expires_on / applies_to / status`。
导入时由 `app/import_docs.py` 解析，切片时元数据进 `chunks.meta_json`，检索按 `effective_date ≤ today < expires_on` 过滤。
