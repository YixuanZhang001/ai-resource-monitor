"""SQLite 事件存储。本地优先，单文件数据库。"""
from __future__ import annotations

import json
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
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_hit INTEGER,
    latency_ms REAL,
    status_code INTEGER,
    estimated_cost REAL,
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
    "project", "input_tokens", "output_tokens", "total_tokens",
    "cache_read_tokens", "cache_write_tokens", "cache_hit", "latency_ms",
    "status_code", "estimated_cost", "currency", "error", "trace_id",
    "parent_id", "collector", "event_type", "execution_id", "task_id",
    "resource_id", "metadata",
]

# 增量列迁移（幂等）：旧库补齐新列，不修改既有字段与数据
COLUMN_MIGRATIONS = [
    ("cache_read_tokens", "INTEGER"),
    ("cache_write_tokens", "INTEGER"),
    ("cache_hit", "INTEGER"),
    ("collector", "TEXT DEFAULT 'gateway'"),
    ("event_type", "TEXT DEFAULT 'llm_call'"),
    ("execution_id", "TEXT"),
    ("task_id", "TEXT"),
    ("resource_id", "TEXT"),
    ("metadata", "TEXT"),
]

# 列重命名（幂等）：旧名存在且新名不存在才 RENAME
COLUMN_RENAMES = [("parent_span_id", "parent_id")]


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
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            ensure_columns(self._conn)
            self._conn.commit()

    def insert(self, event: AIRequestEvent) -> None:
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
        cost_where = f"{where} AND estimated_cost IS NOT NULL" if where else " WHERE estimated_cost IS NOT NULL"
        costs = self._query(
            f"""SELECT currency, ROUND(SUM(estimated_cost), 6) AS cost
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
            f"""SELECT provider, currency, ROUND(SUM(estimated_cost), 6) AS cost
                FROM events{where + ' AND' if where else ' WHERE'} estimated_cost IS NOT NULL
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
                       ROUND(SUM(estimated_cost), 6) AS cost
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
               FROM events WHERE timestamp >= ?
               GROUP BY day ORDER BY day""",
            (since,),
        )

    @staticmethod
    def _since(since: Optional[float]) -> tuple[str, tuple]:
        if since is None:
            return "", ()
        return " WHERE timestamp >= ?", (since,)

    # ---------- 第二阶段：Analytics（全部 SQL 层聚合） ----------

    RANGES = {"today": 24, "7d": 7 * 24, "30d": 30 * 24, "all": None}
    UNKNOWN = "Unknown"
    # group_by 白名单（防 SQL 注入）
    DIM_COLUMNS = {
        "provider": "provider",
        "model": "model",
        "source": "COALESCE(source, 'Unknown')",
        "project": "COALESCE(project, 'Unknown')",
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
        row = self._query(
            f"""SELECT COUNT(*) AS requests,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors
                FROM events{where}""",
            params,
        )[0]
        cost_where = f"{where} AND estimated_cost IS NOT NULL" if where \
            else " WHERE estimated_cost IS NOT NULL"
        costs = self._query(
            f"""SELECT currency, ROUND(SUM(estimated_cost), 6) AS cost
                FROM events{cost_where} GROUP BY currency""",
            params,
        )
        row["cost_by_currency"] = {c["currency"]: c["cost"]
                                   for c in costs if c["currency"]}
        row["error_rate"] = (row["errors"] / row["requests"]) \
            if row["requests"] else 0.0
        return row

    def analytics_cost(self, dim: str, since: Optional[float] = None) -> list[dict]:
        expr = self._dim_expr(dim)
        where, params = self._since(since)
        return self._query(
            f"""SELECT {expr} AS name, currency,
                       ROUND(SUM(estimated_cost), 6) AS cost,
                       COUNT(*) AS requests
                FROM events{where + ' AND' if where else ' WHERE'}
                     estimated_cost IS NOT NULL
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
                       COALESCE(SUM(total_tokens), 0) AS total_tokens
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
                      COALESCE(SUM(total_tokens), 0) AS total_tokens
               FROM events WHERE timestamp >= ?
               GROUP BY day ORDER BY day""",
            (since,),
        )

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
                       status: Optional[int] = None,
                       since: Optional[float] = None) -> dict:
        page = max(1, page)
        page_size = min(max(1, page_size), 200)
        conds, params = [], []
        if since is not None:
            conds.append("timestamp >= ?")
            params.append(since)
        for col, val in (("provider", provider), ("model", model),
                         ("source", source), ("project", project)):
            if val:
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
                       input_tokens, output_tokens, total_tokens,
                       latency_ms, status_code, estimated_cost, currency, error
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
                      input_tokens, output_tokens, total_tokens,
                      latency_ms, status_code, estimated_cost, currency,
                      error, trace_id, parent_id
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
        where, params = self._since(since)
        return self._query(
            f"""SELECT COALESCE(resource_id, '') AS resource_id,
                       COUNT(*) AS requests,
                       COALESCE(SUM(input_tokens), 0) AS input_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(total_tokens), 0) AS total_tokens,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       MAX(timestamp) AS last_used_at,
                       SUM(CASE WHEN estimated_cost IS NOT NULL
                                THEN 1 ELSE 0 END) AS cost_count,
                       COALESCE(SUM(estimated_cost), 0) AS cost_known_sum
                FROM events{where}
                GROUP BY resource_id""",
            params,
        )

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
                       SUM(CASE WHEN error IS NOT NULL OR status_code >= 400
                                THEN 1 ELSE 0 END) AS errors,
                       COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                       SUM(CASE WHEN estimated_cost IS NOT NULL
                                THEN 1 ELSE 0 END) AS cost_count,
                       COALESCE(SUM(estimated_cost), 0) AS cost_known_sum
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
                      latency_ms, status_code, estimated_cost, currency, error
               FROM events WHERE resource_id = ?
               ORDER BY id DESC LIMIT ?""",
            (resource_id, limit))
        return rows
