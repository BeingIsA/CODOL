"""诊断用例：日志尾部、历史运行证据与健康事实。

只读原则：诊断服务不修改任何现场；server-runs 的回收属 M5 配额管理，
此处仅枚举与摘要。所有输出都来自已脱敏数据（密钥在落盘前已替换）。
"""
from __future__ import annotations

import json
from pathlib import Path

from .clock import iso_now


class DiagnosticsReader:
    """GET /api/diagnostics/* 的应用层实现。"""

    def __init__(self, logs_dir: Path, workspace_root: Path) -> None:
        self._logs_dir = Path(logs_dir)
        self._runs_root = Path(workspace_root) / ".server-runs"

    def recent_log_entries(self, limit: int = 100) -> list[dict]:
        """读取今天（必要时回退到最新一天）的 JSONL 尾部 limit 条。"""
        limit = max(1, min(int(limit), 500))
        files = sorted(self._logs_dir.glob("server-*.jsonl"))
        for path in reversed(files[-2:]):  # 跨天边界时也尽量给满 limit 条
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            entries = []
            for line in lines[-limit:]:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if entries:
                return entries
        return []

    def list_server_runs(self) -> list[dict]:
        """枚举 .server-runs/*/summary.json，摘要 passed/errors/地图/耗时。"""
        if not self._runs_root.is_dir():
            return []
        runs = []
        for run_dir in sorted(self._runs_root.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            summary_path = run_dir / "summary.json"
            item = {"id": run_dir.name, "has_summary": False}
            if summary_path.is_file():
                try:
                    data = json.loads(summary_path.read_text(encoding="utf-8"))
                    item.update({
                        "has_summary": True,
                        "passed": data.get("passed"),
                        "errors": data.get("errors", [])[:3],
                        "map": data.get("map"),
                        "seconds": round(float(data.get("seconds") or 0), 1),
                    })
                except (OSError, json.JSONDecodeError, ValueError):
                    item["has_summary"] = True
                    item["passed"] = None
            runs.append(item)
        return runs

    def bundle(self) -> dict:
        """诊断包：一次请求带回全部脱敏诊断事实（供页面或人工导出）。"""
        return {
            "generated_at": iso_now(),
            "logs": self.recent_log_entries(100),
            "server_runs": self.list_server_runs(),
        }
