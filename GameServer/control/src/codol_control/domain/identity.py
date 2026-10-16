"""管理员身份领域规则：命名、口令策略、会话令牌载荷。

只定义规则与数据形状；散列、签名等实现细节在 infrastructure/tokens.py。
"""
from __future__ import annotations

from dataclasses import dataclass

# 口令最小长度：本地单人管理场景的最低要求，不冒充公网强度标准。
PASSWORD_MIN_LENGTH = 8
# 唯一内置管理员名；M1 不提供多管理员（能力表如实标注）。
ADMIN_USERNAME = "admin"
# 令牌载荷里的口令版本键：改密后 +1，使全部旧令牌失效。
PASSWORD_VERSION_KEY = "ver"


@dataclass(frozen=True)
class AdminAccount:
    """管理员帐号的持久形态（散列等敏感字段由存储层保管）。"""

    username: str
    password_version: int


def validate_password_policy(new_password: str) -> None:
    """新口令必须满足的领域规则；不满足抛 InvalidArgumentError。

    刻意保持简单明确：长度下限 + 非全空白。复杂度规则对单人本地管理
    服务收益有限，反而鼓励弱模式化口令。
    """
    import re

    from .errors import InvalidArgumentError

    if new_password is None or len(new_password) < PASSWORD_MIN_LENGTH:
        raise InvalidArgumentError(f"新密码至少 {PASSWORD_MIN_LENGTH} 个字符。")
    if re.fullmatch(r"\s+", new_password or ""):
        raise InvalidArgumentError("密码不能全为空白字符。")


def token_subject(username: str) -> str:
    """令牌载荷 sub 字段：当前版本即用户名（将来多帐户时可换 ID）。"""
    return username
