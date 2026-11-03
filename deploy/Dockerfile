# 仅构建 API 镜像。迁移与数据初始化不在镜像内执行，
# 由部署流水线显式调用 migrations/apply.py，避免容器启动时隐式改库结构。
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

# 先装依赖，利用层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY contracts ./contracts
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# 以非 root 运行；业务角色在数据库侧同样不应是表 owner
RUN useradd --create-home --uid 10001 fkh && chown -R fkh:fkh /app
USER fkh

EXPOSE 8000
CMD ["uvicorn", "fkh.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
