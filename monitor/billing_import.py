"""平台账单 CSV 导入：把 DeepSeek 开放平台的 amount / cost 导出归一为 llm_call 事件。

设计原则（对齐 monitor 既有约束）：
- 复用既有 AIRequestEvent + EventStore，不新建表、不改前端数据结构 → overview/analytics/CACHE 图表自动包含。
- 成本直接采用平台 CSV 的权威日成本（cost 列），不做重算 → 逐分对齐平台。
- 幂等：每次导入先删除该 provider 既有 source='platform_bill' 事件，再按 CSV 重插，可重复刷新。
- 平台账单为全量权威；默认删除同区间网关采集的该 provider 事件（避免重叠计数），可用 --no-prune 关闭。
- 时间：平台日期按北京时间；存入「北京时间正午」的 UTC epoch，保证 dashboard 按 UTC 日分桶时落在正确日期。
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from .events import AIRequestEvent
from .storage import EventStore

_CN_TZ = timezone(timedelta(hours=8))
SOURCE_TAG = "platform_bill"


def _day_noon_beijing_epoch(date_str: str) -> float:
    """平台日期(北京) -> 存北京时间正午的 UTC epoch，使 dashboard UTC 日分桶对齐。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=12, minute=0, second=0, tzinfo=_CN_TZ)
    return dt.timestamp()


def _parse_amount(amount_csv: str) -> dict:
    """date -> {model: {hit, miss, out, req}} 。平台导出每行带 model。"""
    by = defaultdict(lambda: defaultdict(lambda: {"hit": 0, "miss": 0, "out": 0, "req": 0}))
    with open(amount_csv, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            d = r["start_time_iso"][:10]
            m = r.get("model") or "unknown"
            t = r["type"]
            amt = (r.get("amount") or "").strip()
            slot = by[d][m]
            if t == "request_count":
                slot["req"] += int(amt)
            elif t == "input_cache_hit_tokens":
                slot["hit"] += int(float(amt))
            elif t == "input_cache_miss_tokens":
                slot["miss"] += int(float(amt))
            elif t == "output_tokens":
                slot["out"] += int(float(amt))
    return by


def _parse_cost(cost_csv: str) -> dict:
    """date -> cost(CNY) 。单 model 导出每日一行。"""
    out: dict = {}
    with open(cost_csv, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            out[r["start_time_iso"][:10]] = float(r["cost"])
    return out


def build_events(amount_csv: str, cost_csv: str,
                  provider: str = "deepseek") -> list[AIRequestEvent]:
    by = _parse_amount(amount_csv)
    cost = _parse_cost(cost_csv)
    events: list[AIRequestEvent] = []
    for d, models in by.items():
        c = cost.get(d)
        for m, agg in models.items():
            in_tok = agg["hit"] + agg["miss"]
            tot = in_tok + agg["out"]
            ev = AIRequestEvent(
                provider=provider,
                model=m,
                source=SOURCE_TAG,
                collector="import",
                event_type="llm_call",
                input_tokens=in_tok or None,
                output_tokens=agg["out"] or None,
                total_tokens=tot or None,
                cache_read_tokens=agg["hit"] or None,
                cache_write_tokens=None,
                cache_hit=(1 if agg["hit"] > 0 else (0 if agg["miss"] > 0 else None)),
                cost=c,
                currency="CNY",
                status_code=200,
                metadata={
                    "imported": SOURCE_TAG,
                    "request_count": agg["req"],
                    "bill_date": d,
                },
                timestamp=_day_noon_beijing_epoch(d),
            )
            events.append(ev)
    return events


def import_bills(amount_csv: str, cost_csv: str, db_path: str,
                 provider: str = "deepseek",
                 prune_gateway: bool = True) -> dict:
    """执行导入。返回 {inserted, pruned_gateway, days}。"""
    events = build_events(amount_csv, cost_csv, provider)
    store = EventStore(db_path)
    store.migrate()

    # 幂等：删除既有 platform_bill（同 provider）
    with store._lock, store._conn:
        store._conn.execute(
            "DELETE FROM events WHERE source=? AND provider=?",
            (SOURCE_TAG, provider))

    pruned = 0
    if prune_gateway and events:
        # 平台账单为全量权威；删除同区间网关采集的该 provider 事件，避免重叠计数。
        utc_days = sorted({
            datetime.fromtimestamp(ev.timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
            for ev in events
        })
        qmarks = ",".join("?" * len(utc_days))
        with store._lock, store._conn:
            cur = store._conn.execute(
                f"DELETE FROM events WHERE provider=? AND collector='gateway' "
                f"AND event_type='llm_call' AND date(timestamp,'unixepoch') IN ({qmarks})",
                (provider, *utc_days))
            pruned = cur.rowcount

    n = 0
    for ev in events:
        store.insert(ev)
        n += 1

    # 关闭本次导入新建的局部连接（不影响服务单例 store），避免 Windows 文件锁残留
    try:
        store._conn.close()
    except Exception:
        pass

    days = sorted({ev.metadata["bill_date"] for ev in events})
    return {"inserted": n, "pruned_gateway": pruned, "days": days}
