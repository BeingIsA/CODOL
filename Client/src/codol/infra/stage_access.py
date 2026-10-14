# Version 2.6.9.7 research port; see docs/provenance.json.
"""基础设施层：为受限客户端进程创建独立运行目录并收紧 ACL。

运行目录（stage）必须是一个新建空目录，随后通过 icacls：
- 只保留当前用户的完全控制（撤销继承的宽泛权限）；
- 将目录及继承对象降为低完整性级别，与客户端进程令牌匹配。
"""
from pathlib import Path
import ctypes as C
from ctypes import wintypes as W
import subprocess
from .win32_api import K, A, PTR, api, check


def current_sid():
    """返回当前进程用户 SID 的字符串形式（S-1-5-…），用于 icacls 授权。"""
    token = PTR()
    size = W.DWORD()
    text = PTR()
    check(api(A, 'OpenProcessToken', W.BOOL, PTR, W.DWORD, C.POINTER(PTR))(api(K, 'GetCurrentProcess', PTR)(), 8, C.byref(token)))
    try:
        get = api(A, 'GetTokenInformation', W.BOOL, PTR, C.c_int, PTR, W.DWORD, C.POINTER(W.DWORD))
        get(token, 1, None, 0, C.byref(size))
        buf = C.create_string_buffer(size.value)
        check(get(token, 1, buf, size.value, C.byref(size)))
        sid = C.cast(buf, C.POINTER(PTR))[0]
        check(api(A, 'ConvertSidToStringSidW', W.BOOL, PTR, C.POINTER(PTR))(sid, C.byref(text)))
        return C.wstring_at(text)
    finally:
        api(K, 'CloseHandle', W.BOOL, PTR)(token)
        if text:
            api(K, 'LocalFree', PTR, PTR)(text)


def create_stage(path):
    """创建一个空运行目录并设置受限 ACL，返回该目录的 Path。

    目录必须尚不存在（mkdir 不带 exist_ok），创建后不得是符号链接/
    junction 且必须为空，防止把权限收紧到意外的目标上。
    """
    path = Path(path)
    path.mkdir()
    if path.is_symlink() or path.is_junction() or any(path.iterdir()):
        raise ValueError('运行目录必须是新建的空目录。')
    # '/grant:r' 先撤销继承权限再授予当前用户；(OI)(CI)F 让子对象继承完全控制；
    # '/setintegritylevel (OI)(CI)L' 整目录降为低完整性。
    for args in (['/grant:r', '*' + current_sid() + ':(OI)(CI)F'], ['/setintegritylevel', '(OI)(CI)L']):
        result = subprocess.run(['icacls', str(path), *args], capture_output=True, text=True, encoding='mbcs', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError('无法设置独立运行目录的访问权限：' + result.stdout + result.stderr)
    return path
