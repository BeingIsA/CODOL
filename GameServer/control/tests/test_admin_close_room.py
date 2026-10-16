"""管理页"关闭房间"回归：纯规则、用例幂等、编排顺序与 HTTP 权限。

关闭房间与玩家离房是两种不同的处置，必须分别验证：
- 离房（ADR 0006）只移除本人席位，房间没人了才关闭；
- 关闭是管理端对整个房间的处置，房内所有人同时失去席位，
  房间正在对局时还要先让客户端优雅离场（先停局、后关房）。
"""
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from support import CONTROL_ROOT, ServiceInstance
from codol_control.application.admin_rooms import AdminRooms
from codol_control.application.player_rooms import PlayerRooms
from codol_control.domain import room_rules
from codol_control.domain.errors import ControlError
from codol_control.infrastructure.database import Database, run_migrations
from codol_control.infrastructure.online_repository import SqliteOnlineRepository


def member(player_id: str, native_id: int = 1, team: int = 2) -> dict:
    """构造最小成员记录；关闭规则只关心席位存在与否。"""
    return {"player_id": player_id, "native_id": native_id, "display_name": player_id,
            "team": team, "ready": False}


def snapshot(state: str = "OPEN", members=None, owner: str = "p1", revision: int = 1) -> dict:
    """构造最小房间快照，供纯规则直接消费。"""
    return {"id": "room-1", "name": "Player的房间", "state": state, "revision": revision,
            "owner_id": owner, "map": "mp_cross_fire", "gametype": "war",
            "created_at": "2026-10-16T00:00:00+00:00", "updated_at": "2026-10-16T00:00:00+00:00",
            "members": [member(owner)] if members is None else members,
            "bots": {"count": 0, "team": 1, "difficulty": "normal"}}


class CloseRuleTests(unittest.TestCase):
    """纯规则：不接触数据库、网络与进程。"""

    def test_close_clears_all_members(self):
        """管理端关闭清空全部成员并前进版本，与"只剩一个人"无关。"""
        before = snapshot(members=[member("p1"), member("p2", 2, 1)])
        after = room_rules.close_room(before)
        self.assertEqual(after["state"], "CLOSED")
        self.assertEqual(after["members"], [])
        self.assertEqual(after["revision"], before["revision"] + 1)

    def test_close_during_match_drops_match_view(self):
        """对局中的房间可以被关闭；关闭后不再持有对局视图。"""
        before = {**snapshot(state="IN_MATCH", members=[member("p1")]),
                  "match": {"id": "m1", "state": "RUNNING"}}
        after = room_rules.close_room(before)
        self.assertEqual(after["state"], "CLOSED")
        self.assertNotIn("match", after)

    def test_repeat_close_is_idempotent(self):
        """连点/重试：已关闭房间原样返回，版本不再前进。"""
        closed = snapshot(state="CLOSED", members=[], revision=5)
        self.assertEqual(room_rules.close_room(closed), closed)


class CloseCommandTests(unittest.TestCase):
    """应用层：真实仓库、可控时钟，验证快照、事件与幂等回执。"""

    def setUp(self):
        """每项独立数据库，关闭的幂等与事件计数必须可重复。"""
        self.temp = tempfile.TemporaryDirectory(prefix="codol-close-")
        self.db = Database(Path(self.temp.name) / "control.db")
        run_migrations(self.db, CONTROL_ROOT / "migrations")
        self.now = 1000.0
        self.repository = SqliteOnlineRepository(self.db)
        self.service = PlayerRooms(self.repository, lambda: self.now, secrets.token_hex, "local-secret")
        self.token = self.service.login("local-secret", "a" * 64, "Player", 1)["token"]
        self.admin = AdminRooms(self.service)

    def tearDown(self):
        """先关闭连接再删除临时数据。"""
        self.db.close()
        self.temp.cleanup()

    def events(self) -> int:
        """当前事件总数，用于确认重复命令不产生新事件。"""
        return len(self.db.query("SELECT * FROM events_out"))

    def create(self):
        """建立当前验证地图的唯一房间。"""
        return self.service.create(self.token, "create-1", "mp_cross_fire", "war")["snapshot"]

    def test_close_removes_room_from_list_and_player_snapshot(self):
        """关闭后管理列表不再显示，玩家同步拿到空快照（回大厅）。"""
        room = self.create()
        result = self.admin.close(room["id"], "close-1")
        self.assertEqual(result["snapshot"]["state"], "CLOSED")
        self.assertEqual(result["snapshot"]["members"], [])
        self.assertEqual(self.service.list_rooms()["total"], 0)
        self.assertIsNone(self.service.sync(self.token)["snapshot"])
        self.assertIsNone(result["match"])

    def test_repeat_close_is_idempotent_without_new_events(self):
        """同一编号重放结果一致；不同编号对已关闭房间也不再产生事件。"""
        room = self.create()
        first = self.admin.close(room["id"], "close-1")
        settled = self.events()
        self.assertEqual(self.admin.close(room["id"], "close-1"), first)
        second = self.admin.close(room["id"], "close-2")
        self.assertEqual(second["snapshot"]["state"], "CLOSED")
        self.assertEqual(second["snapshot"]["revision"], first["snapshot"]["revision"])
        self.assertEqual(second["event_id"], 0)
        self.assertEqual(self.events(), settled)

    def test_unknown_room_is_rejected(self):
        """不存在的房间必须明确报错，不能被当成"已关闭"静默成功。"""
        with self.assertRaises(ControlError) as error:
            self.admin.close("no-such-room", "close-1")
        self.assertEqual(error.exception.code, "INVALID_ARGUMENT")

    def test_player_can_create_again_after_close(self):
        """关闭的房间不占用活动房间容量，玩家可以重新建房。"""
        room = self.create()
        self.admin.close(room["id"], "close-1")
        again = self.service.create(self.token, "create-2", "mp_cross_fire", "war")
        self.assertEqual(again["snapshot"]["state"], "OPEN")


class CloseHttpTests(unittest.TestCase):
    """真实 HTTP：管理员权限边界与"先停局后关房"的编排。"""

    def _player_room(self, service: ServiceInstance):
        """用玩家通道建一个真实房间，返回（玩家令牌, 房间快照）。"""
        secret = json.loads((service.data_dir / "development-access.json").read_text())["secret"]
        status, login = service.request("POST", "/api/player/login",
                                        body={"identity": "c" * 64, "display_name": "Native",
                                              "protocol_version": 1},
                                        headers={"X-Development-Key": secret})
        self.assertEqual(status, 200, login)
        token = login["token"]
        status, created = service.request("POST", "/api/player/rooms", token=token,
                                          body={"request_id": "http-create"})
        self.assertEqual(status, 200, created)
        return token, created["snapshot"]

    def _admin(self, service: ServiceInstance) -> str:
        """管理员登录返回会话令牌。"""
        status, admin = service.login(service.initial_password())
        self.assertEqual(status, 200, admin)
        return admin["token"]

    def test_close_requires_admin_session(self):
        """关闭房间是管理端处置：匿名与玩家令牌都不能调用。"""
        service = ServiceInstance()
        try:
            _, admin = service.login(service.initial_password())
            token, room = self._player_room(service)
            path = f"/api/rooms/{room['id']}/close"
            status, _ = service.request("POST", path, body={"request_id": "anon-close"})
            self.assertEqual(status, 401)
            status, _ = service.request("POST", path, token=token, body={"request_id": "player-close"})
            self.assertEqual(status, 401)
            status, _ = service.request("POST", path, token=admin["token"], body={"request_id": "admin-close"})
            self.assertEqual(status, 200)
        finally:
            service.stop()

    def test_close_stops_running_match_then_closes_room(self):
        """对局中关闭：对局先按正常路径结束（FINISHED），房间随后关闭。"""
        service = ServiceInstance(probe_ready=True)
        try:
            admin = self._admin(service)
            player_token, room = self._player_room(service)
            status, started = service.request("POST", f"/api/rooms/{room['id']}/match/start",
                                              token=admin, body={"request_id": "start-1"})
            self.assertEqual(status, 200, started)
            status, closed = service.request("POST", f"/api/rooms/{room['id']}/close",
                                             token=admin, body={"request_id": "close-1"})
            self.assertEqual(status, 200, closed)
            self.assertEqual(closed["snapshot"]["state"], "CLOSED")
            self.assertIsNotNone(closed["match"], "对局中的房间关闭必须先停局")
            self.assertEqual(closed["match"]["match"]["state"], "FINISHED")

            status, matches = service.request("GET", "/api/matches", token=admin)
            self.assertEqual(status, 200)
            self.assertEqual(matches["active"], [])

            status, rooms = service.request("GET", "/api/rooms", token=admin)
            self.assertEqual(status, 200)
            self.assertEqual(rooms["total"], 0)

            # 客户端视角：房间已不存在，同步后回到大厅。
            status, sync = service.request("GET", "/api/player/snapshot", token=player_token)
            self.assertEqual(status, 200, sync)
            self.assertIsNone(sync["snapshot"])
        finally:
            service.stop()

    def test_close_without_match_returns_no_match_outcome(self):
        """没有对局的房间直接关闭，停局结果为空而不是报错。"""
        service = ServiceInstance()
        try:
            admin = self._admin(service)
            _, room = self._player_room(service)
            status, closed = service.request("POST", f"/api/rooms/{room['id']}/close",
                                             token=admin, body={"request_id": "close-plain"})
            self.assertEqual(status, 200, closed)
            self.assertIsNone(closed["match"])
            self.assertEqual(closed["snapshot"]["state"], "CLOSED")
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
