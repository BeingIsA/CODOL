# Version 2.6.9.7 research port; see docs/provenance.json.
"""版本适配层：在恢复镜像上应用静态补丁并生成待启动的客户端副本。

``stage_candidate`` 是补丁核心：以恢复镜像为基底，按 RVA 逐点打补丁
（每点先断言原始字节相符），再复制运行所需 DLL/配置与解码资源，最终
在目标目录写出 ``codol2697-recovered-test.exe``。原始 EXE 永不被修改。

补丁按目的分组：
- 基础替换：OffSafe 组件名、关闭 IIPS 致命报错、离线登录元数据返回假；
- 大厅引导：跳过远端轮询/认证，用本地顺序激活 UI、目录与角色选择；
- 预览链路：3D 角色与武器预览改由本地数据就绪驱动；
- 仓库扩容：调用 inventory_layout.expand 搬迁并扩容两块内存池。

详细的补丁地址含义见 docs/从零理解本地大厅.md 与会话产出的
preview-patches.json。
"""
import hashlib
import json
import shutil
import struct
from pathlib import Path
import pefile
from ..infra.fsutil import link_or_copy
from ..paths import ASSETS as ROOT
from .client import GAME, SOURCE, SOURCE_SHA


def stage_candidate(folder, args):
    """生成补丁后的客户端副本，返回 (exe 路径, staging 报告)。

    ``folder`` 为本次运行目录（须为已建好的空目录）；``args`` 为应用层
    装配的参数对象。所有补丁点断言失败即中止——版本不符时不产出副本。
    """
    raw = bytearray(SOURCE.read_bytes())
    # 基底必须是已知的恢复镜像。
    assert hashlib.sha256(raw).hexdigest() == SOURCE_SHA
    pe = pefile.PE(data=raw)
    # .rdata 恢复为只读（0x40000040），CRT 的浮点初始化检查才能通过。
    rdata = next((s for s in pe.sections if s.Name.rstrip(b'\x00') == b'.rdata'))
    assert rdata.Characteristics == 0xc0000040
    struct.pack_into('<I', raw, rdata.get_file_offset() + 36, 0x40000040)
    # 组件名替换：TerSafe.dll -> OffSafe.dll（副本改用 QA 回调组件）。
    off = pe.get_offset_from_rva(0x8bbea4)
    assert raw[off:off + 12] == b'TerSafe.dll\x00'
    raw[off:off + 12] = b'OffSafe.dll\x00'
    # IIPS 关闭后报告缺失可选文件时不再走致命错误路径（改为跳转越过报错调用）。
    off = pe.get_offset_from_rva(0x2336f5)
    assert raw[off:off + 5] == bytes.fromhex('68d0ddcb00')
    raw[off:off + 5] = bytes.fromhex('e90a000000')
    # 启动器登录元数据不存在时直接返回 false（xor eax,eax; ret）。
    off = pe.get_offset_from_rva(0xe0260)
    assert raw[off:off + 3] == bytes.fromhex('558bec')
    raw[off:off + 3] = bytes.fromhex('33c0c3')
    preview_patches = []
    # 预置三个全局状态：UI 模式=5、连接状态=2、数据状态=6，
    # 让客户端进入"离线大厅"分支而不是等待远端连接。
    code = b''.join((b'\xc7\x05' + struct.pack('<II', addr, value) for addr, value in [(0x747c890, 5), (0x747c8c0, 2), (0xd25f8c, 6)])) + b'\xc3'
    off = pe.get_offset_from_rva(0x322430)
    before = bytes(raw[off:off + len(code)])
    assert before[:5] == bytes.fromhex('a190c84707')
    raw[off:off + len(code)] = code
    preview_patches.append({'rva': '0x322430', 'before': before.hex(), 'after': code.hex(), 'purpose': 'Local lobby UI bootstrap experiment; network remains blocked'})
    # 两个远端轮询/认证泵直接返回（ret），离线 UI 模式下没有远端对象可轮询。
    for rva, expected, purpose in [(0x346690, '56', 'Do not poll an absent remote lobby object in offline UI mode'), (0x324df0, '55', 'Do not run remote authentication pump in offline UI mode')]:
        off = pe.get_offset_from_rva(rva)
        assert raw[off:off + 1].hex() == expected
        raw[off] = 195
        preview_patches.append({'rva': hex(rva), 'before': expected, 'after': 'c3', 'purpose': purpose})
    # 大厅 movie 激活入口：改为检查本地对象后走既有激活路径。
    va = 0x6d16c0
    code = b'\xe8' + struct.pack('<i', 0x6d3b40 - (va + 5)) + bytes.fromhex('83f8017505') + b'\xe9' + struct.pack('<i', 0x6d1460 - (va + 15)) + b'\xc3'
    off = pe.get_offset_from_rva(va - 0x400000)
    before = bytes(raw[off:off + len(code)])
    assert before[:6].hex() == '56e88a31f1ff'
    raw[off:off + len(code)] = code
    preview_patches.append({'rva': hex(va - 0x400000), 'before': before.hex(), 'after': code.hex(), 'purpose': 'Activate the existing lobby movie in isolated local preview mode'})
    # ---- 本地初始化序列：一段内联在 0x6d16c0 处的引导代码 ----
    # 顺序：查本地对象 -> 目录状态=3 -> 等玩家数据就绪 -> 初始化装备缓存
    # -> 刷新 UI -> 角色名检查回调 -> 装备加载状态=4 -> 通知角色选择器。
    va = 0x6d16c0
    code = bytearray()

    def call(target):
        code.extend(b'\xe8' + struct.pack('<i', target - (va + len(code) + 5)))

    call(0x6d3b40)
    code += bytes.fromhex('83f801')
    jret = len(code)
    code += bytes.fromhex('0f8500000000')       # 对象未就绪 -> 直接返回
    code += bytes.fromhex('803d13c7470700')     # UI 已激活？
    jactive = len(code)
    code += bytes.fromhex('0f8500000000')       # 已激活 -> 跳到激活段
    call(0x74cd10)                              # 本地 CSV 目录加载器
    code += b'\xc7\x05' + struct.pack('<II', 0x7454ac0, 3)  # catalog_stage = 3
    active = len(code)
    code += bytes.fromhex('803dd54a450700')     # 玩家数据就绪标记？
    jready = len(code)
    code += bytes.fromhex('0f8500000000')       # 未就绪 -> 稍后再来
    call(0x5a1140)                              # 装备缓存初始化
    call(0x5a1380)                              # 武器缓存初始化
    call(0x6a2cc0)                              # 角色预览框架
    # 读取角色槽并压栈，调用本地空仓库回调（0x4d7450）构造对象池。
    code += bytes.fromhex('a14c71d2006bc0448b804843bf02a35071d200')
    code += bytes.fromhex('83ec2033c0')
    for slot in range(0, 32, 4):
        code += bytes([137, 68, 36, slot])
    code += bytes.fromhex('8bc46a0150')
    call(0x4d7450)
    code += bytes.fromhex('83c428')
    call(0x6dc2b0)                              # 名称服务（本地实现）
    code += bytes.fromhex('c1e0068b80c883c402a330c74707')
    call(0x6b1dc0)                              # 仓库 UI 初始化
    code += bytes.fromhex('ff354c71d200')
    call(0x6a2e10)                              # 预览装备应用
    code += bytes.fromhex('83c404')
    refresh = len(code)
    call(0x6d1460)                              # UI 激活
    call(0x6b1dc0)                              # 仓库 UI（激活后再刷一次）
    ret = len(code)
    code += b'\xc3'
    struct.pack_into('<i', code, jret + 2, ret - (jret + 6))
    struct.pack_into('<i', code, jactive + 2, active - (jactive + 6))
    struct.pack_into('<i', code, jready + 2, refresh - (jready + 6))
    # 名称检查回调：以本地比较完成，不请求远端文本服务。
    callback = va + len(code)
    code += bytes.fromhex('558becff75106a006a0157ff7508ff550c83c4145dc3')
    off = pe.get_offset_from_rva(0x2c5543)
    before = bytes(raw[off:off + 5])
    assert before.hex() == 'e83890e1ff'
    replacement = b'\xe8' + struct.pack('<i', callback - 0x6c5548)
    raw[off:off + 5] = replacement
    preview_patches.append({'rva': '0x2c5543', 'before': before.hex(), 'after': replacement.hex(), 'purpose': 'Complete local UI name check without a remote text service'})
    # 装备加载完成辅助：置 loadout_state=4、选中角色=尤里(545)，
    # 然后调用既有的角色选择器通知。
    helper = va + len(code)
    code += b'\xc7\x05' + struct.pack('<II', 0x7474f00, 4)
    code += bytes.fromhex('c60527c7470701566a006a006a0068bc4acc00bed44acc00')
    call(0x5e4730)
    code += bytes.fromhex('83c4105ec3')
    off = pe.get_offset_from_rva(0x1dc7a2)
    before = bytes(raw[off:off + 10])
    assert before.hex() == 'c705004f470704000000'
    replacement = b'\xe8' + struct.pack('<i', helper - 0x5dc7a7) + bytes.fromhex('9090909090')
    raw[off:off + 10] = replacement
    preview_patches.append({'rva': '0x1dc7a2', 'before': before.hex(), 'after': replacement.hex(), 'purpose': 'Notify the existing character selector after local loadout initialization'})
    assert len(code) < 380
    off = pe.get_offset_from_rva(va - 0x400000)
    before = bytes(raw[off:off + len(code)])
    raw[off:off + len(code)] = code
    preview_patches.append({'rva': hex(va - 0x400000), 'before': before.hex(), 'after': code.hex(), 'purpose': 'Build the catalog with the existing local CSV loader before UI activation'})
    # 内置本地目录开关默认置开（push 0 -> push 1）。
    off = pe.get_offset_from_rva(0x31b80e)
    assert raw[off:off + 2].hex() == '6a00'
    raw[off + 1] = 1
    preview_patches.append({'rva': '0x31b80e', 'before': '6a00', 'after': '6a01', 'purpose': 'Default the built-in local catalog switch to enabled in this copy'})
    # 隔离预览下保持"连接中"遮罩不出现（jnz -> jmp）。
    off = pe.get_offset_from_rva(0x20ce99)
    assert raw[off:off + 2].hex() == '7527'
    raw[off] = 235
    preview_patches.append({'rva': '0x20ce99', 'before': '7527', 'after': 'eb27', 'purpose': 'Keep the network-connecting overlay disabled in isolated lobby preview'})
    for rva, before_hex, after_hex, purpose in []:
        off = pe.get_offset_from_rva(rva)
        before = bytes.fromhex(before_hex)
        after = bytes.fromhex(after_hex)
        assert raw[off:off + len(before)] == before
        raw[off:off + len(after)] = after
        preview_patches.append({'rva': hex(rva), 'before': before_hex, 'after': after_hex, 'purpose': purpose})
    # ---- 大厅统计/资产查询的两处直通补丁 ----
    for rva, before_hex, after_hex, purpose in [(0x327ec0, '558bec83e4f8', 'b830f0bf0dc3', 'Return the local profile buffer without an absent auth-object lookup'), (0x1b94c5, 'e886b30200', 'b001909090', 'Allow the UI stats query to check loaded ui_mp assets without requiring a remote lobby socket')]:
        off = pe.get_offset_from_rva(rva)
        before = bytes.fromhex(before_hex)
        after = bytes.fromhex(after_hex)
        assert raw[off:off + len(before)] == before
        raw[off:off + len(after)] = after
        preview_patches.append({'rva': hex(rva), 'before': before_hex, 'after': after_hex, 'purpose': purpose})
    # ---- 装备刷新与 3D 预览：改由本地数据就绪驱动 ----
    for rva, before_hex, after_hex, purpose in [(0x1dc714, 'e837810000', 'a0d54a4507', 'Use local inventory readiness for the loadout refresh pump'), (0x1dc79a, 'e82139fcff', '9090909090', 'Keep loadout updates in memory; skip the remote profile-save call')]:
        off = pe.get_offset_from_rva(rva)
        before = bytes.fromhex(before_hex)
        after = bytes.fromhex(after_hex)
        assert raw[off:off + len(before)] == before
        raw[off:off + len(after)] = after
        preview_patches.append({'rva': hex(rva), 'before': before_hex, 'after': after_hex, 'purpose': purpose})
    for rva, before_hex, after_hex, purpose in [(0x2223f4, 'e85724fcff', 'a0d54a4507', 'Update 3D preview only after the local inventory is ready'), (0x222401, 'e8fadff7ff', 'e91f000000', 'Use the validated local profile for 3D preview instead of remote profile status')]:
        off = pe.get_offset_from_rva(rva)
        before = bytes.fromhex(before_hex)
        after = bytes.fromhex(after_hex)
        assert raw[off:off + len(before)] == before
        raw[off:off + len(after)] = after
        preview_patches.append({'rva': hex(rva), 'before': before_hex, 'after': after_hex, 'purpose': purpose})
    # ---- 仓库内存池扩容（详见 inventory_layout 模块说明）----
    inventory_layout = None
    from .inventory_layout import expand
    inventory_layout = expand(raw, pe)
    (folder.parent / 'inventory-layout.json').write_text(json.dumps(inventory_layout, indent=2))
    (folder.parent / 'preview-patches.json').write_text(json.dumps(preview_patches, indent=2))
    exe = folder / 'codol2697-recovered-test.exe'
    exe.write_bytes(raw)
    # ---- 运行组件与资源：从原始目录和预解码库复制进副本 ----
    qa_safe = ROOT / 'support/OffSafe.dll'
    qa_data = qa_safe.read_bytes()
    assert hashlib.sha256(qa_data).hexdigest() == '0764b95349877e55cc47bebac7b05f75005d9f2e7a95c3af7e0013527cffa94a'
    (folder / 'OffSafe.dll').write_bytes(qa_data)
    # 这些 DLL/文本直接来自原始安装目录，副本里按相对路径加载。
    # 运行期只读不写，与资源文件一样用硬链接共享原始目录的字节。
    for name in ('awesomium.dll', 'icudt.dll', 'bink2w32.dll', 'mss32.dll', 'D3DX9_43.dll'):
        link_or_copy(GAME / name, folder / name)
    for name in ('localization.txt', 'splash.bmp', 'logo.bmp', 'datapacket.ini'):
        link_or_copy(GAME / name, folder / name)
    # 关闭副本内的 IIPS 在线资源流程（改由本地资源供给）。内容固定，
    # 直接写入而不从原始目录链接——链接后改写会污染原始客户端。
    (folder / 'iips.ini').write_text('enable_iips=0\n', encoding='ascii')
    # 预解码资源整体进副本；.tsv/.lst 是提取清单、.md 是说明文档，不带进去。
    resource_root = ROOT / 'decoded-resources'
    if resource_root.exists():
        for source in resource_root.rglob('*'):
            if source.is_file() and source.suffix.lower() not in ('.tsv', '.lst', '.md'):
                target = folder / source.relative_to(resource_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                link_or_copy(source, target)
    shutil.copytree(GAME / 'Microsoft.VC90.CRT', folder / 'Microsoft.VC90.CRT', dirs_exist_ok=True, copy_function=link_or_copy)
    shutil.copytree(GAME / 'miles', folder / 'miles', dirs_exist_ok=True, copy_function=link_or_copy)
    return (exe, {'source_sha256': SOURCE_SHA, 'candidate_sha256': hashlib.sha256(raw).hexdigest(), 'runtime_patches': [{'section': '.rdata', 'before': '0xc0000040', 'after': '0x40000040', 'purpose': 'Restore read-only section so CRT validates and calls FP initializer'}, {'rva': '0x8bbea4', 'before': 'TerSafe.dll', 'after': 'OffSafe.dll'}, {'rva': '0x2336f5', 'before': '68d0ddcb00', 'after': 'e90a000000', 'purpose': 'Disabled IIPS reports missing optional files without fatal error'}, {'rva': '0xe0260', 'before': '558bec', 'after': '33c0c3', 'purpose': 'Absent launcher login metadata returns false'}], 'iips_enabled': False, 'qa_callback_sha256': hashlib.sha256(qa_data).hexdigest(), 'original_files_modified': False})
