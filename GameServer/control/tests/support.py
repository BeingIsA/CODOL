"""测试支撑：在临时目录里以真实装配启动整个服务（线程内 uvicorn）。

与生产差异只有三点：临时 data/logs、随机空闲端口、运行时探测用
FakeProber（显著标识 fake=True）。HTTP 交互用标准库 urllib，
不引入 httpx/testclient 等额外依赖。
"""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTROL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = CONTROL_ROOT.parent.parent


class ReadyProbeStub:
    """测试专用"预检通过"探测器：让对局流程测试通过开局门槛。

    与 FakeProber（ready 恒 False，诚实拒绝）不同，本桩显式声明
    probe_ready=True 用于 MatchService 流程测试；仅测试装配可达，
    生产代码没有引用。支持内容与契约白名单一致。
    """

    def __init__(self, contract):
        """保留契约仅为与 FakeProber 构造签名兼容。"""
        self._contract = contract

    def deep_probe(self):
        from codol_control.domain.runtime import RuntimeProbeResult
        from codol_control.infrastructure.clock import iso_now
        return RuntimeProbeResult(
            checked_at=iso_now(), exe_found=True, exe_sha256="0" * 64,
            audit_passed=True, mirror_ok=True, assets_total=1, assets_verified=1,
            supported_content=[{"map": "mp_cross_fire", "gametype": "war"}], fake=False)

    def port_free(self) -> bool:
        return True


def free_port() -> int:
    """取一个当前空闲的 TCP 端口（绑定后立即释放，存在极小竞争窗口）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServiceInstance:
    """一次完整启动的服务实例；stop() 模拟优雅停服并收尾。"""

    def __init__(self, data_dir: Path | None = None, logs_dir: Path | None = None,
                 port: int | None = None, fake_runtime: bool = True,
                 probe_ready: bool = False) -> None:
        from codol_control.config import load_config
        from codol_control.main import build_container, _uvicorn_config, _cleanup, _write_control_file
        from codol_control.runtime.contract import load_contract
        from codol_control.runtime.fake import FakeProber
        from codol_control.application.runtime_service import RuntimeService
        from codol_control.transport.app import create_app
        import uvicorn

        self._cleanup_fn = _cleanup
        self._tmp = tempfile.TemporaryDirectory(prefix="codol-control-test-")
        tmp = Path(self._tmp.name)
        self.data_dir = data_dir or (tmp / "data")
        self.logs_dir = logs_dir or (tmp / "logs")
        self.port = port or free_port()
        config = load_config(CONTROL_ROOT, overrides={
            "data_dir": str(self.data_dir), "logs_dir": str(self.logs_dir),
                "bind_port": self.port, "open_browser": False, "bind_host": "127.0.0.1",
            })
        from codol_control.runtime.fake import FakeSupervisor
        container, self.logger = build_container(config, supervisor=FakeSupervisor())
        self.container = container
        try:
            _write_control_file(container)  # 停服令牌测试需要与生产一致的运行期文件
            if fake_runtime:
                contract = load_contract(config.gameserver_root)
                # probe_ready=True 时用"预检通过"桩驱动对局流程测试；
                # 默认 FakeProber 保持诚实拒绝（ready=False）。
                prober = ReadyProbeStub(contract) if probe_ready else FakeProber(contract)
                container.runtime = RuntimeService(prober, cache_seconds=600)
                container.runtime.probe(force=True)
                container.overview._runtime = container.runtime  # noqa: SLF001 - 测试注入
                # 对局用例的开局预检也指向注入的探测器。
                container.matches._runtime = container.runtime  # noqa: SLF001 - 测试注入
            app = create_app(container)
            server = uvicorn.Server(_uvicorn_config(app, config))
            container.uvicorn_server = server
            self.server = server
            self.thread = threading.Thread(target=server.run, daemon=True, name="test-service")
            self.thread.start()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if server.started:
                    break
                if not self.thread.is_alive():
                    raise RuntimeError("服务线程提前退出（启动失败）。")
                time.sleep(0.1)
            else:
                raise TimeoutError("服务启动超时。")
        except Exception:
            # 装配/启动失败也要释放已打开的资源，避免句柄泄漏进后续用例。
            self.stop()
            raise
        self.base = f"http://127.0.0.1:{self.port}"

    # ---- HTTP 便捷方法 ---------------------------------------------------

    def request(self, method: str, path: str, body: dict | None = None,
                token: str | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
        """发起 JSON 请求；返回 (状态码, 解析后的 JSON 或原始文本)。"""
        head = {"Content-Type": "application/json"}
        if token:
            head["Authorization"] = "Bearer " + token
        head.update(headers or {})
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, _parse(raw)
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read().decode("utf-8", "replace"))

    def login(self, password: str) -> tuple[int, dict | str]:
        """用管理员帐号登录（密码来自首次初始化凭据文件或调用方指定）。"""
        return self.request("POST", "/api/admin/login",
                            {"username": "admin", "password": password})

    def initial_password(self) -> str:
        """读取首次初始化写入的初始口令（仅测试环境存在）。"""
        path = self.data_dir / "初始管理员凭据.txt"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("密码："):
                return line.split("：", 1)[1].strip()
        raise AssertionError("初始凭据文件缺少密码行：" + str(path))

    # ---- 生命周期 ---------------------------------------------------------

    def stop(self) -> None:
        """优雅停服并等待线程结束、清理临时目录；对未完全启动的实例同样安全。"""
        server = getattr(self, "server", None)
        if server is not None:
            server.should_exit = True
        thread = getattr(self, "thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)
        if getattr(self, "container", None) is not None:
            self._cleanup_fn(self.container, getattr(self, "logger", None))
        self._tmp.cleanup()

    def wait_dead(self, timeout: float = 15.0) -> bool:
        """轮询健康检查直到连接失败（确认真停）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.base + "/api/health", timeout=2).read()
            except Exception:  # noqa: BLE001 - 连接被拒即视为停止
                return True
            time.sleep(0.3)
        return False


def _parse(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
