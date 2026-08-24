"""ProviderRegistry 单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.providers import GeminiAdapter, OpenAICompatibleAdapter
from monitor.registry import PROVIDER_DEFS, ProviderRegistry

registry = ProviderRegistry()


def test_phase1_providers_registered():
    for name in ["openai", "deepseek", "kimi", "minimax", "gemini"]:
        assert registry.get(name) is not None, f"missing provider: {name}"


def test_openai_compatible_reuse():
    for name in ["openai", "deepseek", "kimi", "minimax"]:
        assert isinstance(registry.get(name), OpenAICompatibleAdapter)


def test_gemini_native_adapter():
    assert isinstance(registry.get("gemini"), GeminiAdapter)


def test_extension_providers_preregistered():
    for name in ["openrouter", "groq", "zhipu", "qwen", "doubao"]:
        assert registry.get(name) is not None


def test_new_provider_needs_no_core_change():
    # 新增 provider 只需注册一行 spec
    registry.register("custom-llm", {
        "kind": "openai_compat",
        "default_base_url": "https://llm.example.com",
    })
    assert registry.get("custom-llm").upstream_url(
        type("C", (), {"base_url": "", "api_key": "k"})(),
        "chat/completions",
    ) == "https://llm.example.com/v1/chat/completions"


def test_every_def_has_base_url():
    for name, spec in PROVIDER_DEFS.items():
        assert spec.get("default_base_url"), name
