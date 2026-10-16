"""运行时用例：深度探测的触发、缓存与端口状态。

策略：
- 深度探测（全量哈希，秒级到几十秒）只由显式触发或缓存过期后的
  下一次读取引发；UI 总览只读缓存结果，永不阻塞在探测上；
- 端口检查需要一次独占绑定，同样只在显式探测时附带执行；
- M1 不启动/停止任何原生进程，“运行时管理接口”即本服务 + 能力表。
"""
from __future__ import annotations

import threading
import time

from ..domain.runtime import RuntimeProbeResult
from .ports import RuntimeProbe


class RuntimeService:
    """探测编排与缓存；线程安全（探测可能被并发触发）。"""

    def __init__(self, prober: RuntimeProbe, cache_seconds: int) -> None:
        self._prober = prober
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: RuntimeProbeResult | None = None
        self._cached_at: float | None = None
        self._port_free: bool | None = None

    def peek(self) -> RuntimeProbeResult | None:
        """总览只读最近完成结果，探测锁被占时也不阻塞玩家状态和停服。"""
        return self._cached

    def cached(self) -> RuntimeProbeResult | None:
        """返回缓存结果；过期则同步重探（由显式读取触发，调用方接受耗时）。"""
        with self._lock:
            if self._cached is not None and self._fresh():
                return self._cached
        return self.probe(force=False)

    def probe(self, force: bool) -> RuntimeProbeResult:
        """执行深度探测并刷新缓存；force=True 跳过新鲜度判断。

        串行化执行：两处同时触发只允许一个在跑，后到者等结果后直接复用
        （避免对 1.1GB 资产并发做两遍哈希）。
        """
        with self._lock:
            if not force and self._cached is not None and self._fresh():
                return self._cached
            result = self._prober.deep_probe()
            self._cached = result
            self._cached_at = time.monotonic()
            self._port_free = self._prober.port_free()
            return result

    def port_free(self) -> bool | None:
        """最近一次探测时的端口状态；从未探测过则为 None（不知道就说不知道）。"""
        with self._lock:
            return self._port_free

    def _fresh(self) -> bool:
        return (
            self._cached_at is not None
            and time.monotonic() - self._cached_at < self._cache_seconds
        )
