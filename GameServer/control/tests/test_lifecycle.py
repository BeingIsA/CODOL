"""生命周期用例：页面关闭不关服、优雅停服、Stop.cmd 链路与端口冲突诊断。

端口冲突用例通过真实子进程走 main.run 的完整路径
（环境变量把 data/logs 指到临时目录），验证退出码与诊断文件。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support import CONTROL_ROOT, ServiceInstance


class LifecycleTests(unittest.TestCase):
    """M1 验收的生命周期条款。"""

    def setUp(self):
        self.service = ServiceInstance()
        status, login = self.service.login(self.service.initial_password())
        assert status == 200, login
        self.token = login["token"]

    def tearDown(self):
        self.service.stop()

    def test_page_close_does_not_stop_service(self):
        """多次建立/断开 HTTP 连接（等价于反复开关页面）服务必须存活。"""
        for _ in range(3):
            status, body = self.service.request("GET", "/api/overview", token=self.token)
            self.assertEqual(status, 200)
            # urllib 每次请求结束即断开连接，模拟浏览器关闭页面。
        self.assertTrue(self.service.server.started)

    def test_state_consistent_across_page_reopen(self):
        """“重新打开页面”读到同一真实状态：boot_id 与房间计数一致。"""
        _, first = self.service.request("GET", "/api/overview", token=self.token)
        _, second = self.service.request("GET", "/api/overview", token=self.token)
        self.assertEqual(first["service"]["boot_id"], second["service"]["boot_id"])
        self.assertEqual(first["rooms"]["total"], second["rooms"]["total"])

    def test_shutdown_via_local_token(self):
        """Stop.cmd 同款链路：本机停服令牌 → 优雅退出 → 控制文件删除。"""
        state = json.loads(
            (self.service.data_dir / "control.json").read_text(encoding="utf-8"))
        status, body = self.service.request(
            "POST", "/api/admin/shutdown", body={},
            headers={"X-Shutdown-Token": state["shutdown_token"]})
        self.assertEqual(status, 200, body)
        self.assertTrue(self.service.wait_dead(), "服务应在停服请求后退出")
        # 生产路径中收尾由 main.run 的 finally 完成；测试实例等价调用一次。
        self.service.stop()
        self.assertFalse(self.service.container.config.control_state_path.exists())

    def test_shutdown_rejects_wrong_local_token(self):
        """错误的停服令牌必须 401，且服务继续运行。"""
        status, _ = self.service.request("POST", "/api/admin/shutdown", body={},
                                         headers={"X-Shutdown-Token": "deadbeef"})
        self.assertEqual(status, 401)
        status, _ = self.service.request("GET", "/api/health")
        self.assertEqual(status, 200)

    def test_cleanup_preserves_foreign_control_file(self):
        """收尾只删除属于自己 boot_id 的控制文件。

        回归：端口被占的启动失败实例曾误删在运行实例的 control.json，
        导致 Stop.cmd 找不到停服凭据（2026-10-15 实测）。
        """
        import json as _json

        from codol_control.main import _cleanup

        path = self.service.container.config.control_state_path
        foreign = {"boot_id": "not-this-boot", "shutdown_token": "x"}
        path.write_text(_json.dumps(foreign), encoding="utf-8")
        _cleanup(self.service.container, self.service.logger)
        self.assertTrue(path.is_file(), "外来 boot_id 的控制文件必须保留")
        self.assertEqual(_json.loads(path.read_text(encoding="utf-8")), foreign)


class PortConflictTests(unittest.TestCase):
    """启动失败诊断：端口被占时子进程以退出码 2 结束并写诊断文件。"""

    def test_bind_failure_reported(self):
        python = CONTROL_ROOT.parent / "python" / "python.exe"
        self.assertTrue(python.is_file(), "缺少 GameServer/python 环境")
        with tempfile.TemporaryDirectory(prefix="codol-conflict-") as tmp:
            # 先占住一个端口，再让服务尝试绑定同一端口。
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
                holder.bind(("127.0.0.1", 0))
                holder.listen(1)
                port = holder.getsockname()[1]
                env = dict(os.environ,
                           CODOL_CONTROL_PORT=str(port),
                           CODOL_CONTROL_DATA_DIR=os.path.join(tmp, "data"),
                           CODOL_CONTROL_LOGS_DIR=os.path.join(tmp, "logs"),
                           CODOL_CONTROL_NO_BROWSER="1")
                result = subprocess.run(
                    [str(python), "-X", "utf8", "-m", "codol_control", "--no-browser"],
                    cwd=str(CONTROL_ROOT), env=env, capture_output=True, text=True,
                    timeout=90)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                report = Path(env["CODOL_CONTROL_DATA_DIR"]) / "startup-failure.json"
                self.assertTrue(report.is_file(), "必须写出启动失败诊断文件")
                data = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(data["kind"], "bind_failure")
                self.assertEqual(data["bind_port"], port)

    def test_second_process_preserves_running_stop_credentials(self):
        """完整重复启动路径：同 data/port 的失败实例不得覆盖停服凭据。"""
        service = ServiceInstance()
        try:
            control = service.container.config.control_state_path
            original = control.read_bytes()
            env = dict(os.environ, CODOL_CONTROL_PORT=str(service.port),
                       CODOL_CONTROL_DATA_DIR=str(service.data_dir),
                       CODOL_CONTROL_LOGS_DIR=str(service.container.config.logs_dir),
                       CODOL_CONTROL_NO_BROWSER="1")
            result = subprocess.run([str(CONTROL_ROOT.parent / "python/python.exe"), "-X", "utf8", "-m", "codol_control", "--no-browser"],
                                    cwd=str(CONTROL_ROOT), env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(control.read_bytes(), original)
            token = json.loads(original)["shutdown_token"]
            status, _ = service.request("POST", "/api/admin/shutdown", body={}, headers={"X-Shutdown-Token": token})
            self.assertEqual(status, 200)
            self.assertTrue(service.wait_dead())
        finally:
            service.stop()


if __name__ == "__main__":
    unittest.main()
