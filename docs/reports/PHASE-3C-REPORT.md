# Phase 3C Implementation Report

> 范围（经 Audit 验证，严格受限）：
> MUST = Resource Attribution；SHOULD = 最小 Health；DEFER = rate-limit；DO NOT = credential 映射 / 历史回填 / 新 event_type / schema 变更 / AI 推荐 / 新 Provider / Dashboard redesign。

## 1. Baseline
- **HEAD** = `5aef3c0`（tag `baseline-after-phase2c`）。
- 工作树：Phase 3B 仍未提交（`main.py` +39、`storage.py` +251、`tests/test_phase3b_efficiency.py` 新增）。
- 测试基线 **276 passed**；`monitor.db` 56 events，仅 3（5.4%）带 `resource_id`。
- 本环境无合法 API credential（`DEEPSEEK_API_KEY`/`OPENAI_API_KEY` ABSENT）→ 未做真实 Gateway probe（符合纪律）。

## 2. What Changed
| 文件 | 变更 |
|---|---|
| `monitor/gateway.py` | 新增 `_resolve_resource_id(provider, header, resources)`；`_build_event` 接收已解析 `resource_id`；`proxy` 用 helper 替换原校验块（显式 header 权威 + 唯一 enabled 回退）。 |
| `monitor/storage.py` | 新增 `resource_health(resource_id, since)`（基于 `status_code`/`error`/`event_type`/`resource_id` 的派生，无新字段）。 |
| `monitor/main.py` | 新增 `GET /api/resources/{resource_id}/health`（复用 `store.resource_health`）。 |
| `tests/test_phase3c_attribution.py` | 新增 25 项测试（attribution 1–10 + 端到端 + analytics 11–18 + health 19–22）。 |

**净结果：301 passed（276 基线 + 25 新），零回归。**

## 3. Resource Attribution Design
```
Client
  → Gateway.proxy
  → resolved_rid, reject = _resolve_resource_id(provider, X-Monitor-Resource, resources)
  → 若 reject（未知/禁用资源）→ 400（沿用既有 HTTP contract，不重新设计）
  → _build_event(resource_id=resolved_rid)
  → core.ingest → EventStore → Analytics
```
规则（Frozen-Contract 安全）：
- **显式 `X-Monitor-Resource`**（非空）= 权威来源；必须映射到已注册且 `enabled` 的 Resource，否则 400 拒绝（不自动创建）。
- **header 缺失/为空** → 仅当该 provider 恰好有【唯一】`enabled` Resource 时确定性归因；0 或 >1 个 → `resource_id = NULL`（**绝不随机/猜测**）。
- 空字符串不被当作 `resource_id` 落库（旧代码会把 `""` 写进库，已修正）。
- **不读 Credential Boundary、不碰 `credential_id`、不引入新配置字段**（`default_resource_id` 未加；回退用既有 `ResourceRegistry.list(enabled_only=True)` 按 `.provider` 过滤即可）。
- 历史事件 `resource_id = NULL` 永久保持 NULL（无 UPDATE/回填）。

## 4. Health Design
`resource_health` 仅用既有 events 字段派生（无新采集、无新 schema）：
- `n == 0`（无 llm_call 事件）→ `health = "unknown"`、`error_rate = None`（**无数据 ≠ healthy**）。
- `errs == 0` → `healthy`；`0 < errs < n` → `degraded`；`errs >= n` → `unavailable`。
- `error_rate` 在 `n>0` 时计算，否则 `None`（不伪造成 0）。
- 仅计入 `event_type = 'llm_call'`（拒绝事件不计健康）。

## 5. What Was Explicitly NOT Implemented
- ❌ credential secret → identity → resource 映射（审计已证不可行/非唯一）。
- ❌ 历史 `resource_id` 回填 / 修改历史 events。
- ❌ 新 `event_type` / 改动“一请求=一事件”。
- ❌ `NULL cost` / `unknown pricing` / 跨 CNY·USD 静默换算。
- ❌ 伪造 quota / remaining / rate-limit / health / burn rate。
- ❌ rate-limit header 采集（DEFER：本环境无 credential，未验证 Provider 真实返回）。
- ❌ AI 推荐 / 优化建议 / 新 Provider / Dashboard 大改 / 复杂 Resource hierarchy / billing·subscription / quota system。

## 6. Tests（25 passed）
- **Attribution（1–10）**：valid header / missing+unique fallback / missing+multiple→NULL / invalid→400 / disabled→400 / empty→fallthrough / multi-provider 独立 / same-provider-multiple 不猜 / 历史 NULL 不变 / 未归因保持 NULL。
- **端到端（3）**：显式 header 落库 / 唯一回退落库 / 多 resource→NULL，均经 mock upstream 200 验证。
- **Analytics（11–18）**：Resource token 聚合 / cost 聚合 / error rate / latency / efficiency 存在 / NULL cost 排除 / NULL token 排除 / 多币种分列。
- **Health（19–22）**：healthy / degraded+unavailable / 无数据→unknown / Resource 级分离。

## 7. Real Data Validation（只读 `monitor.db`）
- 56 events → 仍 3 归因 / 53 NULL（**未回填**，历史 NULL 不变）。
- `efficiency_by_dim("resource_id")` 正确返回：`deepseek-paid`(2 req, healthy, CNY 覆盖 1.0)、`verify-p0-5`(1 req)，**外加显式 `None` 未归因桶（53 req）**——不伪装进任何 Resource。
- `resource_health`：`deepseek-paid`→healthy；未知资源→`unknown` + `error_rate:null`（不谎报 healthy）。
- 无 credential → 未发起真实 Gateway probe（符合纪律，不猜）。

## 8. Diff Audit
- `git diff --stat`：`gateway.py` +54/-13、`main.py` +48、`storage.py` +293（含 3B 未提交部分）。仅新增方法/端点与最小逻辑替换；**无 schema 变更、无新表、无新字段**。

## 9. Security Audit
- `gateway.py` 无 `UPDATE/INSERT/DELETE` → **无历史回填**。
- 全仓 forbidden-pattern grep：命中仅出现在**注释**（禁止清单本身 + burn_rate 非目标说明），无可执行 DDL/DML/credential/rate-limit 代码。
- `resource_id` 仅来自客户端 header（校验后）或唯一 enabled 回退；**绝不来自 secret/credential**。
- 响应/事件不含 Authorization/secret；Health 不引入新数据写入。

## 10. Goal Coverage Matrix
| Question | Before 3C | After 3C |
|---|---|---|
| Global Usage | ✅ 可用（token 覆盖 85.7%） | ✅ 不变 |
| Resource Usage | ❌ 仅 5.4% 归因，基本不可靠 | 🟡 **机制正确**：新流量 + 唯一回退可归属；历史 94.6% 仍 NULL（按契约） |
| Global Cost | 🟡 覆盖 85.7%，CNY+USD 分列 | ✅ 不变 |
| Resource Cost | ❌ | 🟡 新归因流量可算；多币种按货币分列，不静默换算 |
| Global Efficiency | ✅ | ✅ 不变 |
| Resource Efficiency | ❌ | 🟡 同 Resource Usage：新流量可经既有 `efficiency_by_dim(resource_id)` 成立 |
| Resource State | 🟡 balance/quota/remaining（Phase 2C collectors） | 🟡 不变（架构 per-Resource，数据稀薄非架构问题） |
| Resource Health | ❌ 无 | ✅ 最小派生（healthy/degraded/unavailable/unknown） |

> 诚实结论：**3C 让“Resource 级判断”的机制成立**，但对历史 94.6% 未归因事件，Resource 视图仍需前向流量累积才成规模。这不是 bug，是设计行为。

## 11. Remaining Gaps
- **P0 已解（机制）**：Resource 归因断裂 → 已通过 header 权威 + 唯一回退修复（对未来流量）。
- **P1**：历史 94.6% 事件按 Frozen Contract 永久未归因，完整历史 Resource 视图需前向累积。
- **P1**：State 数据稀薄（quota/remaining 仅 OpenRouter；DeepSeek 恒 NULL，不伪造）——Phase 3C 未动，仍属 Phase 2C 范畴。
- **P1**：Cache 覆盖 17.9%，无法可靠判 Resource 级 cache 效率。
- **DEFER**：rate-limit header 采集——需用户在持 credential 环境验证 Provider 真实返回 `x-ratelimit-*`/`retry-after` 后，单独开 task（最小改造点已留：网关 `resp.headers` → `metadata`，零 schema 变更）。

## 12. Final Verdict
**GO WITH CONDITIONS**

理由：
- 核心 P0（Resource Attribution）以**最小、安全、唯一、无 credential 改造、无 schema 变更**的方式落地，并经验证（25 新测 + 301 全绿 + 真实数据只读验证）。
- 既有 Frozen Contract 全部守住（一请求=一事件、NULL≠0、unknown pricing→NULL、Analytics 不改 Ledger、不伪造 State、历史 NULL 不回填）。
- Phase 3B 的 Global Usage/Cost/Error/Latency/Efficiency 现可经 `efficiency_by_dim(resource_id)` 在 Resource 维度成立（对新流量）。

剩余条件（非阻塞）：
1. **历史 94.6% 未归因**按契约保持 NULL，完整历史 Resource 视图需前向流量累积——非缺陷。
2. **rate-limit** 仍 DEFER，待用户持 credential 环境验证 Provider 响应头后单独实施（不在本阶段）。
3. **State 数据稀薄**（quota/remaining/health 上游源）属已知 P1，不在 3C 范围，建议后续独立处理。

---
> 纪律：本轮未 commit / 未 tag。Phase 3B 与 Phase 3C 变更均在工作树未提交。建议下一步：审阅后一次性提交 `3B+3C` 并打 `baseline-after-phase3c` tag。
