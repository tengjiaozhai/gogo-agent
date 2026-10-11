"""039 本人政策只读查询的限时缓存。"""

from __future__ import annotations

from threading import RLock
from time import monotonic
from typing import Callable, TypeVar


_Value = TypeVar("_Value")


class UserPolicyCache:
    """按认证用户、目的地、职级和政策版本隔离的短时查询结果。"""

    def __init__(self, ttl_seconds: float = 300, clock: Callable[[], float] = monotonic):
        if ttl_seconds <= 0:
            raise ValueError("缓存 TTL 必须大于零")
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = RLock()
        self._entries: dict[tuple[str, str, str, str, str], tuple[float, object]] = {}

    def get_or_load(
        self, user_id: str, city: str, level: str, version: str, as_of: str,
        loader: Callable[[], _Value],
    ) -> _Value:
        """同一版本同一用户的重复只读查询才命中。"""
        key = (user_id, city.strip(), level, version, as_of)
        with self._lock:
            current = self._entries.get(key)
            if current is not None and current[0] > self._clock():
                return current[1]  # type: ignore[return-value]
        value = loader()
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl_seconds, value)
        return value

    def invalidate_user(self, user_id: str) -> None:
        """档案职级或政策适用条件变化后，删除该用户全部缓存项。"""
        with self._lock:
            for key in [key for key in self._entries if key[0] == user_id]:
                self._entries.pop(key, None)

    def clear(self) -> None:
        """政策语料版本整体替换时清除旧项。"""
        with self._lock:
            self._entries.clear()
