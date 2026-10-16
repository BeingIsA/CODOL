"""无人房间回收：周期性关闭"所有成员都已没有心跳"的房间。

为什么要单独一个线程：
- 客户端正常退出会发 leave，房间自然关闭；但崩溃、被强杀、闪退都不会，
  席位会一直留在房间里，而当前只允许一个活动房间——下一个客户端建房会
  被 ROOM_FULL 直接拒绝，且管理页上只显示"成员离线"，没有任何东西会清理它。
- 判定必须由服务端做（ADR 0006 §4）：客户端只看到自己，判断不了"房间还有
  没有人"。
- 判定规则在 domain（room_rules.is_abandoned），关闭动作复用 PlayerRooms
  的同一条事务与事件路径；本模块只负责"多久检查一次"和"异常不死线程"。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# 遗弃判定阈值（秒）：全员多久没有心跳就认定房间没人了。
# 心跳 3 秒一次、离线判定 15 秒；90 秒远大于一次"崩溃→重启客户端→重连"
# 的时间，因此不会吃掉 M2 的断线重连恢复，又能让挂死的房间及时让位。
ABANDONED_TIMEOUT_SECONDS = 90.0
# 检查周期（秒）：回收不需要实时，15 秒一次足够，也不给 SQLite 加负担。
REAP_INTERVAL_SECONDS = 15.0


class RoomReaper:
    """后台回收线程；服务停服时显式停止，不依赖守护线程的生死。"""

    def __init__(self, player_rooms, interval: float = REAP_INTERVAL_SECONDS,
                 timeout: float | None = ABANDONED_TIMEOUT_SECONDS):
        """interval 为检查周期（秒）；timeout 为遗弃判定阈值，None 表示用用例默认值。"""
        self._player_rooms = player_rooms
        self._interval = interval
        self._timeout = timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动回收线程；重复调用无效果。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="room-reaper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止回收线程；等待不超过一个周期，避免拖住停服收尾。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self._interval + 5)
            self._thread = None

    def _run(self) -> None:
        """周期检查；异常只记不退出，回收线程不允许静默消失。"""
        while not self._stop_event.wait(self._interval):
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001 - 下一周期重试，不允许线程消失
                logger.exception("无人房间回收失败，下一周期重试")

    def reap_once(self) -> list[str]:
        """执行一次回收，返回本次关闭的房间 id。"""
        closed = self._player_rooms.reap_abandoned(self._timeout)
        if closed:
            logger.info("回收无人房间 %d 个：%s", len(closed), ", ".join(closed))
        return closed
