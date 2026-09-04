# PHASE 3B — EFFICIENCY DERIVATION LAYER

> 目标：验证当前 Monitor 已采集的数据，能否真正转化为"效率判断"，并明确哪些结论可靠、哪些因数据缺失无法判断。
> 铁律：宁可 NULL，也不伪造。NULL cost ≠ 0；缺失数据绝不补 0 / 估算 / 推导。

---

## 一、Mandatory Three-Way Check（执行前）

| Check | 结论 |
|---|---|
| 1. 最终目标 | 多 Resource / Provider / Model 下回答：有哪些资源、用了多少、花了多少、谁最贵/最便宜、单次成本、每 1K Token 成本、Token 效率、Cache 有效性、错误率、延迟、消耗趋势、是否存在浪费——且每条结论必须携带**数据可信度**。 |
| 2. Frozen Contract | 确认未破坏：一请求=一 Ledger event；failure=llm_call+error（无 error event_type）；NULL cost≠0；unknown pricing→cost=NULL；secret 仅来自 Credential Boundary；Observation 无数据→NULL；Analytics 不改动底层数据语义。 |
| 3. 当前真实状态 | HEAD=`5aef3c0`（tag `baseline-after-phase2c`）；工作区仅 3 个未跟踪 .md 报告，无代码改动；**260 测试全绿**（已复核）；`monitor.db` 56 事件真实数据。 |

→ 目标、架构、当前实现无根本冲突，**继续**。

---

## 二、本阶段做了什么（最小实现）

**零 schema 变更、零新表、零新 Provider、零 Billing/Generic/Credential 改造。**

`monitor/storage.py`（+251 行，全为新增只读方法）：
- `efficiency_overview(since)` — 全局效率概览（含成本/令牌/错误/缓存/延迟覆盖度）。
- `efficiency_by_dim(dim, since)` — 按 `provider / model / source / project / resource_id` 聚合，每行携带完整覆盖度。
- `balance_trend(resource_id, limit)` — 余额观测趋势，仅给 `balance_delta` / `observed_balance_change`，**绝不命名为 burn rate**。

`monitor/main.py`（+39 行）：新增
- `GET /api/efficiency/overview`
- `GET /api/efficiency/by_dim?dim=...`
- `GET /api/resources/{id}/balance-trend`

既有 `/api/analytics/*` 与响应结构**完全不动**（向后兼容）。

### 覆盖度感知设计（核心）
每条比率都同时返回分母/覆盖度，绝不隐藏未知：
- 成本：`known_cost` + `priced_requests` + `cost_coverage` + `cost_per_request` + `cost_per_1k_tokens`，**按货币分列**（`cost_by_currency`），多货币不混算。
- 令牌：`tokens_per_request` + `tokened_requests` + `token_coverage`；分母为 0 或缺数据 → `None`。
- 缓存：`cache_hit_rate`（请求级，严格成立）+ `cache_read_ratio`（令牌级，明确 cache_read⊆input 约定）+ `cache_coverage` + `cache_data_feasible` 标志。
- 错误：`error_rate`，0 请求 → `None`（非 0%）。
- 延迟：`avg / p50 / p95`，无数据 → `None`。

---

## 三、真实数据验证（monitor.db，56 事件）

| 指标 | 真实值 |
|---|---|
| 请求数 / 错误数 / 错误率 | 56 / 8 / **14.29%** |
| Token 覆盖度 | total=621，`tokened_requests`=48 → **85.71%** |
| 成本覆盖度 | 计价 48/56 → **85.71%**（CNY 44、USD 4，双货币并存） |
| cost_per_1k_tokens | CNY **0.003831** |
| 缓存覆盖度 | `cache_observable`=10/56 → **17.86%**，cache_hit_rate=20% → **PARTIAL** |
| 延迟 | avg 318.2ms / p50 2.7ms / p95 1096.6ms |
| **Resource 归因** | **仅 3/56 带 resource_id（5.4%）**，53 未归因 → Resource 级效率**基本 UNKNOWN** |

### Q1–Q8 验收（真实数据）
- **Q1 今天花了多少？** API 支持 `range=today`；全量已知成本 CNY 0.002134 + USD 5.6e-05。
- **Q2 哪个 Resource 最贵？** 归因资源仅 deepseek-paid(2)、verify-p0-5(1)，**95% 流量未归因 → 不可靠**。
- **Q3 哪个 Provider/Model 单位 Token 最贵？** 同货币可比：deepseek-chat 0.003957 > deepseek-v4-flash 0.002286（CNY）；gemini-2.5-flash 0.000875（USD，单独）。
- **Q4 哪个 Resource 请求最多？** 仍是未归因（53）；归因资源中 deepseek-paid=2。
- **Q5 哪个 Resource 错误率最高？** Provider 级 deepseek 15.38%；Resource 级样本过小不可靠。
- **Q6 哪个 Resource 延迟最高？** 同上，Provider/Model 级可靠。
- **Q7 成本多少真实已知？** **Total 56，Priced 48，Coverage 85.7%**（8 个 unknown）。
- **Q8 能否判断浪费？** **PARTIAL**（见第六节）。

---

## 四、Goal Coverage Matrix

| 最终目标 | Phase 3B 前 | Phase 3B 后 | 仍缺什么 |
|---|---|---|---|
| 有哪些 Resource | ✅ Registry | ✅ + 按 resource_id 效率聚合 | 依赖 Gateway 正确归因（当前仅 5.4%） |
| 用了多少 | ✅ requests/tokens | ✅ + token_coverage 透明 | — |
| 花了多少 | ✅ cost_by_currency | ✅ + known_cost + cost_coverage 透明 | — |
| 谁最贵 | △ by provider/model | ✅ + cost_per_request + cost_per_1k + 覆盖度 | 跨货币不可比（需统一或限定范围） |
| 谁最高效 | ❌ | ✅ tokens_per_request / cost_per_1k / output_share | 跨货币不可比 |
| 谁错误最多 | ✅ errors | ✅ + error_rate NULL 安全 | — |
| 谁延迟最高 | △ avg | ✅ + p50/p95 分维度 | — |
| 是否存在浪费 | ❌ | ◑ 6 项信号可行性判定（GO/PARTIAL） | 完整判定需 State 层 + 推荐引擎（明确不做） |
| 余额多少 | ✅ observation.balance | ✅ 同左 | 依赖 collector 真实回填 |
| 余额变化 | ❌ | ✅ balance_trend（明确非 burn rate） | 真实多期已知余额不足（每资源 0–1 点→多为 None） |
| Quota | 列存在未填 | ❌ NOT FEASIBLE | Phase 3C |
| Rate Limit | ❌ | ❌ NOT FEASIBLE | Phase 3C |
| Health | ❌ | ❌ NOT FEASIBLE | Phase 3C |
| Subscription | ❌ | ❌ NOT FEASIBLE | Phase 3C |
| 优化建议 | ❌ | ❌ NOT FEASIBLE（不做 AI 推荐） | 需推荐引擎（超范围） |

---

## 五、Waste Detection 可行性（仅数据层）

| 信号 | 判定 | 说明 |
|---|---|---|
| 高成本低 Token | **GO** | cost_per_1k_tokens 可计算并排序 |
| 高错误率 Resource | **GO** | error_rate 分维度，Provider/Model 级可靠 |
| 高延迟 Resource | **GO** | avg/p50/p95 分维度 |
| Cache 表现差 | **PARTIAL** | cache 数据仅 17.9% 覆盖，可作信号但置信低 |
| 大量 unknown cost | **GO** | cost_coverage 直接回答（85.7% 已知） |
| 某 Provider/Model 明显偏高 | **PARTIAL** | 同货币内可比；跨货币（CNY/USD）不可直接比 |

---

## 六、验证与审计

- **测试**：新增 `tests/test_phase3b_efficiency.py`（16 项，覆盖 17 个纪律点：NULL 不参与、0 请求→None、unknown 不伪造、失败不计成本、三维度聚合、cost/request、cost/1k、tokens/request、error rate、latency 分位、cache 仅语义成立、balance delta、无 burn_rate 字段）。
- **全量回归**：**276 passed**（260 基线 + 16 新），零回归。
- **Security Audit**：无 INSERT/UPDATE/DELETE/ALTER；无 quota/remaining/health/rate/subscription/billing_status/list_cost/pricing_snapshot 使用；无新表/新 Provider；全部 SELECT/GROUP BY + 白名单维度表达式；成本与余额 null-safety 确认。
- **Diff 规模**：`storage.py` +251、`main.py` +39，仅新增只读方法，未改任何现有方法签名/行为。

---

## 七、最终判定

**GO WITH CONDITIONS（条件满足）。**

当前路线下，**Usage + Cost + Efficiency 已形成新的基本闭环**——Ledger 数据被证明可可靠转为效率信息，且每条结论都标注了数据覆盖范围。

**距离"真正可用的 AI Resource Monitor"还差：**

1. **State Observation 维度（Phase 3C）**：quota / health / rate-limit / subscription 当前全部为 NULL，是剩余最大瓶颈。这是真正的产品缺口，不是计算问题。
2. **数据采集质量**：
   - Resource 归因仅 5.4%（53/56 未带 resource_id）→ "哪个 Resource 最贵" 当前不可靠，需 Gateway 正确下发 `X-Monitor-Resource`。
   - Cache 数据仅 17.9% 覆盖 → 缓存有效性判断置信低。
   - 余额多期已知点不足 → balance_trend 多为 None。
3. **跨货币可比性**：CNY/USD 并存，单位 Token 成本跨货币不可直接比较（需货币归一或限定单货币范围）。
4. **不做**：AI 推荐系统 / 优化建议 / 大规模 Dashboard 重构（本阶段明确禁止）。

**下一步建议（依 Phase 3A）：进入 Phase 3C — State Capability Expansion（MINOR schema 扩展补 quota/health/rate-limit/subscription）。不要继续横向堆 Provider。**

---

*未 commit（按阶段纪律等待用户确认后再走 Commit & Baseline Closure）。*
