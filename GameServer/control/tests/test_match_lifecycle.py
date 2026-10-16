"""M3 对局生命周期测试：状态机、权限、幂等、失败恢复与 HTTP 接线。

单元部分直接驱动 MatchService._step（不走 2 秒观察线程），用
FakeSupervisor 的脚本化采样精确推进 LOADING→READY→RUNNING→FINISHED；
HTTP 部分经 ServiceInstance 验证路由装配与权限边界。
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from support import ReadyProbeStub, ServiceInstance

from codol_control.application.match_service import MatchService
from codol_control.application.player_rooms import PlayerRooms
from codol_control.application.runtime_service import RuntimeService
from codol_control.domain.errors import (ControlError, ForbiddenError, InvalidStateError)
from codol_control.infrastructure.database import Database, run_migrations
from codol_control.infrastructure.online_repository import SqliteOnlineRepository
from codol_control.runtime.contract import load_contract
from codol_control.runtime.fake import FakeProber, FakeSupervisor

CONTROL_ROOT = Path(__file__).resolve().parents[1]
GAMESERVER_ROOT = CONTROL_ROOT.parent
VERIFIED = [{"map": "mp_cross_fire", "gametype": "war"}]


def ready_samples(n=4, start=1000, humans="0", uids=""):
    """构造 n 次满足契约就绪判定的采样（时钟严格递增、总推进≥4000ms）。"""
    step = max(1200, (4000 // max(1, n - 1)) + 1)
    return [{"alive": True, "running": "1", "map": "mp_cross_fire", "gametype": "war",
             "type": "mp", "humans": humans, "bots": "0", "uids": uids,
             "active_humans": humans, "active_uids": uids,
             "simulation_ms": str(start + i * step)} for i in range(n)]


@contextlib.contextmanager
def _no_sleep(module):
    """临时把监督器模块的 time.sleep 变为空操作（等待窗口用真实时钟判定）。"""
    with unittest.mock.patch.object(module.time, "sleep", lambda _s: None):
        yield


class _FakeRunningProcess:
    """假"仍在运行"的子进程：供监督器停止序列测试记录 wait 调用。

    真实子进程无法停在"运行中"状态等待 RCON，且本机不允许为测试长期占用
    战斗端口；停止序列本身是监督器的契约行为，用假进程即可精确核对。
    """

    def __init__(self):
        self.returncode = 0
        self.waited = 0

    def poll(self):
        return None  # 仍在运行

    def wait(self, timeout=None):
        self.waited += 1


class MatchLifecycleTests(unittest.TestCase):
    """不启动 HTTP 的生命周期用例；观察线程由手动 _step 代替。"""

    def setUp(self):
        """每个用例独立临时库与假监督器，避免状态串扰。"""
        self._tmp = tempfile.TemporaryDirectory(prefix="codol-match-test-")
        root = Path(self._tmp.name)
        self.db = Database(root / "control.db")
        run_migrations(self.db, CONTROL_ROOT / "migrations")
        self.repository = SqliteOnlineRepository(self.db)
        contract = load_contract(GAMESERVER_ROOT)
        self.runtime = RuntimeService(ReadyProbeStub(contract), cache_seconds=600)
        self.runtime.probe(force=True)
        self.supervisor = FakeSupervisor()
        import secrets
        self.service = MatchService(self.repository, time.time, secrets.token_hex,
                                     self.supervisor, VERIFIED, self.runtime)
        self.rooms = PlayerRooms(self.repository, time.time, secrets.token_hex,
                                 "dev-secret", {"mp_cross_fire", "mp_estate_tropical"})
        login = self.rooms.login("dev-secret", "i" * 64, "玩家甲", 1)
        self.token = login["token"]
        self.native_id = login["player"]["native_id"]
        self.rooms.create(self.token, "r1", "mp_cross_fire", "war")

    def tearDown(self):
        self.service.stop_observer()
        self.db.close()
        self._tmp.cleanup()

    def _room(self):
        """读取当前权威房间快照。"""
        with self.repository.transaction() as u:
            return next(iter(u.rooms()))

    def _match(self, match_id):
        with self.repository.transaction() as u:
            return u.match(match_id)

    def test_owner_start_to_finished(self):
        """完整链路：房主开局→就绪→入场→离场→FINISHED，房间恢复 OPEN。"""
        result = self.service.start("player", self.token, self._room()["id"], "req-1")
        match_id = result["match_id"]
        self.assertEqual(len(self.supervisor.launches), 1)
        match = self._match(match_id)
        self.assertEqual(match["state"], "LOADING")
        self.assertEqual(self._room()["state"], "STARTING")
        # 四次递增采样 → READY：房间 IN_MATCH，快照携带入场信息与对局视图。
        self.supervisor.script = ready_samples(4)
        for _ in range(4):
            self.service._step()
        match = self._match(match_id)
        self.assertEqual(match["state"], "READY")
        room = self._room()
        self.assertEqual(room["state"], "IN_MATCH")
        self.assertEqual(room["match"]["entry"]["port"], match["port"])
        self.assertEqual(room["match"]["map"], "mp_cross_fire")
        # 玩家入场（humans=1 且 uids 命中成员 native_id）→ RUNNING。
        uid = f"{self.native_id >> 32:08x}{self.native_id & 0xffffffff:08x}"
        self.supervisor.script = ready_samples(2, humans="1", uids=uid)
        self.service._step()
        self.assertEqual(self._match(match_id)["state"], "RUNNING")
        self.assertEqual(self._room()["match"]["state"], "RUNNING")
        self.assertGreater(self._room()["revision"], room["revision"])
        self.assertEqual(self._match(match_id)["result"]["entered_uids"], [uid])
        # 玩家离场 → FINISHED，房间回 OPEN 且不再携带对局字段。
        self.supervisor.script = ready_samples(2)
        self.service._step()
        self.assertEqual(self._match(match_id)["state"], "FINISHED")
        room = self._room()
        self.assertEqual(room["state"], "OPEN")
        self.assertNotIn("match", room)
        self.assertIn(match_id, self.supervisor.stops)

    def test_player_cancel_and_restart(self):
        """房主取消准备后房间解锁；重试不重复停局，旧请求不影响下一局。"""
        room_id = self._room()["id"]
        first = self.service.start("player", self.token, room_id, "ready-1")["match_id"]
        result = self.service.cancel_player(self.token, room_id, first, "cancel-1")
        self.assertEqual(result["match"]["state"], "CANCELLED")
        self.assertEqual(self._room()["state"], "OPEN")
        self.assertTrue(all(not p["ready"] for p in self._room()["members"]))
        second = self.service.start("player", self.token, room_id, "ready-2")["match_id"]
        self.assertEqual(self.service.cancel_player(self.token, room_id, first, "cancel-1"), result)
        self.assertEqual(self._match(second)["state"], "LOADING")
        self.assertNotIn(second, self.supervisor.stops)

    def test_foreign_player_cannot_cancel(self):
        """取消接口不能复用管理员权限，其他玩家和错误房间均拒绝。"""
        room_id = self._room()["id"]
        match_id = self.service.start("player", self.token, room_id, "ready")["match_id"]
        other = self.rooms.login("dev-secret", "j" * 64, "玩家乙", 1)["token"]
        with self.assertRaises(ForbiddenError):
            self.service.cancel_player(other, room_id, match_id, "cancel")
        with self.assertRaises(ForbiddenError):
            self.service.cancel_player(self.token, "wrong-room", match_id, "cancel")
        self.assertEqual(self.supervisor.stops, [])

    def test_cancel_claim_prevents_ready_publication(self):
        """停进程等待期间运行观察轮，不能再次发出可入场通知。"""
        room_id = self._room()["id"]
        match_id = self.service.start("player", self.token, room_id, "ready")["match_id"]
        original_stop = self.supervisor.stop
        def stop_with_observer(handle):
            """在停止窗口模拟观察线程，验证 FINISHING 屏障。"""
            self.assertEqual(self._match(match_id)["state"], "FINISHING")
            self.supervisor.script = ready_samples(4)
            for _ in range(4):
                self.service._step()
            self.assertNotIn("match", self._room())
            return original_stop(handle)
        self.supervisor.stop = stop_with_observer
        self.service.cancel_player(self.token, room_id, match_id, "cancel")
        self.assertEqual(self._match(match_id)["state"], "CANCELLED")

    def test_cancel_stop_failure_can_retry(self):
        """监督器异常必须释放收尾占有权，同一个取消请求可以再次执行。"""
        room_id = self._room()["id"]
        match_id = self.service.start("player", self.token, room_id, "ready")["match_id"]
        with unittest.mock.patch.object(self.supervisor, "stop", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                self.service.cancel_player(self.token, room_id, match_id, "cancel")
        self.assertEqual(self._match(match_id)["state"], "LOADING")
        self.service.cancel_player(self.token, room_id, match_id, "cancel")
        self.assertEqual(self._room()["state"], "OPEN")

    def test_connected_or_foreign_player_does_not_count_as_entered(self):
        """仍在载图、缺少新字段、错误身份均不能推进 RUNNING。"""
        match_id = self.service.start("admin", None, self._room()["id"], "entry-check")["match_id"]
        self.supervisor.script = ready_samples(4)
        for _ in range(4):
            self.service._step()
        uid = f"{self.native_id:016x}"
        loading = ready_samples(1, humans="1", uids=uid)[0]
        loading.update(active_humans="0", active_uids="-")
        legacy = {k: v for k, v in loading.items() if not k.startswith('active_')}
        foreign = ready_samples(1, humans="1", uids="0000000000000001")[0]
        for sample in (loading, legacy, foreign):
            self.supervisor.script = [sample]
            self.service._step()
            self.assertEqual(self._match(match_id)["state"], "READY")

    def test_event_sequence_recorded(self):
        """事件类型按状态推进落库：Preparing→Ready→Started→Finished。"""
        room_id = self._room()["id"]
        match_id = self.service.start("admin", None, room_id, "req-a")["match_id"]
        self.supervisor.script = ready_samples(4)
        for _ in range(4):
            self.service._step()
        uid = f"{self.native_id >> 32:08x}{self.native_id & 0xffffffff:08x}"
        self.supervisor.script = ready_samples(2, humans="1", uids=uid)
        self.service._step()
        self.supervisor.script = ready_samples(2)
        self.service._step()
        with self.repository.transaction() as u:
            types = [e["type"] for e in u.events(room_id, 0)]
        for expected in ("MatchPreparing", "MatchReady", "MatchStarted", "MatchFinished"):
            self.assertIn(expected, types)

    def test_non_owner_cannot_start(self):
        """非房主玩家开局被拒绝（当前单真人即所有者，另建会话验证）。"""
        login2 = self.rooms.login("dev-secret", "j" * 64, "玩家乙", 1)
        with self.assertRaises(ForbiddenError):
            self.service.start("player", login2["token"], self._room()["id"], "req-x")

    def test_unverified_content_rejected(self):
        """房间配置可换到未战斗验证的地图，但开局必须拒绝。"""
        room = self._room()
        self.rooms.change(self.token, "c1", room["id"], room["revision"], map_name="mp_estate_tropical")
        with self.assertRaises(ControlError) as ctx:
            self.service.start("player", self.token, self._room()["id"], "req-u")
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self._room()["state"], "OPEN")

    def test_duplicate_start_and_idempotent_retry(self):
        """同一 request_id 重试返回原结果；进行中的对局阻止再次开局。"""
        room_id = self._room()["id"]
        first = self.service.start("player", self.token, room_id, "req-dup")
        again = self.service.start("player", self.token, room_id, "req-dup")
        self.assertEqual(first["match_id"], again["match_id"])
        with self.assertRaises(InvalidStateError):
            self.service.start("admin", None, room_id, "req-other")

    def test_launch_failure_keeps_room_open_and_retryable(self):
        """启动失败：对局 FAILED、房间 OPEN、同 request_id 可重试。"""
        self.supervisor.launch_error = ControlError("RUNTIME_NOT_READY", "端口占用")
        room_id = self._room()["id"]
        with self.assertRaises(ControlError):
            self.service.start("player", self.token, room_id, "req-f")
        self.assertEqual(self._room()["state"], "OPEN")
        with self.repository.transaction() as u:
            matches = u.recent_matches(4)
        self.assertEqual(matches[-1]["state"], "FAILED")
        # 失败不落幂等回执：同一 request_id 修复后可以再次开局。
        self.supervisor.launch_error = None
        result = self.service.start("player", self.token, room_id, "req-f")
        self.assertEqual(self._match(result["match_id"])["state"], "LOADING")

    def test_process_death_fails_match(self):
        """战斗进程提前退出 → FAILED，房间恢复 OPEN。"""
        match_id = self.service.start("admin", None, self._room()["id"], "req-p")["match_id"]
        self.supervisor.script = [{"alive": False, "returncode": 1}]
        self.service._step()
        self.assertEqual(self._match(match_id)["state"], "FAILED")
        self.assertEqual(self._room()["state"], "OPEN")

    def test_cancel_during_loading(self):
        """LOADING 阶段取消 → CANCELLED，进程被停止，房间恢复 OPEN。"""
        match_id = self.service.start("admin", None, self._room()["id"], "req-c")["match_id"]
        self.service.cancel(match_id, "cancel-1", "测试取消")
        self.assertEqual(self._match(match_id)["state"], "CANCELLED")
        self.assertEqual(self._room()["state"], "OPEN")
        self.assertIn(match_id, self.supervisor.stops)

    def test_probe_gate_blocks_start(self):
        """未完成运行时预检时开局被明确拒绝（不静默跳过检查）。"""
        fresh = RuntimeService(FakeProber(load_contract(GAMESERVER_ROOT)), cache_seconds=600)
        self.service._runtime = fresh
        with self.assertRaises(ControlError) as ctx:
            self.service.start("admin", None, self._room()["id"], "req-g")
        self.assertEqual(ctx.exception.code, "RUNTIME_NOT_READY")

    def test_real_supervisor_collect_with_row_handle(self):
        """对局行重建的句柄（无 exe、started_at 为 ISO 字符串）必须能收集证据。

        实机管线曾因此两次 500：collect 假设句柄来自 launch（浮点 started_at、
        含 exe 字段）。观察/停止路径重建的句柄没有这些保证。
        """
        from codol_control.runtime.supervisor import NativeSupervisor
        supervisor = NativeSupervisor(load_contract(GAMESERVER_ROOT), Path(self._tmp.name) / "runs")
        run_dir = Path(self._tmp.name) / "runs" / "aa0000000001"
        (run_dir / "profiles").mkdir(parents=True)
        handle = {"match_id": "rowhandle", "run_dir": str(run_dir), "pid": None, "port": 28969,
                  "map": "mp_cross_fire", "gametype": "war",
                  "started_at": "2026-10-15T00:00:00+00:00"}
        summary = supervisor.collect(handle, {"reason": "players_left"})
        self.assertEqual(summary["run_dir"], str(run_dir))
        report = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(report["match_id"], "rowhandle")
        self.assertIsNone(report["seconds"])
        # 未知句柄的停止与状态也必须安全返回事实，而不是抛异常。
        stop = supervisor.stop(handle)
        self.assertIn("note", stop)
        status = supervisor.status(handle)
        self.assertFalse(status["alive"])

    def test_real_supervisor_stop_keeps_state_for_collect_redaction(self):
        """stop 不得提前弹出监督状态：RCON 密码会被引擎写进 qconsole.log，
        必须由随后的 collect 脱敏。实机管线曾因此泄漏 48 位十六进制密码。"""
        import subprocess
        import sys as _sys
        from codol_control.runtime.supervisor import NativeSupervisor
        supervisor = NativeSupervisor(load_contract(GAMESERVER_ROOT), Path(self._tmp.name) / "runs")
        run_dir = Path(self._tmp.name) / "runs" / "bb0000000002"
        (run_dir / "profiles").mkdir(parents=True)
        password = "a" * 48
        # 用一个立即退出的真实子进程与含密码的日志模拟引擎把 RCON 命令写盘。
        process = subprocess.Popen([_sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        (run_dir / "main").mkdir()
        (run_dir / "main/qconsole.log").write_text(f'rcon "{password}" quit\n', encoding="utf-8")
        supervisor._live["redact1"] = {"process": process, "password": password,
                                       "run_dir": run_dir, "samples": [], "started": 0.0}
        handle = {"match_id": "redact1", "run_dir": str(run_dir), "pid": process.pid, "port": 28969,
                  "map": "mp_cross_fire", "gametype": "war", "started_at": 0.0}
        supervisor.stop(handle)
        # stop 之后、collect 之前监督状态必须仍在（脱敏依赖密码）。
        self.assertIn("redact1", supervisor._live)
        supervisor.collect(handle, {"reason": "players_left"})
        content = (run_dir / "main/qconsole.log").read_text(encoding="utf-8")
        self.assertNotIn(password, content)
        self.assertIn("<redacted>", content)
        self.assertNotIn("redact1", supervisor._live)

    def test_real_supervisor_stop_sends_endmatch_before_quit(self):
        """契约 revision 3：停止对局必须先 sh1x_endmatch 再 quit。

        实机 M3 验证曾因只发 quit 而失败：服务端进程直接消失，客户端收不到
        原生离场命令，只能等 UDP 超时弹 EXE_ERR_SERVER_TIMEOUT，无法"明确
        断开并回房"。优雅收尾后客户端走引擎自身的 EXE_MATCHENDED 路径。
        """
        from codol_control.runtime import supervisor as supervisor_module
        from codol_control.runtime.supervisor import NativeSupervisor
        supervisor = NativeSupervisor(load_contract(GAMESERVER_ROOT), Path(self._tmp.name) / "runs")
        run_dir = Path(self._tmp.name) / "runs" / "cc0000000003"
        (run_dir / "profiles").mkdir(parents=True)
        process = _FakeRunningProcess()
        supervisor._live["endmatch1"] = {"process": process, "password": "p", "run_dir": run_dir,
                                         "samples": [], "started": 0.0}
        handle = {"match_id": "endmatch1", "run_dir": str(run_dir), "pid": 4242, "port": 28969,
                  "map": "mp_cross_fire", "gametype": "war", "started_at": 0.0}
        replies = {"sh1x_endmatch": "sh1x_endmatch sent=1\n",
                   "sh1x_status": "sh1x_status map=mp_cross_fire gametype=war running=1 humans=0 uids=- "
                                  "simulation_ms=9000 active_humans=0 active_uids=-\n"}
        commands = []

        def fake_rcon(password, command):
            commands.append(command)
            return replies.get(command, "")

        supervisor._rcon = fake_rcon
        with _no_sleep(supervisor_module):
            outcome = supervisor.stop(handle)
        # 顺序：优雅结束 → 轮询确认 humans=0 → 才 quit。
        self.assertEqual(commands[0], "sh1x_endmatch")
        self.assertIn("sh1x_status", commands)
        self.assertEqual(commands[-1], "quit")
        self.assertEqual(outcome["graceful_endmatch"], 1)
        self.assertFalse(outcome["forced_cleanup"])

    def test_real_supervisor_stop_quits_even_if_humans_stay(self):
        """等不到 humans=0 也不得跳过 quit（否则进程泄漏、端口不释放）。"""
        from unittest import mock as _mock
        from codol_control.runtime import supervisor as supervisor_module
        from codol_control.runtime.supervisor import NativeSupervisor
        supervisor = NativeSupervisor(load_contract(GAMESERVER_ROOT), Path(self._tmp.name) / "runs")
        run_dir = Path(self._tmp.name) / "runs" / "dd0000000004"
        (run_dir / "profiles").mkdir(parents=True)
        process = _FakeRunningProcess()
        supervisor._live["endmatch2"] = {"process": process, "password": "p", "run_dir": run_dir,
                                         "samples": [], "started": 0.0}
        handle = {"match_id": "endmatch2", "run_dir": str(run_dir), "pid": 4243, "port": 28969,
                  "map": "mp_cross_fire", "gametype": "war", "started_at": 0.0}
        commands = []

        def fake_rcon(password, command):
            commands.append(command)
            if command == "sh1x_status":
                return "sh1x_status map=mp_cross_fire running=1 humans=1 uids=1a2b simulation_ms=9000\n"
            return ""

        supervisor._rcon = fake_rcon
        # 轮询窗口内 humans 始终为 1：用受控时钟/跳过睡眠跑满等待窗口。
        clock = iter(list(range(0, 20)))
        with _mock.patch.object(supervisor_module.time, "sleep", lambda _s: None), \
             _mock.patch.object(supervisor_module.time, "monotonic", lambda: next(clock)):
            outcome = supervisor.stop(handle)
        self.assertEqual(commands[0], "sh1x_endmatch")
        self.assertEqual(commands[-1], "quit")
        self.assertGreater(commands.count("sh1x_status"), 1)
        self.assertIsNone(outcome["graceful_endmatch"])  # 无应答按"未知"处理

    def test_contract_allows_endmatch_command(self):
        """契约 revision 3 的白名单必须包含 sh1x_endmatch，与监督器一致。"""
        contract = load_contract(GAMESERVER_ROOT)
        document = contract.document
        self.assertIn("sh1x_endmatch", document["control_channel"]["command_whitelist_v1"])
        self.assertEqual(document["stop"]["graceful_command"], "sh1x_endmatch")
        self.assertEqual(document["revision"], 3)

    def test_parse_sent_accepts_only_endmatch_line(self):
        """sent 解析只认 sh1x_endmatch 行；其他应答返回 None（未知而非 0）。"""
        from codol_control.runtime.supervisor import NativeSupervisor
        self.assertEqual(NativeSupervisor._parse_sent("sh1x_endmatch sent=2\n"), 2)
        self.assertIsNone(NativeSupervisor._parse_sent(""))
        self.assertIsNone(NativeSupervisor._parse_sent("sh1x_status humans=0\n"))

    def test_restart_recovery_interrupts_active_match(self):
        """服务重启恢复：未终结对局按 pid 归属终止并标 FAILED，房间回 OPEN。"""
        match_id = self.service.start("admin", None, self._room()["id"], "req-r")["match_id"]
        old = self._match(match_id)
        # 模拟重启：新服务实例（新监督器）+ 注入归属裁决。
        import secrets
        reborn = MatchService(self.repository, time.time, secrets.token_hex,
                              FakeSupervisor(adoption_verdicts=[{"matched": True, "terminated": True}]),
                              VERIFIED, self.runtime)
        recovered = reborn.recover()
        self.assertEqual(recovered[0]["match_id"], match_id)
        self.assertTrue(recovered[0]["terminated"])
        match = self._match(match_id)
        self.assertEqual(match["state"], "FAILED")
        self.assertIn("重启", match["error"])
        self.assertEqual(self._room()["state"], "OPEN")


class MatchHttpTests(unittest.TestCase):
    """HTTP 接线：路由、权限与总览事实（假监督器，不启动进程）。"""

    @classmethod
    def setUpClass(cls):
        cls.service = ServiceInstance(probe_ready=True)
        status, body = cls.service.login(cls.service.initial_password())
        assert status == 200, body
        cls.admin_token = body["token"]

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()

    def _player_room(self, name="HTTP玩家"):
        """经玩家通道建立登录与房间（与真实客户端一致的路径）。"""
        import json as _json
        import urllib.request
        access = _json.loads((self.service.data_dir / "development-access.json").read_text(encoding="utf-8"))["secret"]
        req = urllib.request.Request(
            self.service.base + "/api/player/login", method="POST",
            headers={"Content-Type": "application/json", "X-Development-Key": access},
            data=_json.dumps({"identity": "h" * 64, "display_name": name, "protocol_version": 1}).encode())
        with urllib.request.urlopen(req, timeout=10) as resp:
            login = _json.loads(resp.read())
        token = login["token"]
        status, body = self.service.request("POST", "/api/player/rooms", token=token,
                                            body={"request_id": "http-create"})
        assert status == 200, body
        return token, body["snapshot"]

    def test_admin_start_and_stop_over_http(self):
        """管理员经 HTTP 开局/停止；房间状态与对局事实如实呈现。"""
        token, room = self._player_room()
        self.service.request("POST", "/api/runtime/probe", token=self.admin_token)
        status, body = self.service.request(
            "POST", f"/api/rooms/{room['id']}/match/start", token=self.admin_token,
            body={"request_id": "http-start"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "LOADING")
        status, runtime = self.service.request("GET", "/api/runtime", token=self.admin_token)
        self.assertEqual(runtime["match_processes"], 1)
        match_id = body["match_id"]
        status, stopped = self.service.request(
            "POST", f"/api/matches/{match_id}/stop", token=self.admin_token,
            body={"request_id": "http-stop"})
        self.assertEqual(status, 200, stopped)
        self.assertEqual(stopped["match"]["state"], "FINISHED")
        status, rooms = self.service.request("GET", "/api/rooms", token=self.admin_token)
        self.assertEqual(rooms["items"][0]["state"], "OPEN")

    def test_player_match_start_requires_owner(self):
        """房主玩家可经 /player/match-start 开局；无令牌被拒。"""
        token, room = self._player_room()
        self.service.request("POST", "/api/runtime/probe", token=self.admin_token)
        status, body = self.service.request("POST", "/api/player/match-start", token=token,
                                            body={"request_id": "p-start", "room_id": room["id"]})
        self.assertEqual(status, 200, body)
        status, _ = self.service.request("POST", f"/api/matches/{body['match_id']}/stop",
                                         token=self.admin_token, body={"request_id": "p-stop"})
        self.assertEqual(status, 200)
        # 未认证请求被拒。
        status, _ = self.service.request("POST", "/api/player/match-start",
                                         body={"request_id": "anon", "room_id": room["id"]})
        self.assertEqual(status, 401)

    def test_player_cancel_over_http(self):
        """真实 HTTP 路由需玩家身份；取消后返回 OPEN，重试回执一致。"""
        token, room = self._player_room()
        self.service.request("POST", "/api/runtime/probe", token=self.admin_token)
        status, started = self.service.request("POST", "/api/player/match-start", token=token,
            body={"request_id": "cancel-http-start", "room_id": room["id"]})
        self.assertEqual(status, 200, started)
        body = {"request_id": "cancel-http", "room_id": room["id"], "match_id": started["match_id"]}
        status, _ = self.service.request("POST", "/api/player/match-cancel", body=body)
        self.assertEqual(status, 401)
        status, result = self.service.request("POST", "/api/player/match-cancel", token=token, body=body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["room"]["state"], "OPEN")
        status, retry = self.service.request("POST", "/api/player/match-cancel", token=token, body=body)
        self.assertEqual(status, 200, retry)
        self.assertEqual(retry, result)


if __name__ == "__main__":
    unittest.main()
