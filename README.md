# Policy Decision Service

面向多租户的时间版本化策略决策服务。Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.x（异步）· PostgreSQL 16 · Alembic。无外部 SaaS 依赖，`docker compose up` 一键启动。

## 架构

```
app/
├── main.py          # FastAPI 应用、请求上下文中间件、统一异常处理
├── config.py        # pydantic-settings 配置（环境变量 / .env）
├── db.py            # 异步 engine / session
├── models.py        # SQLAlchemy 模型
├── schemas.py       # Pydantic 请求/响应模型（含规则 DSL 校验）
├── rules.py         # 规则求值器（纯解释执行，无 eval/exec）
├── idempotency.py   # 幂等执行器（与业务写入同事务）
├── deps.py          # 公共依赖（X-Tenant-ID、DB session、Header）
├── routers/
│   ├── policies.py  # 策略 / 草稿 / 发布 / 版本 / 决策
│   └── health.py    # /health/live、/health/ready
└── worker.py        # Outbox Worker：python -m app.worker [--once]
alembic/             # 迁移（唯一建表途径，禁止 create_all）
tests/               # pytest + httpx ASGI 测试
```

分层：Router（HTTP/校验）→ 领域逻辑（rules / idempotency）→ 模型 / DB。请求链路：`中间件(注入 request_id/tenant_id 日志上下文) → 路由 → 事务 → 统一错误结构`。

## 数据模型

| 表 | 说明 |
|---|---|
| `policies` | 策略；`(tenant_id, name)` 唯一 |
| `policy_drafts` | 每策略一个可编辑草稿（`policy_id` 为主键）；`revision` 用于乐观并发 |
| `policy_versions` | 不可变已发布版本；`seq` 每策略递增；双时间字段见下 |
| `idempotency_keys` | `(tenant_id, path, key)` 主键；存请求哈希与响应快照 |
| `outbox_events` | Outbox 事件；`status: pending/sent/dead`，`attempts`、`next_attempt_at` |

`policy_versions` 的双时间：

- **业务时间** `valid_from` / `valid_to`：左闭右开 `[from, to)`，`valid_to = NULL` 表示开放结束（+∞）。
- **系统时间** `recorded_at`：版本对系统可见的时刻（数据库 `now()`）。

时间约束由数据库强制，而非应用层先查后写：

- `EXCLUDE USING gist (policy_id WITH =, tstzrange(valid_from, valid_to, '[)') WITH &&)` —— 同一策略的已发布版本有效区间不得重叠（需要 `btree_gist`，迁移中自动创建）；
- `CHECK (valid_to IS NULL OR valid_to > valid_from)`；
- `UNIQUE (policy_id, seq)`。

查询语义：决策时按 `valid_from <= occurred_at AND (valid_to IS NULL OR occurred_at < valid_to)` 过滤；传入 `known_at` 时追加 `recorded_at <= known_at`，即可还原"过去某个系统时刻能看到哪个版本"。

## 并发策略

- **乐观并发**：草稿更新与发布必须带 `If-Match: "<revision>"`。缺失 → 428；不匹配 → 412。响应带 `ETag`。
- **草稿更新**：`SELECT ... FOR UPDATE` 锁定草稿行后再比对 revision，并发更新只有一个成功。
- **发布**：`SELECT ... FOR UPDATE` 锁定策略行，串行化同一策略的并发发布；区间重叠由数据库排他约束兜底，返回 409。
- **幂等**：`(tenant_id, path, key)` 主键。请求体经规范化 JSON（键排序、紧凑分隔符）后取 SHA-256。首次请求在同一事务内写入幂等记录 + 业务数据 + 响应快照；并发重放阻塞在主键上，待首个事务提交后返回首次的状态码与响应体；同 Key 不同请求体 → 409。
- **Outbox**：`policy.published` 事件与版本写入同一事务。Worker 用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取，多实例不会重复处理；失败按 `base * 2^(attempts-1)` 指数退避，达到 `WORKER_MAX_RETRIES` 后进入 `dead`。

## 规则 DSL

JSON 树，递归解释执行（无 eval/exec）：

```json
{
  "op": "all",
  "children": [
    {"op": "gte", "path": "device.temperature", "value": 20},
    {"op": "any", "children": [
      {"op": "eq", "path": "user.role", "value": "admin"},
      {"op": "in", "path": "user.group", "value": ["ops", "sre"]}
    ]},
    {"op": "not", "child": {"op": "eq", "path": "device.maintenance", "value": true}},
    {"op": "exists", "path": "device"}
  ]
}
```

操作符：`all` / `any`（`children`）、`not`（`child`）、`eq` / `ne` / `gt` / `gte` / `lt` / `lte` / `in`（`path` + `value`）、`exists`（`path`）。`path` 为点路径，从决策请求上下文中取值；路径缺失时叶子节点结果为 `false`（trace 中 `input_missing: true`）。决策响应携带确定性 trace：每个节点的操作符、输入值、期望值与结果。

## 启动

```bash
docker compose up --build
```

- `db`：PostgreSQL 16（带健康检查）
- `app`：先执行 `alembic upgrade head` 再启动 uvicorn（:8000）
- `worker`：`python -m app.worker`

本地开发：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
python -m app.worker --once   # 处理一批 Outbox 事件后退出
```

## 迁移

```bash
alembic upgrade head        # 应用迁移（唯一建表方式）
alembic revision --autogenerate -m "..."  # 新增迁移
```

## 测试

测试需要可访问的 PostgreSQL（默认 `localhost:5432`，可用 `TEST_DATABASE_URL` 覆盖）。测试库自动创建并通过 Alembic 迁移建表：

```bash
docker compose up -d db
pytest
ruff check .
```

覆盖：租户隔离、乐观并发（412/428）、已发布版本不可变、时间边界与开放区间、版本重叠、并发发布、幂等重放与 Key 冲突、嵌套规则与 trace、`occurred_at`/`known_at` 双时间查询、Outbox 并发领取与失败重试、数据库不可用时就绪探针 503。

## API

所有业务接口必须携带 `X-Tenant-ID`；跨租户访问一律 404。

```bash
T='-H "X-Tenant-ID: tenant-a"'

# 创建策略（支持 Idempotency-Key）
curl -X POST localhost:8000/policies -H "X-Tenant-ID: tenant-a" \
  -H "Content-Type: application/json" -H "Idempotency-Key: k-1" \
  -d '{"name": "temp-alert"}'

# 创建/更新草稿（更新必须带 If-Match）
curl -X PUT localhost:8000/policies/<pid>/draft -H "X-Tenant-ID: tenant-a" \
  -H "Content-Type: application/json" -H 'If-Match: "1"' \
  -d '{"rules": {"op": "gte", "path": "device.temperature", "value": 20}}'

# 发布草稿（不可变版本 + Outbox 事件，支持 Idempotency-Key）
curl -X POST localhost:8000/policies/<pid>/publish -H "X-Tenant-ID: tenant-a" \
  -H "Content-Type: application/json" -H 'If-Match: "1"' \
  -d '{"valid_from": "2026-01-01T00:00:00Z", "valid_to": null}'

# 版本历史（known_at 可选，用于系统时间回溯）
curl "localhost:8000/policies/<pid>/versions?known_at=2026-06-01T00:00:00Z" -H "X-Tenant-ID: tenant-a"

# 决策（occurred_at / known_at 可选，默认当前时间）
curl -X POST localhost:8000/policies/<pid>/decisions -H "X-Tenant-ID: tenant-a" \
  -H "Content-Type: application/json" \
  -d '{"context": {"device": {"temperature": 25}}, "occurred_at": "2026-03-01T00:00:00Z"}'

# 健康检查
curl localhost:8000/health/live
curl localhost:8000/health/ready
```

统一错误结构：

```json
{"error": {"code": "REVISION_MISMATCH", "message": "...", "details": {}}, "request_id": "..."}
```

日志为 JSON 结构化输出，自动携带 `request_id`、`tenant_id`、`method`、`path`。
