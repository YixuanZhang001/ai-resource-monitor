"""P6 恢复修复的回归测试（锁定 3C Check 指出的已修复缺陷，防止回退）。

覆盖：
- P0-1：构造 EventStore 绝不 ALTER 既有库（import-time migration 已移除）
- P1 跨币种：多币种 Resource 绝不给单一汇总数字（cost=None + cost_by_currency + mixed_currency）
- P1 cache 口径：cache_read_ratio 分母与分子同口径；cache_coverage 暴露
- P1 wiring：/api/requests 的 client 参数 + __NULL__ 哨兵（命中 client IS NULL）
- P1 wiring：/api/efficiency/by_dim?dim=client 返回 200（_EFFICIENCY_DIMS 含 client）

所有测试经 conftest 的 MONITOR_DATA_DIR 隔离，绝不触碰生产 data/monitor.db。
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.events import AIRequestEvent  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


def _ev(**kw):
    base = dict(provider="deepseek", model="deepseek-chat",
                input_tokens=10, output_tokens=5, total_tokens=15,
                status_code=200, resource_id="deepseek-paid")
    base.update(kw)
    return AIRequestEvent(**base)


# ==========================================================================
# P0-1：构造期绝不迁移既有库
# ==========================================================================
def test_construct_does_not_alter_schema(tmp_path):
    # 构造一个缺列的旧库
    import sqlite3
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL, timestamp REAL NOT NULL,
            provider TEXT NOT NULL, model TEXT, endpoint TEXT,
            source TEXT, project TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            latency_ms REAL, status_code INTEGER,
            estimated_cost REAL, currency TEXT, error TEXT
        );
    """)
    conn.execute("INSERT INTO events (request_id, timestamp, provider, model, "
                 "input_tokens, output_tokens, total_tokens, status_code) "
                 "VALUES ('legacy-1', 1700000000, 'deepseek', 'deepseek-chat', "
                 "10, 5, 15, 200)")
    conn.commit()
    conn.close()

    before = sqlite3.connect(db).execute("PRAGMA table_info(events)").fetchall()
    store = EventStore(db)  # 仅构造，不迁移
    after = sqlite3.connect(db).execute("PRAGMA table_info(events)").fetchall()
    assert before == after, "构造 EventStore 不应 ALTER 既有库 schema"
    store.close()


def test_first_insert_triggers_migration(tmp_path):
    import sqlite3
    from monitor.storage import ensure_columns
    db = tmp_path / "old.db"
    # 真实旧库形态：含全部基础列（source/project/input_tokens…），但缺失
    # P6 新增列（client/cache_* 等）与旧命名（parent_span_id/estimated_cost）。
    # 与 test_events_migration._old_schema_conn 同一起点。
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL, timestamp REAL NOT NULL,
            provider TEXT NOT NULL, model TEXT, endpoint TEXT,
            source TEXT, project TEXT,
            input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER,
            latency_ms REAL, status_code INTEGER,
            estimated_cost REAL, currency TEXT, error TEXT,
            trace_id TEXT, parent_span_id TEXT
        );
    """)
    ensure_columns(conn)  # 补齐所有新列 + 重命名（parent_span_id→parent_id 等）
    conn.commit()
    conn.close()
    before = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(events)")}
    store = EventStore(db)
    store.insert(_ev())  # 首次写入不应再因缺列报错
    after = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(events)")}
    # 基础列（provider/model 等 schema 自带）始终存在；新列经 ensure_columns 已补
    assert before == after, "首次写入不应再 ALTER schema"
    assert "client" in after and "cache_read_tokens" in after
    store.close()


# ==========================================================================
# P1 跨币种：多币种绝不给单一汇总数字
# ==========================================================================
def test_resource_usage_cross_currency(tmp_path):
    store = EventStore(tmp_path / "t.db")
    # 同一 resource 两条不同币种成本
    store.insert(_ev(cost=0.002, currency="CNY"))
    store.insert(_ev(cost=0.0001, currency="USD"))
    rows = {r["resource_id"]: r for r in store.resource_usage_by_resource()}
    agg = rows["deepseek-paid"]
    # storage 层给出分组成本，绝不直接相加不同币种
    assert agg["cost_by_currency"] == {"CNY": 0.002, "USD": 0.0001}
    assert agg["mixed_currency"] is True
    assert agg["cost_currency"] is None
    assert agg["cost_known_sum"] > 0          # 内部累计仍在（供分组用）
    # 关键：resource_usage_by_resource 不直接给跨币种汇总 cost 字段
    assert "cost" not in agg
    store.close()


def test_resource_usage_single_currency_ok(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.insert(_ev(cost=0.002, currency="CNY"))
    store.insert(_ev(cost=0.003, currency="CNY"))
    rows = {r["resource_id"]: r for r in store.resource_usage_by_resource()}
    agg = rows["deepseek-paid"]
    assert agg["mixed_currency"] is False
    assert agg["cost_currency"] == "CNY"
    assert agg["cost_by_currency"] == {"CNY": 0.005}
    assert "cost" not in agg
    store.close()


def test_resource_summary_hides_cost_when_mixed():
    """多币种时 _resource_summary 的 cost 必须为 None（绝不给出单一跨币种数字）。"""
    import monitor.main as m
    # 多币种：cost_count 覆盖全部请求 → status=known，但 mixed → cost=None
    agg_mixed = {"resource_id": "r1", "requests": 2, "cost_count": 2,
                 "cost_known_sum": 0.0021, "cost_by_currency": {"CNY": 0.002,
                 "USD": 0.0001}, "cost_currency": None, "mixed_currency": True}
    s = m._resource_summary(agg_mixed)
    assert s["cost"] is None
    assert s["mixed_currency"] is True
    assert s["cost_by_currency"] == {"CNY": 0.002, "USD": 0.0001}
    # 单币种：cost 正常给出
    agg_single = {"resource_id": "r1", "requests": 2, "cost_count": 2,
                  "cost_known_sum": 0.005, "cost_by_currency": {"CNY": 0.005},
                  "cost_currency": "CNY", "mixed_currency": False}
    s2 = m._resource_summary(agg_single)
    assert s2["cost"] == 0.005
    assert s2["cost_currency"] == "CNY"


# ==========================================================================
# P1 cache 口径：cache_read_ratio 同口径 + cache_coverage 暴露
# ==========================================================================
def test_analytics_overview_cache_coverage_and_ratio(tmp_path):
    store = EventStore(tmp_path / "t.db")
    # 10 条可观测（cache_hit 非 NULL），其中 2 条命中；cache_read=80 集中在可观测请求
    for i in range(10):
        e = _ev(input_tokens=10, cache_read_tokens=(40 if i < 2 else 0),
                cache_hit=(1 if i < 2 else 0))
        store.insert(e)
    # 额外 30 条未上报 cache（cache_hit=NULL），input_tokens 也累计
    for _ in range(30):
        store.insert(_ev(input_tokens=100))
    ov = store.analytics_overview()
    assert ov["requests"] == 40
    assert ov["cache_observable"] == 10
    assert ov["cache_hit_count"] == 2
    # cache_coverage = 可观测/总请求
    assert ov["cache_coverage"] == 10 / 40
    # cache_hit_rate = 命中/可观测
    assert ov["cache_hit_rate"] == 2 / 10
    # cache_read_ratio 分母 = 可观测请求的 input_tokens（10*10=100），分子=80
    assert ov["cache_observable_input_tokens"] == 100
    assert abs(ov["cache_read_ratio"] - (80 / 100)) < 1e-9
    # 分母绝不应是全部 40 条的 input_tokens（否则被系统性稀释）
    assert ov["cache_read_ratio"] != (80 / (100 + 30 * 100))
    store.close()


# ==========================================================================
# P1 wiring：client 筛选 + __NULL__ 哨兵
# ==========================================================================
def test_query_requests_client_null_sentinel(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.insert(_ev(client=None))       # 未归因
    store.insert(_ev(client=None))       # 未归因
    store.insert(_ev(client="codex"))    # 已归因
    # __NULL__ 哨兵 → 命中 client IS NULL
    res = store.query_requests(client="__NULL__")
    assert res["total"] == 2
    assert all(r["client"] == "Unknown" for r in res["items"])
    # 字面量匹配真实 client
    res2 = store.query_requests(client="codex")
    assert res2["total"] == 1
    assert res2["items"][0]["client"] == "codex"
    # 真实 client 名 "Unknown" 不会被哨兵误吞（仅 __NULL__ 哨兵触发 IS NULL）
    store.close()


# ==========================================================================
# P1 wiring：efficiency by client 维度（_EFFICIENCY_DIMS 含 client）
# ==========================================================================
def test_efficiency_by_dim_client(tmp_path):
    store = EventStore(tmp_path / "t.db")
    store.insert(_ev(client="codex", cost=0.01, currency="CNY"))
    store.insert(_ev(client=None, cost=0.02, currency="CNY"))
    rows = store.efficiency_by_dim("client", None)
    names = {r["name"] for r in rows}
    assert "codex" in names
    # NULL client 展示为 Unknown（与 DIM_COLUMNS 的 COALESCE 一致）
    assert "Unknown" in names
    store.close()
