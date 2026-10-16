"""M2/M3 有界本地消息：网络线程处理意图，原生安全队列消费原子投影。

每次运行独占 nonce 与序号；所有网络重试保持 request_id，不写游戏内存。
房间投影升级为 M2S2 / 212 字节，尾部增加准备锁定位，旧 DLL 拒绝新格式。
kind 5 开局、kind 6 取消；对局仍经独立 m3-match.bin 投影。
"""
import json
import secrets
import struct
import threading
import time
import queue
from ..online.channel import ChannelError

REQUEST = struct.Struct("<4s16sIIII64s16s")
PROJECTION = struct.Struct("<4s16sIIIIQIIIII64s64s16sI")
# M3 对局投影：magic/nonce/序号/状态/主机(网络序IPv4)/端口/期望UID/地图/模式。
# 状态：0 空闲 1 准备中 2 可入场 3 进行中 4 已结束 5 失败 6 已取消。
MATCH = struct.Struct("<4s16sIIIIQ64s16s")
MATCH_STATES = {"": 0, "ALLOCATING": 1, "LOADING": 1, "READY": 2, "RUNNING": 3,
                "FINISHED": 4, "FAILED": 5, "CANCELLED": 4}


class RoomMailbox:
    """单次宿主拥有的邮箱工作者；没有模块级连接或共享房间状态。"""
    def __init__(self, channel, stage, run):
        """初始化同会话投影，DLL 加载前即可获得合法身份与 nonce。"""
        self.channel, self.stage, self.run = channel, stage, run
        self.nonce = secrets.token_bytes(16)
        self.sequence, self.status, self.online, self.room = 0, 1, True, None
        self.match = None  # 服务端房间快照的 match 字段；None 表示无对局。
        self._started_match_id = None  # 取消绑定本次开局，不能误取消之后的新对局。
        self._match_state = 0
        self._stop = threading.Event()
        self._thread = None
        self._seen_revision = -1
        self._cursor = 0
        self._evidence = {"protocol_version": 1, "native_id": channel.player["native_id"], "events": []}
        self._write()

    def _write(self):
        """先写 pending 再原子替换，原生只会读取完整消息。"""
        room = self.room or {}
        bots = room.get("bots", {"count": 0, "team": 1, "difficulty": "normal"})
        raw = PROJECTION.pack(b"M2S2", self.nonce, self.sequence, self.status, int(self.online),
                              int(self.room is not None), self.channel.player["native_id"], room.get("revision", 0),
                              bots["count"], bots["team"], ("easy", "normal", "hard").index(bots["difficulty"]),
                              room.get("members", [{"team": 2}])[0]["team"],
                              self.channel.player["display_name"].encode("utf-8"), room.get("map", "mp_cross_fire").encode("ascii"),
                              room.get("gametype", "war").encode("ascii"),
                              int(room.get("state") in ("STARTING", "IN_MATCH")))
        pending = self.stage / "m2-projection.pending"
        pending.write_bytes(raw)
        pending.replace(self.stage / "m2-projection.bin")
        self._write_match()

    def _write_match(self):
        """M3 对局投影：服务端 match 视图 → 原生可读的入场/状态文件。

        空闲时也持续写入（状态 0），DLL 以此区分"无对局"与"文件缺失"。
        入场地址只接受回环：开发通道边界与 guard 白名单一致。
        """
        match = self.match or {}
        entry = match.get("entry") or {}
        host = entry.get("host", "127.0.0.1")
        if host not in ("127.0.0.1", "localhost"):
            host = "127.0.0.1"  # 非回环地址不下发：M3 边界，M6 局域网前不放宽。
        try:
            import socket
            host_bytes = socket.inet_aton(host)
        except OSError:
            host_bytes = b"\x7f\x00\x00\x01"
        state = MATCH_STATES.get(match.get("state", ""), 0)
        raw = MATCH.pack(b"M3ST", self.nonce, (self.room or {}).get("revision", 0), state,
                         int.from_bytes(host_bytes, "little"), int(entry.get("port", 28969)),
                         self.channel.player["native_id"],
                         str(match.get("map", "")).encode("ascii"), str(match.get("gametype", "")).encode("ascii"))
        pending = self.stage / "m3-match.pending"
        pending.write_bytes(raw)
        pending.replace(self.stage / "m3-match.bin")

    def start(self):
        """开启事件接收和邮箱线程，调试线程不参与网络等待。"""
        self.channel.start()
        self._thread = threading.Thread(target=self._loop, name="room-mailbox", daemon=True)
        self._thread.start()

    def _record(self, event_type, **values):
        """保留最后 200 条无凭据证据，包括服务器返回的房间版本。"""
        self._evidence["events"].append({"at": time.time(), "type": event_type, **values})
        self._evidence["events"] = self._evidence["events"][-200:]
        (self.run / "m2-online.json").write_text(json.dumps(self._evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    def _snapshot(self, state):
        """快照只从服务端更新；离线消息不能被解释成房间已关闭。"""
        if state.get("offline"):
            if self.online:
                self._record("offline")
            self.online = False
            return
        self.online = True
        if state.get("cursor", 0) < self._cursor:
            return  # 并行到达的旧推送不能覆盖较新的命令响应。
        self._cursor = state.get("cursor", self._cursor)
        self.room = state.get("snapshot")
        revision = self.room["revision"] if self.room else 0
        # M3：房间快照携带对局视图；状态变化单独留证。
        match = (self.room or {}).get("match")
        if (match or {}).get("state") != (self.match or {}).get("state"):
            self._record("match_state", match=match)
        self.match = match
        if match and match.get("id"):
            self._started_match_id = match["id"]
        elif not self.room or self.room.get("state") == "OPEN":
            self._started_match_id = None
        if revision != self._seen_revision:
            self._seen_revision = revision
            self._record("snapshot", room=self.room)

    def _intent(self):
        """只接收定长同会话消息；请求结果明确返回给对应原生序号。"""
        path = self.stage / "m2-intent.bin"
        if not path.is_file():
            return
        raw = path.read_bytes()
        if len(raw) != REQUEST.size:
            return
        magic, nonce, sequence, kind, revision, value, map_raw, mode_raw = REQUEST.unpack(raw)
        if magic != b"M2RQ" or nonce != self.nonce or sequence <= self.sequence or kind not in (1, 2, 3, 4, 5, 6):
            return
        request_id = self.nonce.hex() + ":" + str(sequence)
        try:
            if kind == 1:
                # 原生创建发生在房内选图之前，携带的是运营期随机默认值，
                # 不能把它当成玩家已选定的内容。M2 请求服务端当前验证组合，
                # 随后将权威地图/模式反向投影；后续开放选图需独立命令和能力协商。
                self._record("native_create", requested_map=map_raw.split(b"\0", 1)[0].decode("ascii"),
                             requested_mode=mode_raw.split(b"\0", 1)[0].decode("ascii"))
                result = self.channel.request("/player/rooms", {"request_id": request_id})
            elif kind == 2 and self.room:
                result = self.channel.request("/player/leave", {"request_id": request_id, "room_id": self.room["id"]})
            elif kind in (3, 4) and self.room:
                body = {"request_id": request_id, "room_id": self.room["id"], "expected_room_revision": revision}
                if kind == 3:
                    mode = mode_raw.split(b"\0", 1)[0].decode("ascii")
                    if mode != "war":
                        raise ChannelError("UNSUPPORTED_CONTENT", "M2 当前仅开放战术团队竞技模式。")
                    body["map"] = map_raw.split(b"\0", 1)[0].decode("ascii")
                else:
                    # DLL 已将原生 0/1 转为协议 1/2；此处只传递目标阵营，不能再次切换。
                    body["team"] = value
                result = self.channel.request("/player/room-config", body)
            elif kind == 5 and self.room:
                # M3 原生开局：房主意图交服务端校验；本地不做任何状态假设，
                # 入场只由服务端 MatchReady 投影驱动。
                self._record("native_match_start", room_id=self.room["id"])
                result = self.channel.request("/player/match-start",
                                              {"request_id": request_id, "room_id": self.room["id"]})
                self._started_match_id = result.get("match_id")
            elif kind == 6 and self.room:
                # 优先使用开局回执 ID；快速取消时 SSE 可能还没有发布 match。
                match_id = self._started_match_id or (self.match or {}).get("id")
                if match_id:
                    self.channel.request("/player/match-cancel", {
                        "request_id": request_id, "room_id": self.room["id"], "match_id": match_id})
                # 使用新的完整快照和 cursor，防止排队的旧 READY 推送恢复已取消入场。
                state = self.channel.request("/player/snapshot")
                self._snapshot(state)
                result = {"snapshot": self.room, "event_id": self._cursor}
            else:
                result = {"snapshot": None}
        except ChannelError as exc:
            if exc.code == "UNAUTHENTICATED":
                raise
            self.sequence, self.status = sequence, 0
            self._record("rejected", sequence=sequence, code=exc.code, message=str(exc))
            print("房间请求失败：" + str(exc), flush=True)
            # 失败后仍取权威快照，使原生临时选择恢复为服务端实际配置。
            self._snapshot(self.channel.request("/player/snapshot"))
            return
        self.sequence, self.status = sequence, 1
        self._cursor = max(self._cursor, result.get("event_id", 0) or 0)
        self.room = result.get("snapshot", result.get("room", self.room))
        if self.room and self.room["state"] == "CLOSED":
            self.room = None
        self.match = (self.room or {}).get("match")
        # 开局命令返回的是 STARTING 房间快照；对局字段从后续同步/事件获得。
        self._record("intent_ack", sequence=sequence, kind=kind, room=self.room)

    def _loop(self):
        """顺序命令与三秒心跳；断网保留消息，恢复后按原编号重试。"""
        heartbeat_at = 0
        while not self._stop.is_set():
            try:
                while True:
                    try:
                        self._snapshot(self.channel.snapshots.get_nowait())
                    except queue.Empty:
                        break
                if time.monotonic() >= heartbeat_at:
                    self.channel.request("/player/heartbeat", {})
                    heartbeat_at = time.monotonic() + 3
                self._intent()
            except (OSError, ValueError, ChannelError):
                if self.online:
                    self._record("network_unavailable")
                self.online = False
            except Exception as exc:
                # 编程错误也不能让后台线程静默消失、令原生界面永远等待。
                # 保留明确诊断，当前请求失败并离线；不把异常当成正常建房。
                self.online, self.status = False, 0
                self._record("worker_error", error=type(exc).__name__, message=str(exc))
                print("房间通道异常：" + str(exc), flush=True)
            try:
                self._write()
            except OSError:
                pass  # 与短暂的游戏读句柄竞争，下一轮重新发布。
            self._stop.wait(0.2)

    def close(self):
        """结束线程并撤销会话，复制原生日志以免被下次 runtime 回收。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        self.channel.close()
        for name in ("m2-native.log", "m3-transport.log"):
            path = self.stage / name
            if path.exists():
                (self.run / name).write_bytes(path.read_bytes())
