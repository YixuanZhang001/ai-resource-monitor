"""SQLite 事件存储。本地优先，单文件数据库。"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .events import AIRequestEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    endpoint TEXT,
    source TEXT,
    project TEXT,
    client TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_hit INTEGER,
    latency_ms REAL,
    status_code INTEGER,
    cost REAL,
    currency TEXT,
    error TEXT,
    trace_id TEXT,
    parent_id TEXT,
    collector TEXT DEFAULT 'gateway',
    event_type TEXT DEFAULT 'llm_call',
    execution_id TEXT,
    task_id TEXT,
    resource_id TEXT,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_provider ON events(provider, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_model ON events(model);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status_code);

-- Resource Observation 快照（与 events 分离：Usage 记录使用，State 记录状态）
CREATE TABLE IF NOT EXISTS resource_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL,
    observed_at REAL NOT NULL,
    status TEXT NOT NULL,               -- no_observation|known|unavailable|error
    balance REAL,
    quota REAL,
    remaining REAL,
    reset_at REAL,
    expires_at REAL,
    source TEXT,
    error TEXT,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_states_res_ts
    ON resource_states(resource_id, observed_at DESC);
"""

COLUMNS = [
    "request_id", "timestamp", "provider", "model", "endpoint", "source",
    "project", "client", "input_tokens", "output_tokens", "total_tokens",
    "reasoning_tokens", "cache_read_tokens", "cache_write_tokens", "cache_hit",
    "latency_ms", "status_code", "cost", "currency", "billing_status",
    "list_cost", "pricing_snapshot_id", "error", "trace_id",
    "parent_id", "collector", "event_type", "execution_id", "task_id",
    "resource_id", "occurred_at", "schema_version", "usage_extension", "metadata",
]

# 增量列迁移（幂等）：旧库补齐新列，不修改既有字段与数据
COLUMN_MIGRATIONS = [
    ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"),
    ("cache_hit", "INTEGER"),
    ("client", "TEXT"),
    ("collector", "TEXT DEFAULT 'gateway'"),
    ("event_type", "TEXT DEFAULT 'llm_call'"),
    ("execution_id", "TEXT"),
    ("task_id", "TEXT"),
    ("resource_id", "TEXT"),
    ("metadata", "TEXT"),
    ("reasoning_tokens", "INTEGER"),
    ("occurred_at", "TEXT"),
    ("schema_version", "INTEGER DEFAULT 2"),
    ("billing_status", "TEXT"),
    ("list_cost", "REAL"),
    ("pricing_snapshot_id", "TEXT"),
    ("usage_extension", "TEXT"),
]

# 列重命名（幂等）：旧名存在且新名不存在才 RENAME
COLUMN_RENAMES = [("parent_span_id", "parent_id"), ("estimated_cost", "cost")]


def ensure_columns(conn: sqlite3.Connection) -> list[str]:
    """为 events 表补齐缺失的新列 + 重命名（PRAGMA 检查，幂等）。"""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    added = []
    for col, typ in COLUMN_MIGRATIONS:
        if col not in existing:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typ}")
            added.append(col)
    for old, new in COLUMN_RENAMES:
        if old in existing and new not in existing:
            conn.execute(f"ALTER TABLE events RENAME COLUMN {old} TO {new}")
            added.append(f"rename:{old}->{new}")
    return added


class EventStore:
    """SQLite 事件存储。

    安全约束（P6 审计修复）：**构造期绝不执行 ALTER TABLE 迁移**。
    SCHEMA 全部为 CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，
    对既有库是 no-op（已实证：生产库 SHA 不变）；而增量列迁移会改写 schema，
    必须由显式动作触发（应用启动 lifespan / 首次写入），不能由「import 一次
    monitor.main」隐式执行 —— 否则只读审计也会污染生产库。
    """

    def __init__(self, db_path: str | Path, auto_migrate: bool = False):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._migrated = False
        if auto_migrate:
            self.migrate()

    def migrate(self) -> list[str]:
        """显式增量列迁移（幂等）。返回本次新增/重命名的列。

        调用点：应用启动（lifespan）、首次写入（安全网）。
        """
        with self._lock, self._conn:
            added = ensure_columns(self._conn)
            self._conn.commit()
        self._migrated = True
        return added

    def insert(self, event: AIRequestEvent) -> None:
        if not self._migrated:
            self.migrate()
        d = event.to_dict()
        values = [d.get(c) for c in COLUMNS]
        sql = f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})"
        with self._lock, self._conn:
            self._conn.execute(sql, values)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 查询（供统计 API 使用） ----------

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        # metadata 列存 JSON text，读回反序列化为 dict（失败保留原文）
        for r in rows:
            m = r.get("metadata")
            if isinstance(m, str) and m:
                try:
                    r["metadata"] = json.loads(m)
                except (ValueError, TypeError):
                    pass
        return rows

    # ---------- Resource Observation（独立于 events） ----------

    def insert_observation(self, obs) -> None:
        """写入一条状态快照。obs: ResourceObservation（或带同名字段的 dict）。"""
        with self._lock:
            self._conn.execute(
                """INSERT INTO resource_states
                   (resource_id, observed_at, status, balance, quota, remaining,
                    reset_at, expires_at, source, error, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (obs.resource_id, obs.observed_at, obs.status, obs.balance,
                 obs.quota, obs.remaining, obs.reset_at, obs.expires_at,
                 obs.source, obs.error,
                 json.dumps(obs.metadata) if obs.metadata else None))
            self._conn.commit()

    def latest_observation(self, resource_id: str) -> Optional[dict]:
        rows = self._query(
            """SELECT resource_id, observed_at, status, balance, quota, remaining,
                      reset_at, expires_at, source, error, metadata
               FROM resource_states WHERE resource_id = ?
               ORDER BY observed_at DESC LIMIT 1""",
            (resource_id,))
        return rows[0] if rows else None

    def observations_for(self, resource_id: str,
                         limit: int = 20) -> list[dict]:
        """历史快照（保留；删除 Resource 不删除）。"""
        return self._query(
            """SELECT resource_id, observed_at, status, balance, quota, remaining,
                      reset_at, expires_at, source, error, metadata
               FROM resource_states WHERE resource_id = ?
               ORDER BY observed_at DESC LIMIT ?""",
            (resource_id, limit))

    def all_latest_observations(self) -> list[dict]:
        """每个 resource_id 的最新一条快照。"""
        return self._query(
            """SELECT s.resource_id, s.observed_at, s.status, s.balance,
                      s.quota, s.remaining, s.reset_at, s.expires_at,
                      s.source, s.error, s.metadata
               FROM resource_states s
               JOIN (SELECT resource_id, MAX(observed_at) AS mx
                     FROM resource_states GROUP BY resource_id) m
                 ON s.resource_id = m.resource_id AND s.observed_at = m.mx""")

    def recent_events(self, limit: int = 100, provider: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM events"
        params: tuple = ()
        if provider:
            sql += " WHERE provider = ?"
            params = (provider,)
        sql += " ORDER BY id DESC LIMIT ?"
        return self._query(sql, params + (limit,))

    def overview(self, since: Optional[float] = None) -> dict:
        where, params = self._since(since)
        row = self._query(
            f"""SELECT COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       COALESCE(SUM(CASE WHEN error IS NOT NULL OR status_code >= 400 THEN 1 ELSE 0 END), 0) AS errors
                FROM events{where}""",
            params,
        )[0]
        cost_where = f"{where} AND cost IS NOT NULL" if where else " WHERE cost IS NOT NULL"
        costs = self._query(
            f"""SELECT currency, ROUND(SUM(cost), 6) AS cost
                FROM events{cost_where}
                GROUP BY currency""",
            params,
        )
        row["cost_by_currency"] = {c["currency"]: c["cost"] for c in costs if c["currency"]}
        row["error_rate"] = (row["errors"] / row["requests"]) if row["requests"] else 0.0
        return row

    def by_provider(self, since: Optional[float] = None) -> list[dict]:
        where, params = self._since(since)
        return self._query(
            f"""SELECT provider,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       ROUND(COALESCE(AVG(latency_ms), 0), 1) AS avg_latency_ms,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400 THEN 1 ELSE 0 END) AS errors
                FROM events{where}
                GROUP BY provider ORDER BY requests DESC""",
            params,
        )

    def cost_by_provider(self, since: Optional[float] = None) -> list[dict]:
        where, params = self._since(since)
        return self._query(
            f"""SELECT provider, currency, ROUND(SUM(cost), 6) AS cost
                FROM events{where + ' AND' if where else ' WHERE'} cost IS NOT NULL
                GROUP BY provider, currency ORDER BY cost DESC""",
            params,
        )

    def by_model(self, since: Optional[float] = None) -> list[dict]:
        where, params = self._since(since)
        return self._query(
            f"""SELECT provider, model,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       currency,
                       ROUND(SUM(cost), 6) AS cost
                FROM events{where}
                GROUP BY provider, model, currency
                ORDER BY requests DESC""",
            params,
        )

    def timeseries(self, days: int = 14) -> list[dict]:
        since = time.time() - days * 86400
        return self._query(
            """SELECT date(timestamp, 'unixepoch', 'localtime') AS day,
                      COUNT(*) AS requests,
                      COALESCE(SUM(total_tokens), 0) AS tokens
               FROM events WHERE timestamp >= ? AND event_type = 'llm_call'
               GROUP BY day ORDER BY day""",
            (since,),
        )

    @staticmethod
    def _since(since: Optional[float]) -> tuple[str, tuple]:
        # 仅统计成功调用（event_type='llm_call'）；rejected/error 事件不进入 Usage 统计
        if since is None:
            return " WHERE event_type = 'llm_call'", ()
        return " WHERE timestamp >= ? AND event_type = 'llm_call'", (since,)

    # ---------- 第二阶段：Analytics（全部 SQL 层聚合） ----------

    RANGES = {"today": 24, "7d": 7 * 24, "30d": 30 * 24, "all": None}
    UNKNOWN = "Unknown"
    # group_by 白名单（防 SQL 注入）
    DIM_COLUMNS = {
        "provider": "provider",
        "model": "model",
        "source": "COALESCE(source, 'Unknown')",
        "project": "COALESCE(project, 'Unknown')",
        "client": "COALESCE(client, 'Unknown')",
        "resource_id": "COALESCE(resource_id, '')",
    }

    @classmethod
    def parse_range(cls, range_name: Optional[str]) -> Optional[float]:
        """range=today|7d|30d|all → since 时间戳；未知值视为 all。"""
        hours = cls.RANGES.get((range_name or "").lower())
        return (time.time() - hours * 3600) if hours is not None else None

    @staticmethod
    def _dim_expr(dim: str) -> str:
        if dim not in EventStore.DIM_COLUMNS:
            raise ValueError(f"unsupported dimension: {dim}")
        return EventStore.DIM_COLUMNS[dim]

    def analytics_overview(self, since: Optional[float] = None) -> dict:
        where, params = self._since(since)
        # 历史用量聚合（additive 字段；NULL token/cost 不参与 SUM，SUM 为 0 但
        # 不表示「无未知」——cost 仍走 cost_by_currency，未知成本绝不显示为 0）。
        row = self._query(
            f"""SELECT COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       SUM(CASE WHEN cache_hit IS NOT NULL THEN 1 ELSE 0 END) AS cache_observable,
                       SUM(CASE WHEN cache_hit = 1 THEN 1 ELSE 0 END) AS cache_hit_count,
                       -- 仅统计「可观测 cache」请求的 input_tokens：
                       -- cache_read_tokens ⊆ input_tokens，分母必须与分子同口径，
                       -- 否则 ratio 会被未上报 cache 字段的请求系统性稀释。
                       COALESCE(SUM(CASE WHEN cache_hit IS NOT NULL
                                         THEN input_tokens ELSE 0 END), 0)
                           AS cache_observable_input_tokens,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors
                FROM events{where}""",
            params,
        )[0]
        cost_where = f"{where} AND cost IS NOT NULL" if where \
            else " WHERE cost IS NOT NULL"
        costs = self._query(
            f"""SELECT currency, ROUND(SUM(cost), 6) AS cost,
                       COALESCE(SUM(total_tokens), 0) AS tokens
                FROM events{cost_where} GROUP BY currency""",
            params,
        )
        row["cost_by_currency"] = {c["currency"]: c["cost"]
                                   for c in costs if c["currency"]}
        # 按币种 token（用于面板算 cost/1M；多币种分别计价，绝不跨币种汇总）
        row["cost_tokens_by_currency"] = {c["currency"]: c["tokens"]
                                          for c in costs if c["currency"]}
        row["error_rate"] = (row["errors"] / row["requests"]) \
            if row["requests"] else 0.0
        # 缓存指标（绝不伪造；口径必须自洽）：
        #   cache_coverage   = 可观测请求数 / 总请求数   ← 「这些数字覆盖了多少请求」
        #   cache_hit_rate   = 命中请求数 / 可观测请求数（请求级）
        #   cache_read_ratio = cache 命中 token / 可观测请求的 input token（token 级）
        # 修因（P6 审计）：旧实现分母用「全部请求的 input_tokens」，分子只来自
        # 上报了 cache 字段的请求，导致 ratio 被系统性低估。现分母与分子同口径。
        requests = row.get("requests") or 0
        cache_obs = row.get("cache_observable") or 0
        cache_hit = row.get("cache_hit_count") or 0
        obs_in_t = row.get("cache_observable_input_tokens") or 0
        cr = row.get("cache_read_tokens")
        row["cache_coverage"] = (cache_obs / requests) if requests else None
        row["cache_hit_rate"] = (cache_hit / cache_obs) if cache_obs else None
        row["cache_read_ratio"] = (cr / obs_in_t) if (cr and obs_in_t) else None
        row["cache_data_feasible"] = bool(cache_obs)
        return row

    def analytics_cost(self, dim: str, since: Optional[float] = None) -> list[dict]:
        expr = self._dim_expr(dim)
        where, params = self._since(since)
        return self._query(
            f"""SELECT {expr} AS name, currency,
                       ROUND(SUM(cost), 6) AS cost,
                       COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM events{where + ' AND' if where else ' WHERE'}
                     cost IS NOT NULL
                GROUP BY {expr}, currency
                ORDER BY cost DESC""",
            params,
        )

    def analytics_tokens(self, dim: str, since: Optional[float] = None) -> list[dict]:
        expr = self._dim_expr(dim)
        where, params = self._since(since)
        return self._query(
            f"""SELECT {expr} AS name,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
                FROM events{where}
                GROUP BY {expr}
                ORDER BY total_tokens DESC""",
            params,
        )

    def tokens_timeseries(self, days: int = 14) -> list[dict]:
        since = time.time() - days * 86400
        return self._query(
            """SELECT date(timestamp, 'unixepoch', 'localtime') AS day,
                      COUNT(*) AS requests,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(total_tokens), 0) AS total_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
               FROM events WHERE timestamp >= ? AND event_type = 'llm_call'
               GROUP BY day ORDER BY day""",
            (since,),
        )

    def resource_timeseries(self, resource_id: Optional[str] = None,
                            days: int = 30) -> list[dict]:
        """单 Resource（或 NULL 未归因桶）每日时间序列。

        铁律（Phase 4 Step 1 范围）：
        - 仅聚合 events，不新增表、不改 schema、不回填历史。
        - 仅统计 event_type='llm_call'（与全局 timeseries 一致）。
        - resource_id 严格等于给定值；resource_id IS NULL 的事件绝不混入
          （不猜测资源）。NULL 桶由 resource_id=None 显式查询，保持独立。
        - cost 仅在 cost IS NOT NULL 时计入；按 currency 分别聚合，
          绝不跨货币求和（多货币分别计价）。
        - days 滑动窗口：since = now - days*86400，含窗口起始日（timestamp >= since）。
        """
        if days is None or days <= 0:
            days = 30
        since = time.time() - days * 86400
        where, _ = self._since(since)  # " WHERE timestamp >= ? AND event_type='llm_call'"
        if resource_id is None:
            rid_cond = " AND resource_id IS NULL"
            rid_params: tuple = ()
        else:
            rid_cond = " AND resource_id = ?"
            rid_params = (resource_id,)
        params = (since,) + rid_params
        rows = self._query(
            f"""SELECT date(timestamp, 'unixepoch', 'localtime') AS day,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
                FROM events{where}{rid_cond}
                GROUP BY day ORDER BY day""",
            params)
        # cost 仅 cost IS NOT NULL 的事件，按 (day, currency) 分别聚合
        cost_rows = self._query(
            f"""SELECT date(timestamp, 'unixepoch', 'localtime') AS day,
                       currency,
                       ROUND(SUM(cost), 6) AS cost
                FROM events{where}{rid_cond} AND cost IS NOT NULL
                GROUP BY day, currency""",
            params)
        cost_by_day: dict = {}
        for c in cost_rows:
            if c["currency"]:
                cost_by_day.setdefault(c["day"], {})[c["currency"]] = c["cost"]
        out = []
        for r in rows:
            day = r["day"]
            cbc = cost_by_day.get(day)  # dict|None
            # 便利标量 cost：仅当该日恰好单一货币时给出；多货币/无 cost -> None（不求和）
            cost = (next(iter(cbc.values())) if len(cbc) == 1 else None) \
                if cbc else None
            out.append({
                "day": day,
                "requests": r["requests"],
                "input_tokens": r["input_tokens"],
                "output_tokens": r["output_tokens"],
                "total_tokens": r["total_tokens"],
                "errors": r["errors"],
                "avg_latency_ms": round(r["avg_latency_ms"], 1)
                                  if r["avg_latency_ms"] is not None else None,
                "cost_by_currency": cbc,
                "cost": cost,
            })
        return out

    def analytics_performance(self, since: Optional[float] = None) -> dict:
        """avg/p50/p95/error_rate/request_count；数据不足时百分位返回 None（不伪造）。"""
        where, params = self._since(since)
        agg = self._query(
            f"""SELECT COUNT(*) AS request_count,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors
                FROM events{where}""",
            params,
        )[0]
        count = agg["request_count"]
        out = {"request_count": count,
               "avg_latency_ms": round(agg["avg_latency_ms"], 1) if count else None,
               "p50_latency_ms": self._percentile("latency_ms", 0.5, where, params),
               "p95_latency_ms": self._percentile("latency_ms", 0.95, where, params),
               "error_rate": (agg["errors"] / count) if count else None}
        return out

    def performance_by_model(self, since: Optional[float] = None) -> list[dict]:
        where, params = self._since(since)
        return self._query(
            f"""SELECT provider, model,
                       COUNT(*) AS requests,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors
                FROM events{where}
                GROUP BY provider, model
                ORDER BY requests DESC""",
            params,
        )

    def _percentile(self, col: str, q: float, where: str,
                    params: tuple) -> Optional[float]:
        """SQL 层分位数（nearest-rank）：ceil(n*q) 对应行，不把全量数据载入 Python。"""
        row = self._query(
            f"SELECT COUNT(*) AS n FROM events{where}", params)[0]
        n = row["n"]
        if n == 0:
            return None
        import math
        offset = max(0, math.ceil(n * q) - 1)
        r = self._query(
            f"SELECT {col} AS v FROM events{where} ORDER BY {col} "
            f"LIMIT 1 OFFSET ?", params + (offset,))
        return round(r[0]["v"], 1) if r and r[0]["v"] is not None else None

    def query_requests(self, page: int = 1, page_size: int = 20,
                       provider: Optional[str] = None,
                       model: Optional[str] = None,
                       source: Optional[str] = None,
                       project: Optional[str] = None,
                       client: Optional[str] = None,
                       status: Optional[int] = None,
                       since: Optional[float] = None) -> dict:
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        conds, params = [], []
        if since is not None:
            conds.append("timestamp >= ?")
            params.append(since)
        for col, val in (("provider", provider), ("model", model),
                         ("source", source), ("project", project),
                         ("client", client)):
            if not val:
                continue
            # 保留字 __NULL__ 表示「未归因」：client 在 DB 中为 NULL，
            # 而展示层用 COALESCE(client,'Unknown') 呈现；筛选器据此精确命中 NULL，
            # 而非误匹配字面量 "Unknown"（避免 Unknown 与真实同名值混淆）。
            if col == "client" and val == "__NULL__":
                conds.append("client IS NULL")
            else:
                conds.append(f"{col} = ?")
                params.append(val)
        if status is not None:
            conds.append("status_code = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        total = self._query(f"SELECT COUNT(*) AS n FROM events{where}",
                            tuple(params))[0]["n"]
        rows = self._query(
            f"""SELECT request_id, timestamp, provider,
                       COALESCE(model, '?') AS model,
                       COALESCE(source, 'Unknown') AS source,
                       COALESCE(project, 'Unknown') AS project,
                       COALESCE(client, 'Unknown') AS client,
                       input_tokens, output_tokens, total_tokens,
                       cache_read_tokens, cache_write_tokens, cache_hit,
                       latency_ms, status_code, cost, currency, error
                FROM events{where}
                ORDER BY id DESC
                LIMIT ? OFFSET ?""",
            tuple(params) + (page_size, (page - 1) * page_size))
        return {"page": page, "page_size": page_size, "total": total,
                "pages": (total + page_size - 1) // page_size, "items": rows}

    def get_request(self, request_id: str) -> Optional[dict]:
        rows = self._query(
            """SELECT request_id, timestamp, provider, model, endpoint,
                      COALESCE(source, 'Unknown') AS source,
                      COALESCE(project, 'Unknown') AS project,
                      COALESCE(client, 'Unknown') AS client,
                      input_tokens, output_tokens, total_tokens,
                      cache_read_tokens, cache_write_tokens, cache_hit,
                      latency_ms, status_code, cost, currency,
                      error, trace_id, parent_id, metadata
               FROM events WHERE request_id = ? LIMIT 1""",
            (request_id,))
        return rows[0] if rows else None

    def distinct_dim_values(self, dim: str) -> list[str]:
        expr = self._dim_expr(dim)
        rows = self._query(
            f"SELECT DISTINCT {expr} AS v FROM events ORDER BY v")
        return [r["v"] for r in rows]

    # ---------- P0-3：Resource-aware Analytics ----------
    # resource_id=''（COALESCE）代表 unattributed（NULL），不伪造资源

    def resource_usage_by_resource(self, since: Optional[float] = None) -> list[dict]:
        """按 resource 聚合 Usage。

        修因（P6 审计）：旧实现用 COALESCE(SUM(cost),0) 把**不同币种金额直接相加**
        （真实数据已发生：CNY 0.002086 + USD 0.000056 = 0.002142，且被标成 ¥）。
        现同时给出 cost_by_currency，并把「单一币种」判定交给上层：
        mixed_currency=True 时 cost 不可作为单一数字展示（不伪造汇率）。
        """
        where, params = self._since(since)
        rows = self._query(
            f"""SELECT COALESCE(resource_id, '') AS resource_id,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       MAX(timestamp) AS last_used_at,
                       SUM(CASE WHEN cost IS NOT NULL
                                THEN 1 ELSE 0 END) AS cost_count,
                       COALESCE(SUM(cost), 0) AS cost_known_sum
                FROM events{where}
                GROUP BY resource_id""",
            params,
        )
        cost_where = f"{where} AND cost IS NOT NULL" if where \
            else " WHERE cost IS NOT NULL"
        costs = self._query(
            f"""SELECT COALESCE(resource_id, '') AS resource_id, currency,
                       COUNT(*) AS priced_requests,
                       ROUND(SUM(cost), 6) AS cost
                FROM events{cost_where}
                GROUP BY resource_id, currency""",
            params,
        )
        by: dict = {}
        for c in costs:
            # currency 为 NULL 的金额语义不明，不参与汇总展示
            if not c["currency"]:
                continue
            by.setdefault(c["resource_id"], {})[c["currency"]] = c["cost"]
        for r in rows:
            m = by.get(r["resource_id"], {})
            r["cost_by_currency"] = m
            r["cost_currency"] = next(iter(m)) if len(m) == 1 else None
            r["mixed_currency"] = len(m) > 1
        return rows

    def model_usage_for_resource(self, resource_id: str,
                                 since: Optional[float] = None) -> list[dict]:
        where, params = self._since(since)
        cond = " AND resource_id = ?" if where else " WHERE resource_id = ?"
        return self._query(
            f"""SELECT COALESCE(model, '?') AS model,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       SUM(CASE WHEN cost IS NOT NULL
                                THEN 1 ELSE 0 END) AS cost_count,
                       COALESCE(SUM(cost), 0) AS cost_known_sum
                FROM events{where}{cond}
                GROUP BY model ORDER BY requests DESC""",
            params + (resource_id,),
        )

    def recent_for_resource(self, resource_id: str,
                            limit: int = 10) -> list[dict]:
        rows = self._query(
            """SELECT request_id, timestamp, provider, model,
                      COALESCE(source, 'Unknown') AS source,
                      input_tokens, output_tokens, total_tokens,
                      cache_read_tokens, cache_write_tokens,
                      latency_ms, status_code, cost, currency, error
               FROM events WHERE resource_id = ?
               ORDER BY id DESC LIMIT ?""",
            (resource_id, limit))
        return rows

    # ---------- Phase 3B：Efficiency Derivation Layer ----------
    # 纯 SQL 聚合原始值 + Python 计算比率/覆盖度（分母语义严格）。
    # 不新增字段、不新增表、不改 events schema。
    # 铁律：NULL cost != 0；NULL tokens 不参与比率；0 请求 -> 比率 NULL；
    #       多货币分别计价；cache 仅在观测到语义时计算；balance_delta 绝不叫 burn rate。

    @staticmethod
    def _r(v, n: int = 6):
        """None 透传；否则四舍五入（避免浮点噪声）。"""
        return None if v is None else round(v, n)

    def _efficiency_raw(self, expr: str, since: Optional[float]):
        """返回 (base_rows, cost_rows, latency_rows)。
        base_rows: 每 group 的原始计数/求和（含 NULL 透传）。
        cost_rows: 每 group 每 currency 的 priced_requests / known_cost / priced_total_tokens。
        latency_rows: 每 group 的 p50/p95（Python 分位数，无 SQLite 数学函数依赖）。"""
        where, params = self._since(since)
        base = self._query(
            f"""SELECT {expr} AS name,
                       COUNT(*) AS requests,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(CASE WHEN total_tokens IS NOT NULL
                                THEN 1 ELSE 0 END) AS tokened_requests,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(CASE WHEN cache_hit IS NOT NULL
                                THEN 1 ELSE 0 END) AS cache_observable,
                       SUM(CASE WHEN cache_hit = 1
                                THEN 1 ELSE 0 END) AS cache_hit_count,
                       -- 与 cache_read_tokens 同口径的分母（仅可观测请求）
                       SUM(CASE WHEN cache_hit IS NOT NULL
                                THEN input_tokens ELSE 0 END)
                           AS cache_observable_input_tokens,
                       AVG(latency_ms) AS avg_latency_ms
                FROM events{where}
                GROUP BY {expr}""",
            params,
        )
        cost = self._query(
            f"""SELECT {expr} AS name, currency,
                       COUNT(*) AS priced_requests,
                       ROUND(SUM(cost), 6) AS known_cost,
                       SUM(total_tokens) AS priced_total_tokens
                FROM events{where} AND cost IS NOT NULL
                GROUP BY {expr}, currency""",
            params,
        )
        # 延迟分位数：拉取 (group, latency) 后在 Python 计算（nearest-rank），
        # 避免依赖 SQLite 可选的数学函数（ceil 等在某些构建缺失）。
        lat_raw = self._query(
            f"SELECT {expr} AS name, latency_ms FROM events{where} "
            f"AND latency_ms IS NOT NULL",
            params,
        )
        from collections import defaultdict
        lat_by_group: dict = defaultdict(list)
        for r in lat_raw:
            lat_by_group[r["name"]].append(r["latency_ms"])

        def _pct(vals, q):
            if not vals:
                return None
            s = sorted(vals)
            idx = max(0, math.ceil(len(s) * q) - 1)
            return round(s[idx], 1)

        latency = [
            {"name": g,
             "p50_latency_ms": _pct(v, 0.5),
             "p95_latency_ms": _pct(v, 0.95)}
            for g, v in lat_by_group.items()
        ]
        return base, cost, latency

    @staticmethod
    def _merge_efficiency(base_rows, cost_rows, latency_rows, dim: str) -> list[dict]:
        cost_by_name: dict = {}
        for r in cost_rows:
            cost_by_name.setdefault(r["name"], []).append(r)
        lat_by_name = {r["name"]: r for r in latency_rows}
        out = []
        for b in base_rows:
            name = b["name"]
            requests = b["requests"] or 0
            errors = b["errors"] or 0
            input_t = b["input_tokens"]
            output_t = b["output_tokens"]
            total_t = b["total_tokens"]
            tokened = b["tokened_requests"] or 0
            cache_obs = b["cache_observable"] or 0
            cache_hit = b["cache_hit_count"] or 0
            cr = b["cache_read_tokens"]
            cw = b["cache_write_tokens"]

            tokens_per_request = (total_t / tokened) \
                if (total_t is not None and tokened) else None
            input_output_ratio = (input_t / output_t) \
                if (input_t and output_t) else None
            output_share = (output_t / total_t) \
                if (output_t and total_t) else None
            token_coverage = (tokened / requests) if requests else None

            cache_hit_rate = (cache_hit / cache_obs) if cache_obs else None
            # token 级缓存复用占比：cache_read_tokens / 可观测请求的 input_tokens。
            # 约定（OpenAI 兼容）：cache_read_tokens ⊆ input_tokens（cached 为 input 子集）。
            # 该关系无法从 schema 单独验证，故仅在 cache_observable>0 时给出，
            # 并随 cache_data_feasible 标记；切勿等同于请求级 cache_hit_rate。
            # 修因（P6 审计）：旧分母为「全部请求 input_tokens」，与分子口径不一致，
            # 会被未上报 cache 字段的请求系统性稀释。
            obs_input_t = b.get("cache_observable_input_tokens") or 0
            cache_read_ratio = (cr / obs_input_t) if (cr and obs_input_t) else None
            cache_coverage = (cache_obs / requests) if requests else None

            cost_block: dict = {}
            for c in cost_by_name.get(name, []):
                cur = c["currency"]
                priced = c["priced_requests"] or 0
                known = c["known_cost"]
                ptok = c["priced_total_tokens"]
                cov = (priced / requests) if requests else None
                cpr = (known / priced) if (known is not None and priced) \
                    else None
                # 计数单位：每 100 万 token（industry standard）。
                # 与 pricing_data.yaml 的 unit: per_1m_tokens 对齐（旧 cost_per_1k_tokens 已废弃）。
                cp1m = (known / (ptok / 1_000_000.0)) \
                    if (known is not None and ptok) else None
                cost_block[cur] = {
                    "known_cost": known,
                    "priced_requests": priced,
                    "cost_coverage": EventStore._r(cov, 4),
                    "cost_per_request": EventStore._r(cpr),
                    "cost_per_1m_tokens": EventStore._r(cp1m, 4),
                }

            lat = lat_by_name.get(name, {}) or {}
            row = {
                # resource_id 维度下 '' 代表 unattributed（合法状态，不伪造资源）
                "name": (None if (dim == "resource_id" and name == "")
                         else name),
                "requests": requests,
                "errors": errors,
                "error_rate": (EventStore._r(errors / requests, 4)
                               if requests else None),
                "tokens": {
                    "input": input_t, "output": output_t, "total": total_t,
                    "tokened_requests": tokened,
                    "tokens_per_request": EventStore._r(tokens_per_request),
                    "input_output_ratio": EventStore._r(input_output_ratio, 3),
                    "output_share": EventStore._r(output_share, 4),
                    "token_coverage": EventStore._r(token_coverage, 4),
                },
                "cost_by_currency": cost_block,
                "cache": {
                    "cache_read_tokens": cr,
                    "cache_write_tokens": cw,
                    "cache_observable_requests": cache_obs,
                    "cache_hit_requests": cache_hit,
                    "cache_hit_rate": EventStore._r(cache_hit_rate, 4),
                    "cache_read_ratio": EventStore._r(cache_read_ratio, 4),
                    "cache_coverage": EventStore._r(cache_coverage, 4),
                    "cache_data_feasible": cache_obs > 0,
                },
                "latency": {
                    "avg_latency_ms": (EventStore._r(b["avg_latency_ms"], 1)
                                       if b["avg_latency_ms"] is not None
                                       else None),
                    "p50_latency_ms": lat.get("p50_latency_ms"),
                    "p95_latency_ms": lat.get("p95_latency_ms"),
                },
            }
            out.append(row)
        out.sort(key=lambda r: r["requests"], reverse=True)
        return out

    def efficiency_by_dim(self, dim: str,
                         since: Optional[float] = None) -> list[dict]:
        if dim not in EventStore.DIM_COLUMNS:
            raise ValueError(f"unsupported dimension: {dim}")
        expr = EventStore.DIM_COLUMNS[dim]
        base, cost, latency = self._efficiency_raw(expr, since)
        return self._merge_efficiency(base, cost, latency, dim)

    def efficiency_overview(self, since: Optional[float] = None) -> dict:
        """全局效率概览（单一聚合组）。返回 dict（无 name 字段）。

        无事件时返回全 None/0 的空结构，绝不伪造数据（请求数 0 -> 所有比率为 None）。"""
        base, cost, latency = self._efficiency_raw("'__ALL__'", since)
        if not base:
            return {
                "requests": 0, "errors": 0, "error_rate": None,
                "tokens": {"input": None, "output": None, "total": None,
                           "tokened_requests": 0, "tokens_per_request": None,
                           "input_output_ratio": None, "output_share": None,
                           "token_coverage": None},
                "cost_by_currency": {},
                "cache": {"cache_read_tokens": None, "cache_write_tokens": None,
                          "cache_observable_requests": 0, "cache_hit_requests": 0,
                          "cache_hit_rate": None, "cache_read_ratio": None,
                          "cache_coverage": None, "cache_data_feasible": False},
                "latency": {"avg_latency_ms": None, "p50_latency_ms": None,
                            "p95_latency_ms": None},
            }
        rows = self._merge_efficiency(base, cost, latency, "overview")
        g = rows[0]
        g.pop("name", None)
        return g

    def balance_trend(self, resource_id: str,
                      limit: int = 20) -> dict:
        """余额观测趋势。返回每期 balance 与相邻已知余额差（balance_delta）。

        铁律：绝不命名为 burn rate / API 消费速度。余额变化可能来自充值/赠送/
        外部消费等，observed_balance_change 仅为观测差值，不代表 API 消耗。"""
        rows = self.observations_for(resource_id, limit)  # DESC
        asc = list(reversed(rows))
        out = []
        prev_bal = None
        for r in asc:
            bal = r.get("balance")
            delta = None
            if bal is not None and prev_bal is not None:
                delta = round(bal - prev_bal, 6)
            out.append({
                "observed_at": r.get("observed_at"),
                "status": r.get("status"),
                "balance": bal,
                "balance_delta": delta,  # 相邻两期已知余额之差；缺失其一则 None
            })
            if bal is not None:
                prev_bal = bal
        out.reverse()
        known = [(p["observed_at"], p["balance"])
                 for p in out if p["balance"] is not None]
        # 按时间升序取首/末，确保 observed_balance_change = 末 - 首（真实时间方向）
        known_asc = sorted(known, key=lambda x: x[0])
        summary = {
            "resource_id": resource_id,
            "observations": len(out),
            "known_balance_points": len(known),
            "first_balance": known_asc[0][1] if known_asc else None,
            "last_balance": known_asc[-1][1] if known_asc else None,
            "observed_balance_change": (known_asc[-1][1] - known_asc[0][1])
                if len(known_asc) >= 2 else None,
            "observed_balance_change_rate_per_hour": None,
            "note": ("observed_balance_change 仅为观测余额差值，不等同于 API 消费速度；"
                     "余额变化可能来自充值/赠送/外部消费等，不得命名为 burn rate"),
        }
        if len(known_asc) >= 2:
            dt_h = (known_asc[-1][0] - known_asc[0][0]) / 3600.0
            if dt_h > 0:
                summary["observed_balance_change_rate_per_hour"] = \
                    round((known_asc[-1][1] - known_asc[0][1]) / dt_h, 6)
        return {"points": out, "summary": summary}

    def resource_health(self, resource_id: str,
                        since: Optional[float] = None) -> dict:
        """Resource 级最小 Health 派生（基于真实请求 status/error，非探测）。

        语义（绝不猜测）：
        - unknown：该 resource 无任何 llm_call 事件 -> 不可判定健康，返回 unknown
          （无数据 ≠ healthy）
        - healthy：有事件且零失败
        - degraded：有事件且部分失败
        - unavailable：有事件且全部失败
        error_rate 在 n=0 时为 None（不伪造成 0）。
        last_observed_at：该 resource 最近一条 llm_call 的时间戳（epoch 秒），
          none_observation 语义下为 None（从未观察）。用于判断 Health 新鲜度，
          绝不假装成实时探测。
        仅用既有 events 字段（status_code/error/event_type/resource_id/timestamp），
        无新增 schema、无新增采集、不改 Ledger 语义。
        """
        where = "WHERE resource_id = ? AND event_type = 'llm_call'"
        params: list = [resource_id]
        if since is not None:
            where += " AND timestamp >= ?"
            params.append(since)
        rows = self._query(
            f"SELECT COUNT(*) AS n, "
            f"SUM(CASE WHEN status_code >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) "
            f"AS errs, MAX(timestamp) AS last_observed_at FROM events {where}",
            tuple(params))
        row = rows[0]
        n = row["n"] or 0
        errs = row["errs"] or 0
        last_observed_at = row["last_observed_at"]
        if n == 0:
            return {"resource_id": resource_id, "health": "unknown",
                    "requests": 0, "errors": 0, "error_rate": None,
                    "last_observed_at": None,
                    "note": "no attributed llm_call events -> unknown (not healthy)"}
        rate = (errs / n) if n else None
        if errs == 0:
            health = "healthy"
        elif errs >= n:
            health = "unavailable"
        else:
            health = "degraded"
        return {"resource_id": resource_id, "health": health,
                "requests": n, "errors": errs,
                "error_rate": round(rate, 4) if rate is not None else None,
                "last_observed_at": last_observed_at,
                "note": "derived from request status/error; not a probe"}
