"""真实链路验证：Client → Monitor → Provider → Event → SQLite → Stats API。

前置：Monitor 已启动（python -m monitor.main），且目标 provider 已在
data/config.yaml（或 Dashboard）中启用并配置 API Key。

用法：
    python scripts/verify.py                  # 验证所有已启用且有 key 的 provider
    python scripts/verify.py deepseek gemini  # 只验证指定 provider
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.config import ConfigManager  # noqa: E402

BASE = "http://127.0.0.1:8787"
DATA = Path(__file__).resolve().parent.parent / "data"


def post(url: str, payload: dict, timeout: int = 120) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json",
                 "x-monitor-source": "verify-script"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, "<non-json>"


def verify_one(name: str, cfg) -> bool:
    model = cfg.test_model
    if not model:
        print(f"[SKIP] {name}: 未配置 test_model")
        return False

    if name == "gemini":
        path = f"/gateway/gemini/v1beta/models/{model}:generateContent"
        payload = {"contents": [{"parts": [{"text": "用一句话回答：1+1=?"}]}]}
    else:
        path = f"/gateway/{name}/chat/completions"
        payload = {"model": model, "max_tokens": 32,
                   "messages": [{"role": "user", "content": "用一句话回答：1+1=?"}]}

    print(f"[CALL] {name} model={model}")
    status, resp = post(BASE + path, payload)
    if status != 200:
        print(f"  [FAIL] HTTP {status}: {str(resp)[:300]}")
        return False

    time.sleep(0.5)  # 等事件落库
    with urllib.request.urlopen(
            f"{BASE}/api/events?limit=1&provider={name}", timeout=10) as r:
        events = json.loads(r.read())["events"]
    if not events:
        print("  [FAIL] 事件未落库")
        return False
    e = events[0]
    ok = e["status_code"] == 200 and e["total_tokens"] is not None
    print(f"  [{'PASS' if ok else 'WARN'}] event: model={e['model']} "
          f"in={e['input_tokens']} out={e['output_tokens']} "
          f"latency={e['latency_ms']}ms cost={e['cost']} {e['currency']}")
    return ok


def main() -> int:
    targets = sys.argv[1:]
    cfg_mgr = ConfigManager(DATA / "config.yaml")
    results = {}
    for name, cfg in cfg_mgr.providers.items():
        if targets and name not in targets:
            continue
        if not cfg.enabled or not cfg.api_key:
            print(f"[SKIP] {name}: 未启用或未配置 API Key")
            continue
        results[name] = verify_one(name, cfg)
    if not results:
        print("没有可验证的 provider（请先在 Dashboard 或 data/config.yaml 配置）")
        return 1
    failed = [k for k, v in results.items() if not v]
    print("\n==== 结果 ====")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
