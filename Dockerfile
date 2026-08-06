# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# 前端构建产物已提交进 src/qb2api/web/dist，无需在此构建
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---- runtime ----
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH" \
    QB2API_MODE=control \
    QB2API_CONTROL_HOST=0.0.0.0 \
    QB2API_CONTROL_PORT=9999 \
    QB2API_WORKER_HOST=127.0.0.1 \
    QB2API_WORKER_PORT=10001 \
    QB2API_DATA_DIR=/data \
    QB2API_LOG_DIR=/logs \
    QB2API_MODEL_CONFIG=/config/models.json

COPY --from=builder /opt/venv /opt/venv
# 整体拷贝 qb2api 包，含 web/dist；app.py 按 __file__ 定位 dist，不可拆分
COPY src/qb2api /app/qb2api
WORKDIR /app

# 数据 / 日志 / 配置 全部由 volume 注入
VOLUME ["/data", "/logs", "/config"]
EXPOSE 9999
CMD ["qb2api", "--mode", "control"]
