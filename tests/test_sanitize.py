"""Sanitizer 单元测试：错误文本中的敏感信息必须被统一脱敏，普通文本不受影响。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.sanitize import sanitize_error, sanitize_json_text  # noqa: E402


class TestSanitize:
    def test_sk_prefix_key(self):
        out = sanitize_error("Your api key sk-FAKE-NOT-A-REAL-KEY-0000 is invalid")
        assert "sk-FAKE-NOT-A-REAL-KEY-0000" not in out
        assert "sk-[REDACTED]" in out

    def test_sk_variants(self):
        for k in ("sk-FAKE-PROJ-EXAMPLE-NOT-REAL", "sk-ws-H.EPHRDEH.EPhm.ME",
                  "sk-api-v9VOtJ63LoVhWZnsJ", "sk-admin-DPeS2Bi6VeMiY"):
            out = sanitize_error(f"key {k} bad")
            assert k not in out
            assert "sk-[REDACTED]" in out, k

    def test_ark_prefix(self):
        out = sanitize_error("ark-0b345d5c-6417-44db-bf0e-52f0823e703a-29be6 denied")
        assert "ark-0b345d5c-6417" not in out
        assert "ark-[REDACTED]" in out

    def test_ak_account_id(self):
        out = sanitize_error("account <ak-fc3zspa4ezki11bcdut1> suspended")
        assert "ak-fc3zspa4ezki11bcdut1" not in out
        assert "ak-[REDACTED]" in out

    def test_org_account_id(self):
        out = sanitize_error("org-fd100b4b24bb4c5fb86d5699ce20abd9 suspended")
        assert "org-fd100b4b24bb4c5fb86d5699ce20abd9" not in out
        assert "org-[REDACTED]" in out

    def test_bearer_token(self):
        out = sanitize_error("Authorization: Bearer abc123XYZ-_token")
        assert "abc123XYZ-_token" not in out
        assert "[REDACTED]" in out
        # 纯 Bearer 行（无 Authorization 前缀）也脱敏
        out2 = sanitize_error("Bearer abc123XYZ-_token")
        assert "abc123XYZ-_token" not in out2
        assert "Bearer [REDACTED]" in out2

    def test_authorization_header(self):
        out = sanitize_error("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in out

    def test_url_query_key(self):
        out = sanitize_error("upstream 连接失败: https://x.com/v1?key=sk-leak123456")
        assert "sk-leak123456" not in out
        out2 = sanitize_error("?api_key=SECRETVALUE99&model=x")
        assert "SECRETVALUE99" not in out2
        out3 = sanitize_error("?access_token=TOK12345")
        assert "TOK12345" not in out3

    def test_upstream_masked_key(self):
        out = sanitize_error("Authentication Fails, Your api key: ****test is invalid")
        assert "****test" not in out
        assert "[REDACTED]" in out

    def test_openai_masked_key_format(self):
        """OpenAI 错误格式：Incorrect API key provided: sk-inval*******test"""
        out = sanitize_error(
            "Incorrect API key provided: sk-inval*******test. You can find "
            "this key at https://platform.openai.com/api-keys.")
        assert "sk-inval" not in out and "test" not in out.split("sk-")[-1]
        assert "sk-[REDACTED]" in out

    def test_normal_text_untouched(self):
        samples = [
            "HTTP 429",
            "insufficient balance (1008)",
            "Invalid Authentication",
            "model not found: doubao-seed-1-6",
            "upstream 连接失败: ConnectTimeout",
            "The server had an error processing your request",
        ]
        for s in samples:
            assert sanitize_error(s) == s, s

    def test_none_and_empty(self):
        assert sanitize_error(None) is None
        assert sanitize_error("") == ""

    def test_combined(self):
        out = sanitize_error(
            "api_key=sk-leak111111 Authorization: Bearer sk-leak111111 "
            "ak-abcdef12345 org-abcdef12345")
        assert "sk-leak111111" not in out
        assert "ak-abcdef12345" not in out
        assert "org-abcdef12345" not in out

    def test_json_text_with_escaped_angle(self):
        """上游 JSON 把 < > 编码为 \\u003c \\u003e 时，ak-/org- 前缀仍须脱敏
        （\u003c 结尾字符是 c，会破坏正则单词边界，须先解码再脱敏）。"""
        text = ('{"error": {"message": "Your account org-fd100b4b24bb4c5fb86d'
                "5699ce20abd9 \\u003cak-fc3zspa4ezki11bcdut1\\u003e is "
                'suspended"}}')
        out = sanitize_json_text(text)
        assert "org-fd100b4b24bb4c5fb86d5699ce20abd9" not in out
        assert "ak-fc3zspa4ezki11bcdut1" not in out
        assert "org-[REDACTED]" in out and "ak-[REDACTED]" in out
        # 输出仍是合法 JSON
        obj = json.loads(out)
        assert "[REDACTED]" in obj["error"]["message"]

    def test_json_text_non_json_fallback(self):
        assert "sk-abc12345" not in sanitize_json_text("boom sk-abc12345")
        assert sanitize_json_text("plain 429") == "plain 429"
