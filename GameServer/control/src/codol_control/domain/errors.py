"""领域错误：稳定错误码 + 中文用户可读信息。

错误码与实施方案 §5.3 的命名保持一致并按 M1 需要裁剪；
transport 层依据 http_status 映射状态码，其余层只抛领域错误，不碰 HTTP。
"""
from __future__ import annotations


class ControlError(Exception):
    """领域错误基类：code 供程序判断，message 给用户看，detail 可选补充。"""

    code = "INTERNAL"
    http_status = 500

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class UnauthenticatedError(ControlError):
    """未认证：没有令牌、令牌无效/过期、口令错误。"""

    code = "UNAUTHENTICATED"
    http_status = 401


class ForbiddenError(ControlError):
    """已认证但无权执行该操作（如非管理员调用管理接口）。"""

    code = "FORBIDDEN"
    http_status = 403


class InvalidArgumentError(ControlError):
    """请求参数不满足领域规则（如新密码太短）。"""

    code = "INVALID_ARGUMENT"
    http_status = 400


class InvalidStateError(ControlError):
    """当前状态不允许该操作（预留给房间/对局状态机，M2 起）。"""

    code = "INVALID_STATE"
    http_status = 409


class UnsupportedContentError(ControlError):
    """请求的内容/能力当前版本不支持（如 M1 尝试创建房间）。"""

    code = "UNSUPPORTED_CONTENT"
    http_status = 422


class BattleNotReadyError(ControlError):
    """M3：战斗运行时未就绪（预检未通过/端口占用），开局请求被拒绝。"""

    code = "RUNTIME_NOT_READY"
    http_status = 409
