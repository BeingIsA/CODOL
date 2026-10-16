"""持久化用例：改密与重启——M1 验收“重启服务不会丢失配置”。

用两个先后启动、共享同一 data 目录的服务实例验证：
改密跨重启生效；旧令牌随口令版本失效；审计记录保留。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import ServiceInstance

NEW_PASSWORD = "restart-survives-9"


class PersistenceTests(unittest.TestCase):
    """跨重启的数据一致性。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="codol-persist-")
        root = Path(cls._tmp.name)
        cls.data_dir = root / "data"
        cls.logs_dir = root / "logs"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_01_password_change_survives_restart(self):
        """第一次运行：初始口令登录→改密→旧令牌失效→停服。"""
        first = ServiceInstance(data_dir=self.data_dir, logs_dir=self.logs_dir)
        try:
            status, login = first.login(first.initial_password())
            self.assertEqual(status, 200)
            old_token = login["token"]
            status, _ = first.request("POST", "/api/admin/password", token=old_token,
                                      body={"old_password": first.initial_password(),
                                            "new_password": NEW_PASSWORD})
            self.assertEqual(status, 200)
            # 改密后旧令牌立即失效（口令版本提升）。
            status, _ = first.request("GET", "/api/overview", token=old_token)
            self.assertEqual(status, 401)
            rows = first.container.db.query("SELECT COUNT(*) AS n FROM audit_log")
            self.assertGreaterEqual(rows[0]["n"], 2)  # 登录 + 改密
        finally:
            first.stop()

        # 第二次运行：同一 data 目录，旧口令失效、新口令可用。
        second = ServiceInstance(data_dir=self.data_dir, logs_dir=self.logs_dir)
        try:
            status, _ = second.login(second.initial_password())
            self.assertEqual(status, 401, "初始口令文件仍在，但口令已改，不能登录")
            status, login = second.login(NEW_PASSWORD)
            self.assertEqual(status, 200)
            status, _ = second.request("GET", "/api/overview", token=login["token"])
            self.assertEqual(status, 200)
            # 口令版本保持 2（首次初始化=1，第一次运行改密+1），
            # 证明第二次启动沿用了同一份数据而不是重新初始化。
            account = second.container.admin._settings.get("admin.account")  # noqa: SLF001
            self.assertEqual(account["password_version"], 2)
        finally:
            second.stop()


if __name__ == "__main__":
    unittest.main()
