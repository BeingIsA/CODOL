"""管理员凭据基础设施：随机/散列适配与首次本机凭据文件发布。

应用只调用端口；文件格式和安装路径由本模块拥有，不进入登录规则。
"""
from .tokens import generate_secret_hex, hash_password, new_salt


class Credentials:
    """组合根注入的口令工具；沿用 M1 已验证的算法和盐长度。"""
    @staticmethod
    def random(size):
        """生成指定字节长度的随机十六进制凭据。"""
        return generate_secret_hex(size)

    @staticmethod
    def salt():
        """创建独立账号盐。"""
        return new_salt()

    @staticmethod
    def hash(password, salt):
        """沿用受控 PBKDF2 参数，不在用例层复制密码算法。"""
        return hash_password(password, salt)


class InitialCredentialsWriter:
    """只在应用确认首次初始化时发布凭据，不提供重置密码功能。"""
    def __init__(self, data_dir):
        """安装级路径由配置层提供。"""
        self._path = data_dir / "初始管理员凭据.txt"

    def __call__(self, username, password):
        """文件格式与 M1 保持兼容；明文从不记录到日志。"""
        self._path.write_text(
            "CODOL 控制服务 · 首次生成的管理员凭据（仅此一次）\n\n"
            f"用户名：{username}\n密码：{password}\n\n"
            "请立即登录并在“账户”页修改密码；此文件不会再次生成。\n"
            "修改密码后可删除本文件。\n", encoding="utf-8")
