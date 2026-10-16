"""契约级深度探测：不启动原生进程的结构化预检（契约 §2 的 3/4/5 项）。

实现约束：
- 只读：探测绝不修改任何被检文件，也不写 .server-runs；
- 全量哈希：资产清单 1.2 万条约 1.1GB，耗时可达数十秒，
  因此由 RuntimeService 缓存并按需触发，不挂在轮询路径上；
- 端口占用检查（契约 §2 第 2 项）单独提供：它要做一次独占绑定，
  高频调用会干扰真实运行，只允许显式触发。
"""
from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

from ..domain.runtime import RuntimeProbeResult
from .contract import RuntimeContract

# 失败明细最多保留条数：防止一次全盘失败生成巨型响应。
MAX_FAILURE_DETAIL = 20


def digest(path: Path) -> str | None:
    """流式计算文件 SHA-256；文件不可读时返回 None（探测不因单文件崩溃）。"""
    try:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


class RuntimeProber:
    """对一份契约执行预检；无状态，可重复调用。"""

    def __init__(self, contract: RuntimeContract) -> None:
        self._c = contract

    # ---- 各项检查 -------------------------------------------------------

    def _check_exe(self) -> tuple[bool, str | None, bool]:
        """EXE 存在性、哈希，以及与网络审计记录的一致性。"""
        exe = self._c.exe_path
        if not exe.is_file():
            return False, None, False
        sha = digest(exe)
        try:
            audit = json.loads(self._c.audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True, sha, False
        audit_ok = bool(audit.get("passed")) and audit.get("sha256") == sha
        return True, sha, audit_ok

    def _check_mirror(self) -> bool:
        """恢复镜像哈希与契约期望值比对。"""
        mirror = self._c.mirror_path
        if not mirror.is_file():
            return False
        return digest(mirror) == self._c.mirror_sha256

    def _check_assets(self) -> tuple[int, int, list[str]]:
        """按清单逐条核对资产文件哈希；返回 (总数, 通过数, 失败样例)。"""
        try:
            entries = json.loads(self._c.assets_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0, 0, ["资产清单不可读：" + self._c.assets_manifest_path.name]
        # 清单条目的 path 相对 GameServer 根（形如 assets/...），与
        # verify_m0_runtime.py 的解析方式保持一致，避免两套口径。
        gameserver = self._c.gameserver_root
        verified, failures = 0, []
        for item in entries:
            target = (gameserver / item["path"]).resolve()
            if digest(target) == item.get("sha256"):
                verified += 1
            elif len(failures) < MAX_FAILURE_DETAIL:
                failures.append(item.get("path", "<unknown>"))
        return len(entries), verified, failures

    def deep_probe(self) -> RuntimeProbeResult:
        """执行契约预检并汇总为 RuntimeProbeResult（纯只读）。"""
        from ..infrastructure.clock import iso_now

        exe_found, sha, audit_ok = self._check_exe()
        mirror_ok = self._check_mirror()
        total, verified, failures = self._check_assets()
        return RuntimeProbeResult(
            checked_at=iso_now(),
            exe_found=exe_found,
            exe_sha256=sha,
            audit_passed=audit_ok,
            mirror_ok=mirror_ok,
            assets_total=total,
            assets_verified=verified,
            asset_failures=failures,
            supported_content=self._c.supported_content,
        )

    def port_free(self) -> bool:
        """契约端口独占绑定测试：能绑上即当前无人占用（绑完立即释放）。"""
        port = self._c.control_port
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
