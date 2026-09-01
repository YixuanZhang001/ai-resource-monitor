# Phase 1F — Read-Only Architecture Audit

- Canonical repo: `D:\AI\projects\ai-resource-monitor`
- 基线 commit: `dc00d58`（Phase 1E-C Credential Boundary Closure）
- 本轮性质：**READ-ONLY 三重 Check**，未修改任何源码 / 测试 / config / SQLite / 未 commit / 未 tag
- 审计依据：当前工作树真实源码、`tests/`（231 项）、`data/monitor.db`（只读挂载，56 行真实事件）

---

## 1. Three-Way Check

### Project Goal（重新确认）
免费、轻量、开源、本地优先的 AI Resource Monitor。核心价值是**可靠记录**不同 AI API 的 调用 / usage / resource / cost / pricing / 错误 / 生命周期状态。
优先级铁律：**数据真实性 > 可靠记录 > 安全 > Provider-Agnostic > 正确 NULL 语义 > 可演进 > 功能丰富**。
两条不可违反的底线：**宁可 NULL，也不伪造**；**宁可 unknown，也不当 0**；**宁可暂不支持，也不为"通用"绕过 Provider 安全边界**。

### Previous Frozen Contract（Phase 1E 已冻结，本轮不重新设计）
- 统一 Event Ledger + `event_type ∈ {llm_call, rejected, error}`（error 当时是预防性枚举，非强制发射）。
- 调用即事件；usage/billing/cost 都不是落库前置。
- cost：NULL=unknown / 0=confirmed zero / >0=charged；统计必须区分 known/unknown/zero，禁止 `COALESCE(SUM(cost),0)` 冒充 unknown。
- Parser 链：Raw → Provider Parser → normalized usage → sanitizer → Ledger；已知 Provider 的未知 usage 字段不得静默丢失。
- Sanitizer 已冻结：数值不按键名删、凭据键精确匹配删、secret 值形态删、递归、未知默认保留。
- Credential：env/runtime only；config 不持真实 secret；Gateway 不得修改共享 ProviderConfig 后 save。

### Actual dc00d58 / 231-test State（真实验证，非报告推断）
- **Ledger GO**：`event_type` 判别、`cost` NULL 语义、rejected 排除统计、未知 usage→extension、migration 幂等，全部经测试 + 真实 DB 双重验证。
- **Credential GO**：`data/config.yaml` 无明文；`ConfigManager.save()` 有 secret stripping；Gateway 用 `copy.copy(cfg)` 注入 env key；H1/H2/H3 已闭合（Phase 1E-C 回归测试覆盖）。
- 实时 DB 实证：56 行全部 `event_type='llm_call'`；cost 48 known / 8 NULL；`billing_status`/`list_cost`/`pricing_snapshot_id` **全 NULL（0 行填充）**；8 行 `llm_call` 带 `error`+`status>=400`（失败调用以 llm_call+error 表示）；`rejected`/`error` 事件在实时数据里均为 0 行。

---

## 2. Billing Audit

| Item | Defined（schema 列存在） | Actually populated（实际写入） | Real source（真实数据来源） | Verdict |
|---|---|---|---|---|
| `billing_status` | ✅ TEXT 列（`storage.COLUMN_MIGRATIONS`） | ❌ 全 NULL（0 行） | 无。pipeline 仅 `core.ingest` 设 `cost`/`currency`；`billing_status` 既不从 pricing 也不从 anywhere 赋值 | 空转 |
| `list_cost` | ✅ REAL 列 | ❌ 全 NULL（0 行） | 无。pricing 只返回 `Cost(amount, currency)`，无"零售参考价"概念；与 `cost` 无独立数据源 | 空转 |
| `pricing_snapshot_id` | ✅ TEXT 列 | ❌ 全 NULL（0 行） | 无。`pricing_data.yaml` 是静态文件，**无版本/快照机制**，无从生成真 ID | 空转 |
| `currency` | ✅ TEXT 列 | ✅ 由 `Cost.currency` 填充 | `PricingRegistry` | 真实 |

**A1** `pricing.compute_cost` 返回 `Optional[Cost]`（amount, currency），unknown model → `None` → cost=NULL。不返回 billing_status/list_cost/pricing_snapshot_id。

**A2** 当前 pricing **不能**区分 unknown/known/free/included/paid。它只有"有价格→算；无价格→None"。`ResourceDefinition.billing_mode`（known/free/unknown…）是 **Resource 维度**的独立字段，**不流入 Event Ledger 的 `billing_status`**。

**A3** 若现在硬填 `billing_status`，唯一能填的就是 `unknown`（因为 pricing 不提供 paid/free 判定），结果恒为 `unknown`——典型"为字段齐全而制造空转架构"，违反"不伪造"铁律。

**A4** `list_cost` 与 `cost` 在本项目当前数据下**无真实可区分来源**：pricing 只产出一个 cost 数字，没有"厂商标价"另一份数字。强行造一个 list_cost=同样的 cost 是冗余伪造。

**A5** `pricing_snapshot_id` 要求存在 pricing 版本/快照源；当前**不存在**。生成假 ID 违反"不伪造"。

**A6 结论**：**BILLING: DEFER（保持 schema，暂不实现）**。原因：三个 billing 字段均无真实数据源，实现即伪造。正确做法=保留列（不破坏历史），等将来确有 pricing 来源（如厂商价目表/快照）时再填。

---

## 3. Error Event Audit

真实网关生命周期（来自 `gateway.py` 源码追溯）：
```
request
 → proxy(): adapter=get(provider)
     ├─ 未知 provider / 未启用 / 无 env key → _record_rejected(event_type='rejected')  ← 唯一 rejected 来源
     └─ 通过 → runtime_cfg=copy.copy(cfg)，注入 env key
 → _proxy_once / _proxy_stream:
     ├─ upstream 连接失败 (httpx.HTTPError) → _finalize(event, 502, error=...)   event_type 仍是默认 'llm_call'
     ├─ 非 2xx/含 error → _finalize(event, status_code, error=...)                event_type 仍是 'llm_call'
     └─ 成功 → _finalize(event, 200, usage)                                       event_type='llm_call'
```
**关键事实**：`_finalize` 从不修改 `event_type`，只设 `error`/`status_code`。因此一次真实失败调用 = **一个 `llm_call` 事件 + `error` 字段 + `status_code>=400`**。全程**没有任何代码路径 `event_type='error'`**（grep 确认：monitor/ 内唯一 `event_type=` 赋值是 `gateway.py:96` 的 `rejected`；`gateway.py:216/262` 的 `"error"` 是 SSE 广播 kind，非 event_type；`observe.py`/`openrouter.py` 的 `"error"` 是 `resource_states.status`，独立轴）。

**B1** 上游 500 是一次真实 API 尝试 → 应被记录（现已记录为 llm_call）。✅
**B2** 若改用 `event_type='error'` 且 `_since` 恒过滤 `llm_call`，失败调用将**从 requests 计数消失**（当前 requests 含失败 llm_call）。当前设计规避了此坑。
**B3** analytics：`requests = COUNT(*) WHERE event_type='llm_call'`（含失败尝试）；`errors = SUM(error IS NOT NULL OR status_code>=400)` 子集。请求数 = 全部尝试，错误数 = 其中失败，语义清晰。
**B4** `event_type='error'` 非必需；`llm_call + error` 字段已充分表达"调用发生且失败"，且保持"一次逻辑调用 = 一个事件"。
**B5** 引入 error 事件会冒"双发(llm_call+error) 或漏计"风险；当前方案无此风险。
**B6 结论**：**ERROR EVENT: DEFER（保留枚举，不发射）**。维持 `event_type='error'` 在枚举/模型中作为前向兼容占位，但上游失败沿用 `llm_call + error` 字段。删除枚举会破坏 `test_phase1e_invariants.py:35-37` 的构造测试，且无收益。

---

## 4. Generic Adapter Audit

| 维度 | 现状（真实） |
|---|---|
| 当前职责 | `GenericAdapter`（providers/generic.py）：**usage Parser fallback**——识别 OpenAI-compat 与 Gemini 两种 usage 形态，未知字段进 extension。`usage_supported=True`。同时它也有完整请求侧方法（upstream_url/headers），意味着若被当作 provider adapter 启用会转发请求。 |
| 实际调用链 | `registry.generic()` 暴露实例，但**全代码零生产调用方**（grep：仅 registry.py 自身 import/实例化、providers/__init__.py 导出、测试与文档引用）。Gateway 只用 `registry.get(provider)`。 |
| 是否绕过 allowlist | 当前**未接线 → 不绕过**。但因其带请求侧方法，若将来误把它接到未知 provider，会**绕过 Provider allowlist + Credential policy + Base URL policy**——这是 C3 安全边界风险。 |
| 真正价值 | C4 冻结结论：**"已允许 Provider 的 usage fallback"，不是"未知 Provider 的请求 fallback"**。已知 Provider 的 Parser 已各自把未知 usage 字段路由到 extension（openai_compat.py:85），故即使是已知 Provider 也暂不需要 Generic 兜底。 |
| 是否删死代码 | C5：不宜删。它有真实 Parser 价值、被测试引用（test_phase1e_invariants.py:256）、且前向兼容。留着无害。 |

**结论**：**GENERIC: DEFER（保留，不接线到 allowlist）**。若未来某已知 Provider 主 Parser 无法识别新 usage 形态，可作为该 Provider 的 usage 兜底解析器接入（仅响应侧，不接管请求侧），**绝不可用于服务未知 provider**。

---

## 5. Analytics Semantics

基于 `storage.py` + `main.py` 真实查询：

| 指标 | 统计口径 | 语义正确性 |
|---|---|---|
| `requests` | `COUNT(*) WHERE event_type='llm_call'`（含失败 llm_call） | ✅ 全部尝试计入 |
| `successful calls` | `requests - errors`（隐式） | ✅ |
| `failed calls` | `errors = SUM(error IS NOT NULL OR status_code>=400)` within llm_call | ✅ |
| `rejected` | `event_type='rejected'`（独立计，不进 requests） | ✅ 不污染 |
| `tokens` | `COALESCE(SUM(total_tokens),0)` within llm_call | ✅ 仅 token 求和，可接受 0 |
| `cost` | `SUM(cost) WHERE cost IS NOT NULL`，按 currency 分组 | ✅ unknown cost 排除 |
| `unknown cost` | `requests - cost_count`（_cost_status: unknown/mixed/known/none） | ✅ 与 0 区分 |
| `confirmed zero` | pricing 返回 0.0 → cost=0.0 入统计 | ✅ 与 NULL 区分 |
| `errors`（计数） | 同上 `errors` 字段 | ✅ |

NULL 语义实测无破坏：实时 DB `cost` 8 行 NULL 未被当 0；全仓无 `COALESCE(cost,0)` 用于冒充 unknown。

---

## 6. Test Gap（仅列真正缺失）

现有 231 测试已覆盖：cost NULL（test_pricing::unknown_model_returns_none / test_core::ingest_no_cost_when_pricing_absent / test_phase1e_invariants::unknown_cost_excluded / test_resource_analytics::free_resource_unknown_cost / test_cost_status_transitions）、rejected（多）、sanitizer（多）、credential boundary（10 项）、parser extension（test_unknown_usage_field_captured_in_extension / test_generic_adapter_captures_unknown）、event_type 判别（构造级）。

**真实缺口（建议补，但非阻塞）**：
1. **失败路径单一事件不变量**：尚无测试断言"上游 500/超时 → 恰好产生 1 个 `llm_call` 事件（带 error+status>=400），而非 0 个或 2 个"。`test_single_event_per_request` 只覆盖成功路径。
2. **负向测试：error event_type 不发射**：断言失败调用 `event_type != 'error'`（锁定 B6 决策，防回归）。
3. **billing 列恒 NULL 不变量**（可选）：断言 ingest 后 `billing_status/list_cost/pricing_snapshot_id` 为 NULL（当未实现时，防止未来误填）。

以上均是小量测试增量，可在"下一阶段最小闭环"中顺带补，不作为 P1 功能。

---

## 7. Architecture Decision（下一阶段最小闭环）

三项 P1 候选经审计**均不应现在实现**：

- **Billing 轴**：无真实数据源，实现即伪造 → **DEFER**。保留 schema 列，等 pricing 来源就绪再填。
- **error event_type**：已被 `llm_call + error` 字段充分覆盖，引入独立 error 事件有双发/漏计风险 → **DEFER**（保留枚举，不发射）。
- **GenericAdapter**：死代码但无害、有前向价值 → **DEFER**（保留，不接线到 allowlist）。

**诚实结论：当前系统对"已冻结 Contract"已处于 GO 稳定态；三个候选 P1 均属过度设计风险。**

因此下一阶段**最小闭环**建议二选一（均不扩大范围）：

- **方案 A — Phase 1 收尾 + 稳定性加固**（推荐）：补 §6 的 3 个测试（失败路径单一事件、error 不发射、billing 列恒 NULL），使关键不变量被锁定；随后可正式宣告 **Phase 1 完成（Ledger GO + Credential GO）**。
- **方案 B — 进入新价值区**：如 Resource Observation 生产可用性（openrouter collector 真实拉取余额/配额并落 `resource_states`）、或 Dashboard 真实展示 usage/error/cost 分布。这些不在本次 P1 候选内，需用户新指令。

**不建议**现在同时推进 Billing + Error + Generic——那会制造空转架构，违背产品铁律。

---

## 8. Gate

> **DEFER**

- 非 BLOCKED：当前 Contract 全部满足，Ledger GO + Credential GO，无安全/数据正确性缺陷。
- 非 GO（指"立即实现 Billing/Error/Generic"）：三项均无真实需求且不实现不造成功能缺失；强行实现违反"不伪造/不空转"原则。
- 行动建议：先以**方案 A**（3 个不变量测试）收尾 Phase 1，或等待用户指定新价值区（方案 B）。

---

## 真实证据索引（本轮只读，未改动任何文件）

- `monitor/pricing.py:103-137` — compute_cost 仅返回 amount/currency，unknown→None
- `monitor/core.py:83-86` — ingest 仅设 cost/currency，不碰 billing 三字段
- `monitor/storage.py:73,93-95` — billing 三列在 COLUMNS/迁移中，但无任何写入查询
- `monitor/gateway.py:66-81,196-216,229-262` — 失败路径保持 llm_call + error，_finalize 不改 event_type
- `monitor/gateway.py:96` — 唯一 `event_type='rejected'` 赋值
- `monitor/registry.py:67-71` — generic() 存在但无生产调用方
- `monitor/providers/openai_compat.py:83-93` — 已知 Provider 未知 usage 字段→extension（Case B 已满足）
- 实时 DB：`event_type` 仅 `llm_call`；billing 三列全 NULL；cost 48 known/8 NULL
- 测试：231 passed（Phase 1E-C 基线）；本审计未运行测试（仅静态+DB 核实）
