"""版本适配层：客户端副本构建编排与本地身份注入。

staging.build 是 v2697.patches.stage_candidate 之上的一层编排：
1. 调用补丁核心生成补丁后的 EXE 与运行组件；
2. 把解码缓存里已有的资源预填进副本（缺失部分留给运行时补件）；
3. 恢复用户原生设置（domain.settings_store.stage）；
4. 在 EXE 尾部追加 ``.lobby`` 节，写入本地身份引导代码与用户名。

用户名有两份编码（GBK 供前端显示、UTF-8 供另一处 native name），
必须同时写入，只改一处会出现两处显示不一致。
"""
import hashlib
import struct
import pefile
from . import patches
from ..domain import settings_store
from ..paths import ASSETS
from ..infra.fsutil import link_or_copy


def install_identity(exe, name):
    """在补丁后的 EXE 尾部加 ``.lobby`` 节，注入本地身份，返回摘要。

    节内容 = 一段引导代码 + 两份编码的用户名：
    - 代码改写 0x6d16c0 处的身份查询入口，让它跳到本节的引导代码；
    - 引导代码调用既有本地对象查询（0x6d3b40），未就绪则返回"无身份"，
      就绪则分别用 GBK/UTF-8 两份名字调用文本服务（0x638ef0）；
    - 本地 UID 固定为 4294967297（2^32+1，表示"本地测试账号"）。
    """
    raw = bytearray(exe.read_bytes())
    pe = pefile.PE(data=raw)
    base = pe.OPTIONAL_HEADER.ImageBase
    rva = pe.OPTIONAL_HEADER.SizeOfImage
    entry = base + rva
    code = bytearray()

    def call(address):
        code.extend(b'\xe8' + struct.pack('<i', address - entry - len(code) - 5))

    # 调本地对象查询；返回值 != 1 视为"尚未就绪"，直接返回空。
    call(0x6d3b40)
    code.extend(bytes.fromhex('9c6083f8010f85'))
    not_ready = len(code)
    code.extend(bytes(4))
    # 两个文本指针（GBK 与 UTF-8 名字）分别调用文本服务登记。
    for address, text_pointer in [(0x743da00, entry + 0x100), (0x1bbbd10, entry + 0x140)]:
        code.extend(b'\x8b\x35' + struct.pack('<I', address) + b'\x85\xf6\x0f\x84')
        missing = len(code)
        code.extend(bytes(4))
        code.extend(b'\x68' + struct.pack('<I', text_pointer))
        call(0x638ef0)
        code.extend(bytes.fromhex('83c404'))
        struct.pack_into('<i', code, missing, len(code) - missing - 4)
    struct.pack_into('<i', code, not_ready, len(code) - not_ready - 4)
    code.extend(bytes.fromhex('619dc3'))  # popad; ret
    if len(code) >= 0x100:
        raise ValueError('Identity stub overflow')
    content = bytearray(0x200)
    content[:len(code)] = code
    # 偏移 0x100 起：GBK 名字；0x140 起：UTF-8 名字（均含 NUL 结尾）。
    for offset, value in [(0x100, name.encode('gbk')), (0x140, name.encode('utf-8'))]:
        content[offset:offset + len(value) + 1] = value + b'\0'
    # 节内容按文件对齐追加到文件尾；节表项写在最后一个节表之后的空洞。
    alignment = pe.OPTIONAL_HEADER.FileAlignment
    raw_at = (len(raw) + alignment - 1) // alignment * alignment
    raw.extend(bytes(raw_at - len(raw)))
    raw.extend(content)
    header = pe.sections[-1].get_file_offset() + 40
    if header + 40 > pe.OPTIONAL_HEADER.SizeOfHeaders or raw[header:header + 40] != bytes(40):
        raise ValueError('No space for lobby PE section')
    struct.pack_into('<8sIIIIIIHHI', raw, header, b'.lobby\0\0', len(content), rva,
                     len(content), raw_at, 0, 0, 0, 0, 0x60000020)
    struct.pack_into('<H', raw, pe.FILE_HEADER.get_field_absolute_offset('NumberOfSections'), pe.FILE_HEADER.NumberOfSections + 1)
    struct.pack_into('<I', raw, pe.OPTIONAL_HEADER.get_field_absolute_offset('SizeOfImage'), rva + 0x1000)
    struct.pack_into('<I', raw, pe.OPTIONAL_HEADER.get_field_absolute_offset('SizeOfCode'), pe.OPTIONAL_HEADER.SizeOfCode + len(content))

    def replace(address, before, after):
        # 按地址改写并断言原始字节（身份入口重定向与两个旁路）。
        offset = pe.get_offset_from_rva(address - base)
        if raw[offset:offset + len(before)] != before or len(before) != len(after):
            raise ValueError(f'Client patch mismatch at {address:#x}')
        raw[offset:offset + len(before)] = after

    # 身份查询入口重定向到 .lobby 节的引导代码。
    replace(0x6d16c0, b'\xe8' + struct.pack('<i', 0x6d3b40 - 0x6d16c0 - 5),
            b'\xe8' + struct.pack('<i', entry - 0x6d16c0 - 5))
    replace(0x5b95e9, bytes.fromhex('751e'), bytes.fromhex('eb1e'))
    # 一段依赖远端对象状态的判断序列替换为本地常量路径。
    before = bytes.fromhex('8b0d00ca4707535633d233c05785c9750a8b0d0cca470785c9740be8597c4e008b50208b4024')
    after = bytes.fromhex('535657ba01000000b801000000')
    replace(0x5b9717, before, after + b'\x90' * (len(before) - len(after)))
    exe.write_bytes(raw)
    return {'name': name, 'local_uid': 4294967297, 'section': '.lobby', 'sha256': hashlib.sha256(raw).hexdigest()}


def build(stage, args):
    """构建完整客户端副本，返回 (exe 路径, staging 报告)。

    ``stage`` 为受限运行目录；``args`` 为应用层装配的参数对象
    （使用其 resource_provider 与 username）。
    """
    exe, report = patches.stage_candidate(stage, args)
    args.resource_provider.stage = stage
    # 预填解码缓存：把 local-resource-cache/decoded 里已有的一切放进副本，
    # 已存在的目标跳过——staging 阶段不覆盖任何文件。
    cache = ASSETS / 'local-resource-cache/decoded'
    for source in cache.rglob('*'):
        if source.is_file():
            target = stage / source.relative_to(cache)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                link_or_copy(source, target)
    report['local_settings'] = settings_store.stage(stage)
    report['identity'] = install_identity(exe, args.username)
    report['candidate_sha256'] = report['identity']['sha256']
    return exe, report
