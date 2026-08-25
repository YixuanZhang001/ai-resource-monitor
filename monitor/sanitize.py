"""统一敏感信息脱敏器。

所有进入事件 / SQLite / Dashboard / 日志 / 客户端响应的 error 文本，
必须先经过 sanitize_error()，确保 API Key、Authorization、Bearer Token、
ak/sk 前缀、账号标识等敏感信息不会泄漏。

识别并脱敏（保留标识前缀，值替换为 [REDACTED]）：
- sk-xxx 类 API Key（含 sk-proj- / sk-admin- / sk-ws- / sk-api- 等变体）
- ark-xxx（火山方舟 API Key）
- ak-xxx（Kimi / 火山方舟账号标识）
- org-xxx（OpenAI 等账号标识）
- Bearer <token>
- Authorization 头内容
- URL query 中的 key / api_key / token / access_token 参数值
- 上游已打码的 key（如 "api key: ****test"）
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

# (pattern, replacement)；按序执行，互不重叠
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # API Key：sk- 前缀 + 至少 6 位 token 字符（含 . 以覆盖 sk-ws- 变体）
    (re.compile(r"\bsk-[A-Za-z0-9_\-\.]{6,}"), "sk-[REDACTED]"),
    # OpenAI 打码格式：sk-inval*******test（前段 + 星号掩码 + 尾缀）
    (re.compile(r"\bsk-[A-Za-z0-9_\-\.]{0,12}[*x]{4,}[\w\-\.]{0,12}"),
     "sk-[REDACTED]"),
    # 火山方舟 API Key
    (re.compile(r"\bark-[A-Za-z0-9\-]{6,}"), "ark-[REDACTED]"),
    # Kimi / 火山方舟账号标识
    (re.compile(r"\bak-[A-Za-z0-9\-]{6,}"), "ak-[REDACTED]"),
    # OpenAI 账号标识
    (re.compile(r"\borg-[A-Za-z0-9\-]{6,}"), "org-[REDACTED]"),
    # Bearer token（Authorization: Bearer sk-... 先被 sk- 规则命中，此处兜底其他类型 token）
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9_\-\.]{8,}"), "Bearer [REDACTED]"),
    # Authorization 头（若值是 key 类已被上规则命中，此处兜底）
    (re.compile(r"(?i)(authorization\s*[:=]\s*)[A-Za-z0-9_\-\.\s]{8,}"),
     r"\1[REDACTED]"),
    # URL query 参数值
    (re.compile(r"(?i)([?&](?:api[_-]?key|key|token|access[_-]?token)=)[^&\s\"']{4,}"),
     r"\1[REDACTED]"),
    # 上游已打码的 key（OpenAI/DeepSeek 等返回 "api key: ****test"）
    (re.compile(r"(?i)(api[ _-]?key\s*[:=]?\s*)[*x]{4,}[\w\-\.]*"), r"\1[REDACTED]"),
]


# 精确凭据键名（小写精确匹配，非子串匹配）— 命中且值为非数字即删除该键
_CREDENTIAL_KEYS = {
    "api_key", "apikey", "api-key", "api_secret", "apisecret", "secret",
    "secret_key", "secretkey", "secret-access-key", "token", "access_token",
    "accesstoken", "refresh_token", "refreshtoken", "password", "passwd",
    "pwd", "authorization", "auth", "auth_token", "cookie", "set_cookie",
    "private_key", "privatekey", "client_secret", "clientsecret",
    "credential", "credentials",
}

# 值形态：疑似凭据的字符串（sk-/ark-/Bearer/JWT/长随机/URL 含 secret 参数）
_CREDENTIAL_VALUE_RE = re.compile(
    r"(?i)("
    r"sk-[A-Za-z0-9_\-\.]{6,}"            # sk- 类 API Key
    r"|ark-[A-Za-z0-9\-]{6,}"             # 火山方舟
    r"|ak-[A-Za-z0-9\-]{6,}"              # Kimi/火山账号标识
    r"|org-[A-Za-z0-9\-]{6,}"             # OpenAI 账号标识
    r"|Bearer\s+[A-Za-z0-9_\-\.]{8,}"     # Bearer token
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"  # JWT
    r"|[A-Za-z0-9_\-]{32,}"               # 长随机串（>=32，疑似 token/secret）
    r"|[?&](?:api[_-]?key|key|token|access[_-]?token|secret)=[^&\s\"']{4,}"  # URL 含 secret 参数
    r")"
)

_DROP = object()  # 内部哨兵：表示该节点应被删除


def sanitize_usage_dict(data: Any) -> Optional[dict]:
    """清洗 Provider 返回的未知 usage 扩展字段，供落库前使用。

    决策树（严格优先级）：
      1. 数字（int/float）→ 一律保留（绝不因键名像凭据而删除，保护 *_tokens 等数值）
      2. 字符串 → 命中凭据值形态则删除（DROP），否则保留
      3. 字典/列表 → 递归
      4. 其它（None/bool）→ 原样保留
      递归后，若键名精确命中 _CREDENTIAL_KEYS 且值非数字 → 删除该键

    设计原则：绝不静默丢弃"未知但合法"的 usage 数值字段（Unknown Usage ≠ Secret）；
    绝不把凭据写入事件/数据库。
    """
    if not isinstance(data, dict):
        return None
    out: dict = {}
    for k, v in data.items():
        cleaned = _clean_node(v)
        if cleaned is _DROP:
            continue
        if (isinstance(k, str) and k.lower() in _CREDENTIAL_KEYS
                and not isinstance(cleaned, (int, float, bool))):
            continue
        out[k] = cleaned
    return out


def _clean_node(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v  # 数字永远保留
    if isinstance(v, str):
        return _DROP if _CREDENTIAL_VALUE_RE.search(v) else v
    if isinstance(v, list):
        out = []
        for x in v:
            c = _clean_node(x)
            if c is not _DROP:
                out.append(c)
        return out
    if isinstance(v, dict):
        out: dict = {}
        for k, x in v.items():
            c = _clean_node(x)
            if c is _DROP:
                continue
            if (isinstance(k, str) and k.lower() in _CREDENTIAL_KEYS
                    and not isinstance(c, (int, float, bool))):
                continue
            out[k] = c
        return out
    return v  # None / 其它类型原样


def sanitize_error(text: Optional[str]) -> Optional[str]:
    """对错误文本做统一脱敏。None 原样返回；无敏感信息时文本不变。"""
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _sanitize_value(v: Any) -> Any:
    if isinstance(v, str):
        return sanitize_error(v)
    if isinstance(v, list):
        return [_sanitize_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _sanitize_value(x) for k, x in v.items()}
    return v


def sanitize_json_text(text: str) -> str:
    """对 JSON 文本做全树字符串脱敏。

    先解析再脱敏再序列化，避免上游把 `<`/`>` 编码为 `\\u003c` 等转义序列
    时破坏正则的单词边界（如 `<ak-xxx>` → `\\u003cak-xxx\\u003e`）。
    非 JSON 文本退回纯文本脱敏。
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return sanitize_error(text)
    return json.dumps(_sanitize_value(obj), ensure_ascii=False)
