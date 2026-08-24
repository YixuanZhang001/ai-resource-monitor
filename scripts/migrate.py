"""安全增量迁移脚本（幂等，可重复执行）。

- 补充 Analytics 查询所需索引
- 增量补齐事件表新列（cache_read_tokens/cache_write_tokens/cache_hit）
- 不改表结构其余部分、不修改任何已有事件数据

用法：
    python scripts/migrate.py [db_path]
默认 db_path = data/monitor.db
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.storage import ensure_columns  # noqa: E402

# 与 monitor/storage.py 的 SCHEMA 保持一致；CREATE INDEX IF NOT EXISTS 保证幂等
INDEX_MIGRATIONS = [
    ("idx_events_model",
     "CREATE INDEX IF NOT EXISTS idx_events_model ON events(model)"),
    ("idx_events_source",
     "CREATE INDEX IF NOT EXISTS idx_events_source ON events(source)"),
    ("idx_events_project",
     "CREATE INDEX IF NOT EXISTS idx_events_project ON events(project)"),
    ("idx_events_status",
     "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status_code)"),
]

# Resource Observation 快照表（幂等建表；历史 observation 保留，删除 Resource 不删）
STATE_TABLE_MIGRATIONS = [
    ("resource_states",
     """CREATE TABLE IF NOT EXISTS resource_states (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        resource_id TEXT NOT NULL,
        observed_at REAL NOT NULL,
        status TEXT NOT NULL,
        balance REAL,
        quota REAL,
        remaining REAL,
        reset_at REAL,
        expires_at REAL,
        source TEXT,
        error TEXT,
        metadata TEXT
    )"""),
    ("idx_states_res_ts",
     "CREATE INDEX IF NOT EXISTS idx_states_res_ts "
     "ON resource_states(resource_id, observed_at DESC)"),
]


def migrate(db_path: str | Path) -> dict:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"数据库不存在: {path}")
    conn = sqlite3.connect(str(path))
    result = {"indexes": [], "columns": [], "tables": []}
    try:
        existing_idx = {r[1] for r in conn.execute(
            "PRAGMA index_list(events)")}
        for name, sql in INDEX_MIGRATIONS:
            if name not in existing_idx:
                conn.execute(sql)
                result["indexes"].append(name)
        result["columns"] = ensure_columns(conn)
        existing_tbl = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        for name, sql in STATE_TABLE_MIGRATIONS:
            if name not in existing_tbl:
                conn.execute(sql)
                result["tables"].append(name)
        conn.commit()
    finally:
        conn.close()
    return result


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "data" / "monitor.db")
    result = migrate(db_path)
    print(f"迁移完成（幂等，可重复执行）: {db_path}")
    for name in result["indexes"]:
        print(f"  + index {name}")
    for col in result["columns"]:
        print(f"  + column {col}")
    for t in result["tables"]:
        print(f"  + table {t}")
    if not result["indexes"] and not result["columns"] and not result["tables"]:
        print("  无变更（已是最新）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
