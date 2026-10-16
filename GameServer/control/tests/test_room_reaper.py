"""无人房间回收回归：客户端异常退出后不留悬空房间。

对应 ADR 0006"会话失效回收"这一遗留项：玩家没发来 leave（崩溃、被强杀、
闪退）时，服务端按会话心跳判定房间已经没人并关闭它，避免房间一直占着
唯一的活动房间名额让新房建不起来。
"""
import secrets
import tempfile
import unittest
from pathlib import Path

from support import CONTROL_ROOT
from codol_control.application.player_rooms import PlayerRooms
from codol_control.application.room_reaper import ABANDONED_TIMEOUT_SECONDS, RoomReaper
from codol_control.domain import room_rules
from codol_control.infrastructure.database import Database, run_migrations
from codol_control.infrastructure.online_repository import SqliteOnlineRepository


def snapshot(state: str = "OPEN", revision: int = 1) -> dict:
    """构造最小房间快照，供纯规则直接消费。"""
    return {"id": "room-1", "name": "Player的房间", "state": state, "revision": revision,
            "owner_id": "p1", "map": "mp_cross_fire", "gametype": "war",
            "created_at": "2026-10-16T00:00:00+00:00", "updated_at": "2026-10-16T00:00:00+00:00",
            "members": [{"player_id": "p1", "native_id": 1, "display_name": "Player",
                         "team": 2, "ready": False}],
            "bots": {"count": 0, "team": 1, "difficulty": "normal"}}


class AbandonedRuleTests(unittest.TestCase):
    """纯规则：不接触数据库、网络与进程。"""

    def test_all_members_silent_beyond_timeout(self):
        """全员心跳都超过阈值：判定被遗弃。"""
        self.assertTrue(room_rules.is_abandoned(snapshot(), {"p1": 1000.0}, 1000.0 + 91, 90.0))

    def test_recent_heartbeat_keeps_room(self):
        """刚心跳过：不回收（否则重连恢复会被一起干掉）。"""
        self.assertFalse(room_rules.is_abandoned(snapshot(), {"p1": 1000.0}, 1000.0 + 30, 90.0))

    def test_any_live_member_keeps_room(self):
        """多人房间只要还有一个人活着，房间就不能被回收。"""
        room = {**snapshot(), "members": [{"player_id": "p1"}, {"player_id": "p2"}]}
        self.assertFalse(room_rules.is_abandoned(room, {"p1": 900.0, "p2": 1090.0}, 1091.0, 90.0))

    def test_match_rooms_are_left_to_the_match_observer(self):
        """对局中的房间不归回收管：由观察线程按入场人数先收敛。"""
        for state in ("STARTING", "IN_MATCH"):
            self.assertFalse(room_rules.is_abandoned(snapshot(state=state), {"p1": 0.0},
                                                     10_000.0, 90.0))

    def test_member_without_any_session_counts_as_long_gone(self):
        """从没连接过的席位按"很久以前"处理，不会因为缺记录而永久占用房间。"""
        self.assertTrue(room_rules.is_abandoned(snapshot(), {}, 1000.0, 90.0))


class ReapCommandTests(unittest.TestCase):
    """应用层：真实仓库、可控时钟。"""

    def setUp(self):
        """每项独立数据库，时钟手动推进不需要真实等待。"""
        self.temp = tempfile.TemporaryDirectory(prefix="codol-reap-")
        self.db = Database(Path(self.temp.name) / "control.db")
        run_migrations(self.db, CONTROL_ROOT / "migrations")
        self.now = 1000.0
        self.repository = SqliteOnlineRepository(self.db)
        self.service = PlayerRooms(self.repository, lambda: self.now, secrets.token_hex,
                                   "local-secret", reap_timeout=ABANDONED_TIMEOUT_SECONDS)
        self.token = self.service.login("local-secret", "a" * 64, "Player", 1)["token"]
        self.reaper = RoomReaper(self.service)

    def tearDown(self):
        """先关闭连接再删除临时数据。"""
        self.db.close()
        self.temp.cleanup()

    def events(self) -> int:
        """当前事件总数，用于确认重复回收不产生新事件。"""
        return len(self.db.query("SELECT * FROM events_out"))

    def create(self):
        """建立当前验证地图的唯一房间。"""
        return self.service.create(self.token, "create-1", "mp_cross_fire", "war")["snapshot"]

    def test_room_survives_while_player_is_alive(self):
        """客户端还在：无论过多久都不回收。"""
        self.create()
        for _ in range(10):
            self.now += 60
            self.service.heartbeat(self.token)
        self.assertEqual(self.reaper.reap_once(), [])
        self.assertEqual(self.service.list_rooms()["total"], 1)

    def test_abandoned_room_is_reaped_after_timeout(self):
        """客户端崩了不再心跳：超过阈值后房间自动关闭，名额释放。"""
        room = self.create()
        self.now += 60
        self.assertEqual(self.reaper.reap_once(), [])
        self.now += 60  # 累计 120 秒无心跳 > 90
        self.assertEqual(self.reaper.reap_once(), [room["id"]])
        self.assertEqual(self.service.list_rooms()["total"], 0)
        self.assertIsNone(self.service.sync(self.token)["snapshot"])

    def test_reap_is_idempotent_across_cycles(self):
        """回收只发生一次：后续周期拿到同一回执，不重复发事件。"""
        self.create()
        self.now += 120
        self.reaper.reap_once()
        settled = self.events()
        self.assertEqual(self.reaper.reap_once(), [])
        self.assertEqual(self.events(), settled)

    def test_reaped_room_frees_capacity_for_new_room(self):
        """回收的意义：崩溃留下的房间不再把新客户端挡在 ROOM_FULL 外面。"""
        self.create()
        self.now += 120
        self.reaper.reap_once()
        again = self.service.create(self.token, "create-2", "mp_cross_fire", "war")
        self.assertEqual(again["snapshot"]["state"], "OPEN")

    def test_match_room_is_not_reaped(self):
        """对局中的房间交给观察线程：回收不抢着关，避免与对局收尾打架。"""
        room = self.create()
        with self.repository.transaction() as u:
            current = u.rooms()[0]
            current["state"] = "IN_MATCH"
            u.save_room(current)
        self.now += 600
        self.assertEqual(self.reaper.reap_once(), [])
        self.assertEqual(self.service.list_rooms()["total"], 1)
        self.assertEqual(room["id"], self.service.list_rooms()["items"][0]["id"])

    def test_admin_close_still_available_when_not_expired(self):
        """回收只是兜底：管理员关闭不受超时限制，随时可用。"""
        room = self.create()
        self.now += 10
        from codol_control.application.admin_rooms import AdminRooms

        result = AdminRooms(self.service).close(room["id"], "manual-1")
        self.assertEqual(result["snapshot"]["state"], "CLOSED")

    def test_list_reports_reap_timeout(self):
        """管理页需要知道阈值，才能解释"离线的房间什么时候会消失"。"""
        self.assertEqual(self.service.list_rooms()["reap_timeout_seconds"],
                         ABANDONED_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
