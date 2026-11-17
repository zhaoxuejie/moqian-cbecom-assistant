# 前端

原生 HTML/CSS/JavaScript，由 FastAPI 同源托管，无构建步骤、无框架、无 CDN。

```text
frontend/
├── design/tokens.css       共用设计令牌
├── admin/
│   ├── pages/              后台页面
│   ├── controllers/        交互与状态
│   ├── adapters/           后端请求与字段映射
│   └── styles/             后台样式
└── widget/
    ├── index.html          虚构订单宿主演示页
    ├── widget.html         浮层 iframe 内容
    ├── loader.js           加载、上下文传递与浮层控制
    ├── controllers/        访客交互
    ├── adapters/           API 与 SSE 请求
    └── styles/             浮层及演示页样式
```

入口为 `/widget-app/`、`/admin-app/`，共享令牌挂在 `/ui-design/`。

展示层负责结构和样式；控制器处理交互，不直接发请求；适配层发请求和转换字段，不操作 DOM。
品牌、欢迎语、示例问题、转人工文案从后端配置注入。
宿主演示页里的品牌与订单只用于展示，生产接入时使用自己的页面。

修改接口同步 [契约](../contracts/openapi.yaml)；前端分层与资源检查执行
`python -m pytest tests/test_frontend_ui.py -q`。
Widget 嵌入示例与身份边界见 [README](../README.md#integration)。
