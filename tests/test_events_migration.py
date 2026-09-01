"""PH1 测试：事件模型 cache 字段 + 幂等 migration + 旧库兼容。"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.events import AIRequestEvent, Usage  # noqa: E402
from monitor.storage import COLUMN_MIGRATIONS, EventStore  # noqa: E402

from scripts.migrate import migrate  # noqa: E402


def _old_schema_conn(db_path):
    """构造一个不含 cache 列的旧库。"""
    conn = sqlite3.connect(str(db_path))
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
    conn.execute("INSERT INTO events (request_id, timestamp, provider, model, "
                 "input_tokens, output_tokens, total_tokens, status_code) "
                 "VALUES ('legacy-1', 1700000000, 'deepseek', 'deepseek-chat', "
                 "10, 5, 15, 200)")
    conn.commit()
    conn.close()


def test_open_does_not_alter_schema(tmp_path):
    """P6 审计修复的核心不变量：**构造 EventStore 绝不 ALTER 既有库**。

    否则「import 一次 monitor.main」或任何只读审计都会改写生产库 schema。
    迁移必须由显式动作（store.migrate() / 应用启动）或首次写入触发。
    """
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    before = sqlite3.connect(db).execute("PRAGMA table_info(events)").fetchall()
    store = EventStore(db)
    after = sqlite3.connect(db).execute("PRAGMA table_info(events)").fetchall()
    assert before == after, "构造 EventStore 不应改动 schema"
    # 显式迁移才补列
    added = store.migrate()
    assert "cache_read_tokens" in added
    cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(events)")}
    assert {"cache_read_tokens", "cache_write_tokens", "cache_hit"} <= cols
    store.close()


def test_open_then_insert_migrates(tmp_path):
    """安全网：首次写入自动补齐列，避免调用方忘记 migrate 而丢数据。"""
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    store = EventStore(db)
    store.insert(AIRequestEvent(provider="deepseek", model="deepseek-chat",
                                input_tokens=1, output_tokens=1))
    cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(events)")}
    assert "cache_read_tokens" in cols and "client" in cols
    store.close()


def test_legacy_db_gets_cache_columns_on_migrate(tmp_path):
    """旧库由 EventStore.migrate() 补列，历史行 cache 为 NULL。"""
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    store = EventStore(db)
    store.migrate()
    cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(events)")}
    assert {"cache_read_tokens", "cache_write_tokens", "cache_hit"} <= cols
    row = store.recent_events(1)[0]
    assert row["request_id"] == "legacy-1"          # 旧数据保留
    assert row["cache_read_tokens"] is None         # 旧数据 cache 为 NULL
    store.close()


def test_migrate_script_idempotent(tmp_path):
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    r1 = migrate(db)
    added_cols = [c for c in r1["columns"] if not c.startswith("rename:")]
    assert set(added_cols) == {c for c, _ in COLUMN_MIGRATIONS}
    assert any(c.startswith("rename:parent_span_id->parent_id")
               for c in r1["columns"])     # 旧库 rename
    assert r1["indexes"]                       # 补齐索引
    r2 = migrate(db)
    assert r2["columns"] == []                  # 二次无新列
    assert r2["indexes"] == []                  # 二次无新索引
    # 数据未受损
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()


def test_event_cache_fields_roundtrip(tmp_path):
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="gemini", model="gemini-2.5-flash",
                       input_tokens=100, output_tokens=20,
                       total_tokens=120)
    e.apply_usage(Usage(input_tokens=100, output_tokens=20, total_tokens=120,
                        cache_read_tokens=80))
    assert e.cache_read_tokens == 80
    assert e.cache_hit == 1
    store.insert(e)
    row = store.recent_events(1)[0]
    assert row["cache_read_tokens"] == 80
    assert row["cache_write_tokens"] is None
    assert row["cache_hit"] == 1
    store.close()


def test_cache_hit_semantics():
    """cache_read_tokens: None→cache_hit None；0→0；>0→1。"""
    e = AIRequestEvent(provider="x", model="m")
    e.apply_usage(Usage(input_tokens=1, output_tokens=1))
    assert e.cache_read_tokens is None and e.cache_hit is None   # 无数据
    e2 = AIRequestEvent(provider="x", model="m")
    e2.apply_usage(Usage(input_tokens=1, output_tokens=1, cache_read_tokens=0))
    assert e2.cache_hit == 0                                    # 有数据且未命中
    e3 = AIRequestEvent(provider="x", model="m")
    e3.apply_usage(Usage(input_tokens=1, output_tokens=1, cache_read_tokens=5))
    assert e3.cache_hit == 1                                    # 命中


# ---------- Step 2：Revision 2 字段 migration + round-trip ----------

def test_legacy_db_renames_parent_span_id(tmp_path):
    """旧库 parent_span_id 列被 rename 为 parent_id，数据不丢失。"""
    db = tmp_path / "old.db"
    _old_schema_conn(db)   # 旧 schema 含 parent_span_id
    store = EventStore(db)
    store.migrate()
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(events)")}
    assert "parent_id" in cols and "parent_span_id" not in cols
    store.close()


def test_revision2_fields_roundtrip(tmp_path):
    """collector/event_type/execution_id/task_id/parent_id/metadata 完整 round-trip。"""
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="deepseek", model="deepseek-chat",
                       input_tokens=10, output_tokens=5, total_tokens=15,
                       collector="sdk", event_type="llm_call",
                       execution_id="exec-1", task_id="task-1",
                       parent_id="parent-evt-1",
                       metadata={"streaming": True, "thinking": False})
    store.insert(e)
    rid = e.request_id
    # recent_events 返回
    row = store.recent_events(1)[0]
    assert row["collector"] == "sdk"
    assert row["event_type"] == "llm_call"
    assert row["execution_id"] == "exec-1"
    assert row["task_id"] == "task-1"
    assert row["parent_id"] == "parent-evt-1"
    assert row["metadata"] == {"streaming": True, "thinking": False}  # 反序列化回 dict
    # get_request 返回
    detail = store.get_request(rid)
    assert detail["parent_id"] == "parent-evt-1"
    store.close()


def test_default_collector_event_type_on_legacy_data(tmp_path):
    """旧事件行（无 collector/event_type）查询时回退默认值。"""
    db = tmp_path / "old.db"
    _old_schema_conn(db)
    store = EventStore(db)
    store.migrate()          # 显式补列 DEFAULT
    row = store.recent_events(1)[0]
    # ADD COLUMN ... DEFAULT 使旧行也有值
    assert row["collector"] == "gateway"
    assert row["event_type"] == "llm_call"
    assert row["execution_id"] is None and row["task_id"] is None
    store.close()


def test_metadata_none_roundtrip(tmp_path):
    """metadata=None 时落库为 NULL，读回 None。"""
    store = EventStore(tmp_path / "t.db")
    e = AIRequestEvent(provider="x", model="m", input_tokens=1)
    store.insert(e)
    row = store.recent_events(1)[0]
    assert row["metadata"] is None
    store.close()
