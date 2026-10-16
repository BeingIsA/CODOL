"""JSONL 结构化日志：人读控制台 + 机读文件双通道。

约定：
- 文件按天滚动（logs/server-YYYYMMDD.jsonl），一行一条 JSON；
- 统一字段：ts / level / event / boot_id，可选 request_id、detail；
- 任何密码、令牌、密钥不得进入日志；调用方负责只传非敏感字段。
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from .clock import iso_now


class JsonlFileHandler(logging.Handler):
    """把记录序列化成单行 JSON 追加写入当天文件；写失败时静默放弃该行。

    日志丢失不致命，不允许日志故障拖垮服务，因此捕获所有 IO 异常。
    """

    def __init__(self, logs_dir: Path) -> None:
        super().__init__()
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _today_path(self) -> Path:
        return self.logs_dir / ("server-" + iso_now()[:10] + ".jsonl")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": iso_now(),
                "level": record.levelname,
                "event": record.getMessage(),
                "boot_id": getattr(record, "boot_id", None),
            }
            detail = getattr(record, "detail", None)
            if detail:
                entry["detail"] = detail
            if getattr(record, "request_id", None):
                entry["request_id"] = record.request_id
            # log.exception 的调用栈必须进文件：仅靠控制台会话会丢失唯一线索。
            if record.exc_info:
                import traceback
                entry["traceback"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with self._today_path().open("a", encoding="utf-8") as stream:
                stream.write(line + "\n")
        except Exception:  # noqa: BLE001 - 日志通道自身故障不允许影响业务
            pass


class MergingLoggerAdapter(logging.LoggerAdapter):
    """合并 adapter 级与调用级 extra 的适配器。

    标准 LoggerAdapter.process 会用 adapter 自身 extra **覆盖**调用方
    通过 log(..., extra=...) 传入的字段（boot_id 丢失 detail 的根因）；
    此处改为两层合并，调用方字段优先。
    """

    def process(self, msg, kwargs):
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def setup_logging(logs_dir: Path, boot_id: str) -> logging.LoggerAdapter:
    """装配根 logger：控制台给人看（含中文提示），文件给诊断页读。

    返回携带 boot_id 的合并式适配器；每条记录（含调用方 extra 的
    detail/request_id）都会完整落进 JSONL。
    """
    root = logging.getLogger("codol_control")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    root.addHandler(JsonlFileHandler(logs_dir))
    return MergingLoggerAdapter(root, {"boot_id": boot_id})
