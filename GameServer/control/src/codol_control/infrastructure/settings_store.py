"""键值设置存储：管理员凭据、令牌密钥等安装级持久数据。

值统一存 JSON 字符串，读取方负责结构解释；写入是原子的单行 UPSERT。
这层不包含任何业务规则（如密码策略），只做存取。
表结构由 migrations/0001_init.sql 定义，本层不建表。
"""
from __future__ import annotations

import json
from pathlib import Path

from .database import Database


class SettingsStore:
    """settings 表的类型化包装。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str) -> object | None:
        """读取并反序列化；键不存在返回 None。"""
        rows = self.db.query("SELECT value FROM settings WHERE key = ?", (key,))
        if not rows:
            return None
        return json.loads(rows[0]["value"])

    def put(self, key: str, value: object) -> None:
        """写入（UPSERT）；value 必须可 JSON 序列化。"""
        self.db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, json.dumps(value, ensure_ascii=False)),
        )
