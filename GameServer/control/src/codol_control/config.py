"""控制服务配置：加载、默认值与首次生成。

设计约束：
- 配置文件 control/config.json 首次启动自动生成，之后**绝不覆盖**，
  保证“重启服务不丢失配置”的 M1 验收条款。
- 所有目录都从 control 根推导，项目整体移动后仍可运行；
  工作区/GameServer 根也按相对位置解析，不写死绝对路径。
- 敏感内容（令牌、密码）不放在配置文件，统一存数据库或运行期文件。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


# 默认管理端口：只绑定回环；换端口需修改 config.json 后重启。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790


@dataclass(frozen=True)
class Config:
    """服务运行所需的全部外部输入；实例不可变，测试用 overrides 注入。"""

    control_root: Path
    workspace_root: Path          # D:/Games/CODOL
    gameserver_root: Path         # D:/Games/CODOL/GameServer
    data_dir: Path                # 数据库、凭据、运行期文件（git 忽略）
    logs_dir: Path                # JSONL 日志（git 忽略）
    ui_dir: Path                  # 管理页静态文件
    migrations_dir: Path          # SQL 迁移脚本
    bind_host: str = DEFAULT_HOST
    bind_port: int = DEFAULT_PORT
    token_ttl_minutes: int = 240  # 管理会话令牌有效期
    probe_cache_seconds: int = 600  # 运行时深度探测结果缓存时长
    open_browser: bool = True     # 启动成功后是否自动打开管理页

    @property
    def db_path(self) -> Path:
        """SQLite 数据库文件位置（随 data_dir 走，测试可整体指到临时目录）。"""
        return self.data_dir / "codol-control.db"

    @property
    def config_path(self) -> Path:
        """用户可编辑的配置文件路径。"""
        return self.control_root / "config.json"

    @property
    def control_state_path(self) -> Path:
        """每次启动生成的运行期控制文件（boot_id + 停服令牌），随停服删除。"""
        return self.data_dir / "control.json"


def load_config(control_root: Path, overrides: dict | None = None) -> Config:
    """加载配置；文件不存在时写入默认模板，环境变量可覆盖个别键。

    overrides 优先级最高（测试注入用），其后是环境变量，最后是文件/默认值。
    只在配置文件不存在时创建它；已存在的文件即使字段缺失也不回写，
    缺失字段回落默认值，避免抹掉用户手改内容。
    """
    control_root = Path(control_root).resolve()
    gameserver_root = control_root.parent
    workspace_root = gameserver_root.parent
    path = control_root / "config.json"

    stored: dict = {}
    if not path.exists():
        template = {
            "bind_host": DEFAULT_HOST,
            "bind_port": DEFAULT_PORT,
            "token_ttl_minutes": 240,
            "probe_cache_seconds": 600,
            "open_browser": True,
        }
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        stored = json.loads(path.read_text(encoding="utf-8"))

    # 环境变量覆盖：便于自动化脚本临时换端口/目录/禁浏览器，不改动配置文件。
    env = os.environ
    values = {
        "bind_host": env.get("CODOL_CONTROL_HOST", stored.get("bind_host", DEFAULT_HOST)),
        "bind_port": int(env.get("CODOL_CONTROL_PORT", stored.get("bind_port", DEFAULT_PORT))),
        "token_ttl_minutes": int(stored.get("token_ttl_minutes", 240)),
        "probe_cache_seconds": int(stored.get("probe_cache_seconds", 600)),
        "open_browser": bool(stored.get("open_browser", True)),
    }
    if env.get("CODOL_CONTROL_NO_BROWSER"):
        values["open_browser"] = False
    # 目录覆盖主要供自动化测试把 data/logs 指到临时目录，生产不需要设置。
    for key, env_name in (("data_dir", "CODOL_CONTROL_DATA_DIR"), ("logs_dir", "CODOL_CONTROL_LOGS_DIR")):
        if env.get(env_name):
            values[key] = env[env_name]
    if overrides:
        values.update(overrides)

    cfg = Config(
        control_root=control_root,
        workspace_root=workspace_root,
        gameserver_root=gameserver_root,
        data_dir=Path(values.get("data_dir", control_root / "data")),
        logs_dir=Path(values.get("logs_dir", control_root / "logs")),
        ui_dir=control_root / "ui",
        migrations_dir=control_root / "migrations",
        bind_host=str(values["bind_host"]),
        bind_port=int(values["bind_port"]),
        token_ttl_minutes=int(values["token_ttl_minutes"]),
        probe_cache_seconds=int(values["probe_cache_seconds"]),
        open_browser=bool(values["open_browser"]),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg
