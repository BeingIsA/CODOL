"""Stop.cmd 的本机停服客户端：调用管理接口优雅停止服务。

流程：读 data/control.json（启动时生成）→ 用停服令牌请求
POST /api/admin/shutdown → 轮询 /health 直到连接被拒（确认真停）。
控制文件不存在时给出明确提示而不是报错堆栈。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _root() -> Path:
    """control 根目录（本文件位于 src/codol_control/ 下）。"""
    return Path(__file__).resolve().parents[2]


def _post_shutdown(url: str, token: str) -> tuple[bool, str]:
    """发送停服请求；返回 (是否受理, 说明)。"""
    req = urllib.request.Request(
        url + "api/admin/shutdown", method="POST", data=b"{}",
        headers={"Content-Type": "application/json", "X-Shutdown-Token": token},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}：" + exc.read().decode("utf-8", "replace")[:200]
    except urllib.error.URLError as exc:
        return False, f"无法连接服务：{exc.reason}"


def _wait_stopped(url: str, timeout: float = 20.0) -> bool:
    """轮询健康检查直到连接失败，证明监听已关闭。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url + "api/health", timeout=2).read()
        except Exception:  # noqa: BLE001 - 连不上即视为已停止
            return True
        time.sleep(0.4)
    return False


def main() -> int:
    """停服入口；返回进程退出码（0 成功/服务本就未运行）。"""
    control_path = _root() / "data" / "control.json"
    if not control_path.is_file():
        print("未找到运行期控制文件：服务当前没有在运行（或上次未正常退出）。")
        return 0
    state = json.loads(control_path.read_text(encoding="utf-8"))
    url = f"http://{state['host']}:{state['port']}/"
    ok, message = _post_shutdown(url, state["shutdown_token"])
    if not ok:
        print("停服请求被拒绝：" + message)
        return 1
    print("已发送停服请求，等待服务完成收尾……")
    if _wait_stopped(url):
        print("服务已停止，端口已释放。")
        return 0
    print("等待超时：服务可能仍在运行，请查看服务窗口日志。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
