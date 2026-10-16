"""总览与房间用例：M1 阶段的数据必须如实（空房间、零连接、能力关闭）。"""
from __future__ import annotations

import unittest

from support import ServiceInstance


class OverviewRoomTests(unittest.TestCase):
    """共享一个服务实例即可（只读用例）。"""

    @classmethod
    def setUpClass(cls):
        cls.service = ServiceInstance()
        status, body = cls.service.login(cls.service.initial_password())
        assert status == 200, body
        cls.token = body["token"]

    @classmethod
    def tearDownClass(cls):
        cls.service.stop()

    def test_overview_shape_and_honesty(self):
        """总览字段齐全；房间 0、玩家 0、运行时未就绪都如实呈现。"""
        status, body = self.service.request("GET", "/api/overview", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["rooms"]["total"], 0)
        self.assertEqual(body["rooms"]["active"], 0)
        self.assertEqual(body["players"]["connected"], 0)
        # 测试实例使用假探测器：ready 必须为 False（假结果不得冒充可用）。
        self.assertFalse(body["runtime"]["probe_ready"])
        self.assertTrue(body["service"]["version"])
        self.assertTrue(body["service"]["bind"].startswith("127.0.0.1"))

    def test_rooms_empty_with_capability_flags(self):
        """房间列表恒空，但结构、容量与 create_enabled 开关必须明确。"""
        status, body = self.service.request("GET", "/api/rooms", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 0)
        self.assertEqual(body["max_active"], 1)
        self.assertFalse(body["create_enabled"])
        self.assertEqual(body["items"], [])

    def test_capabilities_reflect_milestone(self):
        """M3 对局控制已实现；测试实例使用假监督器时必须如实关闭该能力。"""
        _, caps = self.service.request("GET", "/api/capabilities")
        features = caps["features"]
        self.assertTrue(features["admin_ui"])
        self.assertTrue(features["runtime_probe"])
        self.assertTrue(features["rooms_create"])
        self.assertTrue(features["player_channel"])
        # 假监督器不得冒充可开局（与假探测器 ready=False 同一诚实原则）。
        self.assertFalse(features["match_control"])
        self.assertFalse(features["bots"])


if __name__ == "__main__":
    unittest.main()
