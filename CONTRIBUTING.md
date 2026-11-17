# 贡献指南

欢迎提交问题、文档改进与代码。先说明使用场景与复现步骤；较大改动请先通过 Issue 讨论。

## 开发环境

- Python 3.11+，CI 与 Docker 使用 Python 3.12。
- Docker Compose，用于 PostgreSQL 16 + pgvector。
- 模型与 embedding 配置见 [README](README.md#start)；离线测试不需要真实模型。

```bash
python -m venv .venv
# Bash: source .venv/bin/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

前端为静态 HTML/CSS/JavaScript，无需安装 Node 依赖。修改容器内代码后执行
`docker compose up -d --build app worker`。

## 检查与数据库测试

```bash
ruff check .
pytest
python -m evals.runner --dry-run
```

数据库不可用时，相应用例会跳过。全量测试请参照 CI 创建**独立、可丢弃的测试库**，
把 `DATABASE_URL` 指向它，再执行 `python -m app.migrate` 和 `pytest`。
测试会清理、更新测试数据，不能连接真实业务库，也不要与运行中的 worker 共用库。
真实模型评测会产生调用费用，运行方法和指标局限见 [evals/README.md](evals/README.md)。

## 代码与提交

- 最小改动，保持 `app → modules` 单向依赖，公共模块之间互不 import。
- 机制放 `modules/`，跨境业务规则、品牌和文案由 `app/` 装配。
- 接口改动同步 [OpenAPI](contracts/openapi.yaml)、前后端与测试。
- 保持前端展示、控制器、适配层边界，不引入框架或 CDN。
- 语料保持虚构；新增用例说明要验证的行为，修改语料时检查现有评测期望。
- 提交使用 Conventional Commits，如 `fix: 修复缺订单号时的追问`。
- PR 写清问题、行为变化与验证结果；界面变化附截图，日志需脱敏。

## 安全问题

请按照 [SECURITY.md](SECURITY.md) 私密报告，不要在公开 Issue 中发布漏洞细节、密钥或客户资料。
