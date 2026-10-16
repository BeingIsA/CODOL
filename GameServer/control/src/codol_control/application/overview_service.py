"""总览聚合：真实房间、连接数和非阻塞预检缓存，不创建占位状态。"""
import time
from .. import __version__
from .ports import RoomQueries
from .runtime_service import RuntimeService

class OverviewService:
    """只聚合权威查询；预检不能阻塞玩家状态。"""
    def __init__(self, rooms: RoomQueries, runtime_service: RuntimeService, boot_id: str, started_monotonic: float, matches=None):
        """组合根传入已装配用例，不维护第二份房间状态。"""
        self._rooms = rooms
        self._runtime = runtime_service
        self._matches = matches
        self._boot_id = boot_id
        self._started = started_monotonic

    def build(self):
        """返回租约统计、最近完成的预检与活动对局事实；未知事实明确标记。"""
        rooms = self._rooms.list_rooms()
        probe = self._runtime.peek()
        match_status = self._matches.status() if self._matches else {"match_processes": 0, "active": []}
        return {
            "service": {"version": __version__, "boot_id": self._boot_id,
                        "uptime_seconds": round(time.monotonic()-self._started, 1), "bind": None},
            "rooms": {"total": rooms["total"], "active": rooms["total"],
                      "max_active": rooms["max_active"], "items": rooms["items"]},
            "players": {"connected": self._rooms.connected_players(), "note": "玩家心跳超过 15 秒即显示离线。"},
            "runtime": {"probe_ready": probe.ready if probe else False,
                        "probe_checked_at": probe.checked_at if probe else None,
                        "probe_never_run": probe is None,
                        "match_processes": match_status["match_processes"],
                        "matches": match_status["active"],
                        "note": "M3：活动对局由战斗进程监督器按契约管理。"}}
