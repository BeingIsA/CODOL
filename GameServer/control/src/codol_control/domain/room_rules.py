"""M2 房间纯规则：只接收意图与快照，不接触数据库、网络或版本地址。

当前仅开放已验证的穿越火线/团队竞技，12 个席位。Bot 是配置席位，
并未生成战斗实体；所有准备状态在配置变化后失效。
"""
from __future__ import annotations

from .errors import ControlError, ForbiddenError, InvalidArgumentError, InvalidStateError, UnsupportedContentError


class RevisionConflict(ControlError):
    """调用者持有过期快照，必须重新同步后决定是否重试。"""

    code = "REVISION_CONFLICT"
    http_status = 409


class CapacityError(ControlError):
    """房间或成员数量达到本阶段允许的容量。"""

    code = "ROOM_FULL"
    http_status = 409


def validate_content(map_name: str, gametype: str, maps=frozenset({"mp_cross_fire"})) -> None:
    """只放行当前完成资源与模式验证的组合，客户端不能扩展白名单。"""
    if map_name not in maps or gametype != "war":
        raise UnsupportedContentError("房间地图不在当前安装目录中，或模式不是战术团队竞技。")


def leave_room(room: dict, player_id: str) -> dict:
    """成员离开房间：只移除本人席位，不因单人离场而抢走其他人的房间。

    规则（为多人联机而定，单人是其退化情形）：
    - 任何成员都能离开，不要求房主身份，也不要求房间处于 OPEN：
      玩家在对局中点击原生“返回大厅”就是一次离开，此时房间状态是 IN_MATCH。
    - 移除后仍有成员：房间与其对局状态原样保留；离开的是房主时移交房主，
      否则会留下一个没有房主、谁也开局不了的房间。
    - 移除后没有成员：房间关闭（CLOSED）。此时清掉对局视图，房间不再参与
      任何对局投影；正在跑的战斗进程由对局观察线程按入场人数收敛。
    - 对已关闭房间重复调用是幂等的：原生界面可能连发多次离开意图
      （实测一次点击产生 3 条），重复请求不能变成错误打扰用户。
    """
    if room["state"] == "CLOSED":
        return room
    if not any(p["player_id"] == player_id for p in room["members"]):
        raise ForbiddenError("你不在这个房间里。")
    members = [p for p in room["members"] if p["player_id"] != player_id]
    if not members:
        # 最后一名成员离场：房间归零，连同对局视图一起清空。
        # 保留历史行不删除，事件与幂等结果仍然可追溯。
        without_match = {k: v for k, v in room.items() if k != "match"}
        return {**without_match, "state": "CLOSED", "revision": room["revision"] + 1, "members": []}
    # 房主离开但房内还有别人：把房主顺位交给仍在房间的第一名成员。
    owner_id = room["owner_id"] if any(p["player_id"] == room["owner_id"] for p in members) \
        else members[0]["player_id"]
    return {**room, "owner_id": owner_id, "revision": room["revision"] + 1, "members": members}


def close_room(room: dict) -> dict:
    """管理员强制关闭房间：清空全部成员并进入 CLOSED。

    与 leave_room 的区别只有一点，但它决定了这个入口能做什么：
    leave 是成员处置自己的席位，close 是管理端处置整个房间——
    房内所有成员（在线或离线）同时失去席位，房间立即不可用。

    - 任何状态下都能关闭，包括 OPEN 与对局中的 IN_MATCH/STARTING：
      管理员关闭不要求房间空闲，也不要求房间处于某个特定阶段。
    - 关闭后清掉对局视图：房间不再参与任何对局投影，战斗进程由对局侧
      按入场人数收敛（关闭房间本身不直接杀进程，由用例层先优雅停局）。
    - 已关闭房间重复调用是幂等的：管理页连点、网络重试都不能变成错误。
    """
    if room["state"] == "CLOSED":
        return room
    without_match = {k: v for k, v in room.items() if k != "match"}
    return {**without_match, "state": "CLOSED", "revision": room["revision"] + 1, "members": []}


def is_abandoned(room: dict, last_seen: dict[str, float], now: float, timeout: float) -> bool:
    """房间是否已被遗弃：所有成员的席位都超过 timeout 秒没有任何心跳。

    last_seen 由调用方从会话记录取（player_id → 最后心跳时间戳），
    本函数不接触数据库。

    为什么只看 OPEN 房间：对局中的房间（STARTING/IN_MATCH）由对局观察线程
    按入场人数收敛——客户端崩了，战斗进程 humans 归零，观察线程会结束对局
    并把房间恢复 OPEN，随后才轮到回收。两套规则各管一段，避免同一时刻
    两个执行者抢着关同一个房间。

    为什么超时而不是"一离线就关"：心跳 3 秒一次、离线判定 15 秒，客户端
    崩溃后玩家可能马上重启客户端，重连要能回到原房间（M2 的恢复语义）。
    阈值必须明显大于一次重启的时间，否则回收会把重连恢复一起干掉。
    """
    if room["state"] != "OPEN":
        return False
    if not room["members"]:
        return True
    return all(now - last_seen.get(p["player_id"], 0.0) > timeout for p in room["members"])


def configure_bots(room: dict, count: int, team: int, difficulty: str, revision: int) -> dict:
    """返回新配置；版本/状态/容量全部核对后才产生下一版快照。"""
    if room["revision"] != revision:
        raise RevisionConflict("房间已更新，请刷新后重试。")
    if room["state"] != "OPEN":
        raise InvalidStateError("当前房间不可配置。")
    if type(count) is not int or not 0 <= count <= 11:
        raise CapacityError("Bot 数量必须为 0～11，总席位不能超过 12。")
    if team not in (1, 2) or difficulty not in ("easy", "normal", "hard"):
        raise InvalidArgumentError("Bot 阵营或难度不支持。")
    return {**room, "revision": room["revision"] + 1,
            "bots": {"count": count, "team": team, "difficulty": difficulty},
            "members": [{**p, "ready": False} for p in room["members"]]}
