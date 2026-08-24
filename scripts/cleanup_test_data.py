"""一次性脚本：清理测试/验证事件数据（保留表结构与索引）。

用途：开发与验证阶段产生的测试事件会污染 Dashboard 的 Error Rate / Cost /
Token 统计；本脚本清空 events 表数据。幂等——重复执行无影响（count 恒为 0）。

注意：不删除表结构、不删除索引、不重置自增序列（保持最简）。
用法：
    python scripts/cleanup_test_data.py [db_path]
默认 db_path = data/monitor.db
"""
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "data" / "monitor.db")
    conn = sqlite3.connect(str(db_path))
    n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    if n:
        conn.execute("DELETE FROM events")
        conn.commit()
        print(f"已清理 {n} 条测试/验证事件")
    else:
        print("events 表已为空（幂等，无操作）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
