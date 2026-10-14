"""``python -m codol`` 的进程入口：转发到入口层 cli.entry。"""
from .cli import entry

raise SystemExit(entry())
