"""房间成员生命周期回归：离开语义、房主移交、房间关闭与对局收尾保护。

对应 2026-10-16 的语义修正（见 docs/adr/0006-房间成员生命周期与多人方向.md）：
**离开 = 移除自己；房间没有成员了才关闭。** 对局中的离开同样成立。

单人房间是这套规则的退化情形——人走房空，结果与旧的“离开即关闭”完全一致，
所以 M2/M3 的既有行为不变；多人房间里一个人退出不再把其他人一起赶出去。
"""
import hashlib
import secrets
import tempfile
import unittest
from pathlib import Path

from support import CONTROL_ROOT
from codol_control.application.player_rooms import PlayerRooms
from codol_control.domain import match_rules, room_rules
from codol_control.domain.errors import ControlError
from codol_control.infrastructure.database import Database, run_migrations
from codol_control.infrastructure.online_repository import SqliteOnlineRepository


def member(player_id: str, native_id: int = 1, team: int = 2) -> dict:
    """构造最小成员记录；规则只关心身份、阵营与准备位。"""
    return {"player_id": player_id, "native_id": native_id, "display_name": player_id,
            "team": team, "ready": False}


def snapshot(state: str = "OPEN", members=None, owner: str = "p1", revision: int = 1) -> dict:
    """构造最小房间快照；不含数据库字段，纯规则可直接消费。"""
    return {"id": "room-1", "name": "Player的房间", "state": state, "revision": revision,
            "owner_id": owner, "map": "mp_cross_fire", "gametype": "war",
            "created_at": "2026-10-16T00:00:00+00:00", "updated_at": "2026-10-16T00:00:00+00:00",
            "members": [member(owner)] if members is None else members,
            "bots": {"count": 0, "team": 1, "difficulty": "normal"}}


class LeaveRuleTests(unittest.TestCase):
    """纯规则：不接触数据库、网络与进程。"""

    def test_leave_removes_only_self_and_keeps_room(self):
        """两名成员时一人离开：房间保留、对局状态不动、房主顺位移交。"""
        before = snapshot(state="IN_MATCH", members=[member("p1"), member("p2", 2, 1)])
        after = room_rules.leave_room(before, "p1")
        self.assertEqual([p["player_id"] for p in after["members"]], ["p2"])
        self.assertEqual(after["state"], "IN_MATCH")
        self.assertEqual(after["owner_id"], "p2")
        self.assertEqual(after["revision"], before["revision"] + 1)

    def test_owner_keeps_ownership_when_other_leaves(self):
        """离开的不是房主：房主不变，房间只是少了一个人。"""
        before = snapshot(members=[member("p1"), member("p2", 2, 1)])
        after = room_rules.leave_room(before, "p2")
        self.assertEqual(after["owner_id"], "p1")
        self.assertEqual([p["player_id"] for p in after["members"]], ["p1"])

    def test_last_member_leaving_closes_room(self):
        """最后一名成员离开：房间关闭且不再持有任何对局视图。"""
        before = {**snapshot(members=[member("p1")]), "match": {"id": "m1", "state": "RUNNING"}}
        after = room_rules.leave_room(before, "p1")
        self.assertEqual(after["state"], "CLOSED")
        self.assertEqual(after["members"], [])
        self.assertNotIn("match", after)

    def test_repeat_leave_after_close_is_idempotent(self):
        """原生界面会连发多次离开意图；已关闭房间的重复离开必须是幂等成功。"""
        closed = snapshot(state="CLOSED", members=[], revision=4)
        self.assertEqual(room_rules.leave_room(closed, "p1"), closed)

    def test_outsider_cannot_leave_someone_elses_room(self):
        """不在房间里的玩家不能通过离开接口影响别人的房间。"""
        with self.assertRaises(ControlError) as error:
            room_rules.leave_room(snapshot(members=[member("p1")]), "p9")
        self.assertEqual(error.exception.code, "FORBIDDEN")

    def test_settle_does_not_revive_closed_room(self):
        """对局收尾不得把已关闭的房间变回 OPEN——否则会凭空复活空房间。"""
        closed = snapshot(state="CLOSED", members=[], revision=7)
        self.assertEqual(match_rules.match_settled_room(closed), closed)

    def test_match_progress_does_not_touch_closed_room(self):
        """对局就绪/入场推进同样跳过已关闭房间。"""
        closed = snapshot(state="CLOSED", members=[], revision=7)
        match = {"port": 28969, "config": {"map": "mp_cross_fire", "gametype": "war"}}
        self.assertEqual(match_rules.match_entered_room(match, closed, "now"), closed)
        self.assertEqual(match_rules.match_started_room(match, closed), closed)
        self.assertTrue(match_rules.room_is_closed(closed))
        self.assertFalse(match_rules.room_is_closed(snapshot()))


class LeaveCommandTests(unittest.TestCase):
    """应用层：真实仓库、可控时钟，验证命令、事件与幂等的联合行为。"""

    def setUp(self):
        """每项独立数据库；离开的幂等与事件计数必须可重复。"""
        self.temp = tempfile.TemporaryDirectory(prefix="codol-leave-")
        self.db = Database(Path(self.temp.name) / "control.db")
        run_migrations(self.db, CONTROL_ROOT / "migrations")
        self.now = 1000.0
        self.repository = SqliteOnlineRepository(self.db)
        self.service = PlayerRooms(self.repository, lambda: self.now, secrets.token_hex, "local-secret")
        self.token = self.service.login("local-secret", "a" * 64, "Player", 1)["token"]

    def tearDown(self):
        """先关闭连接再删除临时数据。"""
        self.db.close()
        self.temp.cleanup()

    def events(self) -> int:
        """当前事件总数，用于确认重复命令不产生新事件。"""
        return len(self.db.query("SELECT * FROM events_out"))

    def add_second_member(self) -> str:
        """把第二名玩家放进同一房间，返回其会话令牌。

        房主视角不变：房间仍属于建房者，只是多了一名成员。
        这样“有人离开”才能在真实仓库上被验证。
        """
        identity = "b" * 64
        token = self.service.login("local-secret", identity, "Other", 1)["token"]
        key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self.repository.transaction() as u:
            player = u.player(key)
            room = u.rooms()[0]
            room["members"] = room["members"] + [member(player["id"], player["native_id"], 1)]
            u.save_room(room)
        return token

    def test_leave_during_match_closes_room_of_single_player(self):
        """对局中离开是允许的：单人房间里这就是房间关闭。"""
        room = self.service.create(self.token, "create-1", "mp_cross_fire", "war")["snapshot"]
        with self.repository.transaction() as u:
            current = u.rooms()[0]
            current["state"] = "IN_MATCH"
            current["match"] = {"id": "m1", "state": "RUNNING", "map": "mp_cross_fire", "gametype": "war"}
            u.save_room(current)
        result = self.service.leave(self.token, "leave-1", room["id"])
        self.assertEqual(result["snapshot"]["state"], "CLOSED")
        self.assertNotIn("match", result["snapshot"])
        self.assertIsNone(self.service.sync(self.token)["snapshot"])

    def test_repeat_leave_is_idempotent_without_new_events(self):
        """连发的重复离开：结果相同、事件不重复、版本不前进。"""
        room = self.service.create(self.token, "create-1", "mp_cross_fire", "war")["snapshot"]
        first = self.service.leave(self.token, "leave-1", room["id"])
        settled = self.events()
        second = self.service.leave(self.token, "leave-2", room["id"])
        self.assertEqual(second["snapshot"]["state"], "CLOSED")
        self.assertEqual(second["snapshot"]["revision"], first["snapshot"]["revision"])
        self.assertEqual(second["event_id"], 0)
        self.assertEqual(self.events(), settled)

    def test_other_member_leaving_keeps_room_alive(self):
        """多人房间：一人离开后房间仍在、另一名成员仍能读到自己的房间。"""
        created = self.service.create(self.token, "create-1", "mp_cross_fire", "war")["snapshot"]
        other = self.add_second_member()
        result = self.service.leave(other, "other-leave", created["id"])
        self.assertEqual(result["snapshot"]["state"], "OPEN")
        self.assertEqual([p["player_id"] for p in result["snapshot"]["members"]],
                         [created["owner_id"]])
        # 房主视角：房间还在，成员只剩自己。
        self.assertEqual(self.service.sync(self.token)["snapshot"]["id"], created["id"])

    def test_player_can_create_again_after_leaving(self):
        """离开后可重新建房：关闭的房间不占用活动房间容量。"""
        room = self.service.create(self.token, "create-1", "mp_cross_fire", "war")["snapshot"]
        self.service.leave(self.token, "leave-1", room["id"])
        again = self.service.create(self.token, "create-2", "mp_cross_fire", "war")
        self.assertEqual(again["snapshot"]["state"], "OPEN")
        self.assertNotEqual(again["snapshot"]["id"], room["id"])


if __name__ == "__main__":
    unittest.main()
