"""运行时领域：探测结果的数据形状与判定词汇。

“探测”= 不启动原生进程的结构化预检（契约 §2）；
“监督/启动”自 M3 引入。本文件只描述事实，不做 IO。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# 契约版本；与 GameServer/contracts/native-runtime-v1.json 的 version 对应。
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class RuntimeProbeResult:
    """一次深度探测的全部事实；UI 与 API 原样呈现，不修饰结论。

    fake=True 表示结果来自测试用假探测器（显著标识，防止混入生产判断）。
    problems 收集人可读的问题描述（中文），全部通过时为空。
    """

    checked_at: str                 # ISO-8601 UTC
    exe_found: bool
    exe_sha256: str | None
    audit_passed: bool              # network-audit.json 本身 passed 且哈希匹配
    mirror_ok: bool                 # 版本恢复镜像存在且哈希匹配
    assets_total: int
    assets_verified: int            # 哈希核对通过的资产条数
    asset_failures: list[str] = field(default_factory=list)  # 最多记前 20 条
    supported_content: list[dict] = field(default_factory=list)
    contract_version: int = CONTRACT_VERSION
    fake: bool = False

    @property
    def ready(self) -> bool:
        """按契约 §2：EXE+审计、镜像、资产三者全部通过才算可用。"""
        return (
            self.exe_found
            and self.audit_passed
            and self.mirror_ok
            and self.assets_total > 0
            and self.assets_verified == self.assets_total
            and not self.fake
        )

    @property
    def problems(self) -> list[str]:
        """把各检查项翻译成人可读问题清单（供 UI 直接展示）。"""
        items: list[str] = []
        if not self.exe_found:
            items.append("未找到原生服务端 EXE（尚未构建？运行 tools/build_native.py）。")
        elif not self.audit_passed:
            items.append("EXE 哈希与网络审计记录不一致（重编译后需要重新运行 audit_network.py）。")
        if not self.mirror_ok:
            items.append("2.6.9.7 版本恢复镜像缺失或哈希不符。")
        if self.assets_total == 0:
            items.append("资产清单为空或不可读。")
        elif self.assets_verified != self.assets_total:
            items.append(f"资产校验未全部通过（{self.assets_verified}/{self.assets_total}）。")
        if self.fake:
            items.append("当前结果来自测试用假探测器，不代表真实运行时。")
        return items
