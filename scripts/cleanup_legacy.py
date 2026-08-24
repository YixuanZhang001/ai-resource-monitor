"""一次性脚本：对历史事件的 error 字段重新执行当前 Sanitizer。

用途：Sanitizer 部署之前写入的 error 可能残留 org-/ak-/sk-/Bearer/Authorization
等敏感信息；本脚本全表重扫 error 列并脱敏。幂等——重复执行结果一致（无变化行）。

用法：
    python scripts/cleanup_legacy.py [db_path]
默认 db_path = data/monitor.db
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.sanitize import sanitize_error  # noqa: E402


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else (
        Path(__file__).resolve().parent.parent / "data" / "monitor.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, error FROM events WHERE error IS NOT NULL").fetchall()
    updated, unchanged = 0, 0
    for row in rows:
        cleaned = sanitize_error(row["error"])
        if cleaned != row["error"]:
            conn.execute("UPDATE events SET error = ? WHERE id = ?",
                         (cleaned, row["id"]))
            updated += 1
        else:
            unchanged += 1
    conn.commit()
    conn.close()
    print(f"历史 error 重扫完成: 共 {len(rows)} 条含 error")
    print(f"  已脱敏更新: {updated}")
    print(f"  无需变更(幂等): {unchanged}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
