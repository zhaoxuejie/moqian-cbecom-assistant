# 跨境客服分层评测

评测的目的不是"跑个分"，而是回答三个问题：

1. **检索是否命中**：该题的依据有没有被召回（看关键词是否出现在 topN 证据正文里）；
2. **引用是否可核验**：回答的每一步是否都带本轮证据集里的引用；
3. **该拒答时是否拒答、安全题是否拦截**：纪律 3/4 是否在真实问题上成立。

## 怎么跑

```powershell
# 前置：docker compose up -d（db/app/worker）、Ollama 在跑、.env 配好 LLM_*
python -m evals.runner                       # 全量
python -m evals.runner --limit 5             # 先冒烟 5 条
python -m evals.runner --cases evals/cases.yaml --out-dir evals/reports
```

产物：`evals/reports/eval-<时间戳>.json`（机读）与 `.md`（指标表 + 失败清单 + 逐题明细）。

## 评测集写法

```yaml
- id: A-001                            # 前缀 A-/B-/C-/D-/E-，与 layer 一致
  layer: A                             # 必填：A/B/C/D/E
  question: "How long is the US return window?"
  context: {country: US, locale: en}   # 国家/语言，影响过滤与输出语言
  expect:
    keywords: ["30 days"]              # 仅用于检索命中判定
    citationsMin: 1
    noArchived: true                   # 不得引用归档/过期切片（旧版 60 天那条）
    tool: {name: query_order, status: ok}          # B 层
    handoff: {required: true, category: refund}    # C 层
    lang: es                                       # E 层
    refuse: true                                   # D 层
  note: "考什么"
```

`load_cases` 是**严格加载**：缺 id/question、重复 id、illegal layer、同时期望拒答与安全拦截都会直接报错，
不会"跳过几条"悄悄算分。

> 约定：B 层超时用例与 C 层硬转都不生成答案，因此同时标 `refuse: true`（否则会被
> "应作答却拒答"规则误判）；C 层再用 `handoff.category` 断言类别。

## 指标口径

| 指标 | 怎么算 | 说明 |
|---|---|---|
| 检索命中率 | 可回答题中，关键词出现在 topN 证据正文的比例 | 只测检索面，不评判措辞 |
| 作答率 | 可回答题中未拒答的比例 | 与拒答准确率一起看，防止"全拒答"刷分 |
| 引用覆盖率 | 已作答题目里，带引用步骤 / 全部步骤 | 纪律 1 的可观测面 |
| 拒答准确率 | （应拒答且拒答 + 明示缺口 + 应作答且作答）/ 全部 | 应拒答却作答分两档：明示缺口（不算失败）/ 编造作答（算失败） |
| 安全拦截率 | 安全题被 `safe` 拦截的比例 | 纪律 3 |
| 时延 p50/p90 | 单题端到端耗时 | 现状记录，不是目标值 |

## 现状与限制

- 当前 **90 条**：A 政策 30 / B 工具 25 / C 硬转 15 / D 应拒答 10 / E 多语言 10；
  语料为 `samples/demo-corpus` 的 13 份英文虚构文档（含 B2B 与退款流程）与 15 条 mock 订单；
- **A 层**覆盖 US 30 天、UK/DE/FR/ES 14 天、物流时效、尺码、关税/VAT、折扣、隐私、B2B MOQ、退款流程；
  其中 3 条"新旧冲突"带 `noArchived`，用来卡住已归档的旧版 60 天；
- **B 层**覆盖订单状态（shipped/delivered/refunded/cancelled/lost）、物流轨迹（清关停滞/在途）、
  单号不存在、**5 条 API 超时降级**、3 条缺单号应先追问；
- **C 层**四类各 3 条（平静语气）+ 西/法/德各 1 条；
- **D 层**库外品类、无关服务、他人订单、与平台无关的法律意见；
- **E 层**只测"语言正确 + 政策数字与英文原文一致（14 天/6–8/5–7 等）"，**不测翻译质量**；
- 关键词是粗粒度代理指标，会低估"换了说法但引用正确"的回答；
- 每题只跑一次（temperature=0，但推理模型仍有非确定性）；同一题多跑取多数票留待后续；
- 评测依赖真实 LLM 与 Ollama，**不进 CI**（CI 只跑离线单测 `tests/test_evals.py` / `tests/test_eval_layers.py`）；
- 人工基线列（`baseline_answer`）尚未接入。

## 历史参考报告

仅保留源项目 2026-10-28 的跨境评测 JSON/Markdown，未在开源整理时重新调用模型评测。
报告没有完整的模型、硬件与配置快照，因此数字只能作为历史记录，不能视为性能保证。
当前代码、模型或语料变化后应重新评测；真实评测会调用配置的模型与 embedding 服务并可能产生费用。
全量评测需要本机开发安装、可用数据库与已解析语料，不能只在主机执行而依赖容器内配置。
