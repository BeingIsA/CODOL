"""管理员用例：初始化、登录、改密、停服授权与审计。

凭据策略（M1，单人本地管理）：
- 首次启动生成随机初始口令，写入 data/初始管理员凭据.txt（仅此一次），
  服务日志只提示文件位置，不打印口令本身；
- 口令散列与口令版本存 settings 表；改密成功后旧令牌全部失效；
- 每个敏感动作写 audit_log（动作、时间、行为人，不含口令明文）。
"""
from __future__ import annotations

import json
from datetime import timedelta
from datetime import datetime, timezone

from ..domain.errors import InvalidArgumentError, UnauthenticatedError
from ..domain.identity import ADMIN_USERNAME, PASSWORD_VERSION_KEY, token_subject, validate_password_policy
from .ports import AuditStore, Settings, Tokens, PasswordTools

# settings 表键名；集中定义避免裸字符串散落。
K_ADMIN = "admin.account"
K_TOKEN_SECRET = "auth.token_secret"
CREDENTIALS_FILENAME = "初始管理员凭据.txt"


class AdminService:
    """管理帐号全生命周期用例。"""

    def __init__(self, db: AuditStore, settings: Settings, token_factory,
                 token_ttl_minutes: int, passwords: PasswordTools, write_credentials, clock) -> None:
        self._db = db
        self._settings = settings
        self._signer: Tokens | None = None
        self._token_factory = token_factory
        self._ttl = timedelta(minutes=token_ttl_minutes)
        self._passwords = passwords
        self._write_credentials = write_credentials
        self._clock = clock

    # ---- 初始化 -------------------------------------------------------

    def ensure_provisioned(self) -> bool:
        """确保管理员与令牌密钥存在；返回是否发生了首次初始化。

        初始口令只在创建那一刻写一次凭据文件；文件被删不会重新生成
        （口令可登录后修改），避免“删文件重置口令”成为后门。
        """
        if self._settings.get(K_TOKEN_SECRET) is None:
            self._settings.put(K_TOKEN_SECRET, self._passwords.random(32))
        self._signer = self._token_factory(self._settings.get(K_TOKEN_SECRET))
        account = self._settings.get(K_ADMIN)
        if account is not None:
            return False
        initial_password = self._passwords.random(8)  # 16 位十六进制
        salt = self._passwords.salt()
        account = {
            "username": ADMIN_USERNAME,
            "salt": salt,
            "password_hash": self._passwords.hash(initial_password, salt),
            "password_version": 1,
            "created_at": self._clock(),
        }
        self._settings.put(K_ADMIN, account)
        self._write_credentials(ADMIN_USERNAME, initial_password)
        self.audit("system", "admin.provisioned", {"username": ADMIN_USERNAME})
        return True

    def _account(self) -> dict:
        account = self._settings.get(K_ADMIN)
        if not isinstance(account, dict):
            raise UnauthenticatedError("管理员帐号未初始化。")
        return account

    # ---- 登录与会话 -----------------------------------------------------

    def login(self, username: str, password: str) -> dict:
        """校验口令并签发会话令牌；失败统一 401，不区分用户名/密码错误。"""
        account = self._account()
        ok = (
            username == account.get("username")
            and password is not None
            and self._passwords.hash(password, account["salt"]) == account["password_hash"]
        )
        if not ok:
            self.audit(username or "?", "admin.login_failed", {})
            raise UnauthenticatedError("用户名或密码错误。")
        payload = {
            "sub": token_subject(account["username"]),
            PASSWORD_VERSION_KEY: int(account["password_version"]),
            "iat": self._clock(),
        }
        token = self._signer.sign(payload, self._ttl)
        self.audit(account["username"], "admin.login", {})
        return {
            "token": token,
            "token_type": "bearer",
            "expires_at": _expiry(self._signer.verify(token)),
            "username": account["username"],
        }

    def verify_token(self, token: str | None) -> dict:
        """校验令牌并核对口令版本；返回载荷（sub 即当前用户名）。"""
        if not token:
            raise UnauthenticatedError("缺少会话令牌。")
        payload = self._signer.verify(token)
        account = self._account()
        if int(payload.get(PASSWORD_VERSION_KEY, 0)) != int(account["password_version"]):
            raise UnauthenticatedError("密码已变更，请重新登录。")
        return payload

    # ---- 改密 -----------------------------------------------------------

    def change_password(self, token_payload: dict, old_password: str, new_password: str) -> None:
        """改密：先验旧口令，再按领域策略校验新口令，然后整体升级版本。"""
        account = self._account()
        if old_password is None or self._passwords.hash(old_password, account["salt"]) != account["password_hash"]:
            raise InvalidArgumentError("当前密码不正确。")
        validate_password_policy(new_password)
        salt = self._passwords.salt()
        account.update({
            "salt": salt,
            "password_hash": self._passwords.hash(new_password, salt),
            "password_version": int(account["password_version"]) + 1,
            "password_changed_at": self._clock(),
        })
        self._settings.put(K_ADMIN, account)
        self.audit(account["username"], "admin.password_changed", {})

    # ---- 停服授权与审计 --------------------------------------------------

    def authorize_shutdown(self, bearer_payload: dict | None, shutdown_token: str | None,
                           expected_shutdown_token: str) -> dict:
        """停服授权：管理员会话或本机 control.json 的停服令牌二者其一。

        Stop.cmd 的本机停服令牌每次启动随机生成、随停服删除，
        等价于“能读本机文件的人才可停服”，不向网络暴露额外权力。
        """
        if bearer_payload is not None:
            self.audit(bearer_payload.get("sub", "?"), "server.shutdown", {"via": "session"})
            return {"via": "session", "actor": bearer_payload.get("sub")}
        if shutdown_token and expected_shutdown_token and shutdown_token == expected_shutdown_token:
            self.audit("local", "server.shutdown", {"via": "local-token"})
            return {"via": "local-token", "actor": "local"}
        raise UnauthenticatedError("无停服权限：需要管理员会话或本机停服令牌。")

    def audit(self, actor: str, action: str, detail: dict) -> None:
        """写审计记录；detail 必须已脱敏（调用方责任）。"""
        self._db.execute(
            "INSERT INTO audit_log(at, actor, action, detail) VALUES(?, ?, ?, ?)",
            (self._clock(), actor, action, json.dumps(detail, ensure_ascii=False)),
        )


def _expiry(payload: dict) -> str:
    """从令牌载荷取过期时间转 ISO；供登录响应展示。"""
    exp = payload.get("exp")
    return datetime.fromtimestamp(int(exp), timezone.utc).isoformat() if exp else ""
