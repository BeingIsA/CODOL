"""M3 用例：开局、取消、停止、观察推进与重启恢复。

对局状态机（domain.match_rules）由本服务的单一观察线程驱动：
命令方法只做校验、落库与启动意图；进程监督全部经 BattleSupervisor 端口。
纪律：
- 所有状态落库都在仓库事务内（房间快照、matches 行、事件、幂等回执同事务）；
- 可能阻塞的进程操作（launch 的端口预检、stop 的 15 秒等待、collect 的
  证据收集）一律在事务外执行，数据库写锁不等待子进程；
- 失败路径总是把对局推向终态并让房间恢复 OPEN，不留下"看起来还在开局"的房间。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from .ports import BattleSupervisor, Clock, OnlineRepository
from ..domain.errors import (BattleNotReadyError, ControlError, ForbiddenError,
                             InvalidArgumentError, InvalidStateError)
from ..domain import match_rules
from .player_rooms import digest


class MatchService:
    """对局生命周期的唯一入口；观察线程与命令线程共用仓库串行化。"""

    def __init__(self, repository: OnlineRepository, clock: Clock, random_hex,
                 supervisor: BattleSupervisor, verified_content: list[dict], runtime,
                 observe_interval: float = 2.0):
        """runtime 为 RuntimeService（开局前核对缓存预检结果，不触发重探）。"""
        self._repository = repository
        self._clock = clock
        self._random = random_hex
        self._supervisor = supervisor
        self._verified = list(verified_content)
        self._runtime = runtime
        self._interval = observe_interval
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()
        self.fake = getattr(supervisor, "fake", False)

    # ---- 命令 ----

    def start(self, actor: str, token: str | None, room_id: str, request_id: str) -> dict:
        """开局：房主玩家或管理员发起。进程启动在事务外；启动失败不落
        幂等回执（同 request_id 重试可以再次尝试），成功才落回执。"""
        if not 1 <= len(request_id) <= 96:
            raise InvalidArgumentError("request_id 长度需要为 1～96。")
        intent = {"type": "StartMatch", "room_id": room_id}
        fingerprint = digest(json.dumps(intent, ensure_ascii=False, sort_keys=True))
        now_iso = self._iso()

        with self._repository.transaction() as u:
            room = next((r for r in u.rooms() if r["id"] == room_id), None)
            if actor == "player":
                session = self._session(u, token)
                if room is None or room.get("owner_id") != session["player_id"]:
                    raise ForbiddenError("只有房主可以开局。")
                identity = session["player_id"]
            else:
                if room is None:
                    raise InvalidArgumentError("房间不存在。")
                identity = "admin"
            receipt = u.receipt(identity, request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise InvalidArgumentError("同一 request_id 不可用于不同操作或参数。")
                return receipt["result"]
            if any(m["room_id"] == room_id and m["state"] in match_rules.MATCH_ACTIVE_STATES
                   for m in u.active_matches()):
                raise InvalidStateError("该房间已有进行中的对局。")
            room, config = match_rules.start_match(room, self._verified, now_iso)
            match = {"id": self._random(12), "room_id": room_id, "state": "ALLOCATING",
                     "created_at": now_iso, "updated_at": now_iso, "config": config,
                     "run_dir": "", "pid": None, "port": None, "entry": {}, "result": {},
                     "error": "", "finished_at": None}
            u.save_room(room)
            u.save_match(match)
            event_id = u.emit(room, self._clock(), "MatchPreparing")
            pending = dict(match)
            pending_room = dict(room)

        # 事务外启动：契约预检（缓存结果，不重算）→ 端口独占 → 进程创建。
        # 领域错误（预检/端口）与系统错误（文件/参数）都走失败收敛路径。
        try:
            probe = self._runtime.peek()
            if probe is None or not probe.ready:
                raise BattleNotReadyError("战斗运行时预检未通过，请先在运行时页完成深度探测。")
            handle = self._supervisor.launch(pending["id"], config)
        except (ControlError, OSError, ValueError) as exc:
            with self._repository.transaction() as u:
                failed = {**pending, "state": "FAILED", "error": str(exc), "finished_at": self._iso()}
                settled_room = match_rules.match_settled_room(pending_room)
                u.save_match(failed)
                u.save_room(settled_room)
                u.emit(settled_room, self._clock(), "MatchFailed")
            raise

        with self._repository.transaction() as u:
            loading = {**pending, "state": "LOADING", "run_dir": handle["run_dir"],
                       "pid": handle["pid"], "port": handle["port"], "updated_at": self._iso()}
            u.save_match(loading)
            result = {"match_id": loading["id"], "room": pending_room,
                      "event_id": event_id, "state": loading["state"]}
            u.save_receipt(identity, request_id, {"fingerprint": fingerprint, "result": result})
        self._wake.set()
        return result

    def cancel(self, match_id: str, request_id: str, reason: str = "管理员取消") -> dict:
        """取消对局：可取消阶段内终止进程并恢复房间；语义为 CANCELLED。"""
        return self._terminate(match_id, request_id, "CANCELLED", reason)

    def cancel_player(self, token: str, room_id: str, match_id: str, request_id: str) -> dict:
        """房主取消准备：绑定具体对局，在终止事务内校验玩家与房间归属。"""
        return self._terminate(match_id, request_id, "CANCELLED", "房主取消准备",
                               player_token=token, player_room_id=room_id)

    def stop(self, match_id: str, request_id: str, reason: str = "管理员停止") -> dict:
        """停止对局：按正常结束路径（FINISHED）停止进程；仅管理员路由可达。"""
        return self._terminate(match_id, request_id, "FINISHED", reason)

    def stop_room_match(self, room_id: str, request_id: str, reason: str) -> dict | None:
        """停掉某个房间的活动对局；没有活动对局返回 None（调用方视为"无需停局"）。

        关闭房间必须先走这里：战斗进程只有在收到 endmatch 后才会让已入场的
        客户端走引擎自身断开路径回前台，直接关房会让客户端干等 UDP 超时并弹
        EXE_ERR_SERVER_TIMEOUT（M3 实测教训）。停止过程可能等待进程退出，
        因此本方法必须在事务外完成（内部已是事务外执行）。
        """
        with self._repository.transaction() as u:
            active = next((m for m in u.active_matches() if m["room_id"] == room_id), None)
        if active is None:
            return None
        if active["state"] == "FINISHING":
            # 收尾中的对局不可再取消（观察线程正在把它推向终态）；此时重复停局
            # 会被状态机拒绝并让整个关房请求失败，因此视为"无需停局"。
            return None
        return self.stop(active["id"], request_id, reason)

    def _terminate(self, match_id: str, request_id: str, terminal: str, reason: str,
                   *, player_token: str | None = None, player_room_id: str | None = None) -> dict:
        """管理员停止/取消共用实现：事务外停止进程，事务内收敛状态。"""
        if not 1 <= len(request_id) <= 96:
            raise InvalidArgumentError("request_id 长度需要为 1～96。")
        fingerprint = digest(json.dumps({"type": terminal, "match_id": match_id}, sort_keys=True))
        with self._repository.transaction() as u:
            identity = "admin"
            if player_room_id is not None:
                session = self._session(u, player_token)
                room = next((r for r in u.rooms() if r["id"] == player_room_id), None)
                match = u.match(match_id)
                if (room is None or room.get("owner_id") != session["player_id"]
                        or match is None or match["room_id"] != player_room_id):
                    raise ForbiddenError("只能取消本人所主持房间的对局。")
                identity = session["player_id"]
            receipt = u.receipt(identity, request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise InvalidArgumentError("同一 request_id 不可用于不同操作或参数。")
                return receipt["result"]
            match = u.match(match_id)
            if match is None:
                raise InvalidArgumentError("对局不存在。")
            if match["state"] not in match_rules.MATCH_CANCELLABLE:
                raise InvalidStateError("对局已结束。")
            pending = dict(match)
            # 先占有收尾权，再在事务外停进程；观察线程不得把正在取消的对局
            # 再发布为 READY，也不能将停止中的进程误判为崩溃。
            u.save_match({**match, "state": "FINISHING", "updated_at": self._iso()})
        handle = self._handle(pending)
        try:
            outcome = self._supervisor.stop(handle)
            collect = self._supervisor.collect(handle, {"reason": terminal, "detail": reason})
        except Exception:
            # 停止/取证失败不能留下无人推进的 FINISHING；恢复可重试状态，
            # 若进程实际已退出，下一观察轮会按既有失败路径收敛并解锁房间。
            with self._repository.transaction() as u:
                current = u.match(match_id)
                if current and current["state"] == "FINISHING":
                    u.save_match({**current, "state": pending["state"]})
            raise
        with self._repository.transaction() as u:
            current = u.match(match_id)
            room = next((r for r in u.rooms() if r["id"] == current["room_id"]), None)
            settled = {**current, "state": terminal, "finished_at": self._iso(),
                       "error": "" if terminal == "FINISHED" else reason,
                       "result": {**current.get("result", {}), "stop": outcome, "evidence": collect}}
            if room:
                room = match_rules.match_settled_room(room)
                u.save_room(room)
                u.emit(room, self._clock(),
                       "MatchFinished" if terminal == "FINISHED" else "MatchCancelled")
            u.save_match(settled)
            result = {"match": self._view(settled), "room": room}
            u.save_receipt(identity, request_id, {"fingerprint": fingerprint, "result": result})
            return result

    # ---- 观察线程 ----

    def start_observer(self) -> None:
        """启动唯一观察线程；重复调用无效果。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._observe, name="match-observer", daemon=True)
        self._thread.start()

    def stop_observer(self) -> None:
        """停止观察线程（服务停服时调用；对局进程由 shutdown_matches 收尾）。"""
        self._stop_event.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=self._interval * 2 + 5)

    def _observe(self) -> None:
        """周期驱动状态推进；异常只记不退出，观察线程不允许静默消失。"""
        while not self._stop_event.is_set():
            self._wake.wait(timeout=self._interval)
            self._wake.clear()
            if self._stop_event.is_set():
                return
            try:
                self._step()
            except Exception:  # noqa: BLE001 - 下一周期重试；状态机按事务收敛
                logging.getLogger(__name__).exception("对局观察失败，下一周期重试")
                continue
            self._stop_event.wait(self._interval)

    def _step(self) -> None:
        """对每个活动对局采样一次；纯状态转换在事务内，进程操作在事务外。"""
        with self._repository.transaction() as u:
            actives = u.active_matches()
        for match in actives:
            if not match.get("run_dir"):
                continue  # ALLOCATING 且未启动完成：等待 start() 事务收尾。
            handle = self._handle(match)
            status = self._supervisor.status(handle)
            decision = None
            with self._repository.transaction() as u:
                current = u.match(match["id"])
                if current is None or current["state"] in (*match_rules.MATCH_TERMINAL_STATES, "FINISHING"):
                    continue
                room = next((r for r in u.rooms() if r["id"] == current["room_id"]), None)
                decision = self._advance(u, current, room, status)
            if decision:
                # 事务外停止进程并收集证据，再开事务收敛终态。
                self._settle(match["id"], decision, handle, status)

    def _advance(self, u, match: dict, room: dict | None, status: dict) -> dict | None:
        """按状态与采样推进；返回待事务外执行的收尾决定（或 None 表示已处理）。"""
        # 房间先判定：最后一名成员离场后房间即关闭，对局失去宿主。
        # 这一条要在进程存活判定之前，因为玩家主动离场时战斗进程可能
        # 已经自行退出，不该被记成“战斗进程提前退出”这类失败。
        if room is None or match_rules.room_is_closed(room):
            return {"kind": "finish", "message": "房间已关闭，对局结束。", "reason": "room_closed"}
        if not status.get("alive"):
            return {"kind": "fail", "message":
                    f"战斗进程提前退出（退出码 {status.get('returncode')}）。"}
        if match["state"] == "LOADING":
            return self._advance_loading(u, match, room, status)
        if match["state"] in ("READY", "RUNNING"):
            return self._advance_live(u, match, room, status)
        return None

    def _advance_loading(self, u, match: dict, room: dict | None, status: dict) -> dict | None:
        """LOADING→READY：契约就绪判定（≥4 采样、时钟严格递增、总推进≥4000ms）。"""
        samples = list(match.get("result", {}).get("samples", []))
        if status.get("simulation_ms"):
            samples.append({"simulation_ms": status["simulation_ms"], "at": self._iso()})
            interim = {**match, "updated_at": self._iso(),
                       "result": {**match.get("result", {}), "samples": samples[-16:]}}
            u.save_match(interim)
        ticks = [int(s["simulation_ms"]) for s in samples]
        if status.get("running") != "1" or status.get("map") != match["config"]["map"]:
            if self._clock() - self._parse_iso(match["created_at"]) > 120:
                return {"kind": "fail", "message": "战斗运行时就绪超时（120 秒）。"}
            return None
        ready = (len(samples) >= 4 and len(ticks) == len(samples)
                 and all(b > a for a, b in zip(ticks, ticks[1:]))
                 and ticks[-1] - ticks[0] >= 4000
                 and status.get("gametype") == match["config"]["gametype"]
                 and status.get("type") == "mp")
        if not ready:
            if self._clock() - self._parse_iso(match["created_at"]) > 180:
                return {"kind": "fail", "message": "战斗运行时模拟时钟未按契约推进。"}
            return None
        entry = {"host": "127.0.0.1", "port": match["port"],
                 "map": match["config"]["map"], "gametype": match["config"]["gametype"]}
        loaded = {**match, "state": "READY", "entry": entry, "updated_at": self._iso()}
        u.save_match(loaded)
        if room:
            room = match_rules.match_entered_room(loaded, room, self._iso())
            u.save_room(room)
            u.emit(room, self._clock(), "MatchReady")
        return None

    def _advance_live(self, u, match: dict, room: dict | None, status: dict) -> dict | None:
        """READY→RUNNING（成员入场）与 RUNNING 观察；humans 归零即结束。"""
        humans = int(status.get("humans", "0") or 0)
        # 原生状态使用定宽十六进制，房间 native_id 使用整数，必须先统一表示。
        # 缺少 active_* 字段时保持 READY，绝不把旧引擎的连接数误判为入场。
        member_ids = {f"{int(p['native_id']):016x}" for p in match["config"]["players"]}
        uids = set(status.get("active_uids", "").lower().split(","))
        entered = int(status.get("active_humans", "0") or 0) > 0 and bool(uids & member_ids)
        if match["state"] == "READY":
            if entered:
                started = {**match, "state": "RUNNING", "updated_at": self._iso(),
                           "result": {**match.get("result", {}),
                                      "entered_uids": sorted(uids & member_ids)}}
                u.save_match(started)
                if room:
                    room = match_rules.match_started_room(started, room)
                    u.save_room(room)
                    u.emit(room, self._clock(), "MatchStarted")
            return None
        # RUNNING：记录最近状态；humans 归零按正常路径结束（M3 无终局规则）。
        last = {**match, "updated_at": self._iso(),
                "result": {**match.get("result", {}), "last_status": {
                    k: status.get(k) for k in ("humans", "bots", "simulation_ms", "uids",
                                               "active_humans", "active_uids")}}}
        u.save_match(last)
        if humans == 0:
            return {"kind": "finish", "message": "玩家已全部离场。"}
        return None

    def _settle(self, match_id: str, decision: dict, handle: dict, status: dict) -> None:
        """事务外停止进程、收集证据；事务内写终态并恢复房间。"""
        if decision["kind"] == "finish":
            outcome = self._supervisor.stop(handle)
            collect = self._supervisor.collect(handle, {"reason": decision.get("reason", "players_left"),
                                                        "last_status": status})
            terminal, error, event = "FINISHED", "", "MatchFinished"
        else:
            try:
                outcome = self._supervisor.stop(handle)
                collect = self._supervisor.collect(handle, {"reason": "failed",
                                                            "error": decision["message"],
                                                            "last_status": status})
            except Exception:  # noqa: BLE001 - 失败路径证据收集不得再次抛出
                outcome, collect = {"note": "stop failed"}, {}
            terminal, error, event = "FAILED", decision["message"], "MatchFailed"
        with self._repository.transaction() as u:
            current = u.match(match_id)
            if current is None or current["state"] in match_rules.MATCH_TERMINAL_STATES:
                return
            room = next((r for r in u.rooms() if r["id"] == current["room_id"]), None)
            settled = {**current, "state": terminal, "error": error, "finished_at": self._iso(),
                       "result": {**current.get("result", {}), "stop": outcome, "evidence": collect}}
            if room:
                room = match_rules.match_settled_room(room)
                u.save_room(room)
                u.emit(room, self._clock(), event)
            u.save_match(settled)

    # ---- 查询与恢复 ----

    def status(self) -> dict:
        """当前活动对局与最近对局（管理页/总览用）。"""
        with self._repository.transaction() as u:
            actives = [self._view(m) for m in u.active_matches()]
            recent = [self._view(m) for m in u.recent_matches(8)]
            return {"active": actives, "recent": recent,
                    "match_processes": len(actives), "supervisor_fake": self.fake}

    def recover(self) -> list[dict]:
        """服务启动时核对未终结对局：子进程句柄已丢失，按 pid+可执行路径
        归属核对后终止，对局标 FAILED（中断），房间恢复 OPEN。"""
        recovered = []
        with self._repository.transaction() as u:
            actives = u.active_matches()
        for match in actives:
            verdict = {"matched": False, "terminated": False}
            if match.get("pid"):
                try:
                    verdict = self._supervisor.adopt_or_kill(match["id"], match["pid"],
                                                             match.get("run_dir", ""))
                except Exception:  # noqa: BLE001 - 恢复继续，逐局记录结果
                    verdict = {"matched": False, "terminated": False, "error": "adopt failed"}
            with self._repository.transaction() as u:
                current = u.match(match["id"])
                if current is None or current["state"] in match_rules.MATCH_TERMINAL_STATES:
                    continue
                room = next((r for r in u.rooms() if r["id"] == current["room_id"]), None)
                failed = {**current, "state": "FAILED",
                          "error": "控制服务重启，对局无法恢复（进程已按归属核对终止）。",
                          "finished_at": self._iso(),
                          "result": {**current.get("result", {}), "recovery": verdict}}
                if room:
                    room = match_rules.match_settled_room(room)
                    u.save_room(room)
                    u.emit(room, self._clock(), "MatchFailed")
                u.save_match(failed)
            recovered.append({"match_id": match["id"], **verdict})
        return recovered

    def shutdown_matches(self) -> None:
        """停服收尾：活动对局按正常停止路径结束（方案 §8.2 停服顺序）；
        单局清理失败不阻断其余收尾，也不阻断服务退出。"""
        with self._repository.transaction() as u:
            actives = u.active_matches()
        for match in actives:
            handle = self._handle(match)
            try:
                outcome = self._supervisor.stop(handle)
                collect = self._supervisor.collect(handle, {"reason": "service_shutdown"})
            except Exception:  # noqa: BLE001
                outcome, collect = {"note": "stop failed"}, {}
            with self._repository.transaction() as u:
                current = u.match(match["id"])
                if current is None or current["state"] in match_rules.MATCH_TERMINAL_STATES:
                    continue
                room = next((r for r in u.rooms() if r["id"] == current["room_id"]), None)
                settled = {**current, "state": "FINISHED", "finished_at": self._iso(),
                           "result": {**current.get("result", {}), "stop": outcome,
                                      "evidence": collect}}
                if room:
                    room = match_rules.match_settled_room(room)
                    u.save_room(room)
                    u.emit(room, self._clock(), "MatchFinished")
                u.save_match(settled)

    # ---- 工具 ----

    @staticmethod
    def _handle(match: dict) -> dict:
        """从对局行还原监督句柄；字段含义由监督器定义，本层只透传。"""
        return {"match_id": match["id"], "run_dir": match.get("run_dir", ""),
                "pid": match.get("pid"), "port": match.get("port"),
                "map": match["config"]["map"], "gametype": match["config"]["gametype"],
                "started_at": match.get("created_at")}

    def _view(self, match: dict) -> dict:
        """对局外发视图：保留状态与证据事实，不含 RCON 等内部细节。"""
        return {"id": match["id"], "room_id": match["room_id"], "state": match["state"],
                "map": match["config"].get("map"), "gametype": match["config"].get("gametype"),
                "created_at": match["created_at"], "updated_at": match["updated_at"],
                "finished_at": match.get("finished_at"), "error": match.get("error", ""),
                "players": len(match["config"].get("players", [])),
                "bot_seats": match["config"].get("bot_seats", {}).get("count", 0),
                "run_dir": match.get("run_dir", "")}

    def _session(self, u, token: str) -> dict:
        """玩家会话校验（与 PlayerRooms 相同规则；不在这里刷新心跳）。"""
        row = u.session(digest(token)) if token else None
        if row is None or row["revoked"] or row["expires_at"] <= self._clock():
            raise ForbiddenError("玩家会话已失效。")
        return row

    def _iso(self) -> str:
        """当前 UTC ISO 时间（与仓储事件格式一致）。"""
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()

    @staticmethod
    def _parse_iso(value: str) -> float:
        """解析 ISO 时间为时间戳；失败返回 0（使超时判断更保守）。"""
        try:
            return datetime.fromisoformat(value).timestamp()
        except (TypeError, ValueError):
            return 0.0
