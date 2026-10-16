"""密码散列与会话令牌：PBKDF2 口令 + HMAC 签名的无状态令牌。

安全边界（方案 §5.2/§8.1 的 M1 落地）：
- 口令只存 PBKDF2-SHA256 散列（200k 迭代 + 每帐号随机盐），明文不入库不入日志；
- 令牌是 HMAC-SHA256 签名的 JSON（子帐号/签发时间/过期/口令版本），
  服务端无会话表即可校验；改密码会提升口令版本，旧令牌随之全部失效；
- 签名密钥安装时随机生成并存数据库 settings，绝不写进配置文件或日志。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta

from ..domain.errors import UnauthenticatedError
from .clock import utc_now

_PBKDF2_ITERATIONS = 200_000


def generate_secret_hex(nbytes: int = 32) -> str:
    """生成加密随机十六进制串（令牌密钥、停服令牌、初始口令共用）。"""
    return secrets.token_hex(nbytes)


def hash_password(password: str, salt_hex: str) -> str:
    """口令 → PBKDF2 散列（十六进制）。盐由调用方生成并与其一同存储。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS
    ).hex()


def new_salt() -> str:
    """每帐号独立随机盐。"""
    return secrets.token_hex(16)


class TokenSigner:
    """签发与校验会话令牌；一个实例绑定一个签名密钥。"""

    def __init__(self, secret_hex: str) -> None:
        self._secret = bytes.fromhex(secret_hex)

    def sign(self, payload: dict, ttl: timedelta) -> str:
        """把 payload 加上过期时间后签名，输出可放入 HTTP 头的 urlsafe 串。"""
        body = dict(payload)
        body["exp"] = int((utc_now() + ttl).timestamp())
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        sig = hmac.new(self._secret, raw, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(sig).decode()

    def verify(self, token: str) -> dict:
        """校验签名与有效期；任何不匹配统一抛 UnauthenticatedError（不泄露原因）。"""
        try:
            raw_b64, sig_b64 = token.split(".", 1)
            raw = base64.urlsafe_b64decode(raw_b64.encode())
            expected = hmac.new(self._secret, raw, hashlib.sha256).digest()
            if not hmac.compare_digest(expected, base64.urlsafe_b64decode(sig_b64.encode())):
                raise ValueError
            payload = json.loads(raw)
            if int(payload["exp"]) < int(utc_now().timestamp()):
                raise ValueError
            return payload
        except Exception as exc:
            raise UnauthenticatedError("会话令牌无效或已过期，请重新登录。") from exc
