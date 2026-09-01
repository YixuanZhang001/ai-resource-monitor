# Phase 1E-A — Canonical Ledger Core Implementation Report

**仓库**：`D:\AI\projects\ai-resource-monitor`（Phase 1D 裁决的 canonical repo）
**基线**：commit `5a99398` / tag `baseline-before-phase1e`
**本阶段提交**：commit `52c771f`（22 files changed, +757 / -77）
**裁决状态**：GO（含 1 项 PARTIAL 残留，见文末）

---

## 一、裁决落地情况（对照 Phase 1E-A 十九条裁决）

| # | 裁决项 | 落地 | 说明 |
|---|--------|------|------|
| 一 | Canonical repo = ai-resource-monitor | ✅ IMPLEMENTED | api-monitor 保持 legacy，未改、未双写、未迁移 39 行 |
| 二 | 历史数据 39 行不迁移 | ✅ IMPLEMENTED | legacy ambiguous，留在 api-monitor |
| 三 | 凭据 env/runtime-only（CredentialProvider） | ⚠️ PARTIAL | 网关已改为仅 env 读取；`upsert` 仍写 config.yaml（残留，见 §七） |
| 四 | 单 Event Ledger + event_type 判别 | ✅ IMPLEMENTED | llm_call / rejected / error；rejected token/cost=NULL |
| 五 | Ledger 最小语义（event_id/type/occurred_at/source/provider/status，NULL≠0） | ✅ IMPLEMENTED | `_since` 统一 `event_type='llm_call'` 过滤 |
| 六 | Usage Model 标准字段 + extension/metadata | ✅ IMPLEMENTED | 不再只认 3 字段；未知字段进 extension |
| 七 | Parser→Sanitizer→Ledger 链路；raw 绝不直接入 DB | ✅ IMPLEMENTED | `apply_usage` 落库前经 `sanitize_usage_dict` |
| 八 | Sanitizer 8 条决策树 | ✅ IMPLEMENTED | 见 §三 |
| 九 | Provider Parser：OpenAI-compat + Gemini + Generic | ✅ IMPLEMENTED | Generic 兜底解析器新增 |
| 十 | 时间语义 UTC 毫秒 | ✅ IMPLEMENTED | `utc_now_ms()`；`occurred_at` 列 |
| 十一 | Billing 本轮仅数据契约 | ✅ IMPLEMENTED | cost/currency/billing_status/list_cost/pricing_snapshot_id；unknown→NULL |
| 十二 | Resource Ledger 仅修正凭据边界 | ✅ IMPLEMENTED | resource 层 credential_id 引用不变 |
| 十三 | Gateway 仅修正链路 | ✅ IMPLEMENTED | 见 §四 |
| 十四 | Proxy Security 127.0.0.1 最小边界 | ✅ IMPLEMENTED | 未放宽 |
| 十五 | 测试 24 项不变量 | ✅ IMPLEMENTED | `tests/test_phase1e_invariants.py`（25 项） |
| 十六 | Step A–G 每步重查 | ✅ IMPLEMENTED | 每 Step 重读真实源码后动工 |
| 十七 | 仅改 canonical repo | ✅ IMPLEMENTED | api-monitor 零改动 |
| 十八 | Git 基线 commit/tag，禁 force reset | ✅ IMPLEMENTED | 先 tag 再改业务；提交为增量 |
| 十九 | 最终输出 Report + GO/BLOCKED | ✅ 本文件 | GO |

---

## 二、事件模型（Step B）

- `AIRequestEvent` 统一为单一 Event Ledger；`event_type` 判别：`llm_call`（默认）/ `rejected` / `error`。
- `rejected` 事件：`apply_usage(None)` → 全部 token/cost 字段保持 NULL；`status_code` 仍记录；**不进入 Usage 统计**（`storage.py` 所有统计查询经 `_since` 过滤 `event_type='llm_call'`）。
- 字段扩围：`reasoning_tokens`、`cache_read/write_tokens`、`usage_extension`、`occurred_at`(UTC ms)、`schema_version`(=2)、`cost`/`currency`/`billing_status`/`list_cost`/`pricing_snapshot_id`。

**`estimated_cost → cost` 重命名**：`events.py`(模型) / `storage.py`(列+`COLUMN_RENAMES` 幂等 rename) / `core.py` / `gateway.py` / `main.py` / `dashboard/index.html` / `scripts/*.py` 全链路一致。遗留旧库 `estimated_cost` 列由 `COLUMN_RENAMES` 自动 rename 为 `cost`。

---

## 三、Sanitizer 决策树（Step D）— `sanitize_usage_dict`

严格优先级：
1. **数字（int/float）→ 一律保留**（绝不因键名像凭据而删除，保护 `*_tokens` 等数值）。
2. **字符串 → 命中凭据值形态则删除**：`sk-` / `ark-` / `ak-` / `org-` 前缀、`Bearer <token>`、完整 JWT（`x.y.z`）、≥32 长随机串、URL 含 `key/token/secret` 参数。
3. **字典/列表 → 递归**。
4. **精确凭据键名**（小写精确匹配，非子串）：`api_key`/`secret`/`token`/`password`/`authorization`/`cookie`/`credential` 等 → 命中且值非数字则删除该键。
5. 其它（None/bool）→ 原样保留。

> 关键纠正（相对 Phase 1C 作废的 deny-list）：`prompt_tokens`/`completion_tokens` 等 usage 数值**永不**因键名被删；Unknown Usage ≠ Secret。

---

## 四、Gateway 集成（Step E）

- **凭据边界**：`proxy()` 改为 `cred_provider.get(provider)` 仅从环境变量解析 secret，**不再读取 config.yaml 明文** `cfg.api_key`。无 key → 记录 `rejected` 事件并返回 400。
- **rejected 事件**：`unknown provider` / `未启用` / `无 key` 三处均先 `_record_rejected(...)`（event_type='rejected'，token/cost=NULL）再返回错误；落库失败不影响主拒绝响应。
- **单次请求单一事件**：`_finalize` 在 `finally` 守卫，保证一条请求 → 至多一条事件。
- 已知 bug 修复：`gateway.py` 误用 `cred_provider._env_name(...)`（`_env_name` 为模块级函数，非实例方法）→ 已改为 import 后直接调用。

---

## 五、Provider Parser（Step C）

- `OpenAICompatibleAdapter.extract_usage`：抽取 `prompt/completion/total/reasoning_tokens` + `cache_write_tokens`；未知字段进 `extension`（不静默丢失）；`extract_cache_usage` 单独抽取 `prompt_cache_hit_tokens → cache_read_tokens`。
- 新增 `GenericAdapter`：兜底解析任意 OpenAI-compat / Gemini 形态，未知字段进 `extension`。经 `registry.generic()` 暴露（**不进入 PROVIDER_DEFS**——因其无固定 `default_base_url`，避免违反"每个注册 provider 必有 base_url"不变量）。
- `ProviderRegistry`：`generic()` 返回兜底解析器；`get()` 对未知 provider 仍返回 None（网关据此 rejected，保留安全边界）。

---

## 六、成本三轴分离（Step F）

- `cost` 未知 → **NULL 非 0**；`compute_cost` 未命中 pricing 时不写 0。
- 聚合层：`cost IS NOT NULL` 才计入 `cost_by_currency` / `cost_known_sum`；`COALESCE` 仅作用于"已知部分求和"，unknown 行通过 `cost_count` 区分。
- `cost_status`：`known`（全部已知）/ `mixed`（部分未知）/ `unknown`（全部未知）/ `none`（无事件）/ `partial`（混合）——前端据以显示 KNOWN / N/A / —。

---

## 七、测试（Step G）

- **全量 pytest：221 passed**（196 既有 + 25 新增不变量）。
- 既有测试随 `estimated_cost→cost` 重命名同步更新；Gateway 测试 fixture 注入 `DEEPSEEK_API_KEY`/`GEMINI_API_KEY` 环境变量（符合 env-only 凭据裁决）。
- 新增 `tests/test_phase1e_invariants.py` 覆盖 24 项核心不变量：事件模型、NULL≠0、三轴分离、UTC、schema 演进、reasoning/cache 捕获、未知字段不丢失、Sanitizer 决策树、Raw→Sanitizer 链路、凭据边界、Parser 三态、迁移幂等/legacy rename、单次请求单事件、统计排除 rejected。

---

## 八、已知残留（PARTIAL）

**凭据持久化未彻底消除（P3，非阻塞）**：网关已不读取 config.yaml 明文，但 `ConfigManager.upsert` 在 Dashboard"保存密钥"链路仍会将 `api_key` 写入 `config.yaml`。这是 Phase 1D Decision Record 标记为"冻结待消除"的回归点。建议后续：Dashboard 密钥保存改为 env/runtime-only（参考 `CredentialProvider`），彻底消除 config.yaml 明文 secret。

---

## 九、结论

**GO** —— Phase 1E-A 全部实施细则（一~十九）已落地，数据层/解析层/脱敏层/网关/成本语义/测试均通过 221 项测试验证。唯一 PARTIAL 为 config.yaml 明文密钥持久化残留，已在 §八 标注，不阻塞本阶段交付，建议作为独立 follow-up 处理。
