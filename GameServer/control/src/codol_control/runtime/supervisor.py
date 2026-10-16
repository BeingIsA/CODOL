"""M3 战斗进程监督器：native-runtime-v1 契约的第一个生产实现。

职责（契约 §1）：独占持有本服务启动的战斗子进程句柄；生成每次运行目录与
随机 RCON 密钥；启动前做端口独占绑定预检；提供状态采样与停止；落盘证据；
失败时只清理自己持有的句柄。RCON 密码只存在于内存与进程命令行，
写入任何日志/证据前替换为 <redacted>。

与 M0 验收器（tools/verify_m0_runtime.py）遵守同一契约；差异仅有：
- 验收器的"客户端无活动会话"预检是防止验收互扰的验收器专属约束，
  生产监督不做（开局时客户端在线是正常状态）。
- 就绪采样由应用层观察线程驱动（每次一采样），本模块不做循环判定。

进程/套接字/subprocess/ctypes 只允许出现在本层（架构测试约束）。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

from ..domain.errors import BattleNotReadyError
from .development_profile2697 import publish_development_profiles


class NativeSupervisor:
    """持有活动对局子进程的监督器；一个服务实例最多同时监督一个对局。"""

    def __init__(self, contract, runs_root: Path):
        """contract 为 runtime.contract.RuntimeContract；runs_root 为 .server-runs 目录。"""
        self._contract = contract
        self._runs_root = Path(runs_root)
        self._port = contract.control_port
        # match_id -> 监督状态（含 Popen 与 RCON 密码）；密码绝不进入返回值。
        # 采样与停止由应用层单一观察线程串行调用，无需进程内互斥。
        self._live: dict[str, dict] = {}

    # ---- 启动 ----

    def launch(self, match_id: str, config: dict) -> dict:
        """预检并启动一局战斗进程，返回监督句柄（不含 RCON 密码明文持久化字段）。"""
        if match_id in self._live:
            raise BattleNotReadyError("该对局已有受监督进程。")
        self._require_port_free()
        exe = self._contract.exe_path
        if not exe.is_file():
            raise BattleNotReadyError(f"战斗进程产物缺失：{exe}")
        run_dir = self._runs_root / uuid.uuid4().hex[:12]
        run_dir.mkdir(parents=True)
        (run_dir / "profiles").mkdir()
        # 战斗脚本出生前必须绑定安全档案；本机开发种子由服务端自行生成，
        # 不让客户端宿主越权写战斗目录，也不把本地测试进度当线上权威。
        profile_proof = publish_development_profiles(run_dir / "profiles", config.get("players", []))
        (run_dir / "development-profiles.json").write_text(
            json.dumps(profile_proof, ensure_ascii=False, indent=2), encoding="utf-8")
        owned = run_dir / "server.exe"
        shutil.copy2(exe, owned)
        password = secrets.token_hex(24)
        values = self._launch_cvars(config, run_dir, password)
        command = [str(owned)]
        for key, value in values.items():
            command += ["+set", key, value]
        command += ["+map", config["map"], "+codol_checktargetpublication"]
        env = dict(os.environ)
        env.update({k: str(self._contract.resolve(v)) for k, v in
                    self._contract.document["launch"].get("env_extra", {}).items()})
        output = (run_dir / "console.log").open("wb")
        try:
            process = subprocess.Popen(command, cwd=run_dir, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            output.close()
        handle = {"match_id": match_id, "run_dir": str(run_dir), "pid": process.pid,
                  "port": self._port, "exe": str(owned), "map": config["map"],
                  "gametype": config["gametype"], "started_at": time.time()}
        self._live[match_id] = {"process": process, "password": password,
                                "run_dir": run_dir, "samples": [], "started": time.monotonic()}
        return handle

    def _launch_cvars(self, config: dict, run_dir: Path, password: str) -> dict:
        """从契约 launch_cvars 生成启动参数；本局地图/模式覆盖模板值。

        M3 固定约束：Bot 填充为 0（房间 Bot 名额不注入战斗进程，实体生成属 M4）；
        本地开发组合沿用已验证的 admissionRequired=0（见 ADR 0005 凭据边界）。
        """
        doc = self._contract.document
        mirror = doc["precheck"]["mirror"]["path"]
        assets = self._contract.gameserver_root / "assets"
        overrides = {
            "fs_homepath": str(run_dir),
            "codol_profilePath": str(run_dir / "profiles"),
            "fs_basepath": str(assets / "runtime-assets"),
            "codol_zonePath": str(assets / "runtime-assets/chinese"),
            "codol_scriptPath": str(assets / "scripts"),
            "codol_scriptOverlay": str(assets / "scripts"),
            "codol_scriptNativeOverlay": str(assets / "script-overlay2697"),
            "codol_target2697Registration": str(assets / "registries/client-registration2697.txt"),
            "codol_clientExe": str(self._contract.resolve(mirror)),
            "sv_hostname": f"CODOL M3 match {config['map']}",
            "net_port": str(self._port),
            "g_gametype": config["gametype"],
            "sv_maxclients": str(config.get("max_clients", 12)),
            "scr_codol_bots": "0",
            "scr_codol_botfill": "0",
            "scr_codol_botmax": "0",
            "codol_admissionRequired": "0",
            "codol_securityKeyPath": "",
        }
        values = {}
        for name, spec in doc["launch_cvars"].items():
            if "const" in spec:
                values[name] = spec["const"]
            elif name in overrides:
                values[name] = overrides[name]
            elif "default" in spec:
                values[name] = str(spec["default"])
        # RCON 密码由 launch() 单独生成并注入：绝不经过契约文档或返回值扩散。
        values["rcon_password"] = password
        return values

    def _require_port_free(self) -> None:
        """契约 §2 预检：端口必须可独占绑定；预检查不能替代真实绑定。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", self._port))
        except OSError as exc:
            raise BattleNotReadyError(f"战斗端口 {self._port} 被占用，无法开局：{exc}") from exc

    # ---- 状态与停止 ----

    def status(self, handle: dict) -> dict:
        """一次契约状态采样：RCON sh1x_status + 进程存活事实。"""
        state = self._live.get(handle["match_id"])
        if state is None:
            return {"alive": False, "adopted": False, "note": "进程不受本服务监督（可能已重启）。"}
        process = state["process"]
        alive = process.poll() is None
        parsed = self._parse_status(self._rcon(state["password"], "sh1x_status"))
        row = {"alive": alive, "returncode": process.returncode if not alive else None,
               "exitpath_note": None}
        row.update(parsed)
        state["samples"].append({"at": time.time(), **parsed, "alive": alive})
        return row

    def stop(self, handle: dict) -> dict:
        """契约 §6 停止（revision 3 两段式）：

        1. 优雅收尾：先发 sh1x_endmatch，让已入场的真人客户端收到原生
           EXE_MATCHENDED 离场命令并走引擎自身断开路径回前台（而不是只能
           等 UDP 超时弹 EXE_ERR_SERVER_TIMEOUT）；有界轮询 sh1x_status
           等 humans=0（≤8 秒），等不到也继续 quit，结果记入 outcome。
        2. quit → 15 秒自然退出 → 端口释放核对；兜底强杀标记 forced。

        注意：这里不弹出监督状态——RCON 密码可能已被引擎写进 qconsole.log，
        必须留给 collect() 在脱敏完成后统一清理（见 _live.pop 的唯一位置）。
        """
        state = self._live.get(handle["match_id"])
        outcome = {"forced_cleanup": False, "exit_code": None, "port_released": False,
                   "graceful_endmatch": None, "graceful_wait_s": 0.0}
        if state is None:
            outcome["note"] = "进程不受本服务监督；仅核对端口。"
            outcome["port_released"] = self._port_free()
            return outcome
        process = state["process"]
        if process.poll() is None:
            outcome.update(self._graceful_endmatch(state, outcome))
            self._rcon(state["password"], "quit")
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                outcome["forced_cleanup"] = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        outcome["exit_code"] = process.returncode
        outcome["port_released"] = self._port_free()
        return outcome

    def _graceful_endmatch(self, state: dict, outcome: dict) -> dict:
        """契约 §6 第 1 步：发 sh1x_endmatch 并有界等 humans=0。

        无客户端（空图）场景 sent=0 且 humans 本来就是 0，第一次轮询即返回。
        RCON 无应答/解析失败按"未知"处理：继续尝试直到窗口耗尽，不阻塞 quit。
        """
        started = time.monotonic()
        reply = self._rcon(state["password"], "sh1x_endmatch")
        outcome["graceful_endmatch"] = self._parse_sent(reply)
        deadline = started + 8.0
        while time.monotonic() < deadline:
            time.sleep(1.0)
            if state["process"].poll() is not None:
                break
            parsed = self._parse_status(self._rcon(state["password"], "sh1x_status"))
            if parsed and int(parsed.get("humans", "0") or 0) == 0:
                break
        outcome["graceful_wait_s"] = round(time.monotonic() - started, 1)
        return outcome

    @staticmethod
    def _parse_sent(text: str) -> int | None:
        """解析 sh1x_endmatch 应答行的 sent 数；无应答返回 None（未知）。"""
        for line in text.splitlines():
            if line.startswith("sh1x_endmatch "):
                for part in line.split()[1:]:
                    key, _, value = part.partition("=")
                    if key == "sent" and value.isdigit():
                        return int(value)
        return None

    def collect(self, handle: dict, outcome: dict) -> dict:
        """把本局证据写入运行目录 summary.json；落盘前脱敏 RCON 密码。"""
        state = self._live.get(handle["match_id"])
        run_dir = Path(handle["run_dir"])
        password = state["password"] if state else ""
        # EXE 从运行目录解析：观察/停止路径的句柄由对局行重建，没有 exe 字段；
        # started_at 也可能是 ISO 字符串（对局行重建），只在做差有意义时计算。
        started = handle.get("started_at")
        seconds = time.time() - started if isinstance(started, (int, float)) else None
        report = {"match_id": handle["match_id"], "map": handle["map"], "gametype": handle["gametype"],
                  "pid": handle["pid"], "port": handle["port"],
                  "exe_sha256": self._digest(run_dir / "server.exe"), "samples": [],
                  "outcome": outcome, "seconds": seconds}
        if state:
            report["samples"] = state["samples"][-32:]
            report["seconds"] = time.monotonic() - state["started"]
        logs = self._read_logs(run_dir)
        report["publication_verified"] = "actual_client_crc=1" in logs
        report["fatal_log_lines"] = [line for line in logs.splitlines()
                                     if "script runtime error" in line.lower()
                                     or "script compile error" in line.lower()
                                     or ("ERROR:" in line and "ERROR: Pathnode (" not in line)][:16]
        report["primed_to_active"] = logs.count("Going from CS_PRIMED to CS_ACTIVE")
        if password:
            for path in run_dir.rglob("*.log"):
                content = path.read_bytes()
                if password.encode() in content:
                    path.write_bytes(content.replace(password.encode(), b"<redacted>"))
        (run_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
        self._live.pop(handle["match_id"], None)
        return {"run_dir": str(run_dir), "publication_verified": report["publication_verified"],
                "primed_to_active": report["primed_to_active"],
                "fatal_log_lines": len(report["fatal_log_lines"])}

    # ---- 服务重启后的归属核对 ----

    def adopt_or_kill(self, match_id: str, pid: int, run_dir: str) -> dict:
        """按 pid 与可执行路径核对遗留战斗进程；不是本服务 run 目录下的
        server.exe 一律不动；确认归属则终止（新服务无法接管旧句柄）。"""
        expected = Path(run_dir) / "server.exe"
        image = self._process_image(pid)
        result = {"pid": pid, "matched": False, "terminated": False, "image": image}
        if image and Path(image) == expected and expected.is_file():
            result["matched"] = True
            try:
                handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)  # PROCESS_TERMINATE
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    ctypes.windll.kernel32.CloseHandle(handle)
                    result["terminated"] = True
            except OSError:
                result["terminated"] = False
        result["port_released"] = self._port_free()
        return result

    @staticmethod
    def _process_image(pid: int) -> str | None:
        """读取进程可执行完整路径；进程已退出返回 None。"""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buffer))
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return buffer.value
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except OSError:
            return None
        return None

    # ---- 内部工具 ----

    def _rcon(self, password: str, command: str) -> str:
        """契约 §4 控制通道：回环 UDP、白名单命令、1 秒超时无应答视为空。

        白名单与契约 revision 3 一致：sh1x_status（只读采样）、quit（停止）、
        sh1x_endmatch（优雅结束对局）。
        """
        if command not in ("sh1x_status", "quit", "sh1x_endmatch"):
            raise ValueError("超出契约 v1 RCON 白名单。")
        packet = b"\xff" * 4 + f'rcon "{password}" {command}'.encode("ascii")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1)
            sock.connect(("127.0.0.1", self._port))
            sock.send(packet)
            try:
                return sock.recv(16384).decode("utf-8", errors="replace")
            except (socket.timeout, ConnectionResetError):
                return ""

    @staticmethod
    def _parse_status(text: str) -> dict:
        """解析 sh1x_status 行为 key=value；无状态行返回空字典。"""
        for line in text.splitlines():
            if line.startswith("sh1x_status "):
                return dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
        return {}

    def _port_free(self) -> bool:
        """端口是否可被再次独占绑定（契约停止核对）。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", self._port))
            return True
        except OSError:
            return False

    @staticmethod
    def _read_logs(run_dir: Path) -> str:
        """合并运行目录内全部 qconsole.log 内容（可能嵌套在 main/ 下）。"""
        parts = []
        for path in sorted(run_dir.rglob("qconsole.log")):
            try:
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return "\n".join(parts)

    @staticmethod
    def _digest(path: Path) -> str | None:
        """证据用哈希；文件缺失返回 None 而不是抛错（收集阶段不掩盖结果）。"""
        try:
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError:
            return None
