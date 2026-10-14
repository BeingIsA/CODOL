"""共享内核：配置加载、用户名校验与版本哈希校验。

职责：
- 定义本项目唯一支持的客户端版本指纹（原始 EXE 与恢复镜像的 SHA-256）；
- 从 ``config.json`` 读取用户配置（游戏目录、用户名）；
- 校验用户名满足客户端的编码约束（UTF-8 长度、GBK 兼容）；
- ``validate()`` 在每次启动前核对原始客户端与关键运行组件的完整性。

本模块导入不产生任何进程或副作用，可被测试直接使用。
"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import unicodedata
from .paths import PROJECT, DEFAULT_GAME, ASSETS

# 原始客户端 EXE（codoMP_client_shipRetail.exe）的 SHA-256，仅支持 2.6.9.7。
CLIENT_HASH = '353754047dbbfe3afbf6c7d97de3faca785b3d5d5e34a9053c820ee78814d6e1'
# aPLib 解压恢复镜像的 SHA-256，补丁全部以该镜像为基底（见 v2697.client）。
RECOVERED_HASH = '24b4ef2502d3661df178f557192534098c714353cf8c2a00a2ffb26af2bb97e4'


def digest(path):
    """计算文件内容的 SHA-256 十六进制摘要，用于版本与组件校验。"""
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def username(value):
    """校验并规范化用户名，返回去除首尾空白后的文本。

    客户端侧存在两处名字消费路径（前端 GBK/CP936 与另一处 UTF-8），
    因此这里同时要求 UTF-8 与 GBK 双重可编码，任一失败都直接拒绝：
    - 长度限制 1–24 个 UTF-8 字节（约 8 个汉字）；
    - 只允许文字/数字与少量安全符号；
    - GBK 编码中含 0x92 私用区字节的字符会被客户端传输层拒绝。

    抛出 ``ValueError`` 时附带面向用户的中文原因。
    """
    value = value.strip()
    if not value or len(value.encode('utf-8')) > 24:
        raise ValueError('用户名需要 1–24 个 UTF-8 字节（最多 8 个汉字）。')
    if any(unicodedata.category(c)[0] not in 'LN' and c not in ' _-.[]()' for c in value):
        raise ValueError('用户名含不支持的符号。')
    try:
        encoded = value.encode('gbk')
    except UnicodeEncodeError as exc:
        raise ValueError('用户名包含游戏字库不支持的字符。') from exc
    if b'\x92' in encoded:
        raise ValueError('用户名包含客户端传输不支持的文字。')
    return value


@dataclass(frozen=True)
class Config:
    """一次启动的不可变配置：游戏目录与用户名。"""
    game: Path = DEFAULT_GAME
    name: str = 'Player'

    @classmethod
    def load(cls):
        """从项目根的 ``config.json`` 读取配置；文件不存在时使用默认值。

        相对路径按项目根解析；用户名经 ``username()`` 校验。
        """
        path = PROJECT / 'config.json'
        doc = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
        game = Path(doc.get('game', str(DEFAULT_GAME)))
        if not game.is_absolute():
            game = PROJECT / game
        return cls(game.resolve(), username(doc.get('username', 'Player')))

    def validate(self):
        """启动前校验：必备文件齐全、EXE/恢复镜像/保护组件哈希一致。

        返回用于打印的校验摘要字典；任何一项不满足都抛 ``ValueError``，
        绝不让未知版本的客户端进入后续补丁流程。
        """
        required = ['codoMP_client_shipRetail.exe', 'IIPS/IFS2.dll', 'awesomium.dll',
                    'icudt.dll', 'bink2w32.dll', 'mss32.dll', 'D3DX9_43.dll',
                    'Microsoft.VC90.CRT', 'miles', 'localization.txt', 'splash.bmp',
                    'logo.bmp', 'datapacket.ini', 'iips.ini']
        missing = [name for name in required if not (self.game / name).exists()]
        if missing:
            raise ValueError('游戏文件缺失：' + ', '.join(missing))
        if digest(self.game / required[0]) != CLIENT_HASH:
            raise ValueError('客户端不是支持的 2.6.9.7 原始版本。')
        if digest(ASSETS / 'aplib-recovered/codol2697-static-candidate.bin') != RECOVERED_HASH:
            raise ValueError('恢复后的客户端文件校验失败。')
        # 保护组件的期望哈希记录在证据 JSON 中，EXE 与证据必须成对可信。
        guard = json.loads((ASSETS / 'server2697/evidence/client-loopback-guard.json').read_text())
        if digest(ASSETS / 'server2697/client_loopback_guard.exe') != guard['guard_sha256']:
            raise ValueError('运行保护组件校验失败。')
        if not any((self.game / 'IIPS/IIPSDownload').glob('*.ifs')):
            raise ValueError('缺少 IFS 资源包。')
        return {'version': '2.6.9.7', 'game': str(self.game), 'username': self.name,
                'client_sha256': CLIENT_HASH}
