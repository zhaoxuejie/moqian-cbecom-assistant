# 辅助脚本

在仓库根目录使用 `python -m scripts.<name>`，或用 `docker compose exec app python -m scripts.<name>`。
配置读取本项目 `.env`；容器中数据库地址由 Compose 覆盖。

| 脚本 | 用途 |
|---|---|
| `demo_check.py` | 检查服务、模型、知识库、登录和访客会话；会调用模型并写入会话、登录审计，可能产生费用 |
| `i18n_coverage.py` | 只读扫描界面翻译覆盖情况 |
| `demo_code.py` | 为自行部署的演示入口创建、查询或停用入口码 |
| `seed_demo.py` | 按需创建公开入口码和三个虚构演示客服账号，必须显式提供初始密码 |
| `reset-knowledge-data.cmd` / `.ps1` | 本地知识库数据重置，包含破坏性操作，默认先备份；使用前先执行 `-DryRun` |

```bash
python -m scripts.demo_check --base-url http://localhost:8002
python -m scripts.i18n_coverage
# 以下命令写数据库，只在需要演示入口和演示账号时执行：
python -m scripts.seed_demo --password <your-demo-password>
python -m scripts.demo_code create --label trial --days 7 --base-url https://your-demo.example
python -m scripts.demo_code list
```

演示账号为 `agent_alice`、`agent_bob`、`agent_chen`，只用于宿主演示页的身份切换。
种子脚本不会更改已有账号口令，不会导入知识语料；语料导入见 [README](../README.md#start)。
入口码仅做归因和配额管理，不能替代登录、订单归属校验或访问控制。

重置脚本仅适用于 Windows 本地环境，默认目标为本项目 Compose 容器：

```powershell
.\scripts\reset-knowledge-data.cmd -DryRun
```

确认目标库与备份后再交互执行；请勿对生产库执行，也不要为了跳过确认直接使用 `-Force`。
