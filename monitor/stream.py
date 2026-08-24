"""Live Request Stream 订阅管理器（轻量，无外部依赖）。

- 每个 SSE 客户端一个 asyncio.Queue
- broadcast 无阻塞：无订阅者时直接返回；慢消费者丢最旧保最新
- 客户端断开通过 unsubscribe 清理，不影响 Gateway
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


class SubscriberManager:
    def __init__(self, max_queue: int = 200):
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._max_queue = max_queue

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def broadcast(self, message: dict[str, Any]) -> None:
        if not self._subscribers:
            return  # 无客户端连接时绝不阻塞请求
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                # 慢消费者：丢弃最旧，保留最新
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(message)
                except asyncio.QueueFull:
                    pass

    def __len__(self) -> int:
        return len(self._subscribers)


def sse_payload(message: dict) -> str:
    """构造 SSE data 帧（ensure_ascii=False 保留中文）。"""
    return f"data: {json.dumps(message, ensure_ascii=False)}\n\n"
