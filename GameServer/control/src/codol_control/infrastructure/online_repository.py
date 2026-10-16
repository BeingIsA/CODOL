"""M2 SQLite 工作单元：房间/命令/事件原子提交，会话令牌只存摘要。

沿用 M1 唯一连接的互斥锁，在完整读-判定-写期间持锁；异常整体回滚。
不调用 Database.execute（它会逐句提交），避免幂等和版本校验失去原子性。
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone


class SqliteOnlineRepository:
    """把数据库连接封装为应用端口，不向用例泄露 SQL。"""

    def __init__(self, database):
        """连接与锁由组合根注入；本仓库不拥有关闭权限。"""
        self._db = database

    @contextmanager
    def transaction(self):
        """完整事务内串行化业务判断；成功提交，任何异常均回滚。"""
        with self._db._lock:
            try:
                self._db.conn.execute("BEGIN IMMEDIATE")
                yield SqliteOnlineUnit(self._db.conn)
                self._db.conn.commit()
            except BaseException:
                self._db.conn.rollback()
                raise


class SqliteOnlineUnit:
    """事务内操作集合；只允许由仓库创建，不持有跨事务状态。"""

    def __init__(self, connection):
        """保存当前事务的连接，生命周期由外层 with 控制。"""
        self._conn = connection

    def player(self, key):
        """按不可公开的开发凭据查找稳定身份；首次返回 None。"""
        row = self._conn.execute("SELECT id,native_id,display_name FROM players WHERE development_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def save_player(self, key, player):
        """创建身份并用唯一约束防止原生 ID 碰撞。"""
        self._conn.execute("INSERT INTO players VALUES(?,?,?,?)",
                           (key, player["id"], player["native_id"], player["display_name"]))

    def session(self, token_hash):
        """返回会话与展示身份，明文凭据从不落库。"""
        row = self._conn.execute(
            "SELECT s.*,p.native_id,p.display_name FROM player_sessions s JOIN players p "
            "ON p.id=s.player_id WHERE token_hash=?", (token_hash,)).fetchone()
        return dict(row) if row else None

    def save_session(self, token_hash, session):
        """插入或只更新心跳/撤销标志，不延长原有绝对有效期。"""
        self._conn.execute(
            "INSERT INTO player_sessions VALUES(?,?,?,?,?) ON CONFLICT(token_hash) DO UPDATE SET "
            "last_seen=excluded.last_seen,revoked=excluded.revoked",
            (token_hash, session["player_id"], session["expires_at"], session["last_seen"], session.get("revoked", 0)))

    def revoke_player(self, player_id):
        """新连接接管同一身份时撤销旧连接，避免双宿主互相覆盖状态。"""
        self._conn.execute("UPDATE player_sessions SET revoked=1 WHERE player_id=?", (player_id,))

    def rooms(self):
        """读取完整权威快照，排序保证管理页与重连选择稳定。"""
        return [json.loads(row[0]) for row in self._conn.execute(
            "SELECT payload FROM room_snapshots ORDER BY room_id")]

    def save_room(self, room):
        """更新索引和完整快照，调用方随后在同事务追加事件。"""
        self._conn.execute(
            "INSERT INTO rooms(id,name,state,revision,map,gametype,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state,revision=excluded.revision,updated_at=excluded.updated_at,"
            "map=excluded.map,gametype=excluded.gametype",
            (room["id"], room["name"], room["state"], room["revision"], room["map"], room["gametype"],
             room["created_at"], room["updated_at"]))
        self._conn.execute("INSERT INTO room_snapshots VALUES(?,?) ON CONFLICT(room_id) DO UPDATE SET payload=excluded.payload",
                           (room["id"], json.dumps(room, ensure_ascii=False)))

    def receipt(self, actor, request_id):
        """读取同身份请求的原结果与参数摘要。"""
        row = self._conn.execute("SELECT fingerprint,payload FROM command_receipts WHERE actor=? AND request_id=?",
                                 (actor, request_id)).fetchone()
        return {"fingerprint": row[0], "result": json.loads(row[1])} if row else None

    def save_receipt(self, actor, request_id, value):
        """唯一键防止重复命令提交，参数摘要用于拒绝 ID 复用。"""
        self._conn.execute("INSERT INTO command_receipts VALUES(?,?,?,?)",
                           (actor, request_id, value["fingerprint"], json.dumps(value["result"], ensure_ascii=False)))

    def emit(self, room, now, type="RoomUpdated"):
        """持久化事件日志；订阅者自行持游标，断线不会丢弃未读事件。

        M3 起事件类型扩展为 RoomUpdated / MatchPreparing / MatchReady /
        MatchStarted / MatchFinished / MatchFailed / MatchCancelled；
        载荷仍是完整房间快照（含内嵌 match 视图），客户端消费逻辑不变。
        """
        at = datetime.fromtimestamp(now, timezone.utc).isoformat()
        cursor = self._conn.execute("INSERT INTO events_out(room_id,type,payload,created_at) VALUES(?,?,?,?)",
                                    (room["id"], type, json.dumps(room, ensure_ascii=False), at))
        return cursor.lastrowid

    # ---- M3 对局读写：与房间共用同一事务边界 ----

    def _row_to_match(self, row):
        """把 matches 行还原为完整对局字典；JSON 列解析失败按损坏处理。"""
        match = dict(row)
        for key in ("config", "entry", "result"):
            match[key] = json.loads(match.get(key) or "{}")
        return match

    def match(self, match_id):
        """按 ID 读取对局；不存在返回 None。"""
        row = self._conn.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
        return self._row_to_match(row) if row else None

    def active_matches(self):
        """未终结对局清单（按创建时间排序），重启恢复与观察线程使用。"""
        rows = self._conn.execute(
            "SELECT * FROM matches WHERE state IN ('ALLOCATING','LOADING','READY','RUNNING','FINISHING') "
            "ORDER BY created_at").fetchall()
        return [self._row_to_match(r) for r in rows]

    def save_match(self, match):
        """插入或更新对局；全部字段整体写入，避免部分列漂移。"""
        self._conn.execute(
            "INSERT INTO matches(id,room_id,state,created_at,updated_at,config,run_dir,pid,port,entry,result,"
            "error,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at,"
            "config=excluded.config,run_dir=excluded.run_dir,pid=excluded.pid,port=excluded.port,"
            "entry=excluded.entry,result=excluded.result,error=excluded.error,finished_at=excluded.finished_at",
            (match["id"], match["room_id"], match["state"], match["created_at"], match["updated_at"],
             json.dumps(match.get("config", {}), ensure_ascii=False), match.get("run_dir", ""),
             match.get("pid"), match.get("port"), json.dumps(match.get("entry", {}), ensure_ascii=False),
             json.dumps(match.get("result", {}), ensure_ascii=False), match.get("error", ""),
             match.get("finished_at")))

    def recent_matches(self, limit):
        """最近对局（含终态），管理页展示用。"""
        rows = self._conn.execute("SELECT * FROM matches ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_match(r) for r in rows]

    def events(self, room_id, after):
        """有界批次重放；完整快照随事件携带，消费者按 revision 去重。"""
        rows = self._conn.execute("SELECT * FROM events_out WHERE room_id=? AND id>? ORDER BY id LIMIT 64",
                                  (room_id, after)).fetchall()
        return [{"event_id": r["id"], "type": r["type"], "protocol_version": 1,
                 "room_id": room_id, "revision": json.loads(r["payload"])["revision"],
                 "server_time": r["created_at"], "snapshot": json.loads(r["payload"])} for r in rows]

    def online(self, player_id, now):
        """心跳超过 15 秒或会话失效即离线；无需等待后台清扫才如实展示。"""
        return self._conn.execute("SELECT 1 FROM player_sessions WHERE player_id=? AND revoked=0 "
                                  "AND expires_at>? AND last_seen>? LIMIT 1", (player_id, now, now - 15)).fetchone() is not None

    def last_seen(self, player_id):
        """该玩家最后一次心跳时间；从未连接过返回 0.0（视为"很久以前"）。

        被撤销的会话也计入：主动断开与崩溃都说明这个席位不再有人操作，
        回收只关心"最后一次还活着是什么时候"。
        """
        row = self._conn.execute("SELECT max(last_seen) FROM player_sessions WHERE player_id=?",
                                 (player_id,)).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def connected_players(self, now):
        """大厅内尚未建房的玩家也属于在线连接，不从房间成员数推测。"""
        return self._conn.execute("SELECT count(DISTINCT player_id) FROM player_sessions WHERE revoked=0 "
                                  "AND expires_at>? AND last_seen>?", (now, now - 15)).fetchone()[0]
