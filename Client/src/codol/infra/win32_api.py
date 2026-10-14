# Version 2.6.9.7 research port; see docs/provenance.json.
"""基础设施层：本项目使用的 Win32 API 绑定与进程令牌检查。

集中声明 ctypes 的函数签名与结构体，避免调用点散落各处重复设置。
所有 API 通过 ``api()`` 绑定型别后再调用，错误统一用 ``check()`` 转成异常。

注意：客户端是 32 位进程，宿主通过 WOW64 上下文接口访问它
（Wow64GetThreadContext 等，调用点在 v2697.session）。
"""
import ctypes as C
from ctypes import wintypes as W
import struct

# kernel32：进程/内存/调试事件；advapi32：令牌与 SID 相关。
K = C.WinDLL('kernel32', use_last_error=True)
A = C.WinDLL('advapi32', use_last_error=True)
PTR, SIZE = (C.c_void_p, C.c_size_t)


def api(lib, name, result, *args):
    """按返回值与参数型别绑定一个 Win32 函数，返回可直接调用的对象。"""
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = (result, args)
    return fn


class SI(C.Structure):
    """STARTUPINFOW：CreateProcessAsUserW 的启动参数。"""

    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR)] + [(n, W.DWORD) for n in ('x', 'y', 'cx', 'cy', 'xchars', 'ychars', 'fill', 'flags')] + [('show', W.WORD), ('reservedsize', W.WORD), ('reservedbytes', PTR), ('stdin', PTR), ('stdout', PTR), ('stderr', PTR)]


class PI(C.Structure):
    """PROCESS_INFORMATION：新进程与主线程句柄及 ID。"""

    _fields_ = [('process', PTR), ('thread', PTR), ('pid', W.DWORD), ('tid', W.DWORD)]


def check(ok):
    """断言 Win32 调用成功；失败时抛出携带 GetLastError 的异常。"""
    if not ok:
        raise C.WinError(C.get_last_error())


def token_info(process):
    """读取进程令牌的隔离特征：AppContainer/提权/能力组/完整性级别。

    返回字典供结果记录与断言使用——例如校验受限客户端进程确实运行在
    低完整性（4096 = SECURITY_MANDATORY_LOW_RID）且未提权。
    """
    token = PTR()
    check(api(A, 'OpenProcessToken', W.BOOL, PTR, W.DWORD, C.POINTER(PTR))(process, 8, C.byref(token)))
    result = {}
    get = api(A, 'GetTokenInformation', W.BOOL, PTR, C.c_int, PTR, W.DWORD, C.POINTER(W.DWORD))
    try:
        # (名称, TokenInformationClass)：29=IsAppContainer, 20=Elevation,
        # 30=Capabilities, 25=IntegrityLevel。
        for name, kind in [('is_appcontainer', 29), ('is_elevated', 20), ('capabilities', 30), ('integrity', 25)]:
            needed = W.DWORD()
            get(token, kind, None, 0, C.byref(needed))
            buf = C.create_string_buffer(needed.value)
            check(get(token, kind, buf, len(buf), C.byref(needed)))
            if name == 'integrity':
                # 完整性级别取 SID 最后一个子权威值（2048/4096/8192…）。
                sid = C.cast(buf, C.POINTER(PTR))[0]
                count = api(A, 'GetSidSubAuthorityCount', C.POINTER(C.c_ubyte), PTR)(sid)[0]
                result[name] = api(A, 'GetSidSubAuthority', C.POINTER(W.DWORD), PTR, W.DWORD)(sid, count - 1)[0]
            else:
                result[name] = struct.unpack_from('<I', buf)[0]
    finally:
        api(K, 'CloseHandle', W.BOOL, PTR)(token)
    return result


class SHELLINFO(C.Structure):
    """SHELLEXECUTEINFOW：通过 UAC 提升启动运行保护组件时使用。"""

    _fields_ = [('size', W.DWORD), ('mask', W.ULONG), ('hwnd', PTR), ('verb', W.LPCWSTR), ('file', W.LPCWSTR), ('parameters', W.LPCWSTR), ('directory', W.LPCWSTR), ('show', C.c_int), ('instance', PTR), ('idlist', PTR), ('cls', W.LPCWSTR), ('classkey', PTR), ('hotkey', W.DWORD), ('icon', PTR), ('process', PTR)]
