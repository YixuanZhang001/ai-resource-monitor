"""Phase 0 / Step 1: Monitor Core 最小单元测试。

验证 Core 边界成立：
  raw event → ingest → normalize → persist → event_id
  - collector 默认 gateway
  - event_type 默认 llm_call
  - execution_id / task_id 缺失时 None（不伪造）
  - 持久化走现有 Storage
  - 不依赖任何真实 API
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.core import MonitorCore  # noqa: E402
from monitor.pricing import PricingRegistry  # noqa: E402
from monitor.storage import EventStore  # noqa: E402


@pytest.fixture()
def core(tmp_path):
    return MonitorCore(EventStore(tmp_path / "core.db"), PricingRegistry())


def _raw(**over):
    base = {"provider": "deepseek", "model": "deepseek-chat",
            "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "latency_ms": 320.0, "status_code": 200}
    base.update(over)
    return base


def test_ingest_basic_returns_id(core):
    rid = core.ingest(_raw())
    assert isinstance(rid, str) and rid          # 非空字符串


def test_event_identifier_matches_persisted(core):
    rid = core.ingest(_raw())
    row = core.store.recent_events(1)[0]
    assert row["request_id"] == rid


def test_collector_default_gateway(core):
    e = core.normalize(_raw())
    assert e.collector == "gateway"             # raw 未给 → 默认


def test_collector_from_raw(core):
    e = core.normalize(_raw(collector="sdk"))
    assert e.collector == "sdk"


def test_event_type_default_llm_call(core):
    e = core.normalize(_raw())
    assert e.event_type == "llm_call"


def test_execution_id_null_when_missing(core):
    e = core.normalize(_raw())                  # raw 不含 execution_id
    assert e.execution_id is None               # 不伪造


def test_task_id_null_when_missing(core):
    e = core.normalize(_raw())
    assert e.task_id is None


def test_execution_id_passed_through(core):
    e = core.normalize(_raw(execution_id="exec-1", task_id="task-1"))
    assert e.execution_id == "exec-1" and e.task_id == "task-1"


def test_persist_uses_storage(core):
    core.ingest(_raw())
    core.ingest(_raw(model="deepseek-v4-flash"))
    rows = core.store.recent_events(10)
    assert len(rows) == 2
    assert {r["model"] for r in rows} == {"deepseek-chat", "deepseek-v4-flash"}


def test_request_id_generated_when_not_provided(core):
    e = core.normalize(_raw())
    assert e.request_id and len(e.request_id) >= 8   # dataclass default factory


def test_request_id_passthrough_when_provided(core):
    e = core.normalize(_raw(request_id="client-rid-123"))
    assert e.request_id == "client-rid-123"


def test_no_real_api_dependency():
    """Core 仅依赖 storage，不持有 httpx/网络依赖。"""
    import inspect
    src = inspect.getsource(MonitorCore)
    assert "httpx" not in src and "requests" not in src


def test_gateway_does_not_directly_persist():
    """Step 3/4 边界：Gateway 不直接调 store.insert，不承担 pricing。"""
    import inspect
    import monitor.gateway as gw
    src = inspect.getsource(gw)
    assert "store.insert" not in src            # 不直接持久化
    assert "core.ingest" in src                  # 经 Core
    assert "EventStore" not in src               # 不依赖 EventStore
    assert "pricing.compute_cost" not in src     # 不承担 pricing（Step 4 迁 Core）


def test_metadata_str_passthrough_compat(core):
    """Gateway 经 to_dict 传入的 metadata 是 JSON text，Core 反序列化回 dict。"""
    e = core.normalize(_raw(metadata='{"k": 1}'))
    assert e.metadata == {"k": 1}


def test_ingest_computes_estimated_cost(core):
    """Step 4：pricing 由 Core 承担，ingest 后 estimated_cost 落库。"""
    rid = core.ingest(_raw(input_tokens=100, output_tokens=50, total_tokens=150))
    row = core.store.recent_events(1)[0]
    assert row["estimated_cost"] is not None
    assert row["currency"] == "CNY"   # deepseek 刊例


def test_ingest_no_cost_when_pricing_absent(tmp_path):
    """无 pricing 时 estimated_cost 保持 None（不伪造）。"""
    c = MonitorCore(EventStore(tmp_path / "x.db"))
    c.ingest(_raw(input_tokens=10, output_tokens=5))
    row = c.store.recent_events(1)[0]
    assert row["estimated_cost"] is None
