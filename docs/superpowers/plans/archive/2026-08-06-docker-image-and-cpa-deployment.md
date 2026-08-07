# Docker 镜像化与 CPA 部署对接 Implementation Plan

> **For agentic workers:** 本计划为容器化与部署对接方案，第 1 步涉及新增工程文件（Dockerfile / .dockerignore），第 2 步为外部 CPA 部署目录的 compose 改动。Steps 使用 checkbox（`- [ ]`）语法跟踪。

**Goal:** 把当前工程（qoderbuddy2api）打成 Docker 镜像，并在 `/Users/dmego/docker-space/CPA` 部署目录新增一个 `qb2api` service，使 `cli-proxy-api` 容器能通过内网访问 2api 暴露的 `:9999` 端口，从而在 CPA 里把 2api 作为 OpenAI 兼容供应商添加并使用。

**Architecture:**
- **双进程单容器**：Control Plane（`:9999`）spawn Proxy Worker 子进程（`:10001`）。Worker 由 `control/worker_process.py::worker_command` 以 `sys.executable -m uvicorn qb2api.worker.app:app` 拉起，镜像内只需 Python + 代码，无需拆成两个 service。
- **Worker loopback 安全**：`worker_process.py::worker_environment` 在 `control_host ∈ {0.0.0.0, ::}` 时回落到 `127.0.0.1`，因此容器内 Control 绑 `0.0.0.0:9999` 不会破坏 Worker 握手。
- **`/v1/*` 统一入口**：`control/app.py::forward_proxy_requests` 把 `/v1/*` 转发给 Worker。CPA 只需指向 `http://<2api容器>:9999/v1`，填 `QB2API_PROXY_API_KEY`。
- **dist 定位约束**：`control/app.py` 用 `Path(__file__).resolve().parents[1] / "web" / "dist"` 找前端产物，即 `<qb2api包>/web/dist`。Dockerfile 必须 `COPY src/qb2api /app/qb2api` 整体拷贝（含 `web/dist`），不可把 dist 单独拷到别处，否则 Admin UI 报 "admin UI not packaged"。
- **CPA 加供应商**：在 `cpa/config.yaml` 的 `openai-compatibility` 段追加一项，`base-url` 用容器名走内网（`http://qb2api:9999/v1`），`api-key` 填 `QB2API_PROXY_API_KEY`。

**Tech Stack:** Python 3.12 / Docker（多阶段构建）/ docker-compose / FastAPI / uvicorn

## Global Constraints

- 双进程单所有权不变（CLAUDE.md）：Control 是唯一常驻服务，拥有 Admin UI、SQLite、凭据 vault、Worker 生命周期。镜像只跑 `qb2api --mode control`，不造单进程捷径。
- 三个 Key 分属不同信任域，由 `Settings.validate_startup()` 强制互不相同：`QB2API_PROXY_API_KEY`（模型客户端 → Worker）、`QB2API_ADMIN_KEY`（管理）、`QB2API_CREDENTIAL_KEY`（Fernet，凭据加密，一旦设定不可更换）。
- 凭据 / token / 上游响应正文不得进日志 / 审计 / SQLite / 浏览器存储 / 提交记录（CLAUDE.md 硬约束）。镜像内不硬编码任何 Key，全部经 compose `environment` 注入。
- `data/`（SQLite + 凭据 vault）、`config/models.json`、`logs/` 必须挂卷持久化，不进镜像层。
- 前端构建产物 `src/qb2api/web/dist` 已提交进仓库，镜像直接 COPY，不在镜像内跑 `npm build`——最短路径。
- Worker 子进程通过 loopback 与 Control 通信，不对宿主暴露 `:10001`。

---

### Task 1: 工程内新增 Docker 化文件

**Files:**
- Create: `Dockerfile`（仓库根目录）
- Create: `.dockerignore`（仓库根目录）

**Interfaces:**
- 镜像入口：`qb2api --mode control`
- 暴露端口：`9999`（Control Plane）
- 默认环境（镜像内置，可被 compose 覆盖）：`QB2API_MODE=control`、`QB2API_CONTROL_HOST=0.0.0.0`、`QB2API_CONTROL_PORT=9999`、`QB2API_WORKER_HOST=127.0.0.1`、`QB2API_WORKER_PORT=10001`、`QB2API_DATA_DIR=/data`、`QB2API_LOG_DIR=/logs`、`QB2API_MODEL_CONFIG=/config/models.json`
- 挂卷点：`/data`、`/logs`、`/config`（只读，放 `models.json`）

- [ ] **Step 1: 新增 `Dockerfile`**

多阶段构建：builder 装 wheel，runtime 精简。前端 dist 已在 `src/qb2api/web/dist`，整体 COPY 即可（dist 定位见 Global Constraints）。

```dockerfile
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
```

- [ ] **Step 2: 新增 `.dockerignore`**

排除本地 venv、git、数据、日志、测试缓存、node_modules，缩小构建上下文。

```
.venv/
.git/
.worktrees/
.claude/
data/
logs/
*.sqlite3*
*.log
nohup.out
.pytest_cache/
.ruff_cache/
node_modules/
frontend/node_modules/
frontend/test-results/
```

- [ ] **Step 3: 构建并冒烟验证镜像**

```bash
cd /Users/dmego/vibeCoding/2api
docker build -t dmego/qb2api:latest .
# 冒烟：临时数据目录启动，确认 Control + Worker 子进程都起来
docker run --rm -d --name qb2api-smoke \
  -e QB2API_PROXY_API_KEY=smoke-proxy \
  -e QB2API_ADMIN_KEY=smoke-admin \
  -e QB2API_CREDENTIAL_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') \
  -p 9999:9999 dmego/qb2api:latest
docker logs --tail=30 qb2api-smoke     # 应见 Control 启动 + Worker spawn
curl -s http://127.0.0.1:9999/health   # {"status":"ok",...}
docker stop qb2api-smoke
```

验证要点：日志含 Control 与 Worker 两段启动；`/health` 返回 ok；`/v1/models` 需带 `Authorization: Bearer smoke-proxy` 才可达。

---

### Task 2: CPA 部署目录新增 `qb2api` service

**Files:**
- Modify: `/Users/dmego/docker-space/CPA/docker-compose.yaml`（在 `services:` 下新增 `qb2api`）
- Create（目录/文件）: `/Users/dmego/docker-space/CPA/qb2api/config/models.json`（从工程 `config/models.json` 拷贝）
- Create（空目录）: `/Users/dmego/docker-space/CPA/qb2api/data`、`/Users/dmego/docker-space/CPA/qb2api/logs`

**Interfaces:**
- 网络：复用 CPA 现有 `cpa-network`（bridge），2api 与 `cli-proxy-api` 同网，容器名 `qb2api` 作为内网 DNS。
- CPA 访问 2api：`http://qb2api:9999/v1`，鉴权用 `QB2API_PROXY_API_KEY`。
- 不映射端口：默认只走内网；Admin UI 如需宿主访问再加 `ports: ["9999:9999"]`。

- [ ] **Step 1: 生成三个 Key 并准备挂卷目录**

```bash
cd /Users/dmego/docker-space/CPA
mkdir -p qb2api/data qb2api/logs qb2api/config
cp /Users/dmego/vibeCoding/2api/config/models.json qb2api/config/models.json
# 生成 Fernet key（Credential Key，一旦设定不可更换）
python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
# Proxy Key / Admin Key 自行设定，三者必须互不相同
```

- [ ] **Step 2: 在 `docker-compose.yaml` 新增 `qb2api` service**

```yaml
  qb2api:
    build: /Users/dmego/vibeCoding/2api          # 或先 docker build 打成本地镜像后用 image: dmego/qb2api:latest
    container_name: qb2api
    restart: unless-stopped
    environment:
      QB2API_PROXY_API_KEY: "<你设定的 Proxy Key>"
      QB2API_ADMIN_KEY: "<你设定的 Admin Key>"
      QB2API_CREDENTIAL_KEY: "<Fernet key, Step 1 生成>"
      QB2API_ADMIN_UI_ENABLED: "true"
      QB2API_ADMIN_COOKIE_SECURE: "auto"          # 容器内 loopback HTTP 可登录
      CHECKIN_ENABLED: "false"                    # 按需
      TZ: Asia/Shanghai
    volumes:
      - ./qb2api/data:/data
      - ./qb2api/logs:/logs
      - ./qb2api/config:/config:ro                # 放 models.json
    networks:
      - cpa-network
    # 不映射端口：CPA 容器走内网访问，Admin UI 如需宿主访问再开 9999:9999
```

- [ ] **Step 3: 启动并验证 2api service**

```bash
cd /Users/dmego/docker-space/CPA
docker compose up -d --build qb2api
docker compose logs --tail=30 qb2api             # 确认 Control + Worker 启动正常
# CPA 视角验证内网可达 + 模型列表
docker exec cli-proxy-api wget -qO- http://qb2api:9999/v1/models \
  --header="Authorization: Bearer <Proxy Key>"
```

验证要点：日志见 Control + Worker 启动；从 `cli-proxy-api` 容器内 `wget` 拉到 `/v1/models` 列表。

---

### Task 3: 在 CPA 把 2api 配成 OpenAI 兼容供应商

**Files:**
- Modify: `/Users/dmego/docker-space/CPA/cpa/config.yaml`（在 `openai-compatibility:` 段追加 `qb2api` 项）

**Interfaces:**
- `openai-compatibility[].base-url`：`http://qb2api:9999/v1`（容器名 + Control :9999，走内网）
- `openai-compatibility[].api-key-entries[].api-key`：与 `QB2API_PROXY_API_KEY` 相同
- `openai-compatibility[].models[].name`：取 `config/models.json` 里的 `id`（如 `glm-5.2`、`deepseek-v4-flash`、`Qwen3.7-Max`）
- `openai-compatibility[].models[].alias`：CPA 客户端侧可见名，可自定义

- [ ] **Step 1: 在 `openai-compatibility:` 段追加 `qb2api` 项**

```yaml
openai-compatibility:
  # ... 既有项保持不变 ...
  - name: qb2api
    base-url: http://qb2api:9999/v1          # 容器名 + Control :9999，走内网
    api-key-entries:
      - api-key: "<与 QB2API_PROXY_API_KEY 相同>"
    models:
      - name: glm-5.2
        alias: qb2api-glm-5.2
      - name: deepseek-v4-flash
        alias: qb2api-dsv4-flash
      - name: Qwen3.7-Max
        alias: qb2api-qwen37-max
      # 需要哪个加哪个，name 必须匹配 models.json 里的 id
```

- [ ] **Step 2: 重载 CPA 并验证模型可见**

```bash
cd /Users/dmego/docker-space/CPA
docker compose restart cli-proxy-api          # 重载 config.yaml
# 验证 CPA 合并模型列表里出现 qb2api 别名
curl -s http://127.0.0.1:8317/v1/models \
  --header="Authorization: Bearer ABC-123456" | grep -i qb2api
```

验证要点：CPA `/v1/models` 列表出现 `qb2api-*` 别名；用别名发一次 chat 请求确认端到端通路（CPA → 2api Control → 2api Worker → 上游）。

---

## Verification

**镜像层（Task 1）：**
- `docker build` 成功，无 `npm build` 步骤。
- 冒烟容器日志含 Control 与 Worker 两段启动；`/health` 返回 ok。

**部署层（Task 2）：**
- `docker compose up -d --build qb2api` 成功；`cli-proxy-api` 容器内 `wget http://qb2api:9999/v1/models` 拉到列表。
- `data/`、`logs/` 持久化到 `./qb2api/`，容器重建后数据不丢。

**对接层（Task 3）：**
- CPA `/v1/models` 出现 `qb2api-*` 别名。
- 用别名发 chat 请求，端到端返回正常（流式与非流式各一次）。

## Risks & Notes

- **Credential Key 不可更换**：`QB2API_CREDENTIAL_KEY` 一旦用于加密凭据，更换会导致已加密凭据无法解密；首次设定后妥善保管。
- **Admin Cookie Secure**：`QB2API_ADMIN_COOKIE_SECURE=auto` 允许 loopback HTTP 登录；若把 9999 暴露到非受信网络，应改为 `true`（强制 HTTPS）。
- **CPA config.yaml 含明文上游密钥**：既有 `claude-api-key` 等段已有真实密钥明文，与本计划无关；追加 `qb2api` 项时不动其他项。
- **镜像 rebuild 触发**：用 `build:` 方式时，改 2api 代码需 `docker compose up --build qb2api` 重建；若追求解耦，可先 `docker build -t dmego/qb2api:latest .` 再用 `image:` 引用。
- **Worker 不暴露**：`:10001` 仅容器内 loopback，不映射端口，符合双进程架构约束。
