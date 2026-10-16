"""服务启动入口：装配、生命周期与启动失败诊断。

启动顺序（方案 §8.2 的 M1 形态）：
配置 → 日志 → 数据库迁移 → 管理员初始化 → 契约加载 → 服务装配 →
写运行期控制文件 → 启动 HTTP →（后台预热运行时探测）→ 事件循环 →
退出收尾（删控制文件、关库）。

绑定失败（端口占用等）不抛裸异常：写 startup-failure.json 并在控制台
给出中文定位提示后以退出码 2 结束（Start.cmd 与用户都依赖这一约定）。
"""
from __future__ import annotations

import json
import secrets
import sys
import threading
import time
import webbrowser

from . import __version__
from .application.admin_service import AdminService
from .application.admin_rooms import AdminRooms
from .application.diagnostics_service import DiagnosticsService
from .application.match_service import MatchService
from .application.overview_service import OverviewService
from .application.room_service import RoomService
from .application.room_reaper import RoomReaper, ABANDONED_TIMEOUT_SECONDS
from .application.runtime_service import RuntimeService
from .application.player_rooms import PlayerRooms
from .config import Config, load_config
from .container import Container
from .infrastructure.clock import iso_now
from .infrastructure.database import Database, run_migrations
from .infrastructure.logging_setup import setup_logging
from .infrastructure.settings_store import SettingsStore
from .infrastructure.tokens import TokenSigner
from .infrastructure.online_repository import SqliteOnlineRepository
from .infrastructure.admin_credentials import Credentials, InitialCredentialsWriter
from .infrastructure.diagnostics_reader import DiagnosticsReader
from .runtime.contract import ContractError, load_contract
from .runtime.probe import RuntimeProber
from .runtime.supervisor import NativeSupervisor

# 启动失败专用退出码：0 正常、2 绑定/启动失败、3 安装完整性错误。
EXIT_BIND_FAILURE = 2
EXIT_INSTALL_BROKEN = 3


def build_container(config: Config, supervisor=None) -> tuple[Container, object]:
    """装配全部服务；返回（容器，日志器）。失败直接抛错由 run() 统一处理。

    supervisor 允许测试注入 FakeSupervisor；生产调用不传该参数。
    """
    boot_id = secrets.token_hex(6)
    logger = setup_logging(config.logs_dir, boot_id)

    db = Database(config.db_path)
    applied = run_migrations(db, config.migrations_dir)
    settings = SettingsStore(db)

    admin = AdminService(db, settings, token_factory=TokenSigner,
                         token_ttl_minutes=config.token_ttl_minutes, passwords=Credentials(),
                         write_credentials=InitialCredentialsWriter(config.data_dir), clock=iso_now)
    provisioned = admin.ensure_provisioned()

    contract = load_contract(config.gameserver_root)
    runtime_svc = RuntimeService(RuntimeProber(contract), config.probe_cache_seconds)
    # 开发接入凭据与管理员凭据隔离；仅供本机宿主读取，不返回给管理网页。
    access_path = config.data_dir / "development-access.json"
    if not access_path.exists():
        with access_path.open("x", encoding="utf-8") as output:
            json.dump({"development_only": True, "secret": secrets.token_hex(32)}, output)
    access = json.loads(access_path.read_text(encoding="utf-8"))
    content = json.loads((config.gameserver_root / "contracts/room-content-v1.json").read_text(encoding="utf-8"))
    repository = SqliteOnlineRepository(db)
    player_rooms = PlayerRooms(repository, time.time, secrets.token_hex, access["secret"],
                               content["room_maps"], reap_timeout=ABANDONED_TIMEOUT_SECONDS)
    # 无人房间回收：客户端崩溃/强杀不会发 leave，房间会一直占着唯一名额。
    reaper = RoomReaper(player_rooms)
    # M3 对局监督：生产监督器 + 契约验证过的战斗内容白名单（测试注入假监督器）。
    battle = supervisor if supervisor is not None else NativeSupervisor(
        contract, config.workspace_root / ".server-runs")
    matches = MatchService(repository, time.time, secrets.token_hex, battle,
                           contract.supported_content, runtime_svc)
    # 服务重启恢复：核对上一进程留下的未终结对局（归属确认后终止并标失败）。
    recovered = matches.recover()
    if recovered:
        logger.info("启动恢复：发现 %d 个未终结对局，已按归属核对处理：%s",
                    len(recovered), json.dumps(recovered, ensure_ascii=False))
    matches.start_observer()
    reaper.start()
    started = time.monotonic()
    container = Container(
        config=config, db=db, admin=admin, player_rooms=player_rooms,
        matches=matches, admin_rooms=AdminRooms(player_rooms, matches), reaper=reaper,
        overview=OverviewService(player_rooms, runtime_svc, boot_id, started, matches=matches),
        rooms=RoomService(player_rooms), runtime=runtime_svc,
        diagnostics=DiagnosticsService(DiagnosticsReader(config.logs_dir, config.workspace_root)),
        boot_id=boot_id, shutdown_token=secrets.token_hex(24), started_monotonic=started)
    logger.info("服务装配完成：版本 %s，迁移 %s，管理员 %s",
                __version__, applied or "无待应用",
                "首次初始化（见 data/初始管理员凭据.txt）" if provisioned else "已存在")
    return container, logger


def _write_control_file(container: Container) -> None:
    """写 data/control.json：boot 标识 + 停服令牌，停服时删除。

    该文件只在本机可读路径，等价于“本机可读即可停服”，不构成网络权力。
    """
    payload = {
        "boot_id": container.boot_id,
        "pid": _pid(),
        "host": container.config.bind_host,
        "port": container.config.bind_port,
        "shutdown_token": container.shutdown_token,
        "started_at": iso_now(),
    }
    container.config.control_state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _pid() -> int:
    import os

    return os.getpid()


def run(argv: list[str] | None = None) -> int:
    """主入口：装配并在前台运行 HTTP 服务，返回进程退出码。"""
    import uvicorn

    argv = list(sys.argv[1:] if argv is None else argv)
    config = load_config(__package_root(), overrides=None)
    if "--no-browser" in argv:
        # 直接改运行期标志，不回写配置文件。
        config = Config(**{**config.__dict__, "open_browser": False})

    try:
        container, logger = build_container(config)
    except ContractError as exc:
        print(f"[安装完整性错误] {exc}", file=sys.stderr)
        return EXIT_INSTALL_BROKEN

    url = f"http://{config.bind_host}:{config.bind_port}/"
    logger.info("管理界面：%s （关闭浏览器页面不会停止服务；使用 Stop.cmd 停服）", url)

    from .transport.app import create_app
    app = create_app(container)
    server = uvicorn.Server(_uvicorn_config(app, config))
    container.uvicorn_server = server

    # 先取得真正的监听套接字，再发布停服凭据。重复启动若绑定失败，
    # 不得覆盖仍在运行实例的 control.json；仅在 finally 检查归属还不够。
    try:
        listener = server.config.bind_socket()
    except (OSError, SystemExit) as exc:
        code = _report_bind_failure(config, exc, logger)
        container.matches.stop_observer()
        container.db.close()
        return code
    _write_control_file(container)

    # 后台预热一次深度探测，首次打开页面即有运行时状态。
    threading.Thread(target=container.runtime.probe, kwargs={"force": True},
                     daemon=True, name="runtime-probe-warm").start()

    if config.open_browser:
        # 等服务真正绑定端口后再开浏览器；绑定失败则不打开。
        threading.Timer(1.5, lambda: _open_browser_if_live(server, url)).start()

    exit_code = 0
    try:
        server.run(sockets=[listener])
    except (OSError, SystemExit) as exc:
        # uvicorn 绑定失败可能抛 OSError，也可能内部 sys.exit(1)；
        # 只要没进入运行态都按启动失败诊断处理。
        exit_code = _report_bind_failure(config, exc, logger)
    else:
        if not server.started:
            exit_code = _report_bind_failure(
                config, RuntimeError("服务未进入运行态（疑似端口被占用）"), logger)
        else:
            logger.info("服务已按停服请求正常退出。")
    finally:
        listener.close()
        _cleanup(container, logger)
    return exit_code


def _report_bind_failure(config: Config, exc: OSError, logger) -> int:
    """绑定失败诊断：写 data/startup-failure.json + 控制台中文提示。"""
    hint = (
        f"端口 {config.bind_host}:{config.bind_port} 无法监听（{exc}）。\n"
        "排查建议：\n"
        "  1) 是否已有一个控制服务实例在运行（本窗口只允许一个）；\n"
        "  2) 端口被其他程序占用：netstat -ano | findstr :" + str(config.bind_port) + "\n"
        "  3) 修改 GameServer/control/config.json 的 bind_port 后重试。"
    )
    report = {"at": iso_now(), "kind": "bind_failure", "error": repr(exc),
              "bind_host": config.bind_host, "bind_port": config.bind_port}
    try:
        (config.data_dir / "startup-failure.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    logger.error("启动失败：%s", hint.replace("\n", " | "))
    print("[启动失败] " + hint, file=sys.stderr)
    return EXIT_BIND_FAILURE


def _cleanup(container: Container, logger) -> None:
    """统一收尾：删控制文件、关数据库；每一步失败都记录但不中断后续。

    控制文件只在仍属于本次启动（boot_id 匹配）时才删除：
    启动失败的实例不得误删正在运行实例的控制文件（2026-10-15 实测教训：
    端口被占的启动尝试曾把在运行服务的 control.json 删掉，导致 Stop.cmd 失效）。
    """
    def _remove_own_control_file() -> None:
        path = container.config.control_state_path
        if not path.is_file():
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # 无法确认归属时宁可不删，避免误伤在运行实例
        if state.get("boot_id") == container.boot_id:
            path.unlink()

    for action, fn in (
        ("结束活动对局（方案 §8.2 停服顺序）", lambda: container.matches.shutdown_matches()),
        ("停止对局观察线程", container.matches.stop_observer),
        ("停止无人房间回收线程", container.reaper.stop),
        ("删除运行期控制文件", _remove_own_control_file),
        ("关闭数据库", container.db.close),
    ):
        try:
            fn()
            logger.info("收尾完成：%s", action)
        except Exception as exc:  # noqa: BLE001 - 收尾步骤相互独立，不允许互相阻断
            logger.error("收尾失败：%s %r", action, exc)


def _open_browser_if_live(server, url: str) -> None:
    """仅在服务器已进入运行态时打开浏览器，失败静默（非关键路径）。"""
    try:
        if server.started:
            webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 打开浏览器失败不影响服务
        pass


def _uvicorn_config(app, config: Config):
    """构造 uvicorn 配置：关闭其自带日志（本项目用统一 JSONL 日志）。"""
    import uvicorn

    return uvicorn.Config(app, host=config.bind_host, port=config.bind_port,
                          log_config=None, access_log=False, lifespan="on")


def __package_root():
    """控制服务源码根（control/）：以本文件位置推导，支持任意安装目录。"""
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":  # pragma: no cover - 由 __main__ 包装
    raise SystemExit(run())
