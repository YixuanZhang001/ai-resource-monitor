"""真实客户端接入测试：OpenAI SDK（官方真实客户端）→ Monitor Gateway → Provider → Dashboard。

用法：
    python scripts/verify_client.py [provider] [model]
默认 provider=deepseek model=deepseek-chat。需要 Monitor 已启动且该 provider 已启用。
"""
import json
import sys
import time
import urllib.request

from openai import OpenAI

BASE = "http://127.0.0.1:8787"
DEFAULTS = {"deepseek": "deepseek-chat", "qwen": "qwen-plus",
            "kimi": "kimi-k2-0905-preview", "minimax": "MiniMax-M2",
            "openai": "gpt-4o-mini", "gemini": "gemini-2.5-flash"}


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "deepseek"
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULTS.get(provider, "")
    if not model:
        print(f"[SKIP] 未知 provider: {provider}")
        return 1

    client = OpenAI(base_url=f"{BASE}/gateway/{provider}", api_key="any-client-key")
    print(f"===== 1. 真实 SDK 非流式调用 ({provider}/{model}) =====")
    r = client.chat.completions.create(
        model=model, max_tokens=32,
        messages=[{"role": "user", "content": "用一句话回答：1+1=?"}])
    print(f"  内容: {r.choices[0].message.content!r}")
    print(f"  model回显: {r.model}")
    print(f"  usage: prompt={r.usage.prompt_tokens} completion={r.usage.completion_tokens} "
          f"total={r.usage.total_tokens}")

    print(f"===== 2. 真实 SDK 流式调用 =====")
    collected = ""
    chunks = 0
    for chunk in client.chat.completions.create(
            model=model, max_tokens=32, stream=True,
            messages=[{"role": "user", "content": "用一句话回答：2+2=?"}]):
        chunks += 1
        if chunk.choices and chunk.choices[0].delta.content:
            collected += chunk.choices[0].delta.content
    print(f"  流式chunk数: {chunks} 汇总内容: {collected!r}")

    time.sleep(0.5)
    print("===== 3. Dashboard 记录检查 =====")
    with urllib.request.urlopen(
            f"{BASE}/api/events?limit=2&provider={provider}", timeout=10) as resp:
        evs = json.loads(resp.read())["events"]
    for e in evs:
        print(f"  event: model={e['model']} source={e['source']!r} "
              f"in={e['input_tokens']} out={e['output_tokens']} total={e['total_tokens']} "
              f"cost={e['estimated_cost']}{e['currency']} latency={e['latency_ms']}ms "
              f"status={e['status_code']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
