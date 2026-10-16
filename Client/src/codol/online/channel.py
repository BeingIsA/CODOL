"""M2 HTTP 命令与 SSE 接收：有界超时、独立玩家凭据、有限快照队列。

网络不占调试事件循环；SSE 接收线程只入队，不修改原生 UI。
开发接入仅允许回环地址，不能当成正式账号登录协议。
"""
import json
import queue
import secrets
import threading
import urllib.error
import urllib.request


class ChannelError(RuntimeError):
    """稳定错误码供宿主区分业务拒绝与会话失效。"""
    def __init__(self, code, message):
        """保存中文原因，不携带请求头和凭据。"""
        super().__init__(message)
        self.code = code


class PlayerChannel:
    """一个宿主对应一个独立玩家会话；令牌只在内存中存在。"""
    def __init__(self, workspace, account, name):
        """从本机控制文件读取端口与开发凭据，创建/恢复稳定身份。"""
        root = workspace / "GameServer/control/data"
        state = json.loads((root / "control.json").read_text(encoding="utf-8"))
        if state["host"] != "127.0.0.1":
            raise ValueError("M2 开发通道只支持回环服务。")
        self._url = f"http://127.0.0.1:{int(state['port'])}/api"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        access = json.loads((root / "development-access.json").read_text(encoding="utf-8"))["secret"]
        path = account / "development-identity.json"
        if not path.exists():
            with path.open("x", encoding="utf-8") as output:
                json.dump({"identity": secrets.token_hex(32)}, output)
        identity = json.loads(path.read_text(encoding="utf-8"))["identity"]
        self._token = ""
        login = self.request("/player/login", {"identity": identity, "display_name": name, "protocol_version": 1},
                             {"X-Development-Key": access})
        self._token, self.player = login["token"], login["player"]
        self.snapshots = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread = None

    def request(self, path, body=None, headers=None):
        """命令三秒超时；幂等编号由调用者持有，断网可使用同编号重试。"""
        values = {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"}
        values.update(headers or {})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        try:
            with self._opener.open(urllib.request.Request(self._url + path, data=data, headers=values), timeout=3) as response:
                return json.loads(response.read(1024 * 1024))
        except urllib.error.HTTPError as exc:
            error = json.loads(exc.read(65536)).get("error", {})
            raise ChannelError(error.get("code", "HTTP_ERROR"), error.get("message", "玩家请求失败。")) from None

    def start(self):
        """启动唯一事件接收线程，生命周期由本次宿主会话拥有。"""
        self._thread = threading.Thread(target=self._events, name="player-events", daemon=True)
        self._thread.start()

    def _publish(self, value):
        """队列满时丢最旧快照；每次推送均含全量状态，允许合并。"""
        try:
            self.snapshots.put_nowait(value)
        except queue.Full:
            try:
                self.snapshots.get_nowait()
            except queue.Empty:
                pass
            self.snapshots.put_nowait(value)

    def _events(self):
        """流断开立即标为离线，恢复后从服务端取完整快照。"""
        cursor = 0
        while not self._stop.is_set():
            try:
                request = urllib.request.Request(self._url + f"/player/events?after={cursor}",
                                                 headers={"Authorization": "Bearer " + self._token})
                with self._opener.open(request, timeout=5) as response:
                    while not self._stop.is_set():
                        line = response.readline(1024 * 1024)
                        if not line:
                            raise OSError("事件连接关闭。")
                        if line.startswith(b"data: "):
                            value = json.loads(line[6:])
                            if not value:
                                raise ChannelError("UNAUTHENTICATED", "玩家会话已失效。")
                            cursor = value["cursor"]
                            self._publish(value)
            except (OSError, ValueError, ChannelError):
                self._publish({"offline": True})
                self._stop.wait(1)

    def close(self):
        """停止接收并尽力主动离线；服务已断开时仍能结束本地清理。"""
        self._stop.set()
        try:
            self.request("/player/disconnect", {})
        except (OSError, ValueError, ChannelError):
            pass
        if self._thread:
            self._thread.join(timeout=6)
