# 样本交接链核验服务（Chain of Custody）

面向环境检测机构的样本保管链核验服务：登记样本采集信息，接收**移交 / 签收 / 封签检查 / 实验室入库**四类带唯一事件号的事件，自动按发生时间重算证据链，输出**当前保管人**与 **有效（valid）/ 待补证（pending）/ 无效（invalid）**结论及原因；核定前可对当前有效事件提交**作废 / 替换纠错**（全程留痕、可溯），仅有效链可核定入库，核定后历史冻结。

技术栈：**Python 3.11 + FastAPI + SQLite（标准库 sqlite3）**，无外部服务依赖。

---

## 一、核验规则

| 场景 | 判定 |
|---|---|
| 样本登记 | 记录采集时间、采集人（=初始保管人）、封签号；样本编号唯一 |
| 移交（transfer） | 必须由**当前保管人**发起；**移交至签收期间不得再次移交**，否则无效 |
| 签收（receive） | 必须同时满足：匹配在途**移交单号**、**接收人**与移交单一致、**封签号**与登记一致、签收时间**晚于**移交时间 |
| 重复签收 | 同一移交单被签收两次 → 无效（无效事件不可被后续补录抵消） |
| 封签检查（seal_check） | 检查封签号与登记不符，或封签破损（`seal_intact=false`）→ 无效 |
| 缺签收 | 移交后没有合格签收 → **待补证** |
| 缺入库 | 没有实验室入库记录，或入库缺失前序移交-签收链 → **待补证** |
| 保管人断链 | 移交人不是当前保管人 / 签收无对应在途移交 → **无效** |
| 结论优先级 | **无效优先**：同时存在无效原因与待补证事项时，结论为无效（原因中附注待补证事项） |
| 乱序补录 | 事件可任意顺序上报，每次查询/核定都按 `occurred_at`（发生时间，非录入时间）整体重算 |
| 事件幂等 | 同一 `event_id` + 同内容（SHA-256 指纹）重试 → 幂等返回 HTTP 200；同 `event_id` 异内容 → HTTP 409 冲突；事件号全局唯一且**永久占用**（含被作废/替换的历史号） |
| 事件纠错 | 核定前可按**样本编号 + 当前有效事件号**提交**作废（void）/ 替换（replace）**，附**全局唯一纠错号、操作人、原因**；替换须给出**完整新事件内容**与**未占用的新事件号**；原事件与纠错关系**永久留痕**，核验只纳入有效版本并按发生时间重算结论、原因与当前保管人，链查询可追溯被替换记录 |
| 纠错约束 | **不得跨样本**；同纠错号同内容重试幂等（200）、异内容冲突（409）；并发针对同一有效版本**仅一次成功**，失败整体回滚、链不变；**核定入库后拒绝纠错**；纠错与核定并发时，**核定只基于最终提交的有效链** |
| 入库核定 | 仅 `valid` 可核定；核定后该样本登记、事件与纠错全部**拒绝改写**（HTTP 409），重复核定幂等 |

> 说明：任何事件发生时间早于样本采集时间，直接记为无效（采集前不可能发生保管动作）。

---

## 二、一键启动

### 方式 A：Docker Compose（推荐，自动初始化依赖、建表、写入演示数据）

```bash
docker compose up -d --build
docker compose logs -f          # 看到 uvicorn 启动日志即可
```

- 首次启动自动：安装 Python 依赖（镜像构建阶段）→ 建表 → 写入 3 条演示样本（有效 / 待补证 / 无效）。
- SQLite 数据持久化在宿主机 `./data/chain.db`，容器重建不丢数据；已有数据时演示数据自动跳过。
- 不需要演示数据：把 `docker-compose.yml` 中 `SEED_ON_START` 改为 `"false"`（只建空库）。

停止 / 清理：

```bash
docker compose down             # 停止（保留数据）
docker compose down && rm -f data/chain.db*   # 停止并清空数据
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m app.seed              # 可选：初始化演示数据（幂等，可重复执行）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 三、服务地址与端口

| 用途 | 地址 |
|---|---|
| 服务根地址 | http://localhost:8080/ |
| 健康检查 | http://localhost:8080/health |
| Swagger 交互文档（可直接调试） | http://localhost:8080/docs |
| OpenAPI 规范 | http://localhost:8080/openapi.json |
| 业务 API 前缀 | `/api/v1` |

快速验证：

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/v1/samples        # 查看 3 条演示样本的结论
```

演示样本：

| 样本编号 | 结论 | 说明 |
|---|---|---|
| `DEMO-VALID-001` | valid | 移交→签收→封签检查→入库完整（事件故意乱序写入，演示自动重算） |
| `DEMO-PENDING-002` | pending | 已移交未签收、未入库 |
| `DEMO-INVALID-003` | invalid | 封签检查封签号不符且破损（无效优先于入库事实） |

---

## 四、配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `DB_PATH` | Docker 内 `/data/chain.db`；本地 `./data/chain.db` | SQLite 文件路径 |
| `DATA_DIR` | Docker 内 `/data`；本地 `./data` | 数据目录（仅在未显式设置 `DB_PATH` 时用于拼默认路径） |
| `SEED_ON_START` | `true` | 启动时空库是否自动写入演示数据 |
| `LOCK_AFTER_APPROVAL` | `true` | 核定入库后是否冻结历史（按业务要求应始终为 true） |
| `DB_TIMEOUT` | `10` | SQLite 写锁等待秒数 |

---

## 五、HTTP API

所有时间均使用 ISO 8601（建议 UTC，如 `2026-09-30T02:00:00Z`；带时区偏移亦可，内部统一转 UTC）。

### 5.1 样本登记

`POST /api/v1/samples`

```json
{
  "sample_code": "S-001",
  "collected_at": "2026-09-30T01:00:00Z",
  "collector": "王采集",
  "seal_no": "SEAL-001"
}
```

- `201` 新建；同编号同内容重试 `200`（`idempotent: true`）；同编号异内容 `409 sample_conflict`。
- 响应中 `evaluation` 即时给出当前核验结论。

### 5.2 事件上报（统一入口）

`POST /api/v1/events`

事件类型与各自必填字段：

| event_type | 必填字段 | 含义 |
|---|---|---|
| `transfer` | `transfer_no`、`from_custodian`、`to_custodian` | 移交单号、交出方（须当前保管人）、接收方 |
| `receive` | `transfer_no`、`receiver`、`seal_no` | 匹配单号、签收人（须等于接收方）、签收封签号 |
| `seal_check` | `checked_seal_no`（`seal_intact` 默认 true） | 检查读取的封签号、是否完好 |
| `storage` | 无（建议 `custodian`、`storage_location`） | 实验室入库，`custodian` 默认“实验室” |

公共字段：`event_id`（全局唯一）、`sample_code`、`occurred_at`、可选 `note`（备注参与幂等指纹）。

```bash
# 移交
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-T1","event_type":"transfer","sample_code":"S-001",
  "occurred_at":"2026-09-30T02:00:00Z",
  "transfer_no":"TR-1","from_custodian":"王采集","to_custodian":"李运输"}'

# 签收（单号/接收人/封签号三匹配，且晚于移交）
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-R1","event_type":"receive","sample_code":"S-001",
  "occurred_at":"2026-09-30T04:00:00Z",
  "transfer_no":"TR-1","receiver":"李运输","seal_no":"SEAL-001"}'

# 封签检查
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-C1","event_type":"seal_check","sample_code":"S-001",
  "occurred_at":"2026-09-30T04:30:00Z",
  "checked_seal_no":"SEAL-001","seal_intact":true}'

# 实验室入库
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-S1","event_type":"storage","sample_code":"S-001",
  "occurred_at":"2026-09-30T05:00:00Z",
  "custodian":"实验室","storage_location":"冷库A-01"}'
```

- 新事件 `201`；同 `event_id` 同内容重试 `200`（`idempotent: true`）；异内容 `409 event_conflict`。
- 事件号全局唯一且**永久占用**：被作废/替换的事件同号同内容重试仍幂等 200（事件不复活），异内容 409。
- 响应中 `evaluation.status` / `reasons` / `current_custodian` 为写入后重算结果。

### 5.3 查询

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/samples` | 全部样本核验摘要（结论、原因、当前保管人） |
| `GET /api/v1/samples/{sample_code}` | 单样本登记信息 + 核验摘要 |
| `GET /api/v1/samples/{sample_code}/chain` | **完整证据链**：按发生时间排列的事件时间线、当前保管人、在途移交、结论与全部原因 |

```bash
curl http://localhost:8080/api/v1/samples/S-001/chain
```

`chain` 响应要点：

```json
{
  "sample": {"sample_code": "S-001", "collector": "王采集", "seal_no": "SEAL-001", "...": "..."},
  "status": "valid",
  "reasons": ["保管链完整：采集 王采集 至当前保管人 实验室，封签一致，已入库"],
  "current_custodian": "实验室",
  "pending_transfer": null,
  "has_storage": true,
  "timeline": [
    {"event_id": "EVT-T1", "event_type": "transfer", "occurred_at": "...", "recorded_at": "...",
     "payload": {"...": "..."}, "status": "active", "correction_id": null}
  ],
  "corrections": [
    {"correction_id": "CORR-1", "action": "replace", "target_event_id": "EVT-R0",
     "new_event_id": "EVT-R0B", "operator": "质管员", "reason": "签收人录错", "created_at": "..."}
  ]
}
```

- `timeline` 含**全部历史事件**（按发生时间排列）：`status=active` 为参与核验的有效版本；`voided`（已作废）/ `replaced`（已被替换）仅留痕不参与核验，`correction_id` 指向使其失效的纠错号。
- `corrections` 为该样本全部纠错记录（作废/替换、目标事件号、新事件号、操作人、原因、时间），被替换/作废记录由此可追溯。

### 5.4 入库核定

`POST /api/v1/samples/{sample_code}/approve`

```bash
curl -X POST http://localhost:8080/api/v1/samples/S-001/approve \
  -H 'Content-Type: application/json' -d '{"approver":"质量主管","note":"准予入库"}'
```

- `valid` → `200` 核定成功并冻结；`pending`/`invalid` → `422 approval_rejected`（响应体带回完整核验结果）。
- 核定后再上报事件或改写登记 → `409 history_locked`；重复核定幂等返回。
- 核定与纠错并发时，核定只基于**最终提交的有效链**（写操作串行化：谁先提交，另一方即看到其结果）。

### 5.5 事件纠错（作废 / 替换）

`POST /api/v1/corrections`

核定前对**当前有效事件**纠错；原事件与纠错关系永久留痕，核验只纳入有效版本。

| 字段 | 必填 | 说明 |
|---|---|---|
| `correction_id` | ✔ | 全局唯一纠错号；同号同内容重试幂等 200，异内容 409 |
| `sample_code` | ✔ | 样本编号（纠错不得跨样本） |
| `action` | ✔ | `void`=作废 / `replace`=替换 |
| `target_event_id` | ✔ | 被纠错的**当前有效**事件号 |
| `operator` | ✔ | 操作人 |
| `reason` | ✔ | 纠错原因 |
| `new_event` | replace 必填 | 完整新事件内容（字段同事件上报，**不含** `sample_code`；`event_id` 须全局未占用） |

```bash
# 作废：签收封签号录错，作废该签收（链按剩余有效事件重算）
curl -X POST http://localhost:8080/api/v1/corrections -H 'Content-Type: application/json' -d '{
  "correction_id":"CORR-001","sample_code":"S-001","action":"void",
  "target_event_id":"EVT-R1","operator":"质管员","reason":"签收封签号录入错误"}'

# 替换：移交时间录错，用未占用的新事件号给出完整新事件
curl -X POST http://localhost:8080/api/v1/corrections -H 'Content-Type: application/json' -d '{
  "correction_id":"CORR-002","sample_code":"S-001","action":"replace",
  "target_event_id":"EVT-T1","operator":"质管员","reason":"移交时间录入错误",
  "new_event":{"event_id":"EVT-T1B","event_type":"transfer",
               "occurred_at":"2026-09-30T02:10:00Z",
               "transfer_no":"TR-1","from_custodian":"王采集","to_custodian":"李运输"}}'
```

- 成功 `201`（响应含纠错记录与重算后的 `evaluation`）；同纠错号同内容重试 `200`（`idempotent: true`）。
- 目标事件不存在 `404 event_not_found`；不属于该样本 `409 cross_sample_correction`；已被作废/替换 `409 event_not_active`；新事件号被占用 `409 event_conflict`；核定后纠错 `409 history_locked`。
- 被作废/替换的事件号**永久占用**：`POST /api/v1/events` 同号同内容仍幂等 200（不复活），异内容 409。

### 错误响应统一格式

```json
{"error": {"code": "event_conflict", "message": "……", "details": {}}}
```

| HTTP | code | 触发 |
|---|---|---|
| 404 | `sample_not_found` | 样本不存在 |
| 404 | `event_not_found` | 纠错目标事件不存在 |
| 409 | `sample_conflict` / `event_conflict` | 同编号异内容 / 同事件号异内容 / 替换的新事件号已被占用 |
| 409 | `correction_conflict` | 同纠错号异内容 |
| 409 | `cross_sample_correction` | 纠错目标事件不属于该样本（不得跨样本） |
| 409 | `event_not_active` | 目标事件已被作废/替换，非当前有效版本（含并发仅一次成功） |
| 409 | `history_locked` | 已核定入库后改写历史（含纠错） |
| 409 | `concurrent_modification` | 数据库并发写冲突兜底（未生效，可重试） |
| 422 | `approval_rejected` | 非有效链尝试核定（或 Pydantic 字段校验失败，如 replace 缺 `new_event`） |

---

## 六、Python 调用示例

仓库内置一个**仅用标准库**（无需安装 requests）的端到端示例脚本：

```bash
python examples/call_api.py
```

脚本依次演示：登记 → 移交 → 签收录错 → **纠错作废** → 纠错号幂等/冲突 → 补录正确签收 → **纠错替换**（新事件号）→ 封签检查 → 入库 → 事件幂等/冲突 → 查询证据链（含留痕）→ 核定 → 核定后改写与纠错被拒。也可直接在 http://localhost:8080/docs 的 Swagger UI 中点击调用。

---

## 七、自测

```bash
python -m tests.test_chain
```

通过 FastAPI TestClient 覆盖 19 类场景共 77 项断言：登记幂等/冲突、四类无效情形（非保管人移交、在途再移交、签收四要素不符、重复签收、封签不符/破损）、待补证两情形、乱序补录重算、核定准入与冻结、事件早于采集、无效优先，以及本轮新增：**作废纠错后重算、替换纠错新号生效、原事件与纠错留痕可追溯、纠错号幂等/异内容冲突、跨样本拒绝、非有效版本拒绝、新事件号占用、并发纠错同一有效版本仅一次成功且失败不改链、核定基于最终有效链、核定后拒绝纠错**。

---

## 八、目录结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由、异常映射、启动初始化
│   ├── service.py    # 核心领域逻辑：幂等指纹、保管链重算、纠错（作废/替换）、核定冻结
│   ├── schemas.py    # 请求模型与按事件类型的必填校验（含纠错单）
│   ├── db.py         # SQLite 连接、建表与就地迁移、显式事务（写请求串行化）
│   ├── seed.py       # 演示数据初始化（幂等）
│   └── config.py     # 环境变量配置
├── examples/call_api.py   # 零依赖调用示例（含纠错演示）
├── tests/test_chain.py    # 端到端自测（77 项断言）
├── entrypoint.sh     # 容器入口：建表 → 演示数据 → 启动
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
