"""SQLite 数据库封装：连接、互斥访问与迁移执行。

设计要点：
- FastAPI 的同步端点运行在线程池，多线程共享一条连接；用一把锁串行化
  全部访问。M1 负载极低，这是最简单且正确的做法；未来多 worker 形态
  属于 M6 的架构改造，不在本层预留复杂度。
- 迁移脚本按文件名序执行，逐个事务，已应用的记录在 schema_migrations；
  迁移失败立即抛错并停止启动（方案 §8.4：迁移失败停止启动）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class Database:
    """持有唯一连接与锁；所有读写都经过 execute/commit 包装。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL 模式：读写并发更友好，掉电后一致性由 SQLite 保证。
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """单条写语句：加锁、执行、提交，返回游标（含 lastrowid）。"""
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """只读查询：加锁执行并物化结果，避免游标跨锁生命周期。"""
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def transaction(self, statements: list[tuple[str, tuple]]) -> None:
        """多语句原子执行：任一步失败整体回滚（迁移使用）。"""
        with self._lock:
            try:
                for sql, params in statements:
                    self.conn.execute(sql, params)
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def close(self) -> None:
        """关闭连接；停服顺序中数据库最后关闭。"""
        with self._lock:
            self.conn.close()


def run_migrations(db: Database, migrations_dir: Path) -> list[str]:
    """执行 migrations/ 下尚未应用的 *.sql（按文件名升序）。

    每个脚本与它的版本登记包裹在同一个显式事务里（BEGIN…COMMIT 全部
    写进一段脚本交给 executescript，保证原子性）；任一步失败整体回滚，
    已应用版本跳过。返回本次实际应用的版本列表（供启动日志输出）。
    """
    import re

    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["version"] for row in db.query("SELECT version FROM schema_migrations")}
    used: list[str] = []
    for script in sorted(Path(migrations_dir).glob("*.sql")):
        version = script.stem
        # 版本号来自文件名并会拼进 SQL，仅放行无引号/分号等风险的字符。
        if not re.fullmatch(r"[0-9A-Za-z_.-]+", version):
            raise ValueError(f"迁移文件名不安全：{version}")
        if version in applied:
            continue
        body = script.read_text(encoding="utf-8")
        full = (
            "BEGIN;\n" + body + "\n"
            f"INSERT INTO schema_migrations(version, applied_at) "
            f"VALUES('{version}', datetime('now'));\nCOMMIT;"
        )
        with db._lock:  # noqa: SLF001 - 迁移是数据库封装的内部使用者
            db.conn.executescript(full)
        used.append(version)
    return used
