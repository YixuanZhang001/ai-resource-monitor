"""平台账单导入单测：CSV 解析 / 事件构建 / 幂等 / 重叠裁剪。

隔离规则：使用 tempfile 临时 DB 与临时 CSV，绝不触碰生产 data/monitor.db。
"""
from __future__ import annotations

import csv
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from monitor.billing_import import build_events, import_bills, SOURCE_TAG
from monitor.storage import EventStore


AMOUNT = """user_id,start_time_iso,end_time_iso,model,api_key_name,api_key,type,price,amount
u,2026-08-06T00:00:00+08:00,2026-08-07T00:00:00+08:00,deepseek-v4-flash,k,sk,input_cache_hit_tokens,0.00000002,172391680
u,2026-08-06T00:00:00+08:00,2026-08-07T00:00:00+08:00,deepseek-v4-flash,k,sk,input_cache_miss_tokens,0.000001,866325
u,2026-08-06T00:00:00+08:00,2026-08-07T00:00:00+08:00,deepseek-v4-flash,k,sk,output_tokens,0.000002,472189
u,2026-08-06T00:00:00+08:00,2026-08-07T00:00:00+08:00,deepseek-v4-flash,k,sk,request_count,,779
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,input_cache_hit_tokens,0.00000005,61626240
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,input_cache_hit_tokens,0.0000001,79207296
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,input_cache_miss_tokens,0.0000015,647614
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,input_cache_miss_tokens,0.000003,207677
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,output_tokens,0.0000045,87086
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,output_tokens,0.000009,110621
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,k,sk,request_count,,393
"""

COST = """user_id,start_time_iso,end_time_iso,model,wallet_type,cost,currency
u,2026-08-06T00:00:00+08:00,2026-08-07T00:00:00+08:00,deepseek-v4-flash,Paid,5.2585366,CNY
u,2026-08-20T00:00:00+08:00,2026-08-21T00:00:00+08:00,deepseek-v4-flash,Paid,13.9839696,CNY
"""


@pytest.fixture
def files():
    d = tempfile.mkdtemp()
    a = Path(d) / "amount.csv"
    c = Path(d) / "cost.csv"
    a.write_text(AMOUNT, encoding="utf-8-sig")
    c.write_text(COST, encoding="utf-8-sig")
    yield str(a), str(c)
    for p in (a, c):
        p.unlink(missing_ok=True)


def test_build_events_aggregates_tiers(files):
    a, c = files
    evs = build_events(a, c)
    by_date = {e.metadata["bill_date"]: e for e in evs}
    assert set(by_date) == {"2026-08-06", "2026-08-20"}

    e20 = by_date["2026-08-20"]
    assert e20.input_tokens == 140_833_536 + 855_291
    assert e20.cache_read_tokens == 140_833_536
    assert e20.output_tokens == 197_707
    assert e20.total_tokens == 140_833_536 + 855_291 + 197_707
    assert e20.cost == pytest.approx(13.9839696)
    assert e20.currency == "CNY"
    assert e20.source == SOURCE_TAG
    assert e20.collector == "import"
    assert e20.cache_hit == 1

    e06 = by_date["2026-08-06"]
    assert e06.input_tokens == 172_391_680 + 866_325
    assert e06.cache_read_tokens == 172_391_680
    assert e06.cost == pytest.approx(5.2585366)


def test_import_idempotent(files):
    a, c = files
    db = tempfile.mktemp(suffix=".db")
    r1 = import_bills(a, c, db)
    assert r1["inserted"] == 2
    r2 = import_bills(a, c, db)  # 重复导入应幂等
    assert r2["inserted"] == 2
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM events WHERE source=?", (SOURCE_TAG,)).fetchone()[0]
    assert n == 2
    conn.close()
    os.unlink(db)


def test_import_prunes_overlapping_gateway(files):
    a, c = files
    db = tempfile.mktemp(suffix=".db")
    # 预置一条网关采集的 deepseek 事件（08-20 UTC 正午）
    store = EventStore(db)
    store.migrate()
    from monitor.events import AIRequestEvent
    gw = AIRequestEvent(provider="deepseek", model="deepseek-v4-flash",
                        collector="gateway", event_type="llm_call",
                        input_tokens=10, output_tokens=3, cost=0.0009, currency="CNY",
                        timestamp=__import__("datetime").datetime(2026, 8, 20, 12, 0, 0).timestamp())
    store.insert(gw)
    store._conn.close()
    r = import_bills(a, c, db, prune_gateway=True)
    assert r["pruned_gateway"] == 1
    conn = sqlite3.connect(db)
    gw_left = conn.execute("SELECT COUNT(*) FROM events WHERE collector='gateway'").fetchone()[0]
    assert gw_left == 0
    conn.close()
    os.unlink(db)
