"""真实 Provider 链路完整验证（临时脚本，不入项目）。

对每个 provider 验证：
A. 非流式: 转发响应 / model 识别 / token / cost / latency / status
B. 流式: SSE 转发 / 流式 usage
C. 401: 错误 key → 401 透传 + error 事件
D. 网关错误路径: 404 / 未启用 400
E. Key 泄漏: 真实 key 前缀不出现在 事件API / providers API / SQLite
"""
import json
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

import yaml

BASE = "http://127.0.0.1:8787"
DB = Path("D:/AI/projects/ai-resource-monitor/data/monitor.db")
CFG = yaml.safe_load(Path("D:/AI/projects/ai-resource-monitor/data/config.yaml").read_text(encoding="utf-8"))

PROVIDERS = ["deepseek", "openai", "minimax", "doubao", "qwen", "kimi"]
MODELS = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini",
          "minimax": "MiniMax-M2", "doubao": "doubao-seed-1-6", "qwen": "qwen-plus",
          "kimi": "kimi-k2-0905-preview", "gemini": "gemini-2.5-flash"}

RESULTS = {}


def http_post(url, payload, headers=None, timeout=120):
    h = {"content-type": "application/json", "x-monitor-source": "verify-real"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, str(e)


def http_get(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode()
    except Exception as e:
        return -1, str(e)


def http_put(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"},
                                 method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return -1, str(e)


def latest_event(provider):
    st, body = http_get(f"{BASE}/api/events?limit=5&provider={provider}")
    if st != 200:
        return None
    evs = json.loads(body)["events"]
    return evs[0] if evs else None


def check(provider, item, ok, detail=""):
    RESULTS.setdefault(provider, []).append((item, ok, detail))
    print(f"    [{'PASS' if ok else 'FAIL'}] {item}: {detail}")


def key_fragments():
    """返回 [(provider, key_prefix12)]，用于泄漏检测。"""
    out = []
    for name, p in (CFG.get("providers") or {}).items():
        k = (p or {}).get("api_keys") or (p or {}).get("api_key") or ""
        if len(k) >= 12:
            out.append((name, k[:12]))
    return out


def verify_provider(name, model):
    print(f"\n===== {name} (model={model}) =====")
    path = f"{BASE}/gateway/{name}/chat/completions"

    # A. 非流式
    payload = {"model": model, "max_tokens": 24,
               "messages": [{"role": "user", "content": "用一句话回答：1+1=?"}]}
    t0 = time.time()
    st, body = http_post(path, payload)
    wall = round((time.time() - t0) * 1000, 1)
    resp = json.loads(body) if body.startswith("{") else None
    if st == 200 and resp and resp.get("choices"):
        u = resp.get("usage") or {}
        check(name, "A1 转发成功(200+choices)", True,
              f"HTTP {st} wall={wall}ms choices={len(resp['choices'])}")
        check(name, "A2 上游usage字段",
              u.get("prompt_tokens") is not None and u.get("completion_tokens") is not None,
              f"prompt={u.get('prompt_tokens')} completion={u.get('completion_tokens')} total={u.get('total_tokens')}")
    else:
        check(name, "A1 转发成功(200+choices)", False, f"HTTP {st}: {body[:200]}")
        check(name, "A2 上游usage字段", False, "无正常响应")

    time.sleep(0.4)
    ev = latest_event(name)
    if ev:
        check(name, "A3 事件落库+status200", ev["status_code"] == 200,
              f"status={ev['status_code']}")
        check(name, "A4 model识别", ev["model"] == model, f"event.model={ev['model']}")
        check(name, "A5 token三字段",
              ev["input_tokens"] is not None and ev["output_tokens"] is not None
              and ev["total_tokens"] is not None,
              f"in={ev['input_tokens']} out={ev['output_tokens']} total={ev['total_tokens']}")
        check(name, "A6 cost计算", ev["estimated_cost"] is not None,
              f"cost={ev['estimated_cost']} {ev['currency']} "
              f"{'(pricing未命中→0)' if ev['estimated_cost'] == 0 and ev['total_tokens'] else ''}")
        check(name, "A7 latency记录", ev["latency_ms"] is not None,
              f"latency={ev['latency_ms']}ms")
    else:
        check(name, "A3 事件落库", False, "无事件")

    # B. 流式
    payload_s = {"model": model, "max_tokens": 24, "stream": True,
                 "messages": [{"role": "user", "content": "用一句话回答：2+2=?"}]}
    t0 = time.time()
    st, body = http_post(path, payload_s)
    wall = round((time.time() - t0) * 1000, 1)
    chunks = [l[5:].strip() for l in body.splitlines()
              if l.startswith("data:") and l[5:].strip() and l[5:].strip() != "[DONE]"]
    has_usage = any('"usage"' in l and '"prompt_tokens"' in l for l in chunks)
    check(name, "B1 流式SSE转发", st == 200 and len(chunks) > 0,
          f"HTTP {st} chunks={len(chunks)} wall={wall}ms")
    check(name, "B2 流式usage分片", has_usage, "末分片含usage" if has_usage else "无(记null)")
    time.sleep(0.4)
    ev = latest_event(name)
    if ev and ev["status_code"] == 200:
        check(name, "B3 流式事件token", ev["total_tokens"] is not None,
              f"in={ev['input_tokens']} out={ev['output_tokens']} total={ev['total_tokens']}")
    else:
        check(name, "B3 流式事件token", False, "无事件")

    # C. 401: gateway 会无条件用 Monitor 侧 key 覆盖客户端 authorization（安全设计），
    #    因此 401 只能来自 key 失效 → 临时把配置 key 换成坏值再请求。
    orig_key = (CFG["providers"][name].get("api_keys") or
                [CFG["providers"][name].get("api_key", "")])[0]
    http_put(f"{BASE}/api/providers/{name}", {"api_key": "sk-invalid-401-test"})
    time.sleep(0.3)
    st, body = http_post(path, payload)
    check(name, "C1 401透传", st == 401, f"HTTP {st} (key已临时置坏)")
    time.sleep(0.4)
    ev = latest_event(name)
    if ev:
        check(name, "C2 401事件记录",
              ev["status_code"] == 401 and ev["error"] is not None,
              f"status={ev['status_code']} error={str(ev['error'])[:60]}")
    else:
        check(name, "C2 401事件记录", False, "无事件")
    http_put(f"{BASE}/api/providers/{name}", {"api_key": orig_key})  # 恢复真 key
    time.sleep(0.3)
    st, body = http_post(path, payload)
    check(name, "C3 key恢复后正常", st == 200, f"HTTP {st}")

    # D. 网关错误路径
    st, body = http_post(f"{BASE}/gateway/nonexist/chat/completions", payload)
    check(name, "D1 未知provider→404", st == 404, f"HTTP {st}")
    st, body = http_post(f"{BASE}/gateway/gemini/chat/completions", payload)
    check(name, "D2 未启用provider→400", st == 400, f"HTTP {st}")

    # E. Key 泄漏（用真实 key 前缀检测）
    frags = key_fragments()
    leaks = []
    st, body = http_get(f"{BASE}/api/events?limit=20&provider={name}")
    if st == 200:
        for p, f in frags:
            if f in body:
                leaks.append(f"events-api/{p}")
    st, body = http_get(f"{BASE}/api/providers")
    if st == 200:
        for p, f in frags:
            if f in body:
                leaks.append(f"providers-api/{p}")
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT * FROM events WHERE provider=? ORDER BY id DESC LIMIT 30",
            (name,)).fetchall()
        cols = [d[0] for d in conn.execute("SELECT * FROM events LIMIT 0").description]
        conn.close()
        blob = json.dumps([dict(zip(cols, r)) for r in rows])
        for p, f in frags:
            if f in blob:
                leaks.append(f"sqlite/{p}")
    except Exception as e:
        leaks.append(f"sqlite-read-err:{e}")
    check(name, "E key泄漏检测", not leaks, "无泄漏" if not leaks else f"泄漏: {leaks}")


def main():
    targets = sys.argv[1:] or PROVIDERS
    for name in targets:
        if name not in MODELS:
            print(f"[SKIP] {name}: 本轮无 key 或无模型映射")
            continue
        verify_provider(name, MODELS[name])

    print("\n===== 汇总 =====")
    all_ok = True
    for name in targets:
        if name not in RESULTS:
            continue
        items = RESULTS[name]
        fails = [i for i, ok, _ in items if not ok]
        all_ok = all_ok and not fails
        print(f"  {name}: {'PASS' if not fails else 'FAIL'} "
              f"({len(items)-len(fails)}/{len(items)} 项通过)"
              + (f" 失败项: {fails}" if fails else ""))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
