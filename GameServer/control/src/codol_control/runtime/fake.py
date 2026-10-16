"""测试用假探测器：仅用于单元测试，结果显著标识 fake=True。

生产装配绝不使用本类；它的存在让传输层/总览层测试不必对 1.1GB 资产做哈希。
"""
from __future__ import annotations

from ..domain.runtime import RuntimeProbeResult
from .probe import RuntimeProber


class FakeProber(RuntimeProber):
    """返回固定“通过”结果的替身；fake 字段恒为 True。"""

    def deep_probe(self) -> RuntimeProbeResult:  # noqa: D102 - 见基类
        from ..infrastructure.clock import iso_now

        return RuntimeProbeResult(
            checked_at=iso_now(),
            exe_found=True,
            exe_sha256="0" * 64,
            audit_passed=True,
            mirror_ok=True,
            assets_total=1,
            assets_verified=1,
            supported_content=[{"map": "fake_map", "gametype": "war"}],
            fake=True,
        )

    def port_free(self) -> bool:  # noqa: D102 - 见基类
        return True


class FakeSupervisor:
    """测试用战斗进程监督替身：不启动任何进程，fake=True 显著标识。

    通过脚本化回调模拟真实监督器的状态序列，供对局生命周期单元测试
    驱动 LOADING→READY→RUNNING→FINISHED 等转换；生产装配绝不使用。
    """

    fake = True

    def __init__(self, script: list[dict] | None = None, adoption_verdicts: list[dict] | None = None):
        """script 为每次 status() 依次返回的采样序列；耗尽后沿用最后一项。
        adoption_verdicts 为 adopt_or_kill 依次返回的裁决；耗尽后默认未匹配。"""
        self.script = list(script or [])
        self.adoption_verdicts = list(adoption_verdicts or [])
        self.launches: list[tuple[str, dict]] = []
        self.stops: list[str] = []
        self.collects: list[tuple[str, dict]] = []
        self.launch_error: Exception | None = None

    def launch(self, match_id: str, config: dict) -> dict:
        """记录启动并返回假句柄；launch_error 用于模拟启动失败。"""
        if self.launch_error is not None:
            raise self.launch_error
        self.launches.append((match_id, dict(config)))
        handle = {"match_id": match_id, "run_dir": f".server-runs/fake-{match_id}",
                  "pid": 4000 + len(self.launches), "port": 28969,
                  "exe": "fake-server.exe", "map": config["map"],
                  "gametype": config["gametype"], "started_at": 0.0}
        return handle

    def status(self, handle: dict) -> dict:
        """按脚本返回下一次采样；默认序列为运行中且时钟推进。"""
        if self.script:
            row = dict(self.script.pop(0))
        else:
            row = {"alive": True, "running": "1", "map": handle["map"],
                   "gametype": handle["gametype"], "type": "mp", "humans": "0",
                   "bots": "0", "simulation_ms": "0"}
        row.setdefault("alive", True)
        return row

    def stop(self, handle: dict) -> dict:
        """记录停止并返回契约形状的假结果。"""
        self.stops.append(handle["match_id"])
        return {"forced_cleanup": False, "exit_code": 0, "port_released": True}

    def collect(self, handle: dict, outcome: dict) -> dict:
        """记录证据收集并返回假摘要。"""
        self.collects.append((handle["match_id"], dict(outcome)))
        return {"run_dir": handle["run_dir"], "publication_verified": True,
                "primed_to_active": 1, "fatal_log_lines": 0}

    def adopt_or_kill(self, match_id: str, pid: int, run_dir: str) -> dict:
        """按注入的裁决序列返回；耗尽后默认未匹配。"""
        if self.adoption_verdicts:
            return dict(self.adoption_verdicts.pop(0))
        return {"pid": pid, "matched": False, "terminated": False, "image": None}
