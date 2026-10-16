"""M2 回归：真实 SQLite 并发幂等、事件原子性、租约与 HTTP 权限。

这些测试不启动游戏；原生 UI 的端到端验收单独记录。
"""
import json
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from support import CONTROL_ROOT, ServiceInstance
from codol_control.application.player_rooms import PlayerRooms
from codol_control.domain.errors import ControlError
from codol_control.infrastructure.database import Database, run_migrations
from codol_control.infrastructure.online_repository import SqliteOnlineRepository


class RoomTransactionTests(unittest.TestCase):
    """真实仓库与可控时钟，验证跨命令不变量。"""
    def setUp(self):
        """每项独立数据库，推进时钟无需真实等待。"""
        self.temp = tempfile.TemporaryDirectory(prefix="codol-m2-")
        self.path = Path(self.temp.name) / "control.db"
        self.db = Database(self.path)
        run_migrations(self.db, CONTROL_ROOT / "migrations")
        self.now = 1000.0
        self.service = PlayerRooms(SqliteOnlineRepository(self.db), lambda: self.now, secrets.token_hex, "local-secret")
        self.login = self.service.login("local-secret", "a" * 64, "Player", 1)
        self.token = self.login["token"]

    def tearDown(self):
        """先关闭连接再删除临时数据。"""
        self.db.close()
        self.temp.cleanup()

    def create(self, request_id="create-1"):
        """建立当前验证地图的唯一房间。"""
        return self.service.create(self.token, request_id, "mp_cross_fire", "war")

    def test_concurrent_retry_commits_once(self):
        """十六个并发重试只产生一个房间和一条事件。"""
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.create(), range(16)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(self.db.query("SELECT * FROM rooms")), 1)
        self.assertEqual(len(self.db.query("SELECT * FROM events_out")), 1)

    def test_request_id_reuse_rejects_different_intent(self):
        """同编号不可跨操作复用，不能意外执行离房。"""
        room = self.create()["snapshot"]
        with self.assertRaises(ControlError) as error:
            self.service.leave(self.token, "create-1", room["id"])
        self.assertEqual(error.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self.service.list_rooms()["total"], 1)

    def test_bot_revision_conflict_does_not_emit(self):
        """配置重试只递增一次；版本冲突不能多写事件或原结果。"""
        room = self.create()["snapshot"]
        result = self.service.bots(room["id"], "bots-1", 1, 3, 1, "hard")
        self.assertEqual(result, self.service.bots(room["id"], "bots-1", 1, 3, 1, "hard"))
        with self.assertRaises(ControlError) as error:
            self.service.bots(room["id"], "bots-2", 1, 2, 2, "easy")
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        self.assertEqual(len(self.db.query("SELECT * FROM events_out")), 2)
        self.assertEqual(len(self.db.query("SELECT * FROM command_receipts")), 2)

    def test_capacity_and_owner_authorization(self):
        """其他玩家不能挤占房间、替人离开或读取别人的成员快照。"""
        room = self.create()["snapshot"]
        other = self.service.login("local-secret", "b" * 64, "Other", 1)["token"]
        with self.assertRaises(ControlError) as error:
            self.service.create(other, "other-create", "mp_cross_fire", "war")
        self.assertEqual(error.exception.code, "ROOM_FULL")
        with self.assertRaises(ControlError) as error:
            self.service.leave(other, "other-leave", room["id"])
        self.assertEqual(error.exception.code, "FORBIDDEN")
        self.assertIsNone(self.service.sync(other)["snapshot"])

    def test_heartbeat_timeout_and_disconnect(self):
        """租约超时显示离线，心跳恢复，主动断开立即撤销。"""
        self.create()
        self.now += 16
        self.assertFalse(self.service.list_rooms()["items"][0]["members"][0]["online"])
        self.service.heartbeat(self.token)
        self.assertTrue(self.service.list_rooms()["items"][0]["members"][0]["online"])
        self.service.disconnect(self.token)
        with self.assertRaises(ControlError):
            self.service.heartbeat(self.token)

    def test_reconnect_preserves_identity_and_revokes_old_session(self):
        """同凭据重连保持 UID 与房间，旧连接失去权限。"""
        room = self.create()["snapshot"]
        login = self.service.login("local-secret", "a" * 64, "Player", 1)
        self.assertEqual(login["player"], self.login["player"])
        self.assertEqual(self.service.sync(login["token"])["snapshot"], room)
        with self.assertRaises(ControlError):
            self.service.heartbeat(self.token)

    def test_restart_preserves_receipt_and_events(self):
        """重开连接后房间、幂等结果与事件不依赖进程内字典。"""
        result = self.create()
        self.db.close()
        self.db = Database(self.path)
        self.service = PlayerRooms(SqliteOnlineRepository(self.db), lambda: self.now, secrets.token_hex, "local-secret")
        self.assertEqual(self.create(), result)
        self.assertEqual(self.service.sync(self.token)["events"][0]["snapshot"], result["snapshot"])

    def test_login_version_access_and_expiry(self):
        """开发接入凭据、版本和绝对有效期均校验。"""
        for secret, version in (("bad", 1), ("local-secret", 2)):
            with self.assertRaises(ControlError):
                self.service.login(secret, "a" * 64, "Player", version)
        self.now += 3601
        with self.assertRaises(ControlError):
            self.create()

    def test_player_team_map_and_version(self):
        """阵营和地图由服务端确认，旧版本不能覆盖更新后的 Bot 配置。"""
        room = self.create()["snapshot"]
        changed = self.service.change(self.token, "team-1", room["id"], 1, team=1)
        self.assertEqual(changed["snapshot"]["members"][0]["team"], 1)
        self.assertEqual(changed["snapshot"]["revision"], 2)
        with self.assertRaises(ControlError) as error:
            self.service.change(self.token, "old-map", room["id"], 1, map_name="mp_cross_fire")
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        with self.assertRaises(ControlError) as error:
            self.service.change(self.token, "unknown-map", room["id"], 2, map_name="mp_uninstalled")
        self.assertEqual(error.exception.code, "UNSUPPORTED_CONTENT")
        self.assertEqual(self.service.sync(self.token)["snapshot"]["revision"], 2)

    def test_new_connection_count_before_room_creation(self):
        """大厅连接必须被统计，不能把在线数错误地当成房间成员数。"""
        self.assertEqual(self.service.connected_players(), 1)
        self.now += 16
        self.assertEqual(self.service.connected_players(), 0)

    def test_player_team_map_and_version(self):
        """阵营和地图由服务端确认，旧版本不能覆盖更新后的 Bot 配置。"""
        room = self.create()["snapshot"]
        changed = self.service.change(self.token, "team-1", room["id"], 1, team=1)
        self.assertEqual(changed["snapshot"]["members"][0]["team"], 1)
        self.assertEqual(changed["snapshot"]["revision"], 2)
        with self.assertRaises(ControlError) as error:
            self.service.change(self.token, "old-map", room["id"], 1, map_name="mp_cross_fire")
        self.assertEqual(error.exception.code, "REVISION_CONFLICT")
        with self.assertRaises(ControlError) as error:
            self.service.change(self.token, "unknown-map", room["id"], 2, map_name="mp_uninstalled")
        self.assertEqual(error.exception.code, "UNSUPPORTED_CONTENT")
        self.assertEqual(self.service.sync(self.token)["snapshot"]["revision"], 2)

    def test_new_connection_count_before_room_creation(self):
        """大厅连接必须被统计，不能把在线数错误地当成房间成员数。"""
        self.assertEqual(self.service.connected_players(), 1)
        self.now += 16
        self.assertEqual(self.service.connected_players(), 0)


class PlayerHttpTests(unittest.TestCase):
    """真实 HTTP 权限边界和 SSE 首帧。"""
    def test_player_admin_boundaries_and_sse(self):
        """玩家不能改 Bot，管理员不能当玩家，推送必须是权威版本。"""
        import urllib.request
        service = ServiceInstance()
        try:
            secret = json.loads((service.data_dir / "development-access.json").read_text())["secret"]
            status, login = service.request("POST", "/api/player/login", body={"identity": "c" * 64,
                "display_name": "Native", "protocol_version": 1}, headers={"X-Development-Key": secret})
            self.assertEqual(status, 200, login)
            token = login["token"]
            status, created = service.request("POST", "/api/player/rooms", token=token, body={"request_id": "http-create"})
            self.assertEqual(status, 200, created)
            room = created["snapshot"]
            body = {"request_id": "bots-http", "expected_room_revision": 1, "count": 2, "team": 1, "difficulty": "normal"}
            status, _ = service.request("POST", f"/api/rooms/{room['id']}/bots", token=token, body=body)
            self.assertEqual(status, 401)
            _, admin = service.login(service.initial_password())
            status, _ = service.request("POST", "/api/player/heartbeat", token=admin["token"], body={})
            self.assertEqual(status, 401)
            status, changed = service.request("POST", f"/api/rooms/{room['id']}/bots", token=admin["token"], body=body)
            self.assertEqual(status, 200, changed)
            request = urllib.request.Request(f"http://127.0.0.1:{service.port}/api/player/events",
                                             headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=5) as stream:
                self.assertEqual(stream.readline().strip(), b"event: sync")
                event = json.loads(stream.readline()[6:])
                self.assertEqual(event["snapshot"]["revision"], 2)
                self.assertEqual(event["snapshot"]["bots"]["count"], 2)
        finally:
            service.stop()
