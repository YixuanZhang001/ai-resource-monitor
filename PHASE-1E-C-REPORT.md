# PHASE-1E-C IMPLEMENTATION REPORT — Credential Boundary Closure

- Canonical repo: `D:\AI\projects\ai-resource-monitor`
- Baseline: commit `52c771f` (Phase 1E-A 收尾)
- 上一阶段：Phase 1E-A — Canonical Ledger Core
- 紧前阶段：Phase 1E-B — READ-ONLY BASELINE AUDIT（证明 Credential Persistence 为唯一 P0 BLOCKER）
- 本阶段目标：**仅闭合 Credential Boundary，使 Monitor 不再持久化真实 API Secret**。
  不做 Billing / GenericAdapter 接线 / 新 Provider / Dashboard 重构 / Analytics 扩展 / event_type error / 性能优化 / 大规模重构。
- 数据库：未做任何 schema / migration / `data/monitor.db` 修改（符合第十三节约束）。

---

## 1. 修改文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `monitor/config.py` | 修改 | H1/H2 核心修复：load 丢弃 legacy secret、save 强制剥离 secret 键、upsert 忽略 api_key/api_keys、public_view 改由 env 实时判定、新增 `cleanup_config_secrets` 幂等清理 |
| `monitor/gateway.py` | 修改 | H3 修复：runtime key 注入临时副本，绝不修改共享 config 对象 |
| `monitor/main.py` | 修改 | `upsert_provider` 返回 env 实时 key 可用性，不反映已落盘 secret |
| `tests/test_credential_boundary.py` | 新增 | 10 项 Credential Boundary regression suite（覆盖 H1–H3 + 持久化/回退/清理/保留） |
| `PHASE-1E-C-REPORT.md` | 新增 | 本报告 |

---

## 2. 每个修改的原因

### `monitor/config.py`
- **`load()` 丢弃 legacy secret**：`ProviderConfig` 构造时显式不传 `api_key`/`api_keys`，也不再塞入 `extra`。历史明文 key 即使残留在磁盘，加载后也不进入内存对象 → 切断 `config.yaml → ConfigManager 持有 secret` 的第一条路径。
- **`save()` 强制剥离 secret 键**（`_SECRET_KEYS` + `_strip_secret`）：最后一道防线（Principle 4）。序列化 `providers` 字典前，任何名为 `api_key/api_keys/secret/...` 的键都被剔除。即使未来某处因兼容性把真实 secret 塞进内存 `api_keys`，`save()` 也绝不写出。
- **`upsert()` 忽略 `api_key`/`api_keys`**：Dashboard 经 `PUT /api/providers/{name}` 传入的 key 仅作为兼容接口参数，被显式忽略（不赋值、不持久化）。真实 secret 仅由 `CredentialProvider` 从环境变量解析。
- **`public_view()` 改由 `CredentialProvider` 实时判定 `has_key`/`key_count`**：前端可用性不再从磁盘 secret 派生，彻底消除"读盘 secret 推断可用性"的隐含读取。
- **`cleanup_config_secrets(path)`**：幂等一次性清理，仅 `del` `api_key`/`api_keys` 键、不读不打印值。用于消除历史明文凭据（H1）。可重复运行，结果稳定。

### `monitor/gateway.py`
- **H3 修复**：`runtime_cfg = copy.copy(cfg)`；`runtime_cfg.api_keys = [key]`。env key 只注入**本次请求用的临时副本**，`upstream_url/headers/body` 一律使用 `runtime_cfg`。共享的 `config_mgr.providers[provider]` 对象永不被修改 → 后续任意 `save()` 都不会把 runtime secret 带入磁盘。

### `monitor/main.py`
- **`upsert_provider` 返回 env 可用性**：`has_key`/`key_count` 由 `CredentialProvider().available(name)` 实时判定，响应体不含任何已落盘 secret。`ProviderIn` 仍接受 `api_key/api_keys` 字段（UI 兼容），但传递给 `upsert` 后被忽略。

---

## 3. H1 / H2 / H3 分别如何解决

| 泄漏点 | 描述 | 解决 |
|---|---|---|
| **H1** | `data/config.yaml` 已存在历史明文 API keys（openai/deepseek/minimax/doubao/qwen/kimi） | `cleanup_config_secrets("data/config.yaml")` 幂等删除 `api_key`/`api_keys` 键（实测运行一次删除、二次运行 `removed=False` 幂等）。后续 `load()` 也不再加载它们。 |
| **H2** | Dashboard `PUT /api/providers/{name}` → `upsert` → `save()` 把用户 key 落盘 | `upsert()` 忽略 `api_key/api_keys`；`save()` 经 `_strip_secret` 兜底剥离。实测 PUT 带 `sk-test-xxx` 后磁盘无该值。 |
| **H3（潜伏）** | gateway 把真实 env key 写入共享 `cfg.api_keys` → 后续任意 `save()` 泄露 | 改为 `copy.copy(cfg)` 临时副本注入，共享对象零修改。实测 gateway 用 env key 成功后，`config_mgr.providers[provider].api_keys == []` 且 `save()` 后磁盘无该值。 |

---

## 4. Credential data-flow（修复后）

```
Dashboard（用户输入 api_key —— 仅兼容接口，被忽略）
   → PUT /api/providers/{name}            main.py upsert_provider
   → ConfigManager.upsert(api_key=...)    ← 显式忽略，不持久化
   → ConfigManager.save()                 ← _strip_secret 兜底剥离 secret 键
   → data/config.yaml                     ★ 无真实 secret（H1 已清理，H2 不再写入）

CredentialProvider（env-only，唯一 runtime secret source）
   → os.environ[{PROVIDER}_API_KEY]       credential.py:35
   → gateway: key = cred_provider.get(provider)   gateway.py:147  ← 不读 config
   → runtime_cfg = copy.copy(cfg)         gateway.py:160  ← 临时副本
   → runtime_cfg.api_keys = [key]         gateway.py:161  ← 不污染共享对象
   → adapter.upstream_headers(runtime_cfg, ...)   openai_compat.py:41
   → upstream                            ★ 共享 config_mgr.providers[provider] 始终无 secret
```

**关键结论**：Gateway 请求路径中，env secret 经 `CredentialProvider` → `runtime_cfg` 副本 → upstream，**中间任何一步都不经过 `ConfigManager.save()`**，且共享 config 对象无 secret。即使 gateway 之后再次运行、Dashboard 更新任意配置触发 `save()`，磁盘也不会出现该 secret。

---

## 5. config.yaml 修复前/后的结构差异

> 不展示真实 secret；以下为结构示意。

**修复前（H1 残留）**
```yaml
providers:
  openai:
    enabled: true
    base_url: https://api.openai.com/v1
    api_key: "sk-..."        # ← 明文 secret（已删除）
  deepseek:
    enabled: true
    base_url: ...
    api_keys: ["sk-..."]     # ← 明文 secret（已删除）
  # ... 其余 4 个 provider 同理
```

**修复后（H1 清理 + H2 防御）**
```yaml
providers:
  openai:
    enabled: true
    base_url: https://api.openai.com/v1
    # api_key / api_keys 键已不存在
  deepseek:
    enabled: true
    base_url: ...
  # ... 其余 provider 同理，仅保留 enabled / base_url 等非 secret 字段
```

差异要点：
- `api_key` / `api_keys` 键**从磁盘彻底消失**（已删除，且 save 会持续剥离任何重新出现的企图）。
- 真实 secret **仅**以环境变量形式存在（`OPENAI_API_KEY` 等），由 `CredentialProvider` 解析。
- 非 secret 配置（provider / enabled / base_url / test_model / extra / resources / sources / projects / scheduler）完整保留。

---

## 6. 新增测试

`tests/test_credential_boundary.py`（10 项，全部 passed）：

1. `test_save_never_writes_real_api_key` — save() 永不写出真实 api_key
2. `test_upsert_with_api_key_then_save_no_secret` — upsert(api_key=...) 后 save() 无真实 key
3. `test_gateway_env_key_does_not_pollute_shared_config` — **H3 核心**：gateway 用 env key 后，共享 `config_mgr.providers[provider].api_keys == []`，save() 后磁盘无该 key
4. `test_gateway_request_uses_env_credential` — gateway 成功用 env credential 发起请求
5. `test_dashboard_put_provider_api_key_not_persisted` — Dashboard PUT 带 api_key 不写入 config.yaml
6. `test_historical_config_cleanup_removes_secrets` — 历史 `sk-`/`ark-`/`Bearer` 均被 `cleanup_config_secrets` 移除
7. `test_normal_config_fields_preserved` — 非 secret 字段（enabled/base_url/test_model/extra）保留
8. `test_no_env_credential_no_config_fallback` — 无 env credential 时**不**回退到 config.yaml 旧 secret
9. `test_public_view_derives_has_key_from_env` — `public_view` 的 `has_key` 来自 env，非磁盘
10. `test_cleanup_idempotent` — 清理幂等（二次运行无变化）

---

## 7. 全量测试结果

```
pytest -q  →  231 passed（原 221 + 新增 10），0 failed，0 error
```
- 新增 Credential Boundary suite：10 passed
- 既有 sanitizer / gateway / config / resource / analytics / invariant 测试：221 passed（无回归）

---

## 8. Secret Audit 结果（第十四节 Gate 对应）

**A. config.yaml 值级扫描**
- `sk-` / `ark-` / `Bearer ` / `authorization` / `password` / `secret` / `access_token` / `client_secret` 等模式：**命中 0 处**。

**B. 代码侧 grep（仅文件:行，供审计）**
- 所有 `api_key`/`api_keys`/`credential`/`secret` 引用均为：sanitizer 合法清洗键、env-only `CredentialProvider`、`_SECRET_KEYS` 剥离集合、`_secret_like` 对 `credential_id` 的防护（拒绝疑似 secret 的引用）、安全注释。**无任何代码路径把真实 secret 写入磁盘或日志。**

**C. Gateway data-flow**
- env secret → `CredentialProvider` → `runtime_cfg`（临时副本）→ upstream；**不经过 `ConfigManager` → `save()`**。

**D. Dashboard**
- `PUT /api/providers/{name}` 带 `api_key` 不会持久化（upsert 忽略 + save 剥离）。响应体只回 env 可用性。

**E. Git**
- `git status` 仅 `M monitor/{config,gateway,main}.py` + `?? tests/test_credential_boundary.py` + `?? PHASE-1E-A-REPORT.md`（上一阶段遗留未跟踪文件）。
- `data/config.yaml` 与 `data/monitor.db` 均被 `.gitignore` 忽略，**未进版本库、未出现在 diff**。
- 无 DB 修改、无临时 secret 文件、无测试输出文件。

---

## 9. git diff / commit

**改动范围（git diff --stat）**
```
monitor/config.py  | 85 +++++++++++++++++++++++++++++++---------
monitor/gateway.py | 11 +++++--
monitor/main.py    |  6 +++-
3 files changed, 77 insertions(+), 25 deletions(-)
```

**Baseline tag**：`baseline-before-phase1ec`（打在 `52c771f` 上，提交禁 force reset）。

**Commit**：`Phase 1E-C: Credential Boundary Closure`

---

## 10. 是否 GO

### 三个核心验收问题

1. **ConfigManager 是否还可能持有真实 credential？**
   **否。** `load()` 主动丢弃 legacy `api_key`/`api_keys` 且不入 `extra`；`upsert()` 忽略传入的 key；内存中 `ProviderConfig.api_keys` 恒为 `[]`（除非 Adapter 直接构造测试对象，但那不进持久化层）。`public_view` 也不再从磁盘 secret 派生。

2. **任何 save() 是否可能写出真实 credential？**
   **否。** `save()` 序列化前经 `_strip_secret` 强制剔除 `_SECRET_KEYS` 集合所有键；即使内存对象因兼容性误含 secret，写入磁盘时也会被剥离。这是 Principle 4 的最后一道防线，已用 `test_save_never_writes_real_api_key` / `test_upsert_with_api_key_then_save_no_secret` 证明。

3. **Gateway 是否可能通过共享 config object 把 runtime credential 带入 persistence？**
   **否。** H3 已修复：`runtime_cfg = copy.copy(cfg)`，env key 只注入临时副本，`config_mgr.providers[provider]` 共享对象零修改。已用 `test_gateway_env_key_does_not_pollute_shared_config` 端到端证明——gateway 用 env key 成功后，共享对象 `api_keys == []` 且 `save()` 后磁盘无该 key。

### 结论

**Ledger：GO（Phase 1E-A 已验证）**
**Credential Boundary：GO（本阶段闭合）**
**Overall Phase 1E-C：GO** ✅

> 最终验收问题："即使 Gateway 今天使用了真实环境变量 API Key，之后任意 `ConfigManager.save()`、Dashboard 更新配置、Gateway 再次运行，都不能把这个 Secret 写入磁盘。" —— **已证明 YES**（回归测试 + 值级审计双重覆盖）。

---

## 遗留（非本阶段范围，不阻塞 GO）

- **Billing 轴**（`billing_status`/`list_cost`/`pricing_snapshot_id` 列存在但 pipeline 未填充）：属 P1，本阶段按纪律不做。
- **`event_type='error'`** 声明但未发射：属 P1，本阶段按纪律不做。
- **`GenericAdapter` 无调用方**（死代码，M3）：按纪律不接线、不删除。
- **Phase 1E-B 的 M4**（`occurred_at` 历史行 NULL，新行正常）：非安全项，不阻塞。
