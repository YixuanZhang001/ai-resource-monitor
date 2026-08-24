"""Adapter 层单元测试：usage/error 提取、URL 构造、流式 usage。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.config import ProviderConfig
from monitor.providers import GeminiAdapter, OpenAICompatibleAdapter


def cfg(**kw):
    return ProviderConfig(name="deepseek", enabled=True,
                          api_keys=kw.pop("api_keys", ["sk-test"]), **kw)


# ---------- OpenAI-compatible ----------

def test_openai_compat_url_adds_v1_prefix():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    url = a.upstream_url(cfg(), "chat/completions")
    assert url == "https://api.deepseek.com/v1/chat/completions"


def test_openai_compat_url_keeps_existing_prefix():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    url = a.upstream_url(cfg(), "v1/chat/completions")
    assert url == "https://api.deepseek.com/v1/chat/completions"


def test_openai_compat_auth_header():
    a = OpenAICompatibleAdapter("kimi", "https://api.moonshot.cn")
    h = a.upstream_headers(cfg(), {"content-type": "application/json",
                                   "authorization": "Bearer client-key",
                                   "cookie": "secret"})
    assert h["authorization"] == "Bearer sk-test"
    assert "cookie" not in h  # 客户端 cookie 不透传


def test_openai_compat_stream_injects_stream_options():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    body = a.upstream_body({"model": "deepseek-chat", "stream": True, "messages": []})
    assert body["stream_options"] == {"include_usage": True}


def test_openai_compat_extract_usage():
    a = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    u = a.extract_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5,
                                   "total_tokens": 15}})
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (10, 5, 15)


def test_openai_compat_stream_usage_last_wins():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    chunks = [
        '{"choices":[{"delta":{"content":"hi"}}]}',
        '{"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":2,"total_tokens":10}}',
    ]
    u = a.extract_stream_usage(chunks)
    assert u.total_tokens == 10


def test_openai_compat_error():
    a = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    msg = a.extract_error(401, {"error": {"message": "Invalid key"}})
    assert msg == "Invalid key"
    assert a.extract_error(200, {"choices": []}) is None


# ---------- Gemini ----------

def test_gemini_url_and_auth():
    a = GeminiAdapter()
    url = a.upstream_url(cfg(), "v1beta/models/gemini-2.5-flash:generateContent")
    assert url == ("https://generativelanguage.googleapis.com/"
                   "v1beta/models/gemini-2.5-flash:generateContent")
    h = a.upstream_headers(cfg(), {})
    assert h["x-goog-api-key"] == "sk-test"


def test_multi_key_round_robin():
    """多 Key 轮询：连续请求轮流使用不同 Key；单 Key 时始终同一个；api_key 属性兼容。"""
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    c = cfg(api_keys=["sk-k1", "sk-k2", "sk-k3"])
    got = [a.upstream_headers(c, {})["authorization"] for _ in range(5)]
    assert got == ["Bearer sk-k1", "Bearer sk-k2", "Bearer sk-k3",
                   "Bearer sk-k1", "Bearer sk-k2"]
    single = cfg(api_keys=["sk-only"])
    assert all(a.upstream_headers(single, {})["authorization"] == "Bearer sk-only"
               for _ in range(3))
    assert c.api_key == "sk-k1"   # 兼容旧属性


def test_gemini_model_from_path():
    a = GeminiAdapter()
    m = a.extract_model("v1beta/models/gemini-2.5-pro:generateContent", None, None)
    assert m == "gemini-2.5-pro"


def test_gemini_stream_detection():
    assert GeminiAdapter.is_stream(None, "x:streamGenerateContent") is True
    assert GeminiAdapter.is_stream({"stream": True}, "x:generateContent") is False


def test_gemini_extract_usage():
    a = GeminiAdapter()
    u = a.extract_usage({"usageMetadata": {"promptTokenCount": 100,
                                           "candidatesTokenCount": 20,
                                           "totalTokenCount": 120}})
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (100, 20, 120)


def test_gemini_stream_usage():
    a = GeminiAdapter()
    chunks = [
        '{"candidates":[{"content":{"parts":[{"text":"a"}]}}]}',
        '{"candidates":[{"content":{"parts":[{"text":"b"}]}}],'
        '"usageMetadata":{"promptTokenCount":5,"candidatesTokenCount":2,"totalTokenCount":7}}',
    ]
    u = a.extract_stream_usage(chunks)
    assert u.total_tokens == 7


# ---------- PH2：能力声明 + Cache ----------

def test_adapter_capability_flags():
    openai = OpenAICompatibleAdapter("openai", "https://api.openai.com")
    assert openai.balance_supported is False
    assert openai.usage_supported is False
    assert openai.cache_supported is False
    gemini = GeminiAdapter()
    assert gemini.cache_supported is True
    assert gemini.balance_supported is False


def test_default_cache_extraction_none():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    assert a.extract_cache_usage({"usage": {"prompt_tokens": 1}}) is None
    assert a.extract_cache_usage(None) is None


def test_extract_model_response_overrides_request_alias():
    """响应 model 优先于请求 alias（DeepSeek deepseek-chat→deepseek-v4-flash）。"""
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    # 响应有 model → 用响应
    assert a.extract_model("chat/completions",
                           {"model": "deepseek-chat"},
                           {"model": "deepseek-v4-flash"}) == "deepseek-v4-flash"
    # 响应无 model → 回退请求
    assert a.extract_model("chat/completions",
                           {"model": "deepseek-chat"}, None) == "deepseek-chat"
    # 都无 → None
    assert a.extract_model("chat/completions", None, None) is None


def test_deepseek_cache_extraction_real_fields():
    """DeepSeek V4 真实字段（2026-08-20 真实验证确认）：prompt_cache_hit_tokens。"""
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    out = a.extract_cache_usage({"usage": {
        "prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6,
        "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 5,
        "cached_tokens": 0}})
    assert out == {"cache_read_tokens": 0, "cache_write_tokens": None}
    out2 = a.extract_cache_usage({"usage": {"prompt_cache_hit_tokens": 80,
                                            "prompt_cache_miss_tokens": 20}})
    assert out2 == {"cache_read_tokens": 80, "cache_write_tokens": None}
    # 无 cache 字段 → None（不伪造）
    assert a.extract_cache_usage({"usage": {"prompt_tokens": 5}}) is None


def test_gemini_cache_extraction():
    a = GeminiAdapter()
    # 缓存命中时：cachedContentTokenCount 进入 cache_read_tokens
    out = a.extract_cache_usage({"usageMetadata": {
        "promptTokenCount": 20, "candidatesTokenCount": 5,
        "totalTokenCount": 25, "cachedContentTokenCount": 80}})
    assert out == {"cache_read_tokens": 80, "cache_write_tokens": None}
    # 未使用缓存：字段缺失 → None（不伪造）
    assert a.extract_cache_usage({"usageMetadata": {
        "promptTokenCount": 20, "candidatesTokenCount": 5,
        "totalTokenCount": 25}}) is None
    assert a.extract_cache_usage(None) is None


def test_balance_default_none():
    a = OpenAICompatibleAdapter("deepseek", "https://api.deepseek.com")
    assert a.balance(cfg()) is None
    g = GeminiAdapter()
    assert g.balance(cfg()) is None
