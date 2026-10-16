"""时钟抽象：所有时间获取集中在这里，便于测试替换与统一格式。"""
from __future__ import annotations

import datetime as _dt


def utc_now() -> _dt.datetime:
    """当前 UTC 时间（含时区信息），作为服务内部统一时间源。"""
    return _dt.datetime.now(_dt.timezone.utc)


def iso_now() -> str:
    """UTC ISO-8601 字符串（秒级），用于日志、审计与 API 展示。"""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(ts: _dt.datetime) -> str:
    """把任意 datetime 规范为 UTC ISO 字符串。"""
    return ts.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
