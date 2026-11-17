# 接口契约

[openapi.yaml](openapi.yaml) 是前后端请求、响应与 SSE 事件的机器可读约定。
产品能力与运行说明见 [README](../README.md)。

- JSON 使用 camelCase，Python 与数据库使用 snake_case，由 `app/` 转换。
- 访客 `/widget/*`、`/chat` 与后台 `/admin/*` 凭证分离，不能互换。
- SSE 的事件名和载荷见契约 `/chat`；不提供服务端断线重放。
- 改接口时同步本文件、实现、调用方与测试，不依赖内部过程文档。

```bash
python -m pytest tests/test_openapi_contract.py -q
```

测试检查 schema 合法性、引用、路由覆盖、SSE 和鉴权声明。本地运行时可查看
<http://localhost:8002/docs>；启用演示模式后 API 文档关闭。
