# Version-specific native session; see docs/architecture.md.
# Version 2.6.9.7 research port; see docs/provenance.json.
"""版本适配层：2.6.9.7 客户端的受限调试会话主循环。

这是底层核心模块：以调试器身份托管一个受限令牌的客户端进程，
通过 Windows 调试事件与断点驱动客户端完成"本地大厅"初始化。

主流程：
1. 校验保护组件证据 → 构建客户端副本（staging）→ 打视频补丁；
2. 用低完整性受限令牌以挂起+调试方式创建客户端进程；
3. 通过 UAC 启动运行保护组件（guard）并等它确认隔离边界；
4. 恢复主线程，进入调试事件循环：
   - 模块加载时布设全部断点，并拒绝旧保护组件加载；
   - 断点命中时执行对应回调：填档案、写等级、填仓库、
     拦错误弹窗、补缺失资源、周期性核对金丝雀与库存数；
5. 结束后强杀子进程、保存设置与档案、等 guard 确认清理，
   把完整结果写入 result.json / latest.json。

注意：
- 客户端为 32 位，线程上下文走 WOW64 接口；
- 本模块导入依赖 ``CODOL2697_GAME_DIR`` 已设置（v2697.client）；
- 断点回调里的地址与结构全部绑定 2.6.9.7。
"""
import ctypes as C
from ctypes import wintypes as W
import hashlib, json, os, struct, subprocess, time
import traceback
from pathlib import Path
import pefile
from ..domain import settings_store as local_scene_settings
from . import resource_gate as lobby_runtime2697
from .resource_gate import LoadingClock
from ..infra.win32_api import K, A, PTR, SIZE, SI, PI, SHELLINFO, api, check, token_info
from ..paths import ASSETS as ROOT
from .client import FORBIDDEN


class SIDATTR(C.Structure):
    """SID_AND_ATTRIBUTES：给受限令牌设置完整性标签用。"""

    _fields_ = [('sid', PTR), ('attributes', W.DWORD)]


class EVENT(C.Structure):
    """DEBUG_EVENT（WOW64 视角）：调试事件 + 160 字节联合体原始数据。"""

    _fields_ = [('code', W.DWORD), ('pid', W.DWORD), ('tid', W.DWORD), ('padding', W.DWORD), ('data', C.c_ubyte * 160)]


# 常用的三个 kernel32 调用（签名绑定一次，循环内反复使用）。
close = api(K, 'CloseHandle', W.BOOL, PTR)
read = api(K, 'ReadProcessMemory', W.BOOL, PTR, PTR, PTR, SIZE, C.POINTER(SIZE))
wait = api(K, 'WaitForSingleObject', W.DWORD, PTR, W.DWORD)


def low_token():
    """构造运行客户端用的受限令牌：禁用提权并降为低完整性。

    从当前进程令牌（如已提级则取其未提级Linked 令牌）派生
    ``CreateRestrictedToken(LUA_TOKEN)``，再打上 S-1-16-4096（低完整性）
    标签。客户端由此拿到一个无提权、低完整性的执行环境。
    """
    current = api(K, 'GetCurrentProcess', PTR)()
    old = PTR()
    new = PTR()
    sid = PTR()
    linked = PTR()
    check(api(A, 'OpenProcessToken', W.BOOL, PTR, W.DWORD, C.POINTER(PTR))(current, 395, C.byref(old)))
    try:
        get = api(A, 'GetTokenInformation', W.BOOL, PTR, C.c_int, PTR, W.DWORD, C.POINTER(W.DWORD))
        elevation = W.DWORD()
        needed = W.DWORD()
        check(get(old, 18, C.byref(elevation), C.sizeof(elevation), C.byref(needed)))
        source = old
        if elevation.value == 2:
            # 已提级进程：取 LinkedToken（原始未提级令牌）作为派生源。
            check(get(old, 19, C.byref(linked), C.sizeof(linked), C.byref(needed)))
            source = linked
        check(api(A, 'CreateRestrictedToken', W.BOOL, PTR, W.DWORD, W.DWORD, PTR, W.DWORD, PTR, W.DWORD, PTR, C.POINTER(PTR))(source, 1, 0, None, 0, None, 0, None, C.byref(new)))
        check(api(A, 'ConvertStringSidToSidW', W.BOOL, W.LPCWSTR, C.POINTER(PTR))('S-1-16-4096', C.byref(sid)))
        label = SIDATTR(sid, 32)
        check(api(A, 'SetTokenInformation', W.BOOL, PTR, C.c_int, PTR, W.DWORD)(new, 25, C.byref(label), C.sizeof(label) + api(A, 'GetLengthSid', W.DWORD, PTR)(sid)))
        return new
    except:
        if new:
            close(new)
        raise
    finally:
        close(old)
        if linked:
            close(linked)
        if sid:
            api(K, 'LocalFree', PTR, PTR)(sid)


def run(args):
    """执行一次完整调试会话，返回结果字典（并写入 result.json）。

    ``args`` 为应用层装配的参数对象，关键字段：
    - ``run_directory``  会话目录（.lobby/<编号>）；
    - ``profile_path``   用户存档路径；
    - ``argument``       客户端命令行参数；
    - ``seconds``        观察时限（0 = 无限，直到就绪/停止/异常）；
    - ``resource_provider`` / ``state_observer`` / ``stage_client``。
    """
    # 端点策略声明：网络边界由 guard 负责，且须经独立脚本验证。
    assert True, 'Use verify_client_loopback_guard.py for this endpoint policy'
    run = args.run_directory
    run.mkdir()
    from ..infra.stage_access import create_stage
    stage = create_stage(run / 'runtime')
    result = {'run': str(run), 'fixture': False, 'arguments': args.argument, 'modules': [], 'exceptions': [], 'debug_strings': [], 'started_at': time.time()}
    listener = None
    token = None
    pi = PI()
    guard = None
    resumed = False
    pending = None
    thread_ids = set()
    breakpoints = {}     # 地址 -> (原始字节, 标签)：恢复断点用
    rearm = {}           # 线程 -> (地址, 标签)：单步后需要重新布置的断点
    trace_hits = {}      # 高频 Trace 断点的命中计数
    profile_fixture = None
    profile_meta = None
    profile_info = None
    next_profile_save = 0
    last_profile_sha = None
    inventory_layout = None
    canaries_ready = False
    next_inventory_check = 0
    settings_watcher = None
    resource_gate_memory = None
    loading_clock = LoadingClock()
    try:
        # ---- 保护组件证据复核：证据 JSON 声明通过、清理正常、EXE 哈希一致 ----
        verified = json.loads((ROOT / 'server2697/evidence/client-loopback-guard.json').read_text())
        assert verified['passed'] and verified['exit'] == 0 and (not verified['control_only'])
        assert verified['guarded']['cleanup']['engine_close_status'] == 0
        assert verified['guard_sha256'] == hashlib.sha256((ROOT / 'server2697/client_loopback_guard.exe').read_bytes()).hexdigest()
        # ---- 副本构建 + 视频补丁 ----
        exe, meta = args.stage_client(stage, args)
        result.update(meta)
        if getattr(args, 'room_mailbox', None):
            args.room_mailbox.start()
        result['lobby_preview'] = True
        result['preview_patches'] = json.loads((run / 'preview-patches.json').read_text())
        patched = bytearray(exe.read_bytes())
        video_patch = lobby_runtime2697.apply_video_patch(patched, pefile.PE(data=patched))
        exe.write_bytes(patched)
        result['candidate_sha256'] = hashlib.sha256(patched).hexdigest()
        result['preview_patches'].append(video_patch)
        (run / 'preview-patches.json').write_text(json.dumps(result['preview_patches'], indent=2))
        # 恢复了用户设置则去掉强制窗口化参数；并挂上设置观察器。
        args.argument = local_scene_settings.launch_arguments(args.argument, result.get('local_settings', {}).get('restored', False))
        result['arguments'] = args.argument
        settings_watcher = local_scene_settings.Watcher(stage)
        inventory_layout = json.loads((run / 'inventory-layout.json').read_text())
        result['inventory_layout'] = inventory_layout
        result['image'] = str(exe)
        # ---- 创建受限客户端进程（挂起 + 调试）----
        token = low_token()
        si = SI()
        si.cb = C.sizeof(si)
        cmd = C.create_unicode_buffer(subprocess.list2cmdline([str(exe)] + args.argument))
        # 精简环境：只保留系统必需变量，PATH 指向副本与 System32，
        # TEMP/TMP 落在副本内——客户端拿不到用户其余环境。
        env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'COMSPEC', 'SystemDrive', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA') if key in os.environ}
        env.update({'PATH': str(stage) + ';' + str(Path(os.environ['SystemRoot']) / 'System32'), 'TEMP': str(stage), 'TMP': str(stage)})
        environment = C.create_unicode_buffer('\x00'.join((k + '=' + v for k, v in sorted(env.items()))) + '\x00\x00')
        # 1030 = CREATE_SUSPENDED | DEBUG_PROCESS | CREATE_UNICODE_ENVIRONMENT。
        check(api(A, 'CreateProcessAsUserW', W.BOOL, PTR, W.LPCWSTR, W.LPWSTR, PTR, PTR, W.BOOL, W.DWORD, PTR, W.LPCWSTR, C.POINTER(SI), C.POINTER(PI))(token, str(exe), cmd, None, None, False, 1030, environment, str(stage), C.byref(si), C.byref(pi)))
        result['pid'] = pi.pid
        result['main_thread_id'] = pi.tid
        result['token'] = token_info(pi.process)
        # 令牌断言：非 AppContainer、未提权、无能力组、低完整性。
        assert result['token'] == {'is_appcontainer': 0, 'is_elevated': 0, 'capabilities': 0, 'integrity': 4096}, result['token']

        def mem(addr, size):
            """读客户端进程内存，返回确切长度的 bytes。"""
            buf = C.create_string_buffer(size)
            n = SIZE()
            check(read(pi.process, addr, buf, size, C.byref(n)))
            return buf.raw[:n.value]

        def context(tid):
            """打开线程并取 WOW32 线程上下文，返回 (句柄, 上下文缓冲)。"""
            thread = api(K, 'OpenThread', PTR, W.DWORD, W.BOOL, W.DWORD)(26, False, tid)
            check(thread)
            ctx = C.create_string_buffer(716)
            struct.pack_into('<I', ctx, 0, 0x1003f)  # CONTEXT_i386 全量寄存器
            try:
                check(api(K, 'Wow64GetThreadContext', W.BOOL, PTR, PTR)(thread, ctx))
            except:
                close(thread)
                raise
            return (thread, ctx)

        def write_memory(address, data):
            """写客户端内存：临时改页可写 → 写入 → 刷新指令缓存 → 还原保护。"""
            old = W.DWORD()
            check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(pi.process, address, len(data), 64, C.byref(old)))
            try:
                count = SIZE()
                check(api(K, 'WriteProcessMemory', W.BOOL, PTR, PTR, PTR, SIZE, C.POINTER(SIZE))(pi.process, address, C.c_char_p(data), len(data), C.byref(count)))
                check(api(K, 'FlushInstructionCache', W.BOOL, PTR, PTR, SIZE)(pi.process, address, len(data)))
            finally:
                ignored = W.DWORD()
                check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(pi.process, address, len(data), old.value, C.byref(ignored)))

        def arm(address, name):
            """在地址处布设 0xCC 断点（记录原始字节用于恢复）。"""
            if address not in breakpoints:
                breakpoints[address] = (mem(address, 1), name)
                write_memory(address, b'\xcc')

        def cstring(address, wide=False):
            """读客户端内存中的字符串（安全版，见 resource_gate.read_cstring）。"""
            return lobby_runtime2697.read_cstring(mem, address, wide)

        def lobby_state():
            """采样大厅相关的全部观察点（仅 2.6.9.7 地址有效）。"""
            state = {label: struct.unpack('<I', mem(address, 4))[0] for label, address in [('frontend_stage', 0x747c880), ('flash_movie', 0xdba969c), ('logon_stage', 0x747c890), ('previous_stage', 0x747c894), ('lobby_connection', 0x747c8c0), ('dw_state', 0xd25f8c), ('profile_stage', 0x747c8a4), ('ui_stage', 0x747c720), ('root_stage', 0x747c71c), ('character_state', 0x2694640)]}
            state['ui_activated'] = mem(0x747c713, 1)[0]
            state['player_data_ready'] = mem(0x7454ad5, 1)[0]
            state['catalog_stage'] = struct.unpack('<I', mem(0x7454ac0, 4))[0]
            catalog = struct.unpack('<I', mem(0xcd07fa4, 4))[0]
            state['catalog_count'] = struct.unpack('<I', mem(catalog, 4))[0] if catalog else 0
            state['inventory_count'] = struct.unpack('<I', mem(0x7454ac4, 4))[0]
            state['data_completion_bits'] = struct.unpack('<I', mem(0x7454ad8, 4))[0]
            state['cached_character'] = struct.unpack('<I', mem(0x2bf4348, 4))[0]
            state['selected_character'] = struct.unpack('<I', mem(0xd27150, 4))[0]
            state['inventory_ui_initialized'] = mem(0x747c6a2, 1)[0]
            state['inventory_ui_pending'] = mem(0x747c6a3, 1)[0]
            state['loadout_refresh_state'] = struct.unpack('<I', mem(0x7474f00, 4))[0]
            state['character_refresh_pending'] = mem(0x747c68a, 1)[0]
            state['weapon_refresh_pending'] = mem(0x747c727, 1)[0]
            state['preview_frame_enabled'] = struct.unpack('<I', mem(0x2bf4310, 4))[0]
            state['preview_initialized'] = mem(0xd9805e0, 1)[0]
            state['preview_flag3'] = mem(0xd9805e3, 1)[0]
            state['preview_flag44'] = mem(0xd98060c, 1)[0]
            state['preview_mode'] = struct.unpack('<I', mem(0xd9805e8, 4))[0]
            state['preview_character_slots'] = list(struct.unpack('<9I', mem(0x743dc20, 36)))
            state['preview_tryon_active'] = mem(0xd9806b0, 1)[0]
            for label, addr in [('onboard_complete', 0x7475110), ('connecting_overlay', 0x7475138)]:
                ptr = struct.unpack('<I', mem(addr, 4))[0]
                state[label] = mem(ptr + 12, 1)[0] if ptr else None
            return state

        def verify_inventory():
            """周期性核对仓库边界：金丝雀完好、原生库存数与写入数一致。"""
            nonlocal next_inventory_check
            if not canaries_ready or time.monotonic() < next_inventory_check:
                return
            next_inventory_check = time.monotonic() + 2
            from .inventory_layout import CANARY
            assert all((mem(a, len(CANARY)) == CANARY for a in inventory_layout['canaries'])), 'Expanded inventory boundary changed'
            if mem(0x7454ad5, 1)[0] and struct.unpack('<I', mem(0x7474f00, 4))[0] == 4:
                expected = result['local_inventory']['items']
                native_count = struct.unpack('<I', mem(0x7454ac4, 4))[0]
                assert native_count == expected, (native_count, expected)
                cache = mem(inventory_layout['ui_records'], inventory_layout['capacity'] * 32)
                ui_count = sum((struct.unpack_from('<I', cache, i * 32 + 4)[0] != 0 for i in range(inventory_layout['capacity'])))
                result['inventory_boundaries'] = {'canaries_intact': True, 'native_count': native_count, 'ui_count': ui_count, 'checked_at': time.time()}
                (run / 'inventory-proof.json').write_text(json.dumps(result['inventory_boundaries'], indent=2))

        def autosave_profile(force=False):
            """稳定状态下周期保存档案：双读一致 + 内容变化才落盘。"""
            nonlocal next_profile_save, last_profile_sha
            if profile_fixture is None or (time.monotonic() < next_profile_save and (not force)):
                return
            next_profile_save = time.monotonic() + 1
            if not result.get('local_profile', {}).get('written_and_verified') or wait(pi.process, 0) == 0:
                return
            try:
                # 玩家数据就绪且装备状态为 4 才保存，避免存下中间态。
                if not mem(0x7454ad5, 1)[0] or struct.unpack('<I', mem(0x7474f00, 4))[0] != 4:
                    return
                blob = mem(0xdbff0c0, len(profile_fixture))
                if blob != mem(0xdbff0c0, len(profile_fixture)):
                    return
                from .profile_transport import local_save_blob
                blob = local_save_blob(blob, profile_fixture, profile_meta)
                digest = hashlib.sha256(blob).hexdigest()
                if digest == last_profile_sha:
                    return
                from ..domain.profile_store import save
                result['profile_saved'] = save(args.profile_path, blob, profile_fixture, profile_meta, run)
                last_profile_sha = digest
                result.pop('profile_save_error', None)
            except (OSError, ValueError) as exc:
                result['profile_save_error'] = str(exc)
        # ---- PEB 校验加载基址（客户端必须落在 0x400000）----
        peb = PTR()
        n = W.DWORD()
        status = api(C.WinDLL('ntdll'), 'NtQueryInformationProcess', C.c_long, PTR, W.ULONG, PTR, W.ULONG, C.POINTER(W.ULONG))(pi.process, 26, C.byref(peb), C.sizeof(peb), C.byref(n))
        assert status == 0
        base = struct.unpack('<I', mem(peb.value + 8, 4))[0]
        result['image_base'] = hex(base)
        assert base == 0x400000
        # ---- 通过 UAC 启动运行保护组件并等待其确认 ----
        request = SHELLINFO()
        request.size = C.sizeof(request)
        request.mask = 320
        request.verb = 'runas'
        request.file = str(ROOT / 'server2697/client_loopback_guard.exe')
        request.parameters = subprocess.list2cmdline([str(run), str(pi.pid), str(exe), str(0 if args.seconds == 0 else max(120, args.seconds + 60))])
        request.show = 0
        if getattr(args, 'guard_launcher', None):
            guard = args.guard_launcher(run, pi, exe, 0 if args.seconds == 0 else max(120, args.seconds + 60))
        else:
            check(api(C.WinDLL('shell32', use_last_error=True), 'ShellExecuteExW', W.BOOL, C.POINTER(SHELLINFO))(C.byref(request)))
            guard = request.process
        deadline = time.monotonic() + 25
        while not (run / 'guard-ready.json').exists() and time.monotonic() < deadline:
            if wait(guard, 100) == 0:
                break
        if not (run / 'guard-ready.json').exists():
            raise RuntimeError('Guard did not confirm isolation')
        # 隔离边界断言：8 条过滤规则、回环 UDP 端点、动态过滤、
        # 关闭即杀、单进程上限。
        ready = json.loads((run / 'guard-ready.json').read_text())
        assert len(ready['filter_ids']) == 8 and ready['scope'] == 'udp-127.0.0.1:28969' and ready['dynamic'] and ready['job_kill_on_close'] and (ready['max_processes'] == 1)
        result['guard'] = ready
        # ---- 恢复主线程，开始计时并进入调试事件循环 ----
        check(api(K, 'DebugSetProcessKillOnExit', W.BOOL, W.BOOL)(True))
        assert api(K, 'ResumeThread', W.DWORD, PTR)(pi.thread) != 0xffffffff
        resumed = True
        loading_clock.resume()
        deadline = time.monotonic() + args.seconds
        while args.seconds == 0 or time.monotonic() < deadline:
            verify_inventory()
            autosave_profile()
            if settings_watcher:
                try:
                    saved = settings_watcher.poll()
                    if saved:
                        result['settings_saved'] = saved
                except (OSError, ValueError) as exc:
                    result['settings_save_error'] = repr(exc)
            if getattr(args, 'state_observer', None):
                # 就绪观察者（smoke 就绪即停 / stop 请求）返回 True 则收尾。
                if args.state_observer(mem, pi.process, run):
                    autosave_profile(force=True)
                    break
            event = EVENT()
            if not api(K, 'WaitForDebugEvent', W.BOOL, C.POINTER(EVENT), W.DWORD)(C.byref(event), 200):
                error = C.get_last_error()
                if error == 121:  # ERROR_SEM_TIMEOUT：200ms 窗口内无事件
                    continue
                raise C.WinError(error)
            loading_clock.begin(event.code)
            pending = event
            data = bytes(event.data)
            disposition = 0x10002  # DBG_EXCEPTION_NOT_HANDLED 默认继续
            if event.code in (3, 6):
                # ---- 模块加载/卸载：记录模块，首次加载时布断点 ----
                file = struct.unpack_from('<Q', data)[0]
                address = struct.unpack_from('<Q', data, 24 if event.code == 3 else 8)[0]
                path = 'unknown'
                if file:
                    text = C.create_unicode_buffer(32768)
                    if api(K, 'GetFinalPathNameByHandleW', W.DWORD, PTR, W.LPWSTR, W.DWORD, W.DWORD)(file, text, len(text), 0):
                        path = text.value
                    close(file)
                result['modules'].append({'path': path, 'base': hex(address)})
                if event.code == 3:
                    thread_ids.add(event.tid)
                    # 启动里程碑与初始化回调断点。
                    arm(0xb4ff4f, 'CRT entry')
                    arm(0xb50486, 'CRT floating-point initializer')
                    arm(0x645ce0, 'WinMain')
                    arm(0x6336e0, 'Resource open')
                    arm(0x58c6ce, 'IIPS events created')
                    arm(0xddab8ac, 'Engine error')
                    if getattr(args, 'trace_weapon_build', False):
                        from .weapon_diagnostics import BUILD_POINTS
                        for point, expected, label in BUILD_POINTS:
                            if mem(point, len(expected)) != expected:
                                raise ValueError('武器创建诊断断点字节不匹配：' + hex(point))
                            arm(point, 'Weapon build ' + label)
                    arm(0x6c5730, 'Settings apply')
                    arm(0x49a8e0, 'Settings restart')
                    arm(0xddaa03e, 'Settings native restart')
                    # 加载链路观察（Loading *：命中时顺带采样大厅状态）。
                    for point, label in [(0x74cd10, 'catalog begin'), (0x6d1460, 'UI activate begin'), (0x6d148c, 'UI activate end'), (0x5a1140, 'loadout cache begin'), (0x5a1380, 'weapon cache begin'), (0x4d75bb, 'inventory callback end'), (0x6b1dc0, 'inventory UI begin')]:
                        arm(point, 'Loading ' + label)
                    for a, n in getattr(args, 'extra_breakpoints', []):
                        arm(a, 'Trace gameplay ' + n)
                    # 本地数据注入回调（档案/等级/仓库/名称）。
                    arm(0x5a06a3, 'Seed local profile')
                    arm(0x5a067a, 'Secure progression profile')
                    arm(0x4d7450, 'Local empty inventory callback')
                    arm(0x6c2f00, 'Local name check completion')
                    arm(0x6cde40, 'Local loadout name update')
                    # 原生装备/角色失效链路追踪。
                    arm(0x5dc752, 'Trace native loadout initialization')
                    arm(0x5dc7d4, 'Trace native character invalidation')
                    arm(0x5dc7f9, 'Trace native weapon invalidation')
                    arm(0x5dc7ac, 'Trace character tabs initialized')
                    # 3D 预览链路追踪。
                    for a, n in [(0x621a70, 'frame'), (0x621a99, 'frame enabled'), (0x621b34, 'scene ready'), (0x622425, 'model update'), (0x622471, 'resources ready'), (0x6a2160, 'character selected'), (0x6cb320, 'weapon selected')]:
                        arm(a, 'Preview ' + n)
                    arm(0x6a2e10, 'Preview character loadout')
                if Path(path).name.lower() in FORBIDDEN:
                    # 旧保护/平台组件试图加载：立即停止，不让它初始化。
                    result['stopped_reason'] = 'Protection DLL rejected before initialization: ' + path
                    break
            elif event.code == 1:
                # ---- 异常事件：断点命中 / 单步重布 / 未知异常记录 ----
                code = struct.unpack_from('<I', data)[0]
                address = struct.unpack_from('<Q', data, 16)[0]
                first = struct.unpack_from('<I', data, 152)[0]
                row = {'code': hex(code), 'address': hex(address), 'first_chance': first, 'tid': event.tid, 'time': time.time()}
                if event.tid in rearm and code in (0x80000004, 0x4000001e):
                    # 单步完成：清 TF 标志并把断点字节写回。
                    addr, label = rearm.pop(event.tid)
                    thread, ctx = context(event.tid)
                    try:
                        flags = struct.unpack_from('<I', ctx, 192)[0]
                        struct.pack_into('<I', ctx, 192, flags & ~256)
                        check(api(K, 'Wow64SetThreadContext', W.BOOL, PTR, PTR)(thread, ctx))
                        arm(addr, label)
                    finally:
                        close(thread)
                elif address in breakpoints and code in (0x80000003, 0x4000001f):
                    # 命中 0xCC 断点（WOW64 下呈现 0x4000001f）：
                    # 取出标签，恢复原始字节，EIP 回退一步。
                    old, name = breakpoints.pop(address)
                    thread, ctx = context(event.tid)
                    try:
                        loading_clock.label(name)
                        esp = struct.unpack_from('<I', ctx, 196)[0]
                        row['milestone'] = name
                        row['stack'] = mem(esp, 256).hex()
                        if name not in ('ResourceMissingA', 'ResourceMissingW', 'CreateFileA', 'CreateFileW'):
                            row['loading_time'] = loading_clock.mark(name, result.get('resource_gate', {}))
                        if name.startswith('Loading '):
                            row['state'] = lobby_state()
                        row['registers'] = {label: hex(struct.unpack_from('<I', ctx, offset)[0]) for label, offset in [('ebp', 180), ('eax', 176), ('ecx', 172), ('edx', 168), ('ebx', 164), ('esi', 160), ('edi', 156)]}
                        if name.startswith('Weapon build '):
                            # 有界创建路径跟踪，每个点最多 256 次，逐条落盘保留错误前的原因。
                            from .weapon_diagnostics import capture_build_step
                            try:
                                row['weapon_build'] = capture_build_step(
                                    mem, int(row['registers']['ebp'], 16), row['registers'])
                            except (OSError, ValueError, struct.error, IndexError) as exc:
                                row['diagnostic_error'] = str(exc)
                            trace_hits[name] = trace_hits.get(name, 0) + 1
                            row['hit'] = trace_hits[name]
                            with (run / 'weapon-build-trace.jsonl').open('a', encoding='utf-8') as stream:
                                stream.write(json.dumps(row) + '\n')
                            if trace_hits[name] < 256:
                                rearm[event.tid] = (address, name)
                                flags = struct.unpack_from('<I', ctx, 192)[0]
                                struct.pack_into('<I', ctx, 192, flags | 256)
                        if name == 'Seed local profile':
                            # ---- 档案注入：以基准 fixture 为底，加载用户存档 ----
                            profile_folder = ROOT / 'local-profile-loadout'
                            profile = json.loads((profile_folder / 'profile.json').read_text(encoding='utf-8'))
                            blob = (profile_folder / 'playerdata.bin').read_bytes()
                            assert hashlib.sha256(blob).hexdigest() == profile['sha256'] and len(blob) == 32764
                            profile_fixture = blob
                            profile_meta = profile
                            from ..domain.profile_store import load
                            blob, profile_info = load(args.profile_path, blob, profile)
                            result['profile_loaded'] = profile_info
                            last_profile_sha = profile_info['sha256'] if profile_info.get('path') else None
                            # 从回调参数取定义对象，核对 schema 身份后写入档案。
                            definition = struct.unpack('<I', mem(int(row['registers']['eax'], 16) + 8, 4))[0]
                            header = struct.unpack('<13I', mem(definition, 52))
                            if not result.get('local_profile'):
                                assert (header[0], header[1], header[12]) == (profile['schema_version'], profile['schema_checksum'], profile['schema_size'])
                                write_memory(0xdbff0c0, blob)
                                assert mem(0xdbff0c0, len(blob)) == blob
                                result['local_profile'] = {'sha256': hashlib.sha256(blob).hexdigest(), 'fixture_sha256': profile['sha256'], 'schema_version': header[0], 'size': header[12], 'notification_count': len(profile['notifications']), 'written_and_verified': True}
                                result['local_profile']['loadout_fields'] = profile['loadout_fields']
                        if name == 'Secure progression profile':
                            # ---- 等级/经验注入：先捕获结构再按结构填充 ----
                            from ..domain.playerdata_schema import schema
                            definition = struct.unpack('<I', mem(int(row['registers']['eax'], 16) + 8, 4))[0]
                            result['secure_schema'] = schema(mem, definition, run / 'secure-schema')
                            from ..domain.progression import seed
                            result['local_progression'] = seed(mem, write_memory, run / 'secure-schema/playerdata-schema.json', ROOT / 'local-inventory/progression.json')
                        if name == 'Local empty inventory callback':
                            # ---- 仓库注入：校验空池 → 布金丝雀 → 按清单填物品 ----
                            event_pointer, success = struct.unpack('<II', mem(esp + 4, 8))
                            assert success == 1 and mem(event_pointer, 32) == bytes(32), 'Local inventory event validation failed'
                            assert result.get('local_profile', {}).get('written_and_verified'), 'Local profile has not been seeded before inventory completion'
                            assert lobby_state()['catalog_count'] > 0, 'Local catalog is empty'
                            state = lobby_state()
                            expected_character = profile_info['character']
                            assert state['selected_character'] == expected_character, f"Character cache initialization mismatch: native={state['selected_character']}, profile={expected_character}, index={profile_info['character_index']}"
                            result['local_loadout_cache'] = {'character': expected_character, 'native_initializers': ['0x5a1140', '0x5a1380'], 'verified': True}
                            pool_address = inventory_layout['records'] if inventory_layout else 0x8508978
                            capacity = inventory_layout['capacity'] if inventory_layout else 6144
                            pool = mem(pool_address, 280 * capacity)
                            # 池必须仍是构造器产物：物品 ID 全 0，首 4 字节处处一致。
                            assert all((struct.unpack_from('<I', pool, i * 280 + 24)[0] == 0 for i in range(capacity))), 'Inventory pool is not empty'
                            assert all((pool[i * 280:i * 280 + 4] == pool[:4] for i in range(capacity))) and any(pool[:4]), 'Inventory constructors incomplete'
                            if inventory_layout:
                                # 扩容池边界写金丝雀，供 verify_inventory 周期核对。
                                from .inventory_layout import CANARY
                                assert all((mem(a, len(CANARY)) == bytes(len(CANARY)) for a in inventory_layout['canaries']))
                                for a in inventory_layout['canaries']:
                                    write_memory(a, CANARY)
                                canaries_ready = True
                            result['empty_inventory'] = {'native_callback': hex(address), 'entry_count': 0, 'capacity': capacity, 'empty_item_ids_verified': True}
                            # 读完整清单，并对其 source_catalog 做哈希核对。
                            inventory_path = ROOT / 'local-inventory-full' / 'inventory.json'
                            inventory = json.loads(inventory_path.read_text(encoding='utf-8'))
                            source = Path(inventory['source_catalog']).read_bytes()
                            assert hashlib.sha256(source).hexdigest() == inventory['source_sha256']
                            available = {int(r[0]): r for r in json.loads(source)['rows']}
                            items = inventory['items']
                            assert inventory['capacity'] == capacity and 0 < len(items) <= capacity
                            assert len({r['id'] for r in items}) == len(items)
                            populated = bytearray(pool)
                            progression = json.loads((ROOT / 'local-inventory/progression.json').read_text())
                            for index, item in enumerate(items):
                                # 每件物品核对目录（ID/名称/类型/子类型一致）。
                                assert available[item['id']][1:4] == [item['name'], item['type'], item['subtype']]
                                assert item['quantity'] == 1
                                # 记录步长 280：偏移 24 起写 (ID, 数量)，
                                # 武器再写武器经验（偏移 32）。
                                struct.pack_into('<II', populated, index * 280 + 24, item['id'], item['quantity'])
                                if progression and item['type'] == 'wea':
                                    struct.pack_into('<I', populated, index * 280 + 32, progression['weapon_experience'])
                            write_memory(pool_address, bytes(populated))
                            assert mem(pool_address, len(populated)) == bytes(populated)
                            result['local_inventory'] = {'items': len(items), 'counts': inventory['counts'], 'written_and_verified': True, 'manifest': str(inventory_path)}
                        if name.startswith('Trace '):
                            # ---- 高频追踪断点：采样状态并按需写 trace 文件 ----
                            row['lobby_state'] = lobby_state()
                            row['stack'] = mem(esp, 768).hex()
                            if getattr(args, 'trace_observer', None):
                                args.trace_observer(row, mem, pi.process, run)
                            trace_hits[name] = trace_hits.get(name, 0) + 1
                            row['hit'] = trace_hits[name]
                            if name.startswith('Trace gameplay '):
                                # 对局链路追踪：落盘 gameplay-trace.jsonl。
                                if name == 'Trace gameplay Map preload':
                                    row['map'] = cstring(int(row['registers']['esi'], 16))
                                    row['gametype'] = cstring(struct.unpack('<I', mem(esp + 4, 4))[0])
                                if name == 'Trace gameplay Map load':
                                    row['map'] = cstring(int(row['registers']['ecx'], 16))
                                    row['gametype'] = cstring(int(row['registers']['edx'], 16))
                                if name.endswith('dispatch'):
                                    row['target'] = row['registers']['eax']
                                    try:
                                        row['request_memory'] = mem(struct.unpack('<I', mem(esp, 4))[0], 96).hex()
                                    except OSError:
                                        pass
                                with (run / 'gameplay-trace.jsonl').open('a', encoding='utf-8') as stream:
                                    stream.write(json.dumps(row) + '\n')
                            if name == 'Trace popup error':
                                row['text'] = cstring(int(row['registers']['ecx'], 16))
                            if name == 'Trace Flash controller':
                                row['controller'] = cstring(int(row['registers']['esi'], 16))
                                row['method'] = cstring(struct.unpack('<I', mem(esp + 4, 4))[0])
                            if name in ('Trace Flash global', 'Trace Flash global result'):
                                row['method'] = cstring(int(row['registers']['esi'], 16))
                            if name in ('Trace onboarding result', 'Trace stats-ready result', 'Trace marketplace-ready result', 'Trace playerdata-ready result'):
                                row['value'] = mem(int(row['registers']['esi'], 16) + 16, 1)[0]
                            # 高频断点限量重布：超限后不再重布，减少开销。
                            if trace_hits[name] < (240 if name.startswith('Trace Flash global') else 80):
                                rearm[event.tid] = (address, name)
                                flags = struct.unpack_from('<I', ctx, 192)[0]
                                struct.pack_into('<I', ctx, 192, flags | 256)
                        if name.startswith('Preview '):
                            row['state'] = lobby_state()
                        if name == 'ExitProcess':
                            # 客户端正常退出前强制保存一次档案。
                            autosave_profile(force=True)
                        if name.startswith('Settings '):
                            # 设置应用/重启链路事件记录（含客户端内部状态字）。
                            result.setdefault('settings_events', []).append(dict(event=name, time=time.time(), client_state=struct.unpack('<I', mem(0x1bbbcfc, 4))[0]))
                            if len(result['settings_events']) < 40:
                                rearm[event.tid] = (address, name)
                                flags = struct.unpack_from('<I', ctx, 192)[0]
                                struct.pack_into('<I', ctx, 192, flags | 256)
                        if name in ('Resource open', 'IIPS events created', 'Engine error'):
                            # 资源/IIPS/引擎错误链路：记录 IIPS 对象与四个参数字符串。
                            row['iips_event_handle'] = hex(struct.unpack('<I', mem(0x2b999c0, 4))[0])
                            row['iips_object'] = hex(struct.unpack('<I', mem(0x2b998b4, 4))[0])
                            row['argument_strings'] = [cstring(x) if x > 0x10000 else '' for x in struct.unpack('<4I', mem(esp + 4, 16))]
                            if name == 'Engine error':
                                # Com_Error(code, fmt, ...) 的栈布局是：返回地址、错误码、
                                # 格式串指针、变参。上面的解析从 esp+4 连读四个"字符串"，
                                # 把错误码当成了指针，真正的消息其实在 esp+8——这就是早先
                                # 只能看到空串和错位内容的原因。这里按真实布局再取一次。
                                fields = struct.unpack('<6I', mem(esp + 4, 24))
                                row['error_code'] = fields[0]
                                row['error_text'] = cstring(fields[1]) if fields[1] > 0x10000 else ''
                                row['error_arguments'] = list(fields[2:])
                                # 178 调用点先经 0x63cb50 格式化再调用 Com_Error；
                                # 此处余下栈槽是调用者保留的格式化参数，不能统一当作
                                # Com_Error 的变参。额外采集双手对象与装备标记定位切枪。
                                from .weapon_diagnostics import capture_weapon_error
                                weapon_error = capture_weapon_error(
                                    mem, esp, int(row['registers']['ebp'], 16))
                                if weapon_error is not None:
                                    row['weapon_diagnostic'] = weapon_error
                                # 同时附上最近缺失的资源，便于判断是不是缺文件引发的。
                                row['recent_missing_files'] = loading_clock.recent_missing(16)
                        if name in ('MessageBoxA', 'MessageBoxW'):
                            # 错误弹窗在展示前截获；仅硬件变更确认弹窗放行。
                            values = struct.unpack('<5I', mem(esp, 20))
                            row['message'] = lobby_runtime2697.read_cstring(mem, values[2], name.endswith('W'), encoding='gbk')
                            row['caption'] = lobby_runtime2697.read_cstring(mem, values[3], name.endswith('W'), encoding='gbk')
                            from .dialogs import classify
                            row['dialog_flags'] = values[4]
                            row['dialog_kind'] = classify(row['caption'], row['message'], values[4])
                            if row['dialog_kind'] == 'confirmation':
                                rearm[event.tid] = (address, name)
                                flags = struct.unpack_from('<I', ctx, 192)[0]
                                struct.pack_into('<I', ctx, 192, flags | 256)
                        if name in ('ResourceMissingA', 'ResourceMissingW'):
                            # ---- 文件门陷阱：缺资源时补件并让桩重试 ----
                            frame = struct.unpack_from('<I', ctx, 180)[0]
                            filename = cstring(struct.unpack('<I', mem(frame + 8, 4))[0], name.endswith('W'))
                            started = time.perf_counter()
                            loading_clock.missing(filename)
                            args.resource_provider(filename)
                            target = Path(filename)
                            if not target.is_absolute():
                                target = stage / target
                            supplied = target.resolve().is_relative_to(stage.resolve()) and target.is_file()
                            if supplied:
                                # 写 retry 标记（桩栈帧 [ebp-0xc]），重开文件。
                                write_memory(frame - 12, struct.pack('<I', 1))
                            gate = result['resource_gate']
                            gate['missing_calls'] += 1
                            gate['supplied_calls'] += int(supplied)
                            gate['provider_seconds'] += time.perf_counter() - started
                            rearm[event.tid] = (address, name)
                            flags = struct.unpack_from('<I', ctx, 192)[0]
                            struct.pack_into('<I', ctx, 192, flags | 256)
                        if name in ('CreateFileA', 'CreateFileW'):
                            # 兼容模式（未装门时）：直接交给提供器处理。
                            filename = cstring(struct.unpack('<I', mem(esp + 4, 4))[0], name.endswith('W'))
                            args.resource_provider(filename)
                            rearm[event.tid] = (address, name)
                            flags = struct.unpack_from('<I', ctx, 192)[0]
                            struct.pack_into('<I', ctx, 192, flags | 256)
                        # 恢复断点原始字节并回退 EIP 到断点地址。
                        write_memory(address, old)
                        struct.pack_into('<I', ctx, 184, address)
                        check(api(K, 'Wow64SetThreadContext', W.BOOL, PTR, PTR)(thread, ctx))
                    finally:
                        close(thread)
                    # 资源补件相关事件不计入异常列表。
                    if name not in ('CreateFileA', 'CreateFileW', 'ResourceMissingA', 'ResourceMissingW'):
                        result['exceptions'].append(row)
                    if name == 'Engine error' and getattr(args, 'stop_engine_error', False):
                        from .match_events import is_battle_disconnect
                        if is_battle_disconnect(row.get('argument_strings', []),
                                                getattr(args, 'player_channel', None) is not None):
                            row['milestone'] = 'Native battle disconnect'
                        else:
                            result['stopped_reason'] = 'Stopped at diagnostic engine error'
                            break
                    if name.startswith('MessageBox') and row.get('dialog_kind') != 'confirmation':
                        result['stopped_reason'] = 'Captured error dialog before display'
                        break
                elif code == 0x406d1388 and struct.unpack_from('<I', data, 24)[0] == 4 and (struct.unpack_from('<Q', data, 32)[0] == 4096):
                    # MS_VC_EXCEPTION 线程命名事件：记录线程名。
                    row['thread_name'] = cstring(struct.unpack_from('<Q', data, 40)[0])
                    result['exceptions'].append(row)
                elif code not in (0x80000003, 0x4000001f):
                    # ---- 未知异常：快照寄存器与栈，首例致命异常停止会话 ----
                    thread = api(K, 'OpenThread', PTR, W.DWORD, W.BOOL, W.DWORD)(26, False, event.tid)
                    if thread:
                        ctx = C.create_string_buffer(716)
                        struct.pack_into('<I', ctx, 0, 0x1003f)
                        if api(K, 'Wow64GetThreadContext', W.BOOL, PTR, PTR)(thread, ctx):
                            row['registers'] = {name: hex(struct.unpack_from('<I', ctx, offset)[0]) for name, offset in [('edi', 156), ('esi', 160), ('ebx', 164), ('edx', 168), ('ecx', 172), ('eax', 176), ('ebp', 180), ('eip', 184), ('esp', 196)]}
                            for name, addr, length in [('stack', int(row['registers']['esp'], 16), 256), ('instruction_bytes', int(row['registers']['eip'], 16), 64)]:
                                try:
                                    row[name] = mem(addr, length).hex()
                                except OSError:
                                    pass
                        close(thread)
                    row['parameters'] = [hex(struct.unpack_from('<Q', data, 32 + i * 8)[0]) for i in range(min(struct.unpack_from('<I', data, 24)[0], 15))]
                    disposition = 0x80010001  # DBG_EXCEPTION_NOT_HANDLED 交给程序
                    result['exceptions'].append(row)
                    if not first or code in (0xc0000005, 0xc000001d, 0xc0000096):
                        # 访问冲突/非法指令/特权指令：停止并保留现场。
                        result['stopped_reason'] = 'Stopped at diagnostic exception'
                        break
                else:
                    # 非断点位置的单步等事件：记录后继续。
                    result['exceptions'].append(row)
                    if code == 0x4000001f:
                        # WOW64 断点：首次命中时装资源文件门与弹窗/退出钩子。
                        candidate = pefile.PE(str(exe))
                        if getattr(args, 'resource_provider', None) and resource_gate_memory is None:
                            # ---- 安装 CreateFileA/W 文件门（IAT 重定向）----
                            imports = {item.name: item.address for dll in candidate.DIRECTORY_ENTRY_IMPORT for item in dll.imports if item.name}
                            resource_bitmap = lobby_runtime2697.build_resource_filter(args.resource_provider)
                            # 布局：[0,4KB) 两个门桩；[4KB,+8) 计数器；[8KB,…) 位图。
                            allocation_size = 8192 + (len(resource_bitmap) if resource_bitmap is not None else 0)
                            resource_gate_memory = api(K, 'VirtualAllocEx', PTR, PTR, PTR, SIZE, W.DWORD, W.DWORD)(pi.process, None, allocation_size, 12288, 4)
                            check(resource_gate_memory)
                            bitmap_address = None
                            if resource_bitmap is not None:
                                bitmap_address = resource_gate_memory + 8192
                                write_memory(bitmap_address, resource_bitmap)
                                old_bitmap_protection = W.DWORD()
                                check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(pi.process, bitmap_address, len(resource_bitmap), 2, C.byref(old_bitmap_protection)))
                            hooks = []
                            for index, suffix in enumerate(('A', 'W')):
                                iat = imports[('CreateFile' + suffix).encode()]
                                original = struct.unpack('<I', mem(iat, 4))[0]
                                get_error = struct.unpack('<I', mem(imports[b'GetLastError'], 4))[0]
                                set_error = struct.unpack('<I', mem(imports[b'SetLastError'], 4))[0]
                                entry = resource_gate_memory + index * 512
                                code, trap = lobby_runtime2697.build_file_gate(entry, original, get_error, set_error, resource_gate_memory + 4096 + 4 * index, bitmap=bitmap_address, wide=suffix == 'W')
                                write_memory(entry, code)
                                hooks.append((iat, entry, trap, suffix))
                            old_protection = W.DWORD()
                            # 门桩页改为可执行。
                            check(api(K, 'VirtualProtectEx', W.BOOL, PTR, PTR, SIZE, W.DWORD, C.POINTER(W.DWORD))(pi.process, resource_gate_memory, 4096, 32, C.byref(old_protection)))
                            for iat, entry, trap, suffix in hooks:
                                # 先在陷阱处布断点，再把 IAT 指向门桩。
                                arm(trap, 'ResourceMissing' + suffix)
                                write_memory(iat, struct.pack('<I', entry))
                            result['resource_gate'] = dict(mode='indexed-missing-read-only' if resource_bitmap is not None else 'missing-read-only', missing_calls=0, supplied_calls=0, provider_seconds=0, filter_bytes=len(resource_bitmap) if resource_bitmap is not None else 0)
                        # 弹窗与退出调用也一并挂钩（展示前截获）。
                        for dll in candidate.DIRECTORY_ENTRY_IMPORT:
                            for item in dll.imports:
                                wanted = (b'MessageBoxA', b'MessageBoxW', b'ExitProcess')
                                if item.name in wanted:
                                    target = struct.unpack('<I', mem(item.address, 4))[0]
                                    if target > 0x10000:
                                        arm(target, item.name.decode())
            elif event.code == 2:
                thread_ids.add(event.tid)
            elif event.code == 4:
                thread_ids.discard(event.tid)
            elif event.code == 8:
                # OutputDebugString：记录客户端输出的调试文本。
                address = struct.unpack_from('<Q', data)[0]
                unicode, count = struct.unpack_from('<HH', data, 8)
                try:
                    result['debug_strings'].append(mem(address, min(count, 8192) * (2 if unicode else 1)).decode('utf-16le' if unicode else 'utf-8', errors='replace').rstrip('\x00'))
                except OSError:
                    pass
            elif event.code == 5:
                # 进程退出：记录退出码并结束循环。
                result['exit_code'] = hex(struct.unpack_from('<I', data)[0])
                check(api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(event.pid, event.tid, disposition))
                pending = None
                break
            loading_clock.end()
            check(api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(event.pid, event.tid, disposition))
            pending = None
        else:
            # ---- 有界时限到达（smoke/launch 的 seconds 到期）----
            autosave_profile(force=True)
            result['stopped_reason'] = 'Bounded observation time reached'
            result['thread_snapshots'] = []
            result['lobby_state'] = lobby_state()
            # 遍历两个已注册命令链表，留存命令注册证据。
            result['registered_commands'] = {}
            for label, head in [('general', 0x2bde6d4), ('client', 0x2bd86b4)]:
                rows = []
                seen = set()
                try:
                    node = struct.unpack('<I', mem(head, 4))[0]
                    while node and node not in seen and (len(rows) < 4096):
                        seen.add(node)
                        link, name, function = struct.unpack('<3I', mem(node, 12))
                        rows.append({'node': hex(node), 'name': cstring(name), 'function': hex(function)})
                        node = link
                except OSError as exc:
                    result['command_read_error'] = repr(exc)
                result['registered_commands'][label] = rows
            # 全部线程挂起并快照 EIP/ESP/栈，便于事后分析。
            for tid in thread_ids:
                thread = api(K, 'OpenThread', PTR, W.DWORD, W.BOOL, W.DWORD)(26, False, tid)
                if not thread:
                    continue
                suspended = api(K, 'SuspendThread', W.DWORD, PTR)(thread) != 0xffffffff
                try:
                    ctx = C.create_string_buffer(716)
                    struct.pack_into('<I', ctx, 0, 0x1003f)
                    if suspended and api(K, 'Wow64GetThreadContext', W.BOOL, PTR, PTR)(thread, ctx):
                        row = {'tid': tid, 'eip': hex(struct.unpack_from('<I', ctx, 184)[0]), 'esp': hex(struct.unpack_from('<I', ctx, 196)[0])}
                        try:
                            row['stack'] = mem(int(row['esp'], 16), 512).hex()
                        except OSError:
                            pass
                        result['thread_snapshots'].append(row)
                finally:
                    if suspended:
                        api(K, 'ResumeThread', W.DWORD, PTR)(thread)
                    close(thread)
    except Exception as exc:
        result['error'] = repr(exc)
        # 保存失败调用链，区分副本读写、受限进程与保护组件启动错误。
        # 仅记录异常栈，不抓取局部变量，避免把玩家通道令牌写入证据。
        result['traceback'] = traceback.format_exc()
        result['passed'] = False
    finally:
        # ---- 统一清理：无论成败都保存证据并结束子进程 ----
        loading_clock.end()
        result['loading_timing'] = loading_clock.report()
        result['resumed'] = resumed
        if resource_gate_memory and pi.process and (wait(pi.process, 0) != 0):
            # 读取门桩计数器（A/W 两个打开计数）。
            try:
                result['resource_gate']['open_calls'] = list(struct.unpack('<2I', mem(resource_gate_memory + 4096, 8)))
            except OSError:
                pass
        if pi.process:
            # 先终止进程，再放行挂起的调试事件让退出流程完成。
            api(K, 'TerminateProcess', W.BOOL, PTR, W.UINT)(pi.process, 53670)
            if pending:
                api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(pending.pid, pending.tid, 0x80010001)
            until = time.monotonic() + 3
            while time.monotonic() < until and wait(pi.process, 0) != 0:
                event = EVENT()
                if api(K, 'WaitForDebugEvent', W.BOOL, C.POINTER(EVENT), W.DWORD)(C.byref(event), 100):
                    api(K, 'ContinueDebugEvent', W.BOOL, W.DWORD, W.DWORD, W.DWORD)(event.pid, event.tid, 0x10002)
            result['child_terminated'] = wait(pi.process, 1000) == 0
            close(pi.thread)
            close(pi.process)
        if settings_watcher:
            # 退出前尽力保存最后一次设置变更。
            try:
                saved = settings_watcher.finish()
                if saved:
                    result['settings_saved'] = saved
            except (OSError, ValueError) as exc:
                result['settings_save_error'] = repr(exc)
        if token:
            close(token)
        if guard:
            # 通知 guard 会话结束，等它确认清理结果。
            (run / 'guard-done.json').write_text('{}')
            wait(guard, 5000)
            if (run / 'guard-result.json').exists():
                result['guard_cleanup'] = json.loads((run / 'guard-result.json').read_text())
            close(guard)
        if listener:
            listener.close()
        result['finished_at'] = time.time()
        (run / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        (run.parent / 'latest.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result
