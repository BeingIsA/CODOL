"""原生运行时契约加载：GameServer/contracts/native-runtime-v1.json 的类型化入口。

控制服务与 M0 验收器共享同一份契约文件，避免两处各自维护启动参数；
加载时做最小结构校验，契约不完整属于安装问题，启动即报错。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONTRACT_FILENAME = "native-runtime-v1.json"


class ContractError(RuntimeError):
    """契约文件缺失或结构不符：安装级错误，直接阻止服务运行时功能。"""


@dataclass(frozen=True)
class RuntimeContract:
    """契约要点 + 相对路径解析后的绝对路径。"""

    version: int
    document: dict
    workspace_root: Path
    gameserver_root: Path

    def resolve(self, relative: str) -> Path:
        """把契约内相对工作区的路径解析为绝对路径（不校验存在性）。"""
        return (self.workspace_root / relative).resolve()

    @property
    def exe_path(self) -> Path:
        """原生 EXE 位置。"""
        return self.resolve(self.document["artifact"]["exe"])

    @property
    def audit_path(self) -> Path:
        """网络审计清单位置；EXE 哈希以它为准。"""
        return self.resolve(self.document["artifact"]["audit_manifest"])

    @property
    def mirror_path(self) -> Path:
        """2.6.9.7 恢复镜像位置。"""
        return self.resolve(self.document["precheck"]["mirror"]["path"])

    @property
    def mirror_sha256(self) -> str:
        """镜像期望哈希。"""
        return self.document["precheck"]["mirror"]["sha256"]

    @property
    def assets_manifest_path(self) -> Path:
        """资产来源清单（GameServer 内相对路径列表 + 哈希）。"""
        return self.resolve(self.document["precheck"]["assets_manifest"])

    @property
    def assets_root(self) -> Path:
        """资产根目录：清单里的 path 字段相对它。"""
        return self.resolve(self.document["precheck"]["assets_root"])

    @property
    def supported_content(self) -> list[dict]:
        """当前契约验证过的地图/模式组合。"""
        return list(self.document["launch"]["supported_content_v1"])

    @property
    def control_port(self) -> int:
        """默认 UDP 端口（RCON 与游戏流量共用）。"""
        return int(self.document["launch_cvars"]["net_port"]["default"])


def load_contract(gameserver_root: Path) -> RuntimeContract:
    """读取并校验契约；失败抛 ContractError（带具体缺失项）。"""
    path = Path(gameserver_root) / "contracts" / CONTRACT_FILENAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"运行时契约缺失：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"运行时契约不是有效 JSON：{exc}") from exc
    for key in ("artifact", "precheck", "launch", "launch_cvars"):
        if key not in document:
            raise ContractError(f"运行时契约缺少段落：{key}")
    workspace_root = Path(gameserver_root).resolve().parent
    return RuntimeContract(
        version=int(document.get("version", 0)),
        document=document,
        workspace_root=workspace_root,
        gameserver_root=Path(gameserver_root).resolve(),
    )
