# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# Optional package index override (e.g. a local mirror); defaults to PyPI.
ARG PIP_INDEX_URL

# Resolve dependencies from the project metadata alone, before the sources are
# copied in, so this layer survives ordinary code edits. Copying src first made
# every source change re-download the entire dependency set.
COPY pyproject.toml README.md ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/python -c "import tomllib; \
      print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" \
      > /tmp/requirements.txt \
 && if [ -n "$PIP_INDEX_URL" ]; then \
      /opt/venv/bin/pip install --index-url "$PIP_INDEX_URL" -r /tmp/requirements.txt; \
    else \
      /opt/venv/bin/pip install -r /tmp/requirements.txt; \
    fi

# Frontend build output ships inside src/qb2api/web/dist; no build step needed.
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

# ---- runtime ----
FROM python:3.12-slim AS runtime
# Each core-sized glibc arena keeps its freed heap mapped, so per-request
# JSON/pydantic buffers pin RSS long after the request ends. Two arenas
# retained ~60 MB where the default retained ~130 MB in a 16-way burst test,
# with no measurable throughput cost. Inherited by the spawned worker.
ENV PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH" \
    MALLOC_ARENA_MAX=2 \
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
