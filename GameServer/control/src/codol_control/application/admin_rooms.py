"""管理端房间处置：关闭房间（含对局中房间的"先停局、后关房"）。

这个用例只做编排，不重复实现规则：
- 房间快照的权威写入与幂等回执在 PlayerRooms.close_by_admin（事务内）；
- 战斗进程的优雅停止在 MatchService.stop_room_match（事务外，可能等待）；
- 本模块决定两者的顺序，并保证两条命令的幂等编号互不覆盖。

顺序为什么重要：直接把房间置为 CLOSED 而不停对局，客户端等不到
endmatch，只能干等 UDP 超时弹 EXE_ERR_SERVER_TIMEOUT（M3 实测教训）；
反过来先关房再停局，对局会挂在一个已经没有成员的房间上，观察线程
只能按"房间已关闭"把它判成异常结束。
"""
from __future__ import annotations

# 停局命令的幂等等编号后缀：两条命令都以 admin 身份登记回执，
# 共用一个 request_id 会被"同一编号不可用于不同操作"拒绝。
MATCH_REQUEST_SUFFIX = ":match-stop"


class AdminRooms:
    """管理页房间处置入口；HTTP 层负责认证，本层只负责语义与顺序。"""

    def __init__(self, player_rooms, matches=None):
        """matches 可为空（未装配对局用例时关闭房间只处理房间快照）。"""
        self._player_rooms = player_rooms
        self._matches = matches

    def close(self, room_id: str, request_id: str, reason: str = "管理员关闭房间") -> dict:
        """关闭房间：先优雅停止本房间的活动对局，再清空成员并置为 CLOSED。

        返回关房结果（snapshot/event_id）并附带本轮停局结果；没有活动对局
        时 match 为 None。对已关闭房间重复调用是幂等的。
        """
        stopped = None
        if self._matches is not None:
            stopped = self._matches.stop_room_match(room_id, request_id + MATCH_REQUEST_SUFFIX, reason)
        result = self._player_rooms.close_by_admin(room_id, request_id)
        return {**result, "match": stopped}
