# 应用与 worker 共用镜像；BuildKit 缓存复用依赖下载。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# pip 源：默认官方源；服务器构建时用 build args 指向国内镜像（清华）
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=60

# 依赖层单独缓存：先装包元数据与源码，再装依赖
COPY pyproject.toml LICENSE ./
COPY app ./app
COPY modules ./modules
COPY migrations ./migrations
# 评测用例与报告：/admin/evals 在 eval_cases 表为空时回落 evals/cases.yaml
COPY evals ./evals
# 可选辅助脚本：环境检查、演示码与种子数据
COPY scripts ./scripts
# 前端静态产物（design 令牌 + admin 多页站 + widget）：由 app 同源托管，M0 不构建（D16/D17）
COPY frontend ./frontend
# 缓存卷不进最终镜像；pip 仍会按需下载，但 wheel 不用重复下
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# 构建信息：放在最后两行，改它只让这一层失效，不会重跑 pip install。
# 部署脚本传 --build-arg BUILD_SHA=$(git rev-parse --short HEAD)，
# /health 会把它们返回给部署脚本与管理台 —— 「线上是哪个提交」有答案，不靠人记。
ARG BUILD_SHA=""
ARG BUILD_TIME=""
ENV BUILD_SHA=${BUILD_SHA} \
    BUILD_TIME=${BUILD_TIME}

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
