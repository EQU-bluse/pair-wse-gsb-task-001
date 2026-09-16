# 多租户时间版本化策略决策服务

一个自包含（无外部 SaaS 依赖）的多租户**双时间（bitemporal）**策略决策后端：

- Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.x（async）· PostgreSQL 16 · Alembic
- 每租户多个策略；每个策略有**一个可编辑草稿**与**多个不可变已发布版本**
- 业务生效时间（effective）与系统记录时间（recorded）两条独立时间轴
- 基于 `If-Match`/`revision` 的乐观并发控制；数据库级排他约束防止版本区间重叠
- 声明式 JSON 规则引擎（无 `eval`/`exec`），返回确定性 evaluation trace
- 创建/发布支持 `Idempotency-Key`，幂等记录与业务写入同事务
- Transactional Outbox + 可多实例部署的 Worker（行锁领取、指数退避、dead-letter）
- Docker Compose 一键启动（数据库 → 迁移 → API / Worker）

---

## 1. 架构

```
                         ┌──────────────────────────────┐
   HTTP /api/v1/*  ───►  │ FastAPI 应用（无状态，可水平扩）│
   X-Tenant-ID           │  路由层 → 服务层 → ORM 模型     │
                         └───────────────┬──────────────┘
                                         │ SQLAlchemy async (asyncpg)
                              ┌──────────▼──────────┐
                              │   PostgreSQL 16     │
                              │  policies           │
                              │  policy_versions    │  GiST EXCLUDE 约束（区间不重叠）
                              │  idempotency_keys   │  触发器（版本不可变）
                              │  outbox_events      │
                              └──────────▲──────────┘
                                         │ SKIP LOCKED 行锁领取
                         ┌───────────────┴──────────────┐
                         │ Worker（python -m app.worker）│  可多实例
                         │  退避重试 / dead-letter        │
                         └──────────────────────────────┘
```

分层（`app/`）：

| 模块 | 职责 |
| --- | --- |
| `app/main.py` | FastAPI 应用、请求中间件（request_id/tenant 日志上下文）、健康检查 |
| `app/api/deps.py` | `X-Tenant-ID`、`Idempotency-Key`、`If-Match` 依赖 |
| `app/api/routes/policies.py` | 策略/草稿/发布/历史/决策 HTTP 路由 |
| `app/services/policy.py` | 策略、草稿、发布事务逻辑 |
| `app/services/decision.py` | 双时间版本定位 + 规则求值 |
| `app/services/idempotency.py` | 幂等键获取、请求体哈希、重放 |
| `app/services/outbox.py` | Outbox 领取、投递、退避、DLQ、轮询循环 |
| `app/services/time.py` | 时间归一化与双时间版本查询 |
| `app/rules/engine.py` | 声明式规则解释器 + trace |
| `app/models.py` | ORM 模型与数据库约束 |
| `app/worker.py` | Worker 入口（`--once`） |
| `alembic/` | 迁移（不使用 `create_all`） |

---

## 2. 数据模型

**policies** — 策略主表（每租户隔离）
- `id` (uuid)，`tenant_id`，`name`
- `draft_rule` (jsonb, nullable)：当前草稿；发布后被消费置空
- `draft_revision` (bigint)：单调递增的乐观锁版本号
- `(tenant_id, id)` 复合唯一索引，所有查询都带 `tenant_id` 条件

**policy_versions** — 不可变已发布版本
- `policy_id` + `version`（策略内从 1 递增，唯一）
- `rule` (jsonb)：发布时刻草稿规则的**快照**
- `effective_start` / `effective_end` (timestamptz)：业务生效区间
- `recorded_at` (timestamptz)：系统记录时间
- `revision`：发布时草稿的 revision 快照
- 关键约束：
  - `CHECK (effective_end IS NULL OR effective_end > effective_start)`
  - **GiST 排他约束**：`EXCLUDE USING gist (policy_id WITH =, tstzrange(effective_start, effective_end, '[)') WITH &&)`
    ——同一策略的业务区间绝不重叠，数据库强制，并发也成立
  - **行级触发器**：`BEFORE UPDATE OR DELETE` 直接抛异常，已发布版本在数据库层面不可修改/删除

**idempotency_keys** — 幂等记录
- 唯一键 `(tenant_id, request_path, idempotency_key)`
- `request_hash`：请求体的确定性哈希（SHA-256）
- `response_status` / `response_body`：首次调用的响应，与业务写入同事务提交

**outbox_events** — 发件箱事件
- `event_type`（发布时写 `policy.published`）、`tenant_id`、`aggregate_id`、`payload`
- `status`：`pending → processing → sent`，失败回到 `pending`，超限进 `dead_letter`
- `attempts` / `max_attempts`、`available_at`（退避到期时间）、`locked_by` / `locked_at`

---

## 3. 时间语义

### 3.1 业务时间轴（effective time）

- 区间采用**左闭右开** `[effective_start, effective_end)`；`effective_end = NULL` 表示**开放结束**（持续有效）
- 因此两个首尾相接的区间 `[a, b)` 与 `[b, c)` 不重叠，边界时刻 `b` 属于后者
- 定位规则：`effective_start <= occurred_at AND (effective_end IS NULL OR effective_end > occurred_at)`
  ，同策略下至多命中一条（排他约束保证）

### 3.2 系统时间轴（recorded time）

- 每条版本在发布事务中写入 `recorded_at`
- 决策接口可选传 `known_at`：只可见 `recorded_at <= known_at` 的版本，用于还原“过去某个系统时刻能看到的策略”
- 不传 `known_at` 时表示当前系统时刻；不传 `occurred_at` 时表示当前业务时刻

### 3.3 示例

| occurred_at | known_at | 可见版本 |
| --- | --- | --- |
| 2025-03-01 | （现在） | 当时有效的版本 |
| 2025-08-01 | （现在） | 当前对未来仍有效的开放区间版本 |
| 2025-08-01 | 仅记录了 v1 的历史时刻 | 无版本（v2 当时尚未记录） |

---

## 4. 规则引擎

规则是纯 JSON 树，**全程解释执行，不使用 `eval`/`exec`/编译**。

- 逻辑：`all`（所有子节点为真）、`any`（任一为真）、`not`（单 child 取反）
- 比较：`eq`、`ne`、`gt`、`gte`、`lt`、`lte`
- 集合：`in`（`values` 列表成员判定）
- 存在性：`exists`
- 点路径读取上下文：`device.temperature`、`device.tags.0`；路径缺失记为 missing
  - `exists` 对缺失为 `false`；排序比较对缺失/不可比较类型为 `false`；`ne` 是 `eq` 的严格取反
  - `eq` 为严格类型语义：`1 != true`，int 与 float 按数值比较

```json
{
  "op": "all",
  "rules": [
    {"op": "gt", "path": "device.temperature", "value": 30},
    {"op": "not", "rule": {"op": "eq", "path": "device.mode", "value": "off"}},
    {"op": "any", "rules": [
      {"op": "in", "path": "device.region", "values": ["eu", "us"]},
      {"op": "exists", "path": "device.override"}
    ]}
  ]
}
```

决策响应里的 `trace` 是与规则树同构的确定性求值轨迹，每个节点带 `op`、`path`、
`input`（解析到的实际值）、`value`、`result`，逻辑节点还带有序的 `children`。

---

## 5. 并发与一致性策略

### 5.1 草稿乐观并发控制（OCC）

- 每个策略带单调递增的 `draft_revision`，读取时通过 `ETag: "<revision>"` 暴露
- 更新草稿（`PUT .../draft`）、创建草稿（`POST .../draft`）、发布（`POST .../publish`）
  都必须带 `If-Match: "<revision>"`
- 不匹配返回 **412 Precondition Failed**，响应体给出 `current_revision`
- 草稿更新与发布在事务内先对策略父行 `SELECT ... FOR UPDATE`，序列化同策略的写操作

### 5.2 发布的双保险

1. **行锁**：同一策略的并发发布在父行上排队，版本号 `MAX(version)+1` 在锁内分配
2. **GiST 排他约束**：即使绕过应用路径，区间重叠的插入也会被数据库拒绝（应用转为 409）
3. 发布成功后草稿被消费（`draft_rule = NULL`，revision +1），排队中的第二个发布看到草稿不存在而失败，
   绝不会发布出两份版本

### 5.3 已发布版本不可变

应用层没有任何修改版本的接口；数据库触发器在 `UPDATE/DELETE` 时直接抛异常，
历史无法被任何代码路径改写（测试中用原生 SQL 验证）。

### 5.4 幂等

- 适用于创建策略与发布（携带 `Idempotency-Key` 头时）
- 请求体经 `json.dumps(..., sort_keys=True, separators=(",", ":"))` 规范化后 SHA-256
  ——JSON 键顺序不同但语义相同的请求视为同一请求
- 同租户 + 同路径 + 同 Key + 同请求体重放：返回**首次的状态码与响应体**（带 `Idempotency-Replayed: true`）
- 同 Key 不同请求体：**409**
- 并发重放：唯一索引 + `INSERT ... ON CONFLICT` 级别的竞争通过 SAVEPOINT 与
  `SELECT ... FOR UPDATE` 处理，多个并发请求只会创建一份资源、返回同一个响应
- 幂等标记与业务写入在**同一事务**：业务失败则一起回滚，Key 可被重新使用

### 5.5 Outbox 与 Worker

- 每次成功发布在同一事务写一条 `policy.published` 事件（事务性发件箱）
- Worker 用单条 `UPDATE ... SET status='processing' ... WHERE id IN
  (SELECT id ... FOR UPDATE SKIP LOCKED) RETURNING` 原子领取，多实例领取互不重叠
- 投递失败：`attempts+1`，按 `base_delay * 2^(attempts-1)`（上限 `max_delay`）指数退避；
  达到 `max_attempts` 进入 `dead_letter`，不再被领取
- `processing` 超时（默认 5 分钟，Worker 崩溃）的事件会被其他实例回收重试
- 实际“发送”当前输出结构化日志；事件状态全部真实落库

---

## 6. 快速启动（Docker Compose 一键）

前置：Docker + Docker Compose。

```bash
docker compose up --build
```

启动顺序：`db`（健康检查通过）→ `migrate`（`alembic upgrade head`，成功退出）→ `api` + `worker`。

- API：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- 存活：`GET /healthz`；就绪：`GET /readyz`（对数据库执行 `SELECT 1`，失败返回 503）

停止并清理数据卷：

```bash
docker compose down -v
```

---

## 7. 本地开发

需要 Python 3.12+ 与一个可访问的 PostgreSQL 16。

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env                 # 按需修改 APP_DATABASE_URL
export APP_DATABASE_URL="postgresql+asyncpg://pds:pds@localhost:5432/pds"

# 迁移
alembic upgrade head

# 启动 API
uvicorn app.main:app --reload

# 启动 Worker（持续轮询）
python -m app.worker

# 只处理一批后退出（演示/测试）
python -m app.worker --once
```

配置全部来自环境变量，前缀 `APP_`（见 `app/config.py` 与 `.env.example`）：
`APP_DATABASE_URL`、`APP_LOG_LEVEL`、`APP_WORKER_POLL_INTERVAL`、
`APP_OUTBOX_MAX_ATTEMPTS`、`APP_OUTBOX_BASE_DELAY`、`APP_OUTBOX_MAX_DELAY`、
`APP_READINESS_TIMEOUT` 等。

---

## 8. 迁移

- 初始迁移：`alembic/versions/0001_initial.py`（建表、索引、GiST 排他约束、不可变触发器、`btree_gist` 扩展）
- 使用异步 Alembic（`alembic/env.py`），**不使用 `create_all`**

```bash
alembic upgrade head      # 应用
alembic downgrade base    # 回滚全部
alembic revision -m "xxx" --autogenerate   # 生成后续迁移（需核对）
```

---

## 9. API 速览与调用示例

所有业务接口必须携带 `X-Tenant-ID`；缺失返回 400。跨租户访问统一返回 **404**
（不泄露资源是否属于其他租户）。错误响应统一结构：

```json
{"error": {"code": "not_found", "message": "policy not found", "request_id": "..."}}
```

### 9.1 创建策略（可选初始草稿；支持幂等键）

```bash
curl -X POST localhost:8000/api/v1/policies \
  -H "X-Tenant-ID: tenant-a" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: create-001" \
  -d '{"name":"thermostat","rule":{"op":"gt","path":"device.temperature","value":30}}'
# 201，响应头含 ETag: "1"（草稿 revision）
```

> 创建时 `rule` 可省略，此时策略没有草稿，之后用 `POST .../{id}/draft` 开启编辑周期。

### 9.2 发布草稿为不可变版本（If-Match + 幂等键）

```bash
curl -X POST localhost:8000/api/v1/policies/$PID/publish \
  -H "X-Tenant-ID: tenant-a" -H 'If-Match: "1"' \
  -H "Idempotency-Key: pub-v1" -H "Content-Type: application/json" \
  -d '{"effective_start":"2025-01-01T00:00:00Z","effective_end":"2025-06-01T00:00:00Z"}'
# effective_end 省略表示开放结束；重叠返回 409；revision 过期返回 412
```

发布后草稿被消费。需要继续修改时，携带当前 revision（查看策略的 `draft_revision`）创建新草稿：

```bash
curl -X POST localhost:8000/api/v1/policies/$PID/draft \
  -H "X-Tenant-ID: tenant-a" -H 'If-Match: "2"' -H "Content-Type: application/json" \
  -d '{"rule":{"op":"exists","path":"device.override"}}'
```

### 9.3 更新草稿（OCC）

```bash
curl -X PUT localhost:8000/api/v1/policies/$PID/draft \
  -H "X-Tenant-ID: tenant-a" -H 'If-Match: "3"' -H "Content-Type: application/json" \
  -d '{"rule":{"op":"exists","path":"device.override"}}'
# 成功后 ETag 变为 "4"；用过期 revision 返回 412
```

### 9.4 版本历史

```bash
curl localhost:8000/api/v1/policies/$PID/versions -H "X-Tenant-ID: tenant-a"
```

### 9.5 决策执行（occurred_at / known_at 双时间）

```bash
curl -X POST localhost:8000/api/v1/policies/$PID/decide \
  -H "X-Tenant-ID: tenant-a" -H "Content-Type: application/json" \
  -d '{
    "context": {"device": {"temperature": 42, "mode": "cool"}},
    "occurred_at": "2025-03-01T00:00:00Z",
    "known_at": "2025-12-31T00:00:00Z"
  }'
```

```json
{
  "policy_id": "...",
  "result": true,
  "matched_version": 1,
  "occurred_at": "2025-03-01T00:00:00Z",
  "known_at": "2025-12-31T00:00:00Z",
  "trace": {"op": "gt", "path": "device.temperature", "input": 42, "value": 30, "result": true}
}
```

无匹配版本时 `matched_version` 为 `null`、`result` 为 `false`，trace 为
`{"op": "no_effective_version", ...}`。

### 9.6 健康检查

```bash
curl localhost:8000/healthz   # {"status":"ok"}，不依赖数据库（存活）
curl localhost:8000/readyz    # 数据库可达 200，否则 503（就绪）
```

每个响应都带 `X-Request-ID`（也可用同名请求头传入），结构化日志中同时记录
`request_id` 与 `tenant_id`。

---

## 10. 测试

测试使用真实 PostgreSQL（AsyncSQLAlchemy + httpx ASGITransport，不 mock 数据库），
覆盖：租户隔离、OCC 412、已发布版本不可变、时间边界与开放区间、版本重叠 409、
并发发布、幂等重放/Key 冲突/并发重放、嵌套规则与 trace、occurred_at × known_at
双时间查询、Outbox 并发领取/重试退避/DLQ/stale 回收/`--once` CLI、数据库不可用时的就绪状态。

```bash
# 启动一个测试数据库（或复用 docker compose 的 db）
docker run -d --name pds-test -p 5432:5432 \
  -e POSTGRES_USER=pds -e POSTGRES_PASSWORD=pds -e POSTGRES_DB=pds postgres:16-alpine

export APP_DATABASE_URL="postgresql+asyncpg://pds:pds@localhost:5432/pds"
alembic upgrade head
pytest -q

# Lint
ruff check app alembic tests
```

---

## 11. 关键设计说明与取舍

- **发布即消费草稿**：保证“至多一个可编辑草稿”，也让“创建草稿 / 更新草稿 / 发布”三个动作
  都有明确的 revision 语义；每次发布都开启新的编辑周期（`POST draft`）。
- **时间戳由应用侧生成**：`recorded_at`、`available_at` 等需要与应用时钟比较的字段在进程内
  生成（列上仍保留数据库默认值兜底），避免应用主机与数据库主机之间时钟漂移影响时间轴判断。
- **跨租户一律 404**：而不是 403，避免资源存在性探测。
- **无动态执行**：规则树只经过白名单算子解释器；多余字段在 Pydantic 层即以 `extra="forbid"` 拒绝。
