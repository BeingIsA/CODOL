"""M2 用例：开发身份、会话租约、权威建房、Bot 配置和重连同步。

依赖仅为仓库、时钟和随机凭据工厂。所有写命令以身份+request_id 幂等，
同 ID 不同参数拒绝；事务中同时落房间、事件与原结果，不依赖内存房间字典。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .ports import Clock, OnlineRepository
from ..domain.errors import ForbiddenError, InvalidArgumentError, InvalidStateError, UnauthenticatedError
from ..domain.room_rules import (CapacityError, close_room, configure_bots, is_abandoned,
                                 leave_room, validate_content)


def digest(value: str) -> str:
    """把凭据变成不可逆索引；日志和持久化层都不需要明文令牌。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PlayerRooms:
    """玩家与房间用例的单一入口；会话状态与业务事务拥有同一边界。"""

    def __init__(self, repository: OnlineRepository, clock: Clock, random_hex, development_secret: str,
                 supported_maps=frozenset({"mp_cross_fire"}), reap_timeout: float = 90.0):
        """组合根注入实现；development_secret 仅用于明确标记的本机开发登录。

        reap_timeout 是一个房间的全部成员多久没有心跳后判定被遗弃（秒）。
        """
        self._repository = repository
        self._clock = clock
        self._random = random_hex
        self._development_secret = development_secret
        self._supported_maps = frozenset(supported_maps)
        self._reap_timeout = float(reap_timeout)

    def login(self, access: str, identity: str, name: str, protocol: int) -> dict:
        """安装级开发凭据+客户端私有凭据换取短期会话；重连保持原生 ID。"""
        import hmac

        if not hmac.compare_digest(access, self._development_secret):
            raise UnauthenticatedError("本机开发接入凭据无效。")
        if protocol != 1:
            raise InvalidArgumentError("玩家协议主版本不兼容，需要版本 1。")
        if not 32 <= len(identity) <= 128 or not name or len(name.encode("utf-8")) > 24:
            raise InvalidArgumentError("开发身份或显示名不合法。")
        try:
            name.encode("gbk")
        except UnicodeEncodeError as exc:
            raise InvalidArgumentError("当前客户端显示名需要支持 GBK。") from exc
        if any(ord(c) < 32 or c in '\\";^' for c in name):
            raise InvalidArgumentError("显示名包含不允许的字符。")
        now = self._clock()
        token = self._random(32)
        with self._repository.transaction() as u:
            key = digest(identity)
            player = u.player(key)
            if player is None:
                player = {"id": self._random(16), "native_id": (1 << 32) + int(self._random(4), 16),
                          "display_name": name}
                u.save_player(key, player)
            u.revoke_player(player["id"])
            u.save_session(digest(token), {"player_id": player["id"], "expires_at": now + 3600,
                                          "last_seen": now, "revoked": 0})
        return {"token": token, "player": player, "protocol_version": 1,
                "expires_at": now + 3600, "heartbeat_seconds": 3, "offline_after_seconds": 15,
                "development_only": True}

    def _session(self, u, token: str, touch=False):
        """校验玩家令牌；订阅读取不刷新心跳，避免僵死宿主被假定在线。"""
        row = u.session(digest(token))
        now = self._clock()
        if row is None or row["revoked"] or row["expires_at"] <= now:
            raise UnauthenticatedError("玩家会话已失效，请重新连接。")
        if touch:
            row["last_seen"] = now
            u.save_session(digest(token), row)
        return row

    def heartbeat(self, token: str) -> dict:
        """明确刷新租约，返回当前服务时间。"""
        with self._repository.transaction() as u:
            self._session(u, token, touch=True)
        return {"server_time": self._clock(), "online": True}

    def disconnect(self, token: str) -> dict:
        """主动退出立即撤销会话；网络失联则由租约超时显示离线。"""
        with self._repository.transaction() as u:
            row = self._session(u, token)
            row["revoked"] = 1
            u.save_session(digest(token), row)
        return {"disconnected": True}

    def _command(self, actor, request_id, intent, operation):
        """在单事务中完成幂等查询、规则执行、事件与结果持久化。"""
        if not 1 <= len(request_id) <= 96:
            raise InvalidArgumentError("request_id 长度需要为 1～96。")
        fingerprint = digest(json.dumps(intent, ensure_ascii=False, sort_keys=True))
        with self._repository.transaction() as u:
            identity = self._session(u, actor)["player_id"] if actor != "admin" else "admin"
            receipt = u.receipt(identity, request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise InvalidArgumentError("同一 request_id 不可用于不同操作或参数。")
                return receipt["result"]
            room = operation(u, identity)
            stored = next((r for r in u.rooms() if r["id"] == room.get("id")), None)
            if stored is not None and stored.get("revision") == room.get("revision"):
                # 幂等重放后没有新版本（例如原生界面连发的重复离开意图）：
                # 不刷新时间戳、不重复发事件，事件流与版本号保持一一对应。
                result = {"snapshot": stored, "event_id": 0}
                u.save_receipt(identity, request_id, {"fingerprint": fingerprint, "result": result})
                return result
            room["updated_at"] = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()
            u.save_room(room)
            event_id = u.emit(room, self._clock())
            result = {"snapshot": room, "event_id": event_id}
            u.save_receipt(identity, request_id, {"fingerprint": fingerprint, "result": result})
            return result

    def create(self, token: str, request_id: str, map_name: str, gametype: str) -> dict:
        """真实玩家请求建房；当前只允许一个活动房间，断线重连复用原房间。"""
        validate_content(map_name, gametype, self._supported_maps)

        def operation(u, player_id):
            """在事务内核对容量，身份字段完全来自会话。"""
            active = [r for r in u.rooms() if r["state"] != "CLOSED"]
            if active:
                raise CapacityError("当前已有活动房间，请离开原房间后再创建。")
            session = self._session(u, token)
            at = datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()
            return {"id": self._random(12), "name": session["display_name"] + "的房间", "state": "OPEN",
                    "revision": 1, "owner_id": player_id, "map": map_name, "gametype": gametype,
                    "created_at": at, "updated_at": at,
                    "members": [{"player_id": player_id, "native_id": session["native_id"],
                                 "display_name": session["display_name"], "team": 2, "ready": False}],
                    "bots": {"count": 0, "team": 1, "difficulty": "normal"}}

        return self._command(token, request_id, {"type": "CreateRoom", "map": map_name, "gametype": gametype}, operation)

    def bots(self, room_id: str, request_id: str, revision: int, count: int, team: int, difficulty: str) -> dict:
        """管理员配置 Bot 席位；HTTP 层负责独立管理员认证。"""
        def operation(u, _):
            """读到的快照版本必须与请求匹配。"""
            room = next((r for r in u.rooms() if r["id"] == room_id), None)
            if room is None:
                raise InvalidArgumentError("房间不存在。")
            return configure_bots(room, count, team, difficulty, revision)

        return self._command("admin", request_id, {"type": "ConfigureBots", "room_id": room_id,
                             "revision": revision, "count": count, "team": team, "difficulty": difficulty}, operation)

    def leave(self, token: str, request_id: str, room_id: str) -> dict:
        """成员离开房间：只移除本人席位；房间没人了才关闭。

        与 M2 的差别只有一条，但它决定多人能否成立：离开不再是“关掉整个房间”。
        单人房间里两种解释的结果相同（人走房空），所以 M2/M3 的既有行为不变；
        多人房间里，一个人退出不再把其他人一起踢出。
        """
        def operation(u, player_id):
            """规则在 domain 层：这里只负责取权威快照与调用纯函数。"""
            room = next((r for r in u.rooms() if r["id"] == room_id), None)
            if room is None:
                raise InvalidArgumentError("房间不存在。")
            return leave_room(room, player_id)

        return self._command(token, request_id, {"type": "LeaveRoom", "room_id": room_id}, operation)

    def close_by_admin(self, room_id: str, request_id: str) -> dict:
        """管理员强制关闭房间：清空全部成员，房间进入 CLOSED。

        房间正在对局时，进程侧的优雅停止由 AdminRooms 在本方法之前完成
        （先让客户端离场再关房）；本方法只负责权威快照这一个事务，
        不在同一事务里等待子进程。
        """
        def operation(u, _):
            """规则在 domain 层：这里只负责取权威快照与调用纯函数。"""
            room = next((r for r in u.rooms() if r["id"] == room_id), None)
            if room is None:
                raise InvalidArgumentError("房间不存在。")
            return close_room(room)

        return self._command("admin", request_id, {"type": "CloseRoom", "room_id": room_id}, operation)

    def change(self, token: str, request_id: str, room_id: str, revision: int, *, map_name=None, team=None) -> dict:
        """房主选图、成员改本人阵营；版本与权限在同一事务核对，不先改本地 UI。"""
        from ..domain.room_rules import RevisionConflict

        def operation(u, player_id):
            """新快照仍是唯一权威，任何配置变更都清除旧准备状态。"""
            room = next((r for r in u.rooms() if r["id"] == room_id), None)
            if room is None or not any(p["player_id"] == player_id for p in room["members"]):
                raise ForbiddenError("无权修改这个房间。")
            if room["revision"] != revision:
                raise RevisionConflict("房间已更新，请同步后重试。")
            if room["state"] != "OPEN":
                raise InvalidStateError("当前房间不可修改。")
            if map_name is not None:
                if room["owner_id"] != player_id:
                    raise ForbiddenError("只有房主可以选图。")
                validate_content(map_name, room["gametype"], self._supported_maps)
                room["map"] = map_name
            if team is not None and team not in (1, 2):
                raise InvalidArgumentError("阵营必须是特遣队或暗影军团。")
            room["members"] = [{**p, "ready": False,
                                "team": team if team is not None and p["player_id"] == player_id else p["team"]}
                               for p in room["members"]]
            return {**room, "revision": revision + 1}

        return self._command(token, request_id, {"type": "ChangeRoom", "room_id": room_id,
                             "revision": revision, "map": map_name, "team": team}, operation)

    def sync(self, token: str, after: int = 0) -> dict:
        """重连始终返回完整权威快照，同时提供有限事件重放与单调游标。"""
        with self._repository.transaction() as u:
            row = self._session(u, token)
            room = next((r for r in u.rooms() if r["state"] != "CLOSED" and
                         any(p["player_id"] == row["player_id"] for p in r["members"])), None)
            events = u.events(room["id"], max(0, after)) if room else []
            return {"snapshot": room, "events": events, "cursor": events[-1]["event_id"] if events else after,
                    "server_time": self._clock(), "protocol_version": 1}

    def list_rooms(self) -> dict:
        """管理侧查询即时租约状态；离线玩家仍保留成员身份，明确显示离线。"""
        with self._repository.transaction() as u:
            rooms = [r for r in u.rooms() if r["state"] != "CLOSED"]
            for room in rooms:
                room["members"] = [{**p, "online": u.online(p["player_id"], self._clock())} for p in room["members"]]
            return {"total": len(rooms), "max_active": 1, "create_enabled": False, "items": rooms,
                    "reap_timeout_seconds": self._reap_timeout,
                    "note": "通过客户端原生大厅建房；Bot 当前是配置席位，尚未生成战斗实体。"}

    def reap_abandoned(self, timeout: float | None = None) -> list[str]:
        """关闭"所有成员都已没有心跳"的 OPEN 房间，返回被关闭的房间 id。

        这是客户端异常退出（崩溃、被强杀、切枪闪退）的兜底路径：玩家没发来
        "离开"，席位就一直留在房间里，房间继续占用唯一的房间名额，新客户端
        建房会被 ROOM_FULL 拒绝。这里按会话心跳判定房间已经没人并关闭它。

        只读判定在一个事务里完成，关闭逐个走 close_by_admin（各自一个事务），
        避免在同一事务里嵌套事务。
        """
        limit = self._reap_timeout if timeout is None else float(timeout)
        now = self._clock()
        with self._repository.transaction() as u:
            targets = []
            for room in u.rooms():
                if room["state"] == "CLOSED":
                    continue
                stamps = {p["player_id"]: u.last_seen(p["player_id"]) for p in room["members"]}
                if is_abandoned(room, stamps, now, limit):
                    targets.append((room["id"], room["revision"]))
        closed = []
        for room_id, revision in targets:
            # 幂等编号带房间版本：同一版本只关一次，后续周期拿到的是同一回执，
            # 不重复发事件、不前进版本号。
            self.close_by_admin(room_id, f"reap:{room_id}:{revision}")
            closed.append(room_id)
        return closed

    def connected_players(self) -> int:
        """统计仍持有效心跳租约的独立玩家，包括尚未建房的大厅玩家。"""
        with self._repository.transaction() as u:
            return u.connected_players(self._clock())
