# Version 2.6.9.7 research port; see docs/provenance.json.
"""版本适配层：把客户端的仓库内存池扩容（6144 → 12288 条）。

仓库在客户端里有两块连续内存池：
- 物品记录池：每条 280 字节，容量 6144，地址 0x8508978；
- UI 引用池：每条 32 字节，容量 6144，地址 0x2cfcc68。

本模块在恢复镜像上新增一个 ``.ofinv`` 未初始化节，把两块池搬到新节里
并扩容到 12288 条，同时修正代码中所有引用旧地址/旧容量/旧池内偏移的
立即数与栈访问。原池前后及新池边界布置金丝雀（CANARY），
会话期间周期性核对，确保扩容没有越界踩到相邻数据。
"""
import struct
import pefile

# 扩容后的目标容量；要求能被 6 整除（与池内分组布局相关）。
CAPACITY = 12288
# 2.6.9.7 中两块池的原始地址。
RECORDS = 0x8508978
UI_RECORDS = 0x2cfcc68
# 边界金丝雀模式：写入池边界，会话中周期性验证未被破坏。
CANARY = bytes.fromhex('a73de41952b608cf') * 8


def expand(raw, pe):
    """在镜像 ``raw`` 上执行仓库扩容，返回新布局与补丁清单。

    ``pe`` 为已解析的 pefile 对象。所有补丁点都先断言旧值符合预期，
    不符合立即失败（版本不符时不静默打错补丁）。
    """
    # 版本身份断言：基址 0x400000、镜像大小 0xdc14000。
    assert pe.OPTIONAL_HEADER.ImageBase == 0x400000
    assert pe.OPTIONAL_HEADER.SizeOfImage == 0xdc14000
    assert CAPACITY % 6 == 0
    align = pe.OPTIONAL_HEADER.SectionAlignment
    round_up = lambda n: (n + align - 1) // align * align
    rva = pe.OPTIONAL_HEADER.SizeOfImage
    # 新池布局：新节起始 RVA 后留一页，先放物品池，再对齐放 UI 池。
    records = 0x400000 + rva + 4096
    records_end = records + CAPACITY * 280
    ui = round_up(records_end + 4096)
    ui_end = ui + CAPACITY * 32
    size = round_up(ui_end + 4096 - (0x400000 + rva))
    # 新节表项必须写在现有节表之后的空洞里，且 PE 头里还有空间。
    header = pe.sections[-1].get_file_offset() + 40
    assert header + 40 <= min((s.PointerToRawData for s in pe.sections if s.PointerToRawData))
    assert raw[header:header + 40] == bytes(40)
    # .ofinv：未初始化节（SizeOfRawData=0），运行时由系统分配零页。
    struct.pack_into('<8sIIIIIIHHI', raw, header, b'.ofinv\x00\x00', size, rva, 0, 0, 0, 0, 0, 0, 0xc0000080)
    struct.pack_into('<H', raw, pe.FILE_HEADER.get_field_absolute_offset('NumberOfSections'), pe.FILE_HEADER.NumberOfSections + 1)
    struct.pack_into('<I', raw, pe.OPTIONAL_HEADER.get_field_absolute_offset('SizeOfImage'), rva + size)
    struct.pack_into('<I', raw, pe.OPTIONAL_HEADER.get_field_absolute_offset('SizeOfUninitializedData'), pe.OPTIONAL_HEADER.SizeOfUninitializedData + size)
    patches = []
    # 这些相邻地址必须保持原样：验证补丁没有误伤邻近的表。
    unchanged_sites = [0x5bd566, 0x5bdc72, 0x5dda10, 0x6aeed8, 0x5cd12a, 0x5e138d, 0x5e1502, 0x5e1734, 0x5e1b70, 0xb9c563, 0xbac191, 0xbac19b]
    unchanged = {site: bytes(raw[pe.get_offset_from_rva(site - 0x400000):pe.get_offset_from_rva(site - 0x400000) + 4]) for site in unchanged_sites}

    def patch(site, old, new):
        """改写一处 32 位立即数/地址，先断言旧值。"""
        off = pe.get_offset_from_rva(site - 0x400000)
        assert struct.unpack_from('<I', raw, off)[0] == old, hex(site)
        struct.pack_into('<I', raw, off, new)
        patches.append({'site': hex(site), 'before': hex(old), 'after': hex(new)})

    def group(sites, old, new):
        for site in sites:
            patch(site, old, new)

    # ---- 物品记录池：地址与池内偏移引用 ----
    group([0x4d67cd, 0x4d72e9, 0x4d738f, 0x4d75b0, 0x4d7e40, 0x4d8160, 0x4d8165, 0xb9c4a3], RECORDS, records)
    group([0x4d67da, 0x4d8099, 0x5dcb5d], RECORDS + 24, records + 24)
    patch(0x4d7530, RECORDS + 304, records + 304)
    patch(0x4d7578, RECORDS + 6144 * 280 + 304, records_end + 304)
    patch(0xbac0d8, RECORDS + 6144 * 280 + 8, records_end + 8)
    # 容量与末尾下标。
    group([0x4d657b, 0x4d67a4, 0x4d67ae, 0x4d72e4, 0x4d738a, 0x4d7e3b], 6144, CAPACITY)
    group([0xb9c4a8, 0xbac0d3], 6143, CAPACITY - 1)
    patch(0x4d67ee, 6144 * 280, CAPACITY * 280)
    # ---- UI 引用池：地址、各字段偏移与池末尾 ----
    group([0x46ddfd, 0x46de1e, 0x46de27, 0x46e147, 0x46e16c, 0x46e1ba, 0x46e25d, 0x46e27d, 0x46e2cf, 0x46e360, 0x46e381, 0x46e3c1, 0x4d655d, 0x5c750c, 0x6b1f81, 0x6b2b35, 0x6b3439, 0x6b49b5, 0x6b4cd4], UI_RECORDS, ui)
    group([0x4d6615, 0x5dcb2a, 0x5dcb58, 0x5dce62, 0x6b1d06, 0x6b2213, 0x6b2228], UI_RECORDS + 4, ui + 4)
    group([0x6ad6b7, 0x6b226e, 0x6b2baf, 0x6b2c50, 0x6b2c56], UI_RECORDS + 8, ui + 8)
    group([0x4d6631, 0x6b2d2c, 0x6d6ffc, 0x6d704c, 0x6d718c, 0x6d71dc], UI_RECORDS + 24, ui + 24)
    group([0x6b2b25, 0x6b2b2e], UI_RECORDS + 28, ui + 28)
    group([0x46de11, 0x46e160, 0x46e271, 0x46e374, 0x6b1fa3, 0x6b3472, 0x6b4a9c], UI_RECORDS + 6144 * 32, ui_end)
    group([0x5dcb41, 0x5dce6f, 0x6b1d20], UI_RECORDS + 6144 * 32 + 4, ui_end + 4)
    patch(0x6b2293, UI_RECORDS + 6144 * 32 + 8, ui_end + 8)
    # ---- 三个函数里的栈访问偏移（含 UI 池基址的局部栈缓冲）----
    # 这些函数把池基址放栈上，后续以 [esp+位移] 访问；池变大后位移也要加。
    from capstone import Cs, CS_ARCH_X86, CS_MODE_32
    from capstone.x86_const import X86_OP_IMM, X86_OP_MEM, X86_REG_ESP
    md = Cs(CS_ARCH_X86, CS_MODE_32)
    md.detail = True
    for start, end in [(0x6b27f0, 0x6b2a2d), (0x6b2a30, 0x6b2ae0), (0x6b3360, 0x6b341a)]:
        off = pe.get_offset_from_rva(start - 0x400000)
        for instruction in md.disasm(bytes(raw[off:off + end - start]), start):
            for op in instruction.operands:
                # 24572..24832（0x5ffc..0x6100）是旧池尾相关立即数。
                if op.type == X86_OP_IMM and instruction.imm_size == 4 and (24572 <= op.imm < 24832):
                    patch(instruction.address + instruction.imm_offset, op.imm, op.imm + (CAPACITY - 6144) * 4)
                elif op.type == X86_OP_MEM and op.mem.base == X86_REG_ESP and (instruction.disp_size == 4) and (24576 <= op.mem.disp < 24832):
                    patch(instruction.address + instruction.disp_offset, op.mem.disp, op.mem.disp + (CAPACITY - 6144) * 4)
    # ---- 事后校验：新节属性正确、无重复补丁点、相邻表未被误改 ----
    rebuilt = pefile.PE(data=bytes(raw))
    assert rebuilt.sections[-1].Characteristics == 0xc0000080
    assert rebuilt.sections[-1].SizeOfRawData == 0
    assert len({p['site'] for p in patches}) == len(patches)
    for site, original in unchanged.items():
        off = pe.get_offset_from_rva(site - 0x400000)
        assert raw[off:off + 4] == original, 'Adjacent table changed: ' + hex(site)
    return {'capacity': CAPACITY, 'records': records, 'ui_records': ui, 'record_stride': 280, 'ui_stride': 32, 'canaries': [records - 64, records_end, ui - 64, ui_end], 'section_rva': rva, 'section_size': size, 'patches': patches}
