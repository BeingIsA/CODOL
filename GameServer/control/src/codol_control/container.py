"""组合根容器：main 装配全部服务后交给 transport 使用。

依赖注入的单一入口：transport 层只通过容器字段访问应用服务，
不知道各服务如何构造，测试可以整体替换（如假探测器）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from . import __version__
from .application.admin_service import AdminService
from .application.diagnostics_service import DiagnosticsService
from .application.overview_service import OverviewService
from .application.room_service import RoomService
from .application.runtime_service import RuntimeService
from .config import Config
from .infrastructure.database import Database


@dataclass
class Container:
    """已装配的服务集合；uvicorn_server 与停服事件在运行期填充。"""

    config: Config
    db: Database
    admin: AdminService
    overview: OverviewService
    rooms: RoomService
    runtime: RuntimeService
    diagnostics: DiagnosticsService
    boot_id: str
    shutdown_token: str
    started_monotonic: float
    player_rooms: object = None                 # M2 玩家/房间用例，组合根注入
    matches: object = None                      # M3 对局用例（MatchService），组合根注入
    admin_rooms: object = None                  # 管理端房间处置（关闭房间：先停局后关房）
    reaper: object = None                       # 无人房间回收线程（客户端异常退出兜底）
    version: str = __version__
    uvicorn_server: object = None                 # uvicorn.Server，run() 时注入
    _shutdown_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def request_graceful_shutdown(self) -> None:
        """置位 uvicorn 退出标志；真正的收尾在主循环 finally 中完成。"""
        server = self.uvicorn_server
        if server is not None:
            server.should_exit = True
