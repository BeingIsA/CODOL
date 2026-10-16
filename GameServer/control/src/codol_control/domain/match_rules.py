"""M3 对局领域规则：状态机词汇、战斗内容校验与配置冻结。

纯规则、无 IO：不导入数据库、框架、Win32 或原生地址。
对局（match）是一次独立战斗，房间（room）是可复用容器；
两者状态转换的合法路径与失败恢复规则都定义在这里，
应用层只负责按规则推进并持久化。
"""
from __future__ import annotations

from .errors import InvalidArgumentError, InvalidStateError

# 对局状态机（方案 §6）：ALLOCATING 已入库未启动；LOADING 进程启动待就绪；
# READY 运行时就绪可入场；RUNNING 真人已入场；FINISHING 结束中（停止进程）。
MATCH_ACTIVE_STATES = ("ALLOCATING", "LOADING", "READY", "RUNNING", "FINISHING")
MATCH_TERMINAL_STATES = ("FINISHED", "FAILED", "CANCELLED")
# 允许取消的阶段：入场前后都允许管理员终止，但语义不同（CANCELLED）。
MATCH_CANCELLABLE = ("ALLOCATING", "LOADING", "READY", "RUNNING")

# 房间状态（0001 迁移枚举）：OPEN 可修改配置；STARTING 开局进行中；
# IN_MATCH 对局进行中；CLOSED 已关闭。
ROOM_MATCH_STATES = ("STARTING", "IN_MATCH")


class BattleContentError(InvalidArgumentError):
    """房间配置的地图/模式未通过战斗验证白名单。"""


def validate_battle_content(map_name: str, gametype: str, verified: list[dict]) -> None:
    """校验开局内容必须是已验证可战斗的组合。

    房间目录（room-content-v1 的 room_maps）允许配置 25 张地图，
    但只有 battle_verified 列表内的组合被真实战斗进程加载过；
    用未验证组合开局必须明确拒绝，不能静默降级到默认地图。
    """
    if not any(entry["map"] == map_name and entry["gametype"] == gametype for entry in verified):
        allowed = ", ".join(sorted(f'{e["map"]}/{e["gametype"]}' for e in verified))
        raise BattleContentError(f"地图 {map_name}（{gametype}）尚未通过战斗验证；当前仅支持：{allowed}。")


def freeze_match_config(room: dict, now: str) -> dict:
    """从房间快照冻结本局配置；此后房间配置不再影响已启动的对局。

    M3 不注入 Bot 实体：房间 Bot 名额仅复制到冻结配置留档，
    战斗进程的 botfill/botmax 固定为 0（实体生成属 M4）。
    """
    return {
        "map": room["map"],
        "gametype": room["gametype"],
        "max_clients": 12,
        "players": [{"player_id": p["player_id"], "native_id": p["native_id"],
                     "display_name": p["display_name"], "team": p["team"]} for p in room["members"]],
        "bot_seats": dict(room.get("bots", {"count": 0})),
        "frozen_at": now,
    }


def start_match(room: dict, verified: list[dict], now: str) -> tuple[dict, dict]:
    """开局前置校验并生成（房间下一快照, 冻结配置）。

    校验：房间存在且 OPEN、无成员离线限制（M3 单真人以房间存在为准）、
    内容在战斗白名单内。返回的房间快照进入 STARTING 并清准备位。
    """
    if room["state"] != "OPEN":
        raise InvalidStateError("当前房间状态不可开局。")
    validate_battle_content(room["map"], room["gametype"], verified)
    config = freeze_match_config(room, now)
    members = [{**p, "ready": False} for p in room["members"]]
    return {**room, "state": "STARTING", "revision": room["revision"] + 1, "members": members}, config


def room_is_closed(room: dict) -> bool:
    """房间是否已因最后一名成员离场而关闭。

    对局推进函数必须先用它做闸门：房间关闭是成员离场的最终结果，
    任何“对局结束恢复房间”“对局就绪写入入场信息”都不能把它变回可用，
    否则会凭空复活一个没有成员的空房间（M3 原生按钮验收实测到的悬空房间）。
    """
    return room.get("state") == "CLOSED"


def match_entered_room(match: dict, room: dict, now: str) -> dict:
    """运行时就绪（READY）：房间进入 IN_MATCH，快照携带对局入场信息。

    入场信息是 M3 凭据链的服务端部分：谁能入场（成员 native_id）、
    进哪个对局（match id）、连哪里（回环端口）。
    """
    if room_is_closed(room):
        return room
    entry = {"host": "127.0.0.1", "port": match["port"], "map": match["config"]["map"],
             "gametype": match["config"]["gametype"]}
    return {**room, "state": "IN_MATCH", "revision": room["revision"] + 1,
            "members": [{**p, "ready": False} for p in room["members"]],
            "match": _room_match_view(match, "READY", entry)}


def match_started_room(match: dict, room: dict) -> dict:
    """玩家真实入场（RUNNING）：房间快照的对局状态字段更新。"""
    if room_is_closed(room):
        return room
    return {**room, "revision": room["revision"] + 1,
            "match": _room_match_view(match, "RUNNING", room.get("match", {}).get("entry"))}


def match_settled_room(room: dict) -> dict:
    """对局结束（FINISHED/FAILED/CANCELLED）：房间恢复 OPEN 并清除对局字段。

    房间已经关闭时原样返回：对局收尾只能让“还有人”的房间回到可开局状态。
    """
    if room_is_closed(room):
        return room
    settled = {k: v for k, v in room.items() if k != "match"}
    return {**settled, "state": "OPEN", "revision": room["revision"] + 1,
            "members": [{**p, "ready": False} for p in room["members"]]}


def _room_match_view(match: dict, state: str, entry: dict | None) -> dict:
    """房间快照内嵌的对局视图；只携带客户端入场需要的事实，不含运行目录等内部字段。"""
    view = {"id": match["id"], "state": state, "map": match["config"]["map"],
            "gametype": match["config"]["gametype"]}
    if entry:
        view["entry"] = entry
    return view


def advance_failure(match: dict) -> dict:
    """失败推进：任何未终结状态都可转 FAILED，附失败原因。"""
    if match["state"] in MATCH_TERMINAL_STATES:
        raise InvalidStateError("对局已结束，不可再推进状态。")
    return {**match, "state": "FAILED"}


def validate_transition(match: dict, target: str) -> None:
    """显式校验状态转换合法性，供应用层推进前调用。

    合法路径：ALLOCATING→LOADING→READY→RUNNING→FINISHING→FINISHED；
    任意未终结状态可转 FAILED；可取消状态可转 CANCELLED。
    """
    allowed = {
        "ALLOCATING": {"LOADING", "FAILED", "CANCELLED"},
        "LOADING": {"READY", "FAILED", "CANCELLED"},
        "READY": {"RUNNING", "FAILED", "CANCELLED"},
        "RUNNING": {"FINISHING", "FAILED", "CANCELLED"},
        "FINISHING": {"FINISHED", "FAILED"},
    }
    if match["state"] in MATCH_TERMINAL_STATES:
        raise InvalidStateError("对局已处于终态。")
    if target not in allowed.get(match["state"], set()):
        raise InvalidStateError(f"对局状态 {match['state']} 不可直接推进到 {target}。")
