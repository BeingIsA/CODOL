"""管理员认证用例：登录、令牌校验、改密与公开端点边界。

对应 M1 验收条款“接口授权测试通过”：
未带令牌/坏令牌访问管理接口必须 401；正确令牌必须可用。
"""
from __future__ import annotations

import unittest

from support import ServiceInstance


class AdminAuthTests(unittest.TestCase):
    """每个用例使用独立服务实例，互不共享会话。"""

    @classmethod
    def setUpClass(cls):
        cls.service = ServiceInstance()

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()

    def test_public_endpoints_without_token(self):
        """/health 与 /capabilities 是仅有的公开端点，且不含敏感字段。"""
        for path in ("/api/health", "/api/capabilities"):
            status, body = self.service.request("GET", path)
            self.assertEqual(status, 200, body)
        status, body = self.service.request("GET", "/api/health")
        self.assertEqual(body["status"], "ok")
        self.assertIn("boot_id", body)

    def test_admin_endpoints_require_token(self):
        """总览/房间/运行时/诊断未带令牌一律 401 + UNAUTHENTICATED。"""
        for path in ("/api/overview", "/api/rooms", "/api/runtime",
                     "/api/diagnostics/logs", "/api/admin/session"):
            status, body = self.service.request("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"]["code"], "UNAUTHENTICATED")

    def test_wrong_password_rejected(self):
        """错误口令登录失败，错误信息不泄露帐号是否存在等细节。"""
        status, body = self.service.login("definitely-wrong")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHENTICATED")

    def test_login_and_session(self):
        """正确初始口令换取令牌；whoami 返回管理员名。"""
        status, body = self.service.login(self.service.initial_password())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["token"])
        status, who = self.service.request("GET", "/api/admin/session", token=body["token"])
        self.assertEqual(status, 200)
        self.assertEqual(who["username"], "admin")

    def test_garbage_token_rejected(self):
        """伪造/损坏令牌必须 401，不能进入任何管理接口。"""
        for bad in ("garbage", "a.b", "////", "x" * 200):
            status, body = self.service.request("GET", "/api/overview", token=bad)
            self.assertEqual(status, 401, bad)

    def test_shutdown_requires_auth(self):
        """停服端点无凭据必须 401（不能只靠“知道端口”停服）。"""
        status, body = self.service.request("POST", "/api/admin/shutdown", body={})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
