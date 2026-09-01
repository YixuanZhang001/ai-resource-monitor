# Phase 1F-B — Ledger Contract Lock

**目标**：用最小新增测试，把当前已实现（已 GO）的 Ledger 语义正式锁死，防止后续
Resource Observation / Dashboard 开发时发生回归。本阶段**不修改任何生产架构**。

- HEAD（起点）：`dc00d58`（Phase 1E-C: Credential Boundary Closure）
- HEAD（终点）：`56019d6`（phase1f: lock ledger failure and billing invariants）
- baseline tag：`baseline-before-phase1f`（打在 `dc00d58`）

---

## 1. 三重 Check 结果

### ① 项目总目标（重新确认）
免费、轻量、开源、本地优先的 AI Resource Monitor；可靠记录 AI API 的
调用/Usage/Cost/Resource/状态。**核心原则**：宁可 NULL 也不伪造、不丢 Usage、
不泄漏 Credential、一个真实调用 = 一个逻辑 Event、Provider 差异经 Adapter 隔离。

### ② 上一阶段 Frozen Contract
- 统一 Event Ledger + `event_type ∈ {llm_call, rejected, error}`（生产路径当前仅 `llm_call`/`rejected`）
- unknown cost = NULL（绝不 COALESCE 成 0）
- rejected 不进入 usage analytics；usage 未知字段 → extension → sanitizer → Ledger
- Credential runtime-only；ConfigManager 不持有/持久化真实 secret

### ③ dc00d58 实际源码（重新核对，非凭旧报告）
- `gateway.py`：失败路径走 `_finalize(event, started, 502/500, usage, error)`，
  event 默认 `event_type="llm_call"`；唯一 `event_type=` 赋值是 `gateway.py:96` 的 `"rejected"`。
  `gateway.py:216/262` 的 `"error"` 是 **SSE broadcast kind**，不是 Ledger `event_type`。
- `core.py`：`ingest` 仅 set `cost`/`currency`；`billing_status`/`list_cost`/`pricing_snapshot_id`
  仅当 raw 提供才透传，而 gateway 永不提供 → 这三列恒 NULL。
- `storage.py`：`recent_events` 返回 `SELECT *`，列名即 dict key；`_since` 恒过滤 `event_type='llm_call'`。

**结论**：三重 Check 与 Phase 1E-C 收尾状态一致，无实现漂移，直接进入最小测试锁定。

---

## 2. 修改文件清单

| 文件 | 变更 | 原因 |
|---|---|---|
| `tests/test_phase1f_contract_lock.py` | 新增（192 行，4 项测试） | 锁定 Ledger 失败语义 + Billing 空轴 NULL 契约 |

**未修改**：生产代码 0 处；`config.yaml` 0 改动；`monitor.db` 0 改动；无新字段/表；
无 Billing/error-event/GenericAdapter 实现；凭据边界未触碰。

---

## 3. 三个 Invariant 的具体测试内容

### Invariant 1a — upstream 500 → 恰好一个 llm_call Event
`test_failed_upstream_500_single_llm_call_event`
- 启动返回 500 的上游 stub；经 `TestClient` 打 `/gateway/deepseek/chat/completions`。
- 断言：`recent_events` 恰好 1 行；`event_type == "llm_call"`；`event_type != "error"`；
  `error is not None`；`status_code >= 400`。
- 核心：一个逻辑调用 = 一个 Event，不因 error 再生第二条。

### Invariant 1b — 连接失败/timeout → 同样恰好一个 llm_call Event
`test_failed_connection_single_llm_call_event`
- 端口关闭并 `server_close()`（释放 socket，避免 httpx 连残留端口挂起）→ httpx ConnectError。
- 断言：同样恰好 1 行 `event_type="llm_call"`，`error not None`，`status_code >= 400`。
- 覆盖 timeout/connection-error 类失败路径。

### Invariant 2 — 生产代码不得发射 event_type="error"
`test_failure_never_emits_error_event_type`
- 失败调用后，断言 `event_type == "error"` 的行数为 0；且存在 `event_type=="llm_call" and error` 的行。
- 明确区分 **SSE broadcast kind="error"** 与 **Ledger event_type="error"**（测的是数据库 Event Ledger）。

### Invariant 3 — Billing 空轴在无真实 source 时保持 NULL
`test_billing_axis_null_without_source`
- 成功请求（upstream 200）后，断言 `billing_status`/`list_cost`/`pricing_snapshot_id` **全为 None**。
- 不为 `"unknown"`、不为 `0`、不为 `"free"`、不为伪造 snapshot。锁定「无真实数据源即 NULL」。

---

## 4. pytest 最终结果

- Before：`231 passed`
- After：**`235 passed`**（新增 4 项，原 231 项零回归）
- 命令：`pytest -q`（后台运行，约 132s；前台曾因 120s 沙箱超时误杀，非测试失败）
- 新增测试单独运行：`4 passed in 8.64s`

---

## 5. git diff 审查结果

```
git status --short (commit 后):
  ?? PHASE-1E-A-REPORT.md        # 历史遗留，非本阶段范围，未纳入
  ?? PHASE-1F-AUDIT.md           # 本阶段审计产物，非本阶段范围，未纳入

git show --stat 56019d6:
  1 file changed, 192 insertions(+)
  create mode 100644 tests/test_phase1f_contract_lock.py
```

- 仅新增 1 个测试文件，无生产代码改动。
- `data/`、`config.yaml`、`monitor.db` 均未被触碰（grep 确认无相关变更）。
- 无 secret / temp / audit 产物进入 commit。

---

## 6. Commit Hash

`56019d6` — `phase1f: lock ledger failure and billing invariants`
baseline tag：`baseline-before-phase1f`（锚定 `dc00d58`）

---

## 7. Phase 1 是否正式达到「Ledger + Credential Boundary locked」

✅ **是。**

| 验收项 | 结果 |
|---|---|
| 失败请求 = exactly one Event | ✅ Invariant 1a / 1b |
| 失败 Event = event_type=llm_call | ✅ 同上 |
| error event_type 当前生产路径仍未发射 | ✅ Invariant 2 |
| rejected 与 error 没有混淆 | ✅ 唯一赋值是 `"rejected"`（gateway.py:96） |
| Billing 三列在无真实 source 时保持 NULL | ✅ Invariant 3 |
| cost NULL 语义未改变 | ✅ 未触碰 storage/core，仍 `cost IS NOT NULL` 聚合 |
| Credential Boundary 未改变 | ✅ 未触碰 config/credential/gateway 边界逻辑 |
| 全量 pytest PASS | ✅ 235 passed |
| 无生产代码无必要修改 | ✅ 仅新增测试 |
| 无 DB/config 修改 | ✅ |
| 无 secret/temp 文件进入 git | ✅ |

**后续明确仍 DEFER（非本阶段范围，不实现）**：
- Billing 轴（billing_status/list_cost/pricing_snapshot_id）—— 无真实数据源前保持 NULL。
- error event_type —— 维持 `llm_call + error` 字段语义，不独立发射。
- GenericAdapter —— 保持仅作 usage fallback 暴露，绝不接线到未知 provider / allowlist。

**阶段状态：Phase 1（Ledger Core + Credential Boundary）已 locked。STOP，等待下一轮指令。**
